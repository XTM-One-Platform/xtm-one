# Copyright (c) 2025-2026 Filigran SAS
#
# This file is part of the XTM One Enterprise Edition ("EE") and is
# licensed under the XTM One Enterprise Edition License (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://github.com/XTM-One-Platform/xtm-one/blob/main/LICENSE
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.

"""AI-generated fleet documentation (#9174 slice 7, #9768): the only way it is written.

The model reads what the fleet is made of (its card, each member's role, a
bounded excerpt of its persona, its member configuration, its assignments and
handovers, its connected agents and fleets) and writes the documentation
sections of :mod:`app.services.fleet_docs`. :func:`draft_fleet_documentation`
writes nothing; :func:`regenerate_fleet_documentation` saves the result as the
next version (``POST /fleets/{id}/documentation/regenerate``). Nobody types the
documentation by hand (#9768 S2).

* **Whole or one section.** ``section`` set drafts that section alone
  ("Regenerate section"); unset drafts every section ("Generate
  documentation"). The current sections are given to the model so a regenerate
  keeps the rest of the page coherent.
* **Built-in and custom.** On a built-in fleet the result is a local version
  the next boot keeps; "Restore shipped version" brings the seed back.
* **User-initiated, counted like a chat.** One model call through the caller's
  own model (``get_llm_for_user``), gated by ``require_agentic_ai`` on the
  route; nothing calls it on its own and no run starts.
* **Bounded.** At most :data:`CONTEXT_MAX_MEMBERS` members,
  :data:`CONTEXT_MAX_ASSIGNMENTS` assignments, :data:`PERSONA_EXCERPT_CHARS` of
  each persona. Unknown keys in the answer are dropped and named in ``notes``;
  each section is cut to ``SECTION_MAX_CHARS``.
* **Never fails for its size (#11292).** Each call's output budget is read
  from the model in use (:func:`output_budget`). An answer cut at its budget is
  continued where it stopped (:data:`CONTINUATIONS_MAX`); a whole draft still
  cut or unreadable is drafted in parts: the sections in groups split in halves
  down to one section in plain markdown, the structured parts on their own and
  then by half. A transient provider failure is asked again with backoff
  (:data:`TRANSIENT_RETRIES`); a section that still fails is *pending*: the rest
  is saved and the caller fills it in the background
  (:func:`app.services.fleet_doc_on_create.schedule_pending_sections`). A cut is
  never an error to the user; nothing produced at all answers one honest line
  naming the cause.
"""

from __future__ import annotations

import functools
import json
import logging
import re
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, TypeGuard

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.llm.base import ChatMessage, retry_llm_call, told_not_to_retry
from app.llm.cut_answers import answer_was_cut
from app.llm.model_limits import resolve_provider_max_output_tokens
from app.llm.provider_errors import api_error_message, error_status
from app.models.agent import Agent
from app.models.assignment import Assignment
from app.models.fleet import Fleet, FleetMember
from app.services.fleet_doc_findings import (
    GATES_PRINT_KEY,
    clean_findings,
    decided_points,
    drop_contradicted,
    gates_print,
    member_key,
    merge_findings,
    move_meta_comments,
    role_findings,
    stable_findings,
    with_role_findings,
)
from app.services.fleet_docs import (
    AUTOMATIONS_CHECK,
    BLOCK_NAMES,
    DOC_MODULES,
    DOC_SECTIONS,
    PREREQUISITE_CHECKS,
    SECTION_MAX_CHARS,
    STRUCTURED_FIELDS,
    _stored,
    clean_structured,
    clean_structured_fields,
    doc_modules,
    doc_note,
    field_limit,
    prerequisite_check_known,
    save_documentation,
)
from app.services.fleets import FleetActionRefusedError, effective_member_settings, fleet_members

logger = logging.getLogger(__name__)

#: Output budget of one call when the model's output ceiling is unknown (#11292).
DRAFT_MAX_TOKENS = 8192
#: The most one call asks for when the model's ceiling is known: a larger answer is continued instead.
DRAFT_BUDGET_MAX = 32_768
#: Times a cut answer is continued where it stopped before it is split or kept as written (#11292).
CONTINUATIONS_MAX = 2
#: Retries of a transient provider failure (timeout, 429, 5xx) per call, and the backoff base in seconds.
TRANSIENT_RETRIES = 2
TRANSIENT_BASE_DELAY = 2.0
CONTEXT_MAX_MEMBERS = 40
#: Assignments listed under the members (#10536): every one a member has, so the model never reads a
#: member whose automations sort past a cut as having none (the 60 cap raised false findings).
CONTEXT_MAX_ASSIGNMENTS = 400
#: Automations the computed blocks cover (#10108): every home member's, as the header counts them.
AUTOMATIONS_MAX = 120
PERSONA_EXCERPT_CHARS = 600
_DESCRIPTION_CHARS = 300

#: What each section explains, as the model is told.
SECTION_GUIDE: dict[str, str] = {
    "purpose": "why the fleet exists, in one sentence of at most 25 words",
    "use_case": "the situation it is used in and who uses it, at most 40 words",
    "triggers": "what starts its work: schedules, events, a person's request, as short labels",
    "flow": "how work moves from the orchestrator through the members, as short step titles",
    "objects_created": "what it creates or updates in the platform or connected tools, as short labels",
    "prerequisites": "what must be configured before it works, as short labels",
}


class FleetDocDraftError(ValueError):
    """The draft could not be produced (bad section, cut or unreadable answer)."""


@dataclass
class FleetDocDraft:
    """A drafted set of sections and structured parts; nothing was saved."""

    sections: dict[str, str]
    #: :func:`app.services.fleet_docs.doc_note` codes and params (UX r1 B6 on #9988).
    notes: list[dict[str, Any]] = field(default_factory=list)
    #: The structured parts (``StructuredDoc``), validated; None on a one-section regenerate.
    structured: dict[str, Any] | None = None
    #: What the model found wrong with the configuration (#10246), :func:`fleet_doc_findings.clean_findings`.
    findings: list[dict[str, Any]] = field(default_factory=list)
    #: Sections the provider failed on after their retries (#11292): saved as pending, filled in the background.
    pending: list[str] = field(default_factory=list)
    #: The ``member_without_role`` findings computed from the configuration (:func:`role_findings`) on a
    #: whole draft; ``None`` on a one-section draft. They replace any stored or model role finding.
    role_findings: list[dict[str, Any]] | None = None


# ── Facts computed in code (#9768 S1) ───────────────────────────────────────

#: The trigger kinds in plain words; an unknown kind reads as itself.
TRIGGER_WORDS: dict[str, str] = {
    "schedule": "Schedule",
    "integration_event": "Event",
    "platform_event": "Platform event",
    "work_item": "Work item",
    "webhook": "Webhook",
    "manual": "Manual",
}
_DAYS = ("Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday")
#: Days a monthly estimate counts.
QUOTA_DAYS = 30
#: Rows ``StructuredDoc.quota`` holds.
QUOTA_ROWS_MAX = 20


def describe_cron(cron: object, timezone: object = None) -> str:
    """A cron expression in plain words; an unusual one is named as is, never guessed."""
    if not isinstance(cron, str) or not cron.strip():
        return ""
    zone = f" ({timezone.strip()})" if isinstance(timezone, str) and timezone.strip() else " (UTC)"
    parts = cron.split()
    if len(parts) != 5:
        return f"Cron {cron.strip()}{zone}"
    minute, hour, dom, month, dow = parts
    if minute.startswith("*/") and hour == dom == month == dow == "*" and minute[2:].isdigit():
        return f"Every {int(minute[2:])} minutes"
    if minute.isdigit() and hour == dom == month == dow == "*":
        return f"Every hour at minute {int(minute)}"
    if minute.isdigit() and hour.startswith("*/") and hour[2:].isdigit() and dom == month == dow == "*":
        return f"Every {int(hour[2:])} hours"
    if not (minute.isdigit() and hour.isdigit()):
        return f"Cron {cron.strip()}{zone}"
    at = f"at {int(hour):02d}:{int(minute):02d}{zone}"
    if dom == month == "*":
        if dow == "*":
            return f"Every day {at}"
        if dow in {"1-5", "MON-FRI", "mon-fri"}:
            return f"Every weekday {at}"
        days = dow.split(",")
        if all(d.isdigit() and int(d) % 7 < 7 for d in days):
            return "Every " + ", ".join(_DAYS[int(d) % 7] for d in days) + f" {at}"
    if month == "*" and dow == "*" and dom.isdigit():
        return f"Every month on day {int(dom)} {at}"
    return f"Cron {cron.strip()}{zone}"


def runs_per_month(cron: object) -> int | None:
    """How many times *cron* fires in :data:`QUOTA_DAYS` days, capped at 100,000; None when unreadable."""
    return cron_fires(cron, QUOTA_DAYS)


def cron_fires(cron: object, days: int) -> int | None:
    """How many times *cron* fires in *days* days from a fixed Monday, capped at 100,000; None when unreadable."""
    if not isinstance(cron, str) or not cron.strip():
        return None
    from datetime import UTC, datetime, timedelta

    from croniter import CroniterBadCronError, croniter

    start = datetime(2026, 1, 5, tzinfo=UTC)
    end = start + timedelta(days=days)
    try:
        itr = croniter(cron.strip(), start)
        count = 0
        while count < 100_000 and itr.get_next(datetime) <= end:
            count += 1
        return count
    except (CroniterBadCronError, ValueError, TypeError, AttributeError):
        return None


def _filter_words(value: object) -> str:
    if not value:
        return ""
    if isinstance(value, dict):
        return ", ".join(f"{k} = {v}" for k, v in value.items() if v not in (None, "", [], {}))[:250]
    return str(value)[:250]


def automation_facts(assignment: Assignment, member: str) -> dict[str, Any]:
    """One assignment as the Automations table reads it; no id, secret or webhook URL."""
    raw = getattr(assignment, "trigger_config", None)
    config = raw if isinstance(raw, dict) else {}
    kind = str(assignment.trigger_type or "manual")
    event = ""
    if kind in {"integration_event", "platform_event"}:
        name = config.get("event") or config.get("event_type") or ""
        provider = config.get("provider") or ""
        event = " ".join(str(p) for p in (provider, name) if p)
        flt = _filter_words(config.get("filter"))
        if flt:
            event = f"{event} where {flt}" if event else flt
    elif kind == "work_item":
        prefix = config.get("correlation_prefix")
        event = f"work items keyed {prefix}*" if prefix else "work items assigned to the member"
    if getattr(assignment, "run_as_all_users", False):
        run_as = "every user"
    elif getattr(assignment, "run_as_user_id", None) is not None:
        run_as = "a named user"
    else:
        run_as = "its owner"
    return {
        "name": assignment.name,
        "member": member,
        "trigger": TRIGGER_WORDS.get(kind, kind),
        "schedule": describe_cron(config.get("cron"), config.get("timezone")) if kind == "schedule" else "",
        "event": event[:300],
        "enabled": bool(assignment.enabled),
        "run_as": run_as,
    }


#: The module of a trigger kind that is not an integration event.
TRIGGER_MODULES: dict[str, str] = {
    "schedule": "schedule",
    "platform_event": "platform",
    "work_item": "work_item",
    "webhook": "webhook",
    "manual": "manual",
}


def automation_module(assignment: Assignment) -> str | None:
    """The module an automation's trigger lives in (#9979): its provider for an event, else its kind; None when unknown.

    Never remapped: an event of a provider the catalog does not know has no module.
    """
    kind = str(assignment.trigger_type or "manual")
    if kind == "integration_event":
        raw = getattr(assignment, "trigger_config", None)
        provider = str((raw if isinstance(raw, dict) else {}).get("provider") or "").strip().lower()
        return provider if provider in doc_modules() else None
    return TRIGGER_MODULES.get(kind)


def trigger_cards(assignments: list[Assignment], facts: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    """One trigger card per automation, from the configuration (#9979); grouped by module past *limit*.

    *facts* are the :func:`automation_facts` of *assignments*, in the same order.
    A switched-off automation keeps its card, drawn with its state (``enabled``
    false; UX r1 B1 on #9988): a duplicated built-in fleet's automations are
    created off and still say what starts the fleet. A fleet with more
    automations than the page holds gets one card per module ("Daily brief and
    11 more"), on when any of them is on, so every module it listens to stays
    on the page. An automation whose module is unknown gets no card.
    """
    cards: list[dict[str, Any]] = []
    for assignment, fact in zip(assignments, facts, strict=False):
        module = automation_module(assignment)
        if module is None:
            continue
        card: dict[str, Any] = {
            "module": module,
            "name": str(fact["name"])[:120],
            "filter": str(fact.get("schedule") or fact.get("event") or "")[:300],
        }
        if not fact.get("enabled"):
            card["enabled"] = False
        cards.append(card)
    if len(cards) <= limit:
        return cards
    grouped: dict[str, list[dict[str, Any]]] = {}
    for card in cards:
        grouped.setdefault(card["module"], []).append(card)
    out: list[dict[str, Any]] = []
    for module, items in grouped.items():
        if len(items) == 1:
            out.append(items[0])
            continue
        name = f"{items[0]['name']} and {len(items) - 1} more"[:120]
        group: dict[str, Any] = {"module": module, "name": name, "filter": ", ".join(i["name"] for i in items)[:300]}
        if all(i.get("enabled") is False for i in items):
            group["enabled"] = False
        out.append(group)
    return out[:limit]


def schedule_quota(assignment: Assignment) -> dict[str, Any] | None:
    """The runs per month of a schedule automation, computed from its cron; None for any other trigger."""
    raw = getattr(assignment, "trigger_config", None)
    config = raw if isinstance(raw, dict) else {}
    if assignment.trigger_type != "schedule":
        return None
    runs = runs_per_month(config.get("cron"))
    if runs is None:
        return None
    return {
        "trigger": assignment.name[:120],
        "basis": f"Estimate: one run per tick of the schedule over {QUOTA_DAYS} days",
        "runs_min": runs if assignment.enabled else 0,
        "runs_max": runs,
    }


def automation_quota(assignment: Assignment, observed: int | None = None) -> dict[str, Any]:
    """The quota row of one automation (#10108): every trigger gets one, with its basis.

    A schedule's runs come from its cron (:func:`schedule_quota`). Any other
    trigger (event, platform event, work item, webhook, manual) has no fixed
    top: its row carries the runs observed over the last :data:`QUOTA_DAYS`
    days (*observed*) when there were any, else an explicit "no fixed
    estimate" basis, and ``open_ended`` so the page never reads the range as a
    ceiling. A switched-off automation's floor is 0.
    """
    scheduled = schedule_quota(assignment)
    if scheduled is not None:
        return {**scheduled, "kinds": ["schedule"]}
    kind = str(assignment.trigger_type or "manual")
    runs = max(0, int(observed or 0))
    started = "started by hand" if kind == "manual" else "event-driven"
    basis = (
        f"Observed: {runs:,} runs in the last {QUOTA_DAYS} days; {started}, no fixed top"
        if runs
        else f"{started[0].upper()}{started[1:]}, no fixed estimate"
    )
    return {
        "trigger": assignment.name[:120],
        "basis": basis[:200],
        "runs_min": runs if assignment.enabled else 0,
        "runs_max": runs,
        "open_ended": True,
        # The page words the row from these, in the reader's language (UX r1 B1, B3 on #10112); basis stays for the PDF.
        "kinds": ["manual" if kind == "manual" else "event"],
        "observed": runs > 0,
    }


def tool_use_facts(agent: Agent, member: str, mcp_names: dict[str, str], skill_names: dict[str, str]) -> dict[str, Any]:
    """What *agent* uses, by name: integrations (provider keys), MCP servers, tool groups, skills."""
    bindings = getattr(agent, "integration_bindings", None)
    providers = {str(k) for k in bindings} if isinstance(bindings, dict) else set()
    providers |= {str(p) for p in (getattr(agent, "integration_template_bindings", None) or []) if p}
    mcp = getattr(agent, "mcp_server_ids", None) or []
    skills = getattr(agent, "skill_ids", None) or []
    return {
        "member": member,
        "integrations": sorted(providers)[:20],
        "mcp_servers": sorted({mcp_names[str(i)] for i in mcp if str(i) in mcp_names})[:20],
        "tool_groups": sorted({str(g) for g in (getattr(agent, "builtin_tool_ids", None) or []) if g})[:40],
        "skills": sorted({skill_names[str(i)] for i in skills if str(i) in skill_names})[:20],
    }


def approval_facts(agent: Agent, assignments: list[Assignment]) -> list[str]:
    """Where a person approves before *agent* acts, from its human-in-the-loop and tool approval settings."""
    found: list[str] = []
    if getattr(agent, "human_in_the_loop", False):
        found.append("asks a person before acting (human in the loop)")
    if getattr(agent, "approve_tool_calls", None):
        found.append("each tool call waits for approval")
    for a in assignments:
        if getattr(a, "human_in_the_loop", None):
            found.append(f"automation '{a.name}' asks a person before acting")
    return found


# ── Context ─────────────────────────────────────────────────────────────────


def _excerpt(text: str | None, limit: int) -> str:
    text = (text or "").strip()
    return text if len(text) <= limit else text[:limit].rstrip() + "..."


def draft_context(
    fleet: Fleet,
    rows: list[FleetMember],
    agents: dict[uuid.UUID, Agent],
    assignments: list[Assignment],
    *,
    mcp_names: dict[str, str] | None = None,
    skill_names: dict[str, str] | None = None,
    observed_runs: dict[Any, int] | None = None,
) -> dict[str, Any]:
    """What the model drafts from, bounded; no id, secret or credential is included.

    ``quota`` holds one row per home-member automation (#10108,
    :func:`automation_quota`), *observed_runs* (runs of the last
    :data:`QUOTA_DAYS` days by assignment id) giving the event-driven ones their
    basis; ``schedule_quota`` is its schedule subset.

    ``automations``, ``tools``, ``approvals`` and ``schedule_quota`` are facts
    computed in code (#9768 S1): the generator writes them into the structured
    parts as they are, the model only narrates them.

    Only the HOME members' assignments count (#10094): a connected agent belongs
    to another fleet, its automations and their quota are that fleet's, so it
    appears under ``connected`` and nowhere in the automations, trigger cards or quota.
    """
    home = {r.agent_id for r in rows if r.agent_id is not None and r.role == "member"}
    assignments = [a for a in assignments if a.agent_id in home]
    name_of = {a.id: a.name for a in agents.values()}
    slug_of = {a.id: a.slug or a.name for a in agents.values()}
    member_slug = {r.id: slug_of.get(r.agent_id) for r in rows if r.agent_id is not None}
    by_agent: dict[uuid.UUID, list[dict[str, Any]]] = {}
    for a in assignments[:CONTEXT_MAX_ASSIGNMENTS]:
        by_agent.setdefault(a.agent_id, []).append(
            {"name": a.name, "trigger": a.trigger_type, "enabled": bool(a.enabled)}
        )
    members: list[dict[str, Any]] = []
    connected: list[dict[str, Any]] = []
    for row in rows:
        if row.agent_id is not None and row.role == "member":
            if len(members) >= CONTEXT_MAX_MEMBERS:
                continue
            agent = agents.get(row.agent_id)
            members.append(
                {
                    "agent": slug_of.get(row.agent_id, "agent"),
                    "name": name_of.get(row.agent_id, ""),
                    "role_label": row.role_label or "",
                    "orchestrator": row.agent_id == fleet.orchestrator_agent_id,
                    "parent": member_slug.get(row.parent_member_id) if row.parent_member_id else None,
                    "exposed": bool(row.is_exposed),
                    "description": _excerpt(getattr(agent, "description", None), _DESCRIPTION_CHARS),
                    "persona_excerpt": _excerpt(getattr(agent, "persona", None), PERSONA_EXCERPT_CHARS),
                    "handovers": sorted(
                        f"{slug_of.get(_as_uuid(t), 'agent')} ({(agent.handover_types or {}).get(str(t)) or 'delegate'})"
                        for t in (agent.handover_agent_ids or [])
                    )
                    if agent is not None
                    else [],
                    "conversation_shared": row.context_inclusion or fleet.default_context_inclusion or "none",
                    "collaboration_instruction": _excerpt(
                        row.collaboration_instruction or fleet.default_collaboration_instruction, _DESCRIPTION_CHARS
                    ),
                    "organizations": row.organization_scope or fleet.organization_scope,
                    "assignment_count": len(by_agent.get(row.agent_id, [])),
                    "assignments": by_agent.get(row.agent_id, []),
                }
            )
        else:
            # The row's effective settings, as the run reads them (#12099): its own instruction, else the
            # fleet default; never an empty one for a row that inherits.
            settings = effective_member_settings(fleet, row)
            connected.append(
                {
                    "kind": "agent" if row.agent_id is not None else "fleet",
                    "name": name_of.get(row.agent_id, "") if row.agent_id is not None else "connected fleet",
                    "collaboration_instruction": _excerpt(settings.collaboration_instruction, _DESCRIPTION_CHARS),
                    "conversation_shared": settings.context_inclusion,
                }
            )
    member_agents = [r.agent_id for r in rows if r.agent_id is not None and r.role == "member"][:CONTEXT_MAX_MEMBERS]
    # Every home automation the header counts (#10108), never cut at the model's per-member listing.
    bounded = assignments[:AUTOMATIONS_MAX]
    seen = observed_runs or {}
    tools = [
        tool_use_facts(agents[a], name_of.get(a, ""), mcp_names or {}, skill_names or {})
        for a in member_agents
        if a in agents
    ]
    approvals = {
        name_of.get(a, ""): found
        for a in member_agents
        if a in agents and (found := approval_facts(agents[a], [x for x in bounded if x.agent_id == a]))
    }
    automations = [automation_facts(a, name_of.get(a.agent_id, "")) for a in bounded]
    return {
        "fleet": {
            "name": fleet.name,
            "description": _excerpt(fleet.description, _DESCRIPTION_CHARS),
            "skills": fleet.skills,
        },
        "members": members,
        "connected": connected,
        "automations": automations,
        # The trigger cards the configuration gives (#9979): the model rewrites them, the page falls back to them.
        "trigger_cards": trigger_cards(bounded, automations, field_limit("triggers") or 20),
        "schedule_quota": [q for a in bounded if (q := schedule_quota(a))],
        "quota": [automation_quota(a, seen.get(getattr(a, "id", None))) for a in bounded],
        "tools": [t for t in tools if any(t[k] for k in ("integrations", "mcp_servers", "tool_groups", "skills"))],
        "approvals": approvals,
        **_gate_facts(fleet),
    }


def _gate_facts(fleet: Fleet) -> dict[str, Any]:
    """``{"gates": [...]}`` when *fleet* stores an approval gate catalog (#10000), else ``{}``.

    The catalog is configuration: the generator writes it into the structured
    parts as it is (:func:`with_computed_facts`), the model only narrates it.
    """
    stored = getattr(fleet, "approval_gates", None)
    if not isinstance(stored, list):
        return {}
    return {"gates": [dict(g) for g in stored if isinstance(g, dict)]}


def _as_uuid(value: object) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError):
        return None


def automations_by_member(context: dict[str, Any]) -> dict[str, list[str]]:
    """Each home member's automation names, keyed by :func:`member_key` of its name and of its slug (#10536)."""
    out: dict[str, list[str]] = {}
    for m in context.get("members") or []:
        names = [str(a.get("name") or "") for a in m.get("assignments") or [] if isinstance(a, dict)]
        for key in (m.get("name"), m.get("agent")):
            if key:
                out.setdefault(member_key(key), []).extend(n for n in names if n)
    return out


async def load_draft_context(db: AsyncSession, fleet: Fleet) -> dict[str, Any]:
    """:func:`draft_context` of *fleet* as stored now."""
    rows = await fleet_members(db, fleet.id)
    ids = [r.agent_id for r in rows if r.agent_id is not None]
    # The home members' assignments only (#10094): a connected agent's automations are its own fleet's.
    home_ids = [r.agent_id for r in rows if r.agent_id is not None and r.role == "member"]
    agents: dict[uuid.UUID, Agent] = {}
    assignments: list[Assignment] = []
    if ids:
        agents = {a.id: a for a in (await db.execute(select(Agent).where(Agent.id.in_(ids)))).scalars().all()}
    if home_ids:
        assignments = list(
            (
                await db.execute(
                    select(Assignment)
                    .where(Assignment.agent_id.in_(home_ids))
                    .order_by(Assignment.name)
                    .limit(max(AUTOMATIONS_MAX, CONTEXT_MAX_ASSIGNMENTS))
                )
            )
            .scalars()
            .all()
        )
    mcp_names, skill_names = await names_of_bindings(db, list(agents.values()))
    observed = await observed_runs(db, [a.id for a in assignments[:AUTOMATIONS_MAX] if a.trigger_type != "schedule"])
    return draft_context(
        fleet, rows, agents, assignments, mcp_names=mcp_names, skill_names=skill_names, observed_runs=observed
    )


async def observed_runs(db: AsyncSession, assignment_ids: list[uuid.UUID]) -> dict[uuid.UUID, int]:
    """Runs of each assignment over the last :data:`QUOTA_DAYS` days (#10108), one grouped count, no run read."""
    if not assignment_ids:
        return {}
    from datetime import UTC, datetime, timedelta

    from sqlalchemy import func

    from app.models.assignment import AssignmentRun

    since = datetime.now(UTC) - timedelta(days=QUOTA_DAYS)
    rows = (
        await db.execute(
            select(AssignmentRun.assignment_id, func.count())
            .where(AssignmentRun.assignment_id.in_(assignment_ids), AssignmentRun.created_at >= since)
            .group_by(AssignmentRun.assignment_id)
        )
    ).all()
    return {aid: int(n) for aid, n in rows if aid is not None}


async def names_of_bindings(db: AsyncSession, agents: list[Agent]) -> tuple[dict[str, str], dict[str, str]]:
    """The names of the MCP servers and skills *agents* are bound to, keyed by id string (ids stay here)."""
    from app.models.agent_skill import AgentSkill
    from app.models.mcp_server import MCPServer

    mcp_ids = {u for a in agents for i in (a.mcp_server_ids or []) if (u := _as_uuid(i))}
    skill_ids = {u for a in agents for i in (a.skill_ids or []) if (u := _as_uuid(i))}
    mcp_names: dict[str, str] = {}
    skill_names: dict[str, str] = {}
    if mcp_ids:
        rows = (await db.execute(select(MCPServer.id, MCPServer.name).where(MCPServer.id.in_(mcp_ids)))).all()
        mcp_names = {str(i): n for i, n in rows}
    if skill_ids:
        rows = (await db.execute(select(AgentSkill.id, AgentSkill.name).where(AgentSkill.id.in_(skill_ids)))).all()
        skill_names = {str(i): n for i, n in rows}
    return mcp_names, skill_names


# ── Prompt ──────────────────────────────────────────────────────────────────

_SYSTEM = """You write the documentation of an agent fleet on an AI agent platform: a group of agents
around an orchestrator that routes work, with handovers between members, assignments (automations)
and connected agents or fleets. Readers are the people who use or operate the fleet.

Answer with ONE JSON object and nothing else: each key a section to write, each value markdown text.
Example: {"purpose": "Triages incoming alerts so analysts see the ones that matter.",
"members": "- **Desk Lead**: routes each alert to the analyst."}
When structured parts are asked for, answer {"sections": {...the sections...}, "structured": {...},
"findings": [...]} instead.

Rules:
- Write only from the facts given. Never invent a member, tool, integration, schedule or figure.
  When a fact is missing, say plainly that it is missing.
- "automations", "tools", "approvals", "gates" and "schedule_quota" are computed from the configuration:
  narrate them, never contradict them.
- Name members by their name, never by an id.
- Plain, short sentences; bullets for lists. No heading at the top of a section.
- The quota estimate is labelled as an estimate.
- Write in the language of the current documentation when there is one, else in English.
- A section explains the fleet; it never comments on its configuration. No sentence like "its description
  also names...", "the owner should confirm...", "no X is listed". What looks wrong or missing in the
  configuration goes under "findings" instead (when the answer is {"sections": ...}), each
  {"kind": one of description_mismatch, missing_automation, no_gate_on_sensitive_action,
  missing_integration, other, "member": the member's exact name or "",
  (a member without a role label is computed in code: never raise it),
  "subject": the exact name of the automation, integration, tool or setting concerned, or "",
  "finding": one line, "suggested_fix": one line saying what to change}. No finding: "findings": [].
- A member's "assignments" are all of its automations: never say an automation is missing when it is listed.
- "decided" lists points the fleet's managers already decided: never raise them as findings."""


#: The structured parts the model maps (the page draws them); automations and integrations are computed.
STRUCTURED_GUIDE = f"""Structured parts to write under "structured" (every field optional, omit what the facts do not support):
- purpose_line: one sentence, at most 25 words; outcome_line: what changes for the users, one sentence
- serves: [who the fleet serves, each a short label of 1 to 3 words], at most 6
- members: [{{"name": the member's exact name, "role": its role in at most 12 words}}], one per member, no meta remark
- triggers: [{{"module": M, "name": automation name, "filter": the filter in plain words, "detail": one reader sentence, never a work item key or a cron expression}}],
  at most 20: start from "trigger_cards" (computed from the automations) and keep every module they name
- steps: [{{"title": at most 8 words, "detail": the step in markdown, "gate": true when a person approves first}}],
  3 to 10 steps, near-duplicate steps merged into one. Each "detail" is 25 to 40 words: 2 to 4 sentences,
  or 1 sentence and up to 3 "- " bullets. It says who acts (member names in **bold**), what starts the step,
  what it produces and where it goes next (a member, a work item, an integration such as Teams or Notion),
  and whether a person approves. Markdown only: **bold**, *italic*, `code`, "- " bullets, links;
  no heading, no HTML, no table. Name members, integrations and object types from the facts only;
  never a schedule, a count or a figure (other parts show them), never a remark about the configuration
- gates: [{{"action": what needs a person, "module": M, "approver": who}}], one per approval in "approvals"
  (left out when the fleet gives "gates": its approval gate catalog is kept as it is)
- objects: [{{"module": M, "objects": [object kinds created or updated]}}], from the members' tools only
- prerequisites: [{{"check": one of {", ".join(PREREQUISITE_CHECKS)}, "label": plain words}}]; at most 2
  "manual" lines, only for what no system can read (a contract signed, a person briefed), the label saying
  why it cannot be checked; never a manual line for an integration, account, agent, knowledge base,
  MCP server, automation or profile field of the facts: the page checks those itself
- quota: [{{"trigger": automation name, "basis": "Estimate: ...", "runs_min": n, "runs_max": n}}] for
  event-driven automations without observed runs only (each row of "quota" is computed; runs_min <= runs_max)
M is one of {", ".join(DOC_MODULES)}, or the provider key of an integration a member uses
(the keys under "tools" integrations and the modules of "trigger_cards", e.g. microsoft, github, notion)."""

#: What a whole draft asks once more when its answer carried no structured part (#9979).
STRUCTURED_ONLY = """Your answer carried no structured parts. Answer with ONE JSON object {"structured": {...}} and nothing else,
from the same facts."""


#: How the previous version is handed to the model: for its tone and language, never for its facts (#12099).
#: A connected agent's collaboration instruction rewritten since must be quoted as the fleet states it now.
CURRENT_DOC_HEADER = (
    "The current documentation, for tone and language only: where it differs from the fleet above"
    " (a member, a connected agent's collaboration instruction, an automation), the fleet above wins"
    " and the old text is not repeated:"
)


def build_messages(
    context: dict[str, Any], current: dict[str, str], wanted: list[str], *, structured: bool = False
) -> list[ChatMessage]:
    """The chat messages that ask for *wanted* sections, and the structured parts when *structured*."""
    guide = "\n".join(f"- {k}: {SECTION_GUIDE[k]}" for k in wanted)
    user = f"Sections to write:\n{guide}\n\nThe fleet:\n{json.dumps(context, indent=1, default=str)}"
    if current:
        user += f"\n\n{CURRENT_DOC_HEADER}\n{json.dumps(current, indent=1)}"
    if structured:
        user += f"\n\n{STRUCTURED_GUIDE}"
    return [ChatMessage(role="system", content=_SYSTEM), ChatMessage(role="user", content=user)]


# ── Parsing ─────────────────────────────────────────────────────────────────

_FENCE = re.compile(r"```(?:json)?\s*\n(.*?)\n```", re.S)


def extract_sections(text: str, wanted: list[str]) -> FleetDocDraft:
    """The *wanted* sections of a model answer; anything else is dropped and noted."""
    raw = (text or "").strip()
    fenced = _FENCE.search(raw)
    if fenced:
        raw = fenced.group(1).strip()
    elif not raw.startswith("{") and "{" in raw and "}" in raw:
        raw = raw[raw.index("{") : raw.rindex("}") + 1]
    try:
        data = json.loads(raw) if raw else None
    except ValueError:
        data = None
    if not isinstance(data, dict):
        raise FleetDocDraftError("The model answered no documentation. Try again.")
    notes: list[dict[str, Any]] = []
    model_structured: dict[str, Any] | None = None
    findings: list[dict[str, Any]] = []
    if isinstance(data.get("sections"), dict):
        # The shape asked for with the structured parts; the flat shape still parses.
        raw_structured = data.get("structured")
        model_structured = raw_structured if isinstance(raw_structured, dict) else None
        findings = clean_findings(data.get("findings"))
        data = data["sections"]
    sections: dict[str, str] = {}
    for key, value in data.items():
        if key not in wanted:
            notes.append(doc_note("section_left_out"))
            continue
        text_value = value if isinstance(value, str) else ""
        text_value = text_value.strip()
        if not text_value:
            continue
        if len(text_value) > SECTION_MAX_CHARS:
            text_value = text_value[:SECTION_MAX_CHARS]
            notes.append(doc_note("section_cut", max=SECTION_MAX_CHARS))
        sections[key] = text_value
    missing = [k for k in wanted if k not in sections]
    if missing:
        notes.append(doc_note("sections_not_drafted", count=len(missing)))
    if not sections:
        raise FleetDocDraftError("The model drafted no section. Try again.")
    # A meta-comment the prompt forbids still written in a section is moved out as a finding (#10246).
    sections, moved = move_meta_comments(sections)
    return FleetDocDraft(
        sections={k: sections[k] for k in DOC_SECTIONS if k in sections},
        findings=merge_findings(findings, moved),
        notes=notes,
        structured=model_structured,
    )


def build_structured(
    model_part: dict[str, Any] | None,
    context: dict[str, Any],
    notes: list[dict[str, Any]],
    previous: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """The structured parts: the model's mapping field by field, the computed facts on top; validated.

    A field the model got wrong (unknown module, unknown prerequisite check,
    runs_max below runs_min, a value too long) is dropped and named in *notes*,
    never guessed. ``automations`` and ``integrations`` always come from the
    configuration; a schedule's quota from its cron, the model's estimate kept
    for the other triggers only. A block neither the model nor the configuration
    gives is kept from *previous*, the version replaced (UX r1 B1 on #9988).
    """
    merged: dict[str, Any] = {}
    # Every automation's row is computed (#10108): an invalid AI row of one of them is replaced, never left out.
    scheduled = {
        str(q.get("trigger"))
        for q in [*(context.get("schedule_quota") or []), *(context.get("quota") or [])]
        if isinstance(q, dict)
    }
    for key, value in (model_part or {}).items():
        if key not in STRUCTURED_FIELDS or key in {"automations", "integrations"}:
            notes.append(doc_note("part_not_model"))
            continue
        if key == "quota" and isinstance(context.get("quota"), list):
            # The quota is computed, one row per automation (#10108, #10148): the model's rows are only the
            # estimates with_computed_facts may read by automation name; an extra or malformed row is ignored,
            # never validated here, so nothing of it is ever "left out" on the page.
            merged[key] = [
                repaired_quota(q) for q in (value if isinstance(value, list) else [value]) if isinstance(q, dict)
            ]
            continue
        limit = field_limit(key)
        if limit is not None and isinstance(value, list) and len(value) > limit:
            notes.append(doc_note("part_cut", block=key, kept=limit, total=len(value)))
            value = value[:limit]
        # A list is validated item by item: one wrong item never costs the others.
        kept: list[Any] = []
        invalid = 0
        for item in value if isinstance(value, list) else [value]:
            if key == "prerequisites" and isinstance(item, dict):
                lines = split_prerequisite(item)
                if len(lines) > 1:
                    # One live check per integration it names (#10108); each line validated as the others.
                    kept.extend(
                        line["prerequisites"][0]
                        for line in (_clean_or_none({key: [one]}) for one in lines)
                        if line and line.get("prerequisites")
                    )
                    continue
                item = lines[0]
            if key == "quota" and isinstance(item, dict):
                item = repaired_quota(item)
            try:
                cleaned = clean_structured({key: [item] if isinstance(value, list) else item})
            except FleetActionRefusedError:
                if key == "quota" and isinstance(item, dict) and str(item.get("trigger")) in scheduled:
                    # The automation's row is computed (with_computed_facts): nothing is lost (#10094, #10108).
                    continue
                if key == "triggers" and context.get("trigger_cards"):
                    invalid += 1  # repaired below from the configuration (#10094)
                    continue
                notes.append(doc_note("part_item_left_out", block=key))
                continue
            if cleaned and key in cleaned:
                kept.extend(cleaned[key] if isinstance(value, list) else [cleaned[key]])
        if invalid:
            kept = repaired_triggers(kept, context, limit or len(kept) + invalid)
        if kept:
            merged[key] = kept if isinstance(value, list) else kept[0]
    merged = with_derived_parts(with_computed_facts(merged, context, notes), context, notes)
    merged = with_kept_parts(merged, previous, notes)
    # Field by field (#9979): one wrong field costs that field, never the block and its computed facts.
    return clean_structured_fields(merged, notes)


def _clean_or_none(part: dict[str, Any]) -> dict[str, Any] | None:
    try:
        return clean_structured(part)
    except FleetActionRefusedError:
        return None


def repaired_quota(item: dict[str, Any]) -> dict[str, Any]:
    """*item* with its range put in order when the model wrote runs_max below runs_min (#10094), never dropped for it."""
    low, high = item.get("runs_min"), item.get("runs_max")
    if isinstance(low, int) and isinstance(high, int) and high < low:
        return {**item, "runs_min": high, "runs_max": low}
    return item


def repaired_triggers(kept: list[dict[str, Any]], context: dict[str, Any], limit: int) -> list[dict[str, Any]]:
    """*kept* (the model's valid trigger cards) completed with the computed cards it does not name (#10094).

    Called when an entry the model wrote was invalid: instead of losing that
    trigger, every card of :func:`trigger_cards` whose automation the model's
    valid cards do not name is added, so the page keeps one card per automation
    of the home members. Never past *limit*.
    """
    named = {str(card.get("name") or "").strip().lower() for card in kept}
    out = list(kept)
    for card in context.get("trigger_cards") or []:
        if len(out) >= limit:
            break
        if isinstance(card, dict) and str(card.get("name") or "").strip().lower() not in named:
            out.append(dict(card))
    return out


#: Words of a manual prerequisite that make it a connection to check live.
_CONNECT_WORDS = ("connect", "integration", "bound", "configured", "set up")


def typed_prerequisite(item: dict[str, Any]) -> dict[str, Any]:
    """*item* with a check the page evaluates live wherever its words name one (#10094).

    A ``manual`` line (or an unknown check) whose label names one integration
    provider of the catalog and a connection ("OpenCTI connected") becomes
    ``integration:<provider>``; an unknown check that names none becomes
    ``manual`` with its label kept, never dropped. A known check is unchanged.
    """
    from app.api.v1.integration_catalog import PROVIDER_CATALOG

    check = str(item.get("check") or "").strip()
    if check != "manual" and prerequisite_check_known(check):
        return item
    label = str(item.get("label") or "")
    lowered = label.lower()
    found: list[str] = []
    if any(word in lowered for word in _CONNECT_WORDS):
        for key, entry in PROVIDER_CATALOG.items():
            brand = str(entry.get("name") or "") if isinstance(entry, dict) else ""
            if len(brand) < 4 or not prerequisite_check_known(f"integration:{key}"):
                continue
            if re.search(rf"\b{re.escape(brand.lower())}\b", lowered):
                found.append(key)
    if len(found) == 1:
        return {**item, "check": f"integration:{found[0]}", "fix_route": item.get("fix_route") or INTEGRATIONS_ROUTE}
    return {**item, "check": "manual"}


def split_prerequisite(item: dict[str, Any]) -> list[dict[str, Any]]:
    """*item* as one live check per integration it names (#10108), else :func:`typed_prerequisite` of it.

    "Connect Notion, GitHub and HubSpot" written ``manual`` becomes three
    ``integration:<provider>`` lines ("Notion connected"...), each evaluated for
    the reader; a label naming one provider or none is typed as before.
    """
    from app.api.v1.integration_catalog import PROVIDER_CATALOG

    typed = typed_prerequisite(item)
    if typed.get("check") != "manual":
        return [typed]
    lowered = str(item.get("label") or "").lower()
    if not any(word in lowered for word in _CONNECT_WORDS):
        return [typed]
    found: list[tuple[int, str, str]] = []
    for key, entry in PROVIDER_CATALOG.items():
        brand = str(entry.get("name") or "") if isinstance(entry, dict) else ""
        if len(brand) < 4 or not prerequisite_check_known(f"integration:{key}"):
            continue
        match = re.search(rf"\b{re.escape(brand.lower())}\b", lowered)
        if match:
            found.append((match.start(), key, brand))
    if len(found) < 2:
        return [typed]
    route = item.get("fix_route") or INTEGRATIONS_ROUTE
    return [
        {"check": f"integration:{key}", "label": f"{brand} connected"[:200], "fix_route": route}
        for _, key, brand in sorted(found)
    ]


def derived_steps(context: dict[str, Any]) -> list[dict[str, Any]]:
    """Minimal steps from the configuration (#9979): the orchestrator, then the members it hands over to."""
    members = [m for m in context.get("members") or [] if isinstance(m, dict)]
    approvals = context.get("approvals") or {}
    ordered = sorted(members, key=lambda m: (not m.get("orchestrator"), m.get("parent") is not None))
    steps: list[dict[str, Any]] = []
    for m in ordered:
        name = str(m.get("name") or m.get("agent") or "").strip()
        if not name:
            continue
        role = str(m.get("role_label") or "").strip()
        what = role or _excerpt(str(m.get("description") or ""), 200)
        title = f"{name} routes the work" if m.get("orchestrator") else name
        steps.append({"title": title[:120], "detail": what[:500], "gate": bool(approvals.get(name))})
    return steps


def derived_gates(context: dict[str, Any]) -> list[dict[str, Any]]:
    """One gate per approval setting of the configuration (#9979), on the platform."""
    gates: list[dict[str, Any]] = []
    for member, found in (context.get("approvals") or {}).items():
        for what in found or []:
            gates.append({"action": f"{member}: {what}"[:200], "module": "platform", "approver": "a person"})
    return gates


#: Where the reader fixes a missing prerequisite.
INTEGRATIONS_ROUTE = "/integrations"


def derived_prerequisites(context: dict[str, Any], notes: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    """The prerequisites the configuration states (UX r1 B2 on #9988), each a live check of the checklist.

    One "connected" check per integration provider a member uses (Outlook,
    GitHub, Notion...: ``integration:<provider>``), then one manual check when
    automations wait to be switched on. Nothing is invented: a fleet that uses
    no integration and has every automation on derives nothing. Past the
    block's limit the last providers are grouped in one manual line that names
    them all, with a note (UX r2 B2 on #9988): never a silent cut.
    """
    from app.api.v1.integration_catalog import PROVIDER_CATALOG

    providers: list[str] = []
    for use in context.get("tools") or []:
        for provider in (use.get("integrations") or []) if isinstance(use, dict) else []:
            key = str(provider).strip().lower()
            if key not in providers and prerequisite_check_known(f"integration:{key}"):
                providers.append(key)
    out: list[dict[str, Any]] = []
    for key in providers:
        entry = PROVIDER_CATALOG.get(key)
        brand = str(entry.get("name") or key) if isinstance(entry, dict) else key
        out.append({"check": f"integration:{key}", "label": f"{brand} connected"[:200], "fix_route": INTEGRATIONS_ROUTE})
    off = sum(1 for a in context.get("automations") or [] if isinstance(a, dict) and not a.get("enabled"))
    manual = (
        [
            {
                # Evaluated live (#10094): met once one of the fleet's automations is on.
                "check": AUTOMATIONS_CHECK,
                "label": f"Switch on the automations you want ({off} switched off)"[:200],
                "fix_route": "",
            }
        ]
        if off
        else []
    )
    limit = field_limit("prerequisites") or len(out) + len(manual)
    room = limit - len(manual)
    if len(out) > room:
        head, tail = out[: room - 1], out[room - 1 :]
        brands = ", ".join(item["label"].removesuffix(" connected") for item in tail)
        head.append(
            {
                "check": "manual",
                "label": f"Connect {len(tail)} more integrations: {brands}"[:200],
                "fix_route": INTEGRATIONS_ROUTE,
            }
        )
        # Grouping is a display choice (#10246): no note.
        out = head
    return out + manual


def with_derived_parts(
    parts: dict[str, Any], context: dict[str, Any], notes: list[dict[str, Any]]
) -> dict[str, Any]:
    """*parts* with the triggers, steps, gates and prerequisites derived from the configuration where the model gave none.

    A section with a structured kind always draws its block (#9979): the page
    never falls back to the markdown for it. Each derived part is named in *notes*.
    """
    merged = dict(parts)
    derived = {
        "triggers": list(context.get("trigger_cards") or []),
        "steps": derived_steps(context),
        "gates": derived_gates(context),
        "prerequisites": derived_prerequisites(context, notes if not merged.get("prerequisites") else None),
    }
    for key, value in derived.items():
        if merged.get(key) or not value:
            continue
        merged[key] = value
        notes.append(doc_note("part_derived", block=key))
    return merged


#: The blocks a regenerate keeps from the version it replaces when neither the model nor the configuration gives one.
#: ``gates`` is never kept (#393): the fleet's level acts on the documented gates when it stores no catalog
#: (``fleet_autonomy.fleet_gates``), so a gate the configuration no longer gives must leave with the version.
KEPT_BLOCKS: tuple[str, ...] = (
    "purpose_line",
    "outcome_line",
    "serves",
    "members",
    "triggers",
    "steps",
    "objects",
    "prerequisites",
)


def with_kept_parts(
    parts: dict[str, Any], previous: dict[str, Any] | None, notes: list[dict[str, Any]]
) -> dict[str, Any]:
    """*parts* with each block of :data:`KEPT_BLOCKS` the version replaced drew and this one lacks (UX r1 B1 on #9988).

    A duplicated built-in fleet regenerated keeps every block it shipped with;
    each kept block is named in *notes*. The computed blocks (automations,
    integrations, quota) are never kept: they follow the configuration.
    """
    merged = dict(parts)
    before = previous if isinstance(previous, dict) else {}
    for key in KEPT_BLOCKS:
        if merged.get(key) or not before.get(key):
            continue
        merged[key] = before[key]
        notes.append(doc_note("part_kept", block=key))
    return merged


def group_quota(quota: list[dict[str, Any]], limit: int) -> tuple[list[dict[str, Any]], int]:
    """*quota* in at most *limit* rows, the total kept (UX r1 B5 on #9988); answers the rows and how many were grouped.

    Past *limit*, the last rows are summed into one line ("Daily brief 20 and
    10 more"), as the trigger cards group: the hero's range, the sum of the
    rows, stays the true figure.
    """
    if len(quota) <= limit:
        return quota, 0
    # The largest rows stay on their own line (#10094); the smallest are grouped, never an alphabetical cut.
    quota = sorted(quota, key=lambda q: -int(q.get("runs_max") or 0))
    head, tail = quota[: limit - 1], quota[limit - 1 :]
    grouped = {
        "trigger": f"{tail[0].get('trigger', '')} and {len(tail) - 1} more"[:120],
        "basis": str(tail[0].get("basis") or "")[:300],
        "runs_min": sum(int(q.get("runs_min") or 0) for q in tail),
        "runs_max": sum(int(q.get("runs_max") or 0) for q in tail),
    }
    if not grouped["basis"]:
        grouped.pop("basis")
    if any(q.get("open_ended") for q in tail):
        # One event-driven row in the group keeps the group's range open (#10108).
        grouped["open_ended"] = True
    kinds = sorted({str(k) for q in tail for k in (q.get("kinds") or [])})
    if kinds:
        # Every trigger kind of the group, so the page never words a mixed group as one kind (UX r1 B1 on #10112).
        grouped["kinds"] = kinds
    return [*head, grouped], len(tail)


def _computed_quota(model_rows: list[dict[str, Any]], context: dict[str, Any]) -> list[dict[str, Any]]:
    """One row per automation (#10108): the schedules from their cron; an event-driven row the model estimated
    keeps the model's figures under the computed basis only when nothing was observed, else the observed row."""
    rows = context.get("quota")
    if not isinstance(rows, list):
        return list(context.get("schedule_quota") or [])
    written = {str(q.get("trigger")): q for q in model_rows if isinstance(q, dict)}
    out: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        estimate = written.get(str(row.get("trigger")))
        if row.get("open_ended") and not row.get("runs_max") and estimate is not None:
            low, high = estimate.get("runs_min"), estimate.get("runs_max")
            if _is_count(low) and _is_count(high) and low <= high:
                basis = str(estimate.get("basis") or row.get("basis") or "")
                row = {**row, "basis": basis[:200], "runs_min": low, "runs_max": high, "observed": False}
        out.append(dict(row))
    return out


def _is_count(value: Any) -> TypeGuard[int]:
    """True for a run count a quota row stores (an int, never a bool, within :class:`DocQuota`'s bounds)."""
    return isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 100_000


def with_computed_facts(
    parts: dict[str, Any] | None, context: dict[str, Any], notes: list[dict[str, Any]] | None = None
) -> dict[str, Any]:
    """*parts* with the facts computed from the configuration on top (#9768 S5).

    ``automations`` and ``integrations`` always come from :func:`draft_context`;
    a schedule's quota from its cron, the other triggers' estimate kept. The
    generator and the built-in seed share it, so a shipped documentation and a
    regenerated one carry the same computed blocks. Past :data:`QUOTA_ROWS_MAX`
    rows the last ones are grouped, never dropped, and noted.
    """
    merged = dict(parts or {})
    computed_quota = _computed_quota(merged.get("quota") or [], context)
    if isinstance(context.get("quota"), list):
        # Every automation has its computed row (#10108): the rows are exactly those, a row naming no
        # automation (an AI extra, a shipped estimate of a removed one) never reaches the page (#10148).
        quota = computed_quota
        merged.pop("quota", None)
    else:
        scheduled = {q["trigger"] for q in computed_quota}
        quota = [q for q in merged.get("quota") or [] if q.get("trigger") not in scheduled] + computed_quota
    if quota:
        # Grouping past QUOTA_ROWS_MAX is a display choice (#10246): no note, the total is unchanged.
        merged["quota"], _ = group_quota(quota, QUOTA_ROWS_MAX)
    automations = list(context.get("automations") or [])
    if automations:
        merged["automations"] = automations
    tools = list(context.get("tools") or [])
    if tools:
        merged["integrations"] = tools
    if isinstance(context.get("gates"), list):
        # The fleet's approval gate catalog (#10000) wins over the model's
        # gates: a regenerate never removes, re-keys or weakens an enforced gate.
        gates = [dict(g) for g in context["gates"]]
        if gates:
            merged["gates"] = gates
        else:
            merged.pop("gates", None)
    return merged


# ── Draft ───────────────────────────────────────────────────────────────────


async def configuration_facts(db: AsyncSession, fleet: Fleet) -> dict[str, Any] | None:
    """The structured blocks computed from *fleet*'s configuration alone, no model call (#10316).

    What the About page draws for a fleet with no documentation version yet
    (automations, triggers, steps, gates, integrations, quota), the same
    computed blocks a generated version carries; ``None`` when the fleet has none.
    """
    context = await load_draft_context(db, fleet)
    notes: list[dict[str, Any]] = []
    return clean_structured_fields(with_derived_parts(with_computed_facts({}, context, notes), context, notes), notes)


# ── Calls that never fail for their size (#11292) ───────────────────────────

#: What a cut answer is continued with: the partial answer is handed back as the assistant's turn.
CONTINUE = """Your answer was cut at its length limit. Continue it exactly where it stopped: no repetition,
no preamble, nothing else."""

#: The system prompt of one section drafted on its own in plain markdown, the last split of a draft.
_SECTION_ONLY = """You write ONE section of the documentation of an agent fleet on an AI agent platform, from the
facts given only. Answer with the section's markdown text and nothing else: no JSON, no heading at the top,
plain short sentences, bullets for lists. Never invent a member, tool, integration, schedule or figure, and
name members by their name. The section explains the fleet; it never comments on its configuration.
Write in the language of the current documentation when there is one, else in English."""

#: The structured parts the model writes (``STRUCTURED_GUIDE``); the other fields are computed.
MODEL_PARTS: tuple[str, ...] = (
    "purpose_line",
    "outcome_line",
    "serves",
    "members",
    "triggers",
    "steps",
    "gates",
    "objects",
    "prerequisites",
    "quota",
)


def output_budget(llm: object, model: str | None) -> int:
    """The output budget of one call: the model's own ceiling, capped at :data:`DRAFT_BUDGET_MAX`.

    Read from the provider and model in use (``model_limits``), never hard-coded: a model with a 4k
    ceiling is asked for 4k and its longer answers are continued. :data:`DRAFT_MAX_TOKENS` when unknown.
    """
    ceiling = resolve_provider_max_output_tokens(llm, model)
    if ceiling is None:
        return DRAFT_MAX_TOKENS
    return max(1, min(ceiling, DRAFT_BUDGET_MAX))


def is_transient(exc: BaseException) -> bool:
    """A provider failure worth asking again: a timeout, a dropped connection, a rate limit, a server error.

    Not one the provider says not to retry (a spent quota, a long circuit-open).
    """
    if told_not_to_retry(exc):
        return False
    if isinstance(exc, TimeoutError | ConnectionError):
        return True
    name = type(exc).__name__
    if "Timeout" in name or "Connection" in name:
        return True
    status = error_status(exc)
    return status is not None and (status == 429 or status >= 500)


def _provider_failure(exc: BaseException) -> bool:
    """A failure of the model call itself (transient or an HTTP answer), as opposed to a defect of this code."""
    return is_transient(exc) or error_status(exc) is not None


def failure_cause(exc: BaseException) -> str:
    """The one honest line a person reads when nothing could be produced: the provider's own reason."""
    return f"The AI model did not answer: {api_error_message(exc)}"


async def ask(llm: object, model: str | None, messages: list[ChatMessage], budget: int) -> tuple[str, bool]:
    """The model's whole answer to *messages*, and whether it is still cut.

    A transient provider failure is asked again with backoff (:data:`TRANSIENT_RETRIES`). An answer cut
    at *budget* is continued where it stopped, the partial answer handed back as the assistant's turn,
    at most :data:`CONTINUATIONS_MAX` times; the pieces are joined. Any other failure propagates.
    """
    chat = llm.chat  # type: ignore[attr-defined]
    asked = list(messages)
    text = ""
    for _ in range(CONTINUATIONS_MAX + 1):
        response = await retry_llm_call(
            functools.partial(chat, asked, model=model, temperature=0.3, max_tokens=budget),
            is_retryable=is_transient,
            max_retries=TRANSIENT_RETRIES,
            base_delay=TRANSIENT_BASE_DELAY,
        )
        piece = getattr(response, "content", "") or ""
        text += piece
        if not answer_was_cut(response):
            return text, False
        if not piece:
            break
        asked = [
            *messages,
            ChatMessage(role="assistant", content=text),
            ChatMessage(role="user", content=CONTINUE),
        ]
    logger.info("Fleet documentation: an answer still cut after %d continuation(s)", CONTINUATIONS_MAX)
    return text, True


@dataclass
class _Parts:
    """Sections drafted in parts: what was written, the findings, the notes, and what the provider failed on."""

    sections: dict[str, str] = field(default_factory=dict)
    findings: list[dict[str, Any]] = field(default_factory=list)
    notes: list[dict[str, Any]] = field(default_factory=list)
    pending: list[str] = field(default_factory=list)
    failure: BaseException | None = None

    def add(self, other: _Parts) -> None:
        self.sections.update(other.sections)
        self.findings = merge_findings(self.findings, other.findings)
        self.notes.extend(other.notes)
        self.pending.extend(other.pending)
        if other.failure is not None and (self.failure is None or told_not_to_retry(other.failure)):
            self.failure = other.failure

    @property
    def refused(self) -> bool:
        """The provider said not to retry (a spent quota): every other part would meet the same refusal."""
        return self.failure is not None and told_not_to_retry(self.failure)

    async def add_each_alone(self, keys: list[str], draft_one: Callable[[str], Awaitable[_Parts]]) -> None:
        """*keys* drafted one at a time, the rest left pending unasked once the provider refuses."""
        for index, key in enumerate(keys):
            if self.refused:
                self.pending.extend(keys[index:])
                return
            self.add(await draft_one(key))


def _kept_as_written(text: str) -> str:
    """A section still cut after its continuations, back to its last whole line or sentence."""
    raw = (text or "").strip()
    fenced = re.match(r"^```\w*\s*\n(.*?)(?:\n```)?$", raw, re.S)
    if fenced:
        raw = fenced.group(1).strip()
    end = max(raw.rfind("\n"), raw.rfind(". ") + 1, raw.rfind(".\n") + 1)
    if end > len(raw) // 2:
        raw = raw[:end].rstrip()
    return raw


async def _one_section_markdown(
    llm: object, model: str | None, budget: int, context: dict[str, Any], current: dict[str, str], key: str
) -> _Parts:
    """*key* drafted alone in plain markdown, the last split: kept as written when still cut, never an error."""
    user = f"Section to write: {key}: {SECTION_GUIDE[key]}\n\nThe fleet:\n{json.dumps(context, indent=1, default=str)}"
    if current:
        user += f"\n\n{CURRENT_DOC_HEADER}\n{json.dumps(current, indent=1)}"
    messages = [ChatMessage(role="system", content=_SECTION_ONLY), ChatMessage(role="user", content=user)]
    try:
        text, cut = await ask(llm, model, messages, budget)
    except Exception as exc:
        if not _provider_failure(exc):
            raise
        return _Parts(pending=[key], failure=exc)
    body = _kept_as_written(text) if cut else (text or "").strip()
    out = _Parts()
    if not body:
        out.notes.append(doc_note("sections_not_drafted", count=1))
        return out
    if cut or len(body) > SECTION_MAX_CHARS:
        body = body[:SECTION_MAX_CHARS]
        out.notes.append(doc_note("section_cut", max=len(body)))
    cleaned, moved = move_meta_comments({key: body})
    out.sections = cleaned
    out.findings = moved
    return out


async def sections_in_parts(
    llm: object,
    model: str | None,
    budget: int,
    context: dict[str, Any],
    current: dict[str, str],
    wanted: list[str],
) -> _Parts:
    """The *wanted* sections, asked together and split in halves while an answer is cut or unreadable.

    One section left whose answer is still cut or unreadable is drafted in plain markdown
    (:func:`_one_section_markdown`). A group the provider fails on after its retries is asked one
    section at a time, so a failure costs only its own section, which is returned as *pending*.
    The size of the fleet never decides whether a draft is produced.
    """
    try:
        text, cut = await ask(llm, model, build_messages(context, current, wanted), budget)
        if not cut:
            got = extract_sections(text, wanted)
            out = _Parts(
                sections=dict(got.sections),
                findings=got.findings,
                notes=[n for n in got.notes if n.get("code") == "section_cut"],
            )
            missing = [k for k in wanted if k not in got.sections]
            await out.add_each_alone(
                missing,
                lambda key: _one_section_markdown(llm, model, budget, context, {**current, **out.sections}, key),
            )
            return out
    except FleetDocDraftError:
        pass  # unreadable: split below
    except Exception as exc:
        if not _provider_failure(exc):
            raise
        if len(wanted) == 1 or told_not_to_retry(exc):
            return _Parts(pending=list(wanted), failure=exc)
        out = _Parts()
        await out.add_each_alone(
            wanted, lambda key: _one_section_markdown(llm, model, budget, context, {**current, **out.sections}, key)
        )
        return out
    if len(wanted) == 1:
        return await _one_section_markdown(llm, model, budget, context, current, wanted[0])
    half = len(wanted) // 2
    out = await sections_in_parts(llm, model, budget, context, current, wanted[:half])
    if out.refused:
        out.pending.extend(wanted[half:])
        return out
    out.add(await sections_in_parts(llm, model, budget, context, {**current, **out.sections}, wanted[half:]))
    return out


def _parts_of(text: str, allowed: tuple[str, ...]) -> dict[str, Any] | None:
    """The structured parts of an answer, ``{"structured": {...}}`` or bare; None when it carries none."""
    raw = (text or "").strip()
    fenced = _FENCE.search(raw)
    if fenced:
        raw = fenced.group(1).strip()
    elif "{" in raw and "}" in raw:
        raw = raw[raw.index("{") : raw.rindex("}") + 1]
    try:
        data = json.loads(raw) if raw else None
    except ValueError:
        data = None
    part: Any = None
    if isinstance(data, dict):
        # {"structured": {...}}, or the parts bare; a sections object ("purpose", "members"...) is not parts.
        part = data.get("structured") if "structured" in data else (data if set(data) <= set(allowed) else None)
    return part if isinstance(part, dict) and part else None


async def _structured_fields(
    llm: object, model: str | None, budget: int, messages: list[ChatMessage], fields: tuple[str, ...]
) -> dict[str, Any] | None:
    """Only *fields* of the structured parts, split in halves while cut; a single field still cut is left out."""
    asked = [
        *messages,
        ChatMessage(
            role="user",
            content='Answer with ONE JSON object {"structured": {...}} and nothing else, from the same facts, '
            f"with only these fields: {', '.join(fields)}.",
        ),
    ]
    text, cut = await ask(llm, model, asked, budget)
    if not cut:
        return _parts_of(text, STRUCTURED_FIELDS)
    if len(fields) == 1:
        return None
    half = len(fields) // 2
    first = await _structured_fields(llm, model, budget, messages, fields[:half]) or {}
    second = await _structured_fields(llm, model, budget, messages, fields[half:]) or {}
    return {**first, **second} or None


async def _structured_only(
    llm: object, model: str | None, messages: list[ChatMessage], notes: list[dict[str, Any]], budget: int | None = None
) -> dict[str, Any] | None:
    """The structured parts asked in a call of their own (#9979), split by field while cut (#11292).

    None, noted, when no part could be written: the computed blocks still come from the configuration.
    """
    budget = budget or output_budget(llm, model)
    asked = [*messages, ChatMessage(role="user", content=STRUCTURED_ONLY)]
    try:
        text, cut = await ask(llm, model, asked, budget)
        if not cut:
            part = _parts_of(text, STRUCTURED_FIELDS)
            if part is None:
                notes.append(doc_note("second_call_empty"))
            return part
        half = len(MODEL_PARTS) // 2
        first = await _structured_fields(llm, model, budget, messages, MODEL_PARTS[:half]) or {}
        second = await _structured_fields(llm, model, budget, messages, MODEL_PARTS[half:]) or {}
        part = {**first, **second}
    except Exception as exc:
        if not _provider_failure(exc):
            raise
        logger.info("Fleet documentation: structured parts not written (%s)", api_error_message(exc))
        part = {}
    if not part:
        notes.append(doc_note("structured_cut"))
        return None
    return part


async def draft_fleet_documentation(
    db: AsyncSession,
    user_id: uuid.UUID,
    fleet: Fleet,
    *,
    section: str | None = None,
    llm: object | None = None,
    model: str | None = None,
) -> FleetDocDraft:
    """Draft *fleet*'s documentation (one *section*, or all); nothing is written.

    Built-in and custom fleets alike (#9768 S2): a built-in fleet's result is
    saved as a local version by :func:`regenerate_fleet_documentation`.
    One call is the fast path; an answer cut or unreadable is drafted in parts
    (#11292), so the size of the fleet and the model's output limit never decide
    whether a draft is produced. Sections the provider failed on are returned in
    ``pending``. Raises :class:`FleetDocDraftError` on an unknown section, or
    when nothing at all could be produced (its message names the cause).
    """
    check_section(section)
    wanted = [section] if section is not None else list(DOC_SECTIONS)
    current = dict((_stored(fleet) or {}).get("sections") or {})
    if llm is None:
        from app.llm.router import get_llm_for_user

        llm, model = await get_llm_for_user(db, str(user_id))

    whole = section is None
    context = await load_draft_context(db, fleet)
    stored = _stored(fleet)
    if stored and stored.get("dismissed_findings"):
        # Dismissals with a reason are decisions (#10536): the model is told, so it does not raise them again.
        from app.services.fleet_docs import current_member_fingerprints

        decided = decided_points(stored, await current_member_fingerprints(db, fleet))
        if decided:
            context = {**context, "decided": decided}
    messages = build_messages(context, current, wanted, structured=whole)
    budget = output_budget(llm, model)

    draft: FleetDocDraft | None = None
    try:
        text, cut = await ask(llm, model, messages, budget)
        if not cut:
            draft = extract_sections(text, wanted)
    except FleetDocDraftError:
        draft = None  # unreadable: drafted in parts below
    except Exception as exc:
        if not _provider_failure(exc):
            raise
        if told_not_to_retry(exc):
            # A spent quota: every part would be refused too.
            raise FleetDocDraftError(failure_cause(exc)) from exc
        logger.info("Fleet documentation: one-call draft failed (%s), drafted in parts", api_error_message(exc))

    if draft is None:
        # Cut after its continuations, unreadable or failed: the sections in parts, then the structured parts.
        parts = await sections_in_parts(llm, model, budget, context, current, wanted)
        if not parts.sections:
            if parts.failure is not None:
                raise FleetDocDraftError(failure_cause(parts.failure))
            raise FleetDocDraftError("The AI model wrote no documentation for this fleet.")
        draft = FleetDocDraft(
            sections={k: parts.sections[k] for k in DOC_SECTIONS if k in parts.sections},
            notes=parts.notes,
            findings=parts.findings,
            pending=[k for k in wanted if k in parts.pending],
        )
        if parts.pending:
            logger.info("Fleet documentation %s: section(s) pending %s", fleet.id, parts.pending)
    else:
        missing = [k for k in wanted if k not in draft.sections]
        if missing:
            # A section the answer left out is asked on its own (#10246): a gap is a generator defect.
            draft = await _missing_sections(llm, model, context, {**current, **draft.sections}, missing, draft, budget)
    # A finding the configuration contradicts is dropped before storage, whatever the model wrote (#10536).
    draft.findings = drop_contradicted(draft.findings, automations_by_member(context))
    if whole:
        # Every home member with an empty role label (computed, never the model's): fleets of any size alike.
        draft.role_findings = role_findings(context.get("members"))
        draft.findings = with_role_findings(draft.findings, context.get("members"))
    if not whole:
        # A one-section regenerate keeps the stored structured parts.
        draft.structured = None
        return draft
    if not draft.structured:
        # A large fleet's answer may spend its budget on the sections (#9979): the parts are asked on their own.
        draft.notes.append(doc_note("second_call_asked"))
        draft.structured = await _structured_only(llm, model, messages, draft.notes, budget)
    draft.structured = build_structured(
        draft.structured, context, draft.notes, previous=(_stored(fleet) or {}).get("structured")
    )
    return draft


def check_section(section: str | None) -> None:
    """Refuse an unknown *section* (#393), naming the known ones; ``None`` (every section) passes.

    Called before a generation is claimed, so a wrong name is the caller's refusal,
    never a failure recorded on the About page.
    """
    if section is not None and section not in DOC_SECTIONS:
        raise FleetDocDraftError(
            f"Unknown documentation section: {section}. Known sections: {', '.join(DOC_SECTIONS)}."
        )


async def _missing_sections(
    llm: object,
    model: str | None,
    context: dict[str, Any],
    current: dict[str, str],
    missing: list[str],
    draft: FleetDocDraft,
    budget: int | None = None,
) -> FleetDocDraft:
    """*draft* completed with the *missing* sections, asked in a call of their own and in parts when cut."""
    extra = await sections_in_parts(llm, model, budget or output_budget(llm, model), context, current, missing)
    sections = {**draft.sections, **extra.sections}
    notes = [n for n in draft.notes if n.get("code") != "sections_not_drafted"]
    still = [k for k in missing if k not in extra.sections]
    if still:
        logger.info("Fleet documentation: %d section(s) still missing after their own call", len(still))
        notes.append(doc_note("sections_not_drafted", count=len(still)))
    return FleetDocDraft(
        sections={k: sections[k] for k in DOC_SECTIONS if k in sections},
        notes=notes + [n for n in extra.notes if n.get("code") == "section_cut"],
        structured=draft.structured,
        findings=merge_findings(draft.findings, extra.findings),
        pending=[*draft.pending, *(k for k in missing if k in extra.pending)],
    )


async def regenerate_fleet_documentation(
    db: AsyncSession,
    user_id: uuid.UUID,
    fleet: Fleet,
    *,
    section: str | None = None,
    base_version: int | None = None,
    llm: object | None = None,
    model: str | None = None,
) -> FleetDocDraft:
    """Generate *fleet*'s documentation and save it as its next version (#9768 S2).

    The one AI write path of the documentation (the About page button and the
    fleet tool): nobody edits it by hand. A one-section regenerate keeps the
    other sections and the structured parts; the whole one replaces them. The
    caller audits and commits.

    A section the provider failed on after its retries (``draft.pending``,
    #11292) keeps its current text when it has one; the caller hands the list to
    :func:`app.services.fleet_doc_on_create.schedule_pending_sections` after its
    commit, which fills each one in the background.
    """
    stored_version = int((_stored(fleet) or {}).get("version") or 0)
    if base_version is not None and base_version != stored_version:
        # Refused before the model call: a stale page never spends a generation.
        raise FleetActionRefusedError(
            409, f"The documentation changed since you opened it (version {stored_version}): reload it, then try again."
        )
    draft = await draft_fleet_documentation(db, user_id, fleet, section=section, llm=llm, model=model)
    current = dict((_stored(fleet) or {}).get("sections") or {})
    if section is not None:
        sections = {**current, **draft.sections}
    else:
        # A pending section keeps the text it had until the background fill replaces it (#11292).
        sections = {**{k: current[k] for k in draft.pending if current.get(k)}, **draft.sections}
    if section is None:
        draft.notes.extend(lost_blocks((_stored(fleet) or {}).get("structured"), draft.structured))
    findings: list[dict[str, Any]] | None = None
    prints: dict[str, str] | None = None
    previous_doc = _stored(fleet) or {}
    if section is None and (draft.findings or previous_doc.get("findings") or previous_doc.get("finding_prints")):
        # The same findings while the configuration is unchanged (#10546 retest): the model is not
        # deterministic, so a stored finding stays while its member's print is the one it was drafted against.
        from app.services.fleet_docs import current_member_fingerprints

        prints = {
            **await current_member_fingerprints(db, fleet),
            GATES_PRINT_KEY: gates_print(
                getattr(fleet, "autonomy_level", None), getattr(fleet, "approval_gates", None)
            ),
        }
        # A retaken finding that means a stored or dismissed one keeps its id (#12016).
        findings = stable_findings(
            clean_findings(previous_doc.get("findings")),
            draft.findings,
            previous_doc.get("finding_prints"),
            prints,
            dismissed=[d for d in previous_doc.get("dismissed_findings") or [] if isinstance(d, dict)],
        )
        draft.findings = findings
    elif section is None:
        findings = draft.findings
    if section is None and draft.role_findings is not None:
        # The role finding is computed in code, never kept from a stored set nor taken from the model:
        # every home member with an empty role label, whatever the fleet's size.
        kept = [f for f in findings or [] if f.get("kind") != "member_without_role"]
        findings = merge_findings(kept, draft.role_findings)
        draft.findings = findings
    if section is None and prints is not None and previous_doc.get("dismissed_findings"):
        # This regeneration read every member as it is now (#12181): a former record is moved to its facet
        # print, and a dismissal lapsed by a change of its member that the new findings no longer raise is
        # dropped, so a fixed point never comes back as a lapsed suggestion.
        from app.services.fleet_doc_findings import settled_dismissals, without_unraised_lapsed

        records = [d for d in previous_doc.get("dismissed_findings") or [] if isinstance(d, dict)]
        settled, _ = settled_dismissals(records, prints)
        kept_records = without_unraised_lapsed(settled, prints, findings or [])
        if kept_records != records:
            # A new dict so the JSON column is seen as changed; the save below carries it to the new version.
            fleet.documentation = {**previous_doc, "dismissed_findings": kept_records}
    if draft.notes:
        # Generator notes are for the people who maintain the fleet (#10246): logged and kept on the
        # version's history entry, never shown on the About page.
        logger.info(
            "Fleet documentation %s: generator notes %s", fleet.id, sorted({str(n.get("code")) for n in draft.notes})
        )
    await save_documentation(
        db,
        fleet,
        sections,
        author=user_id,
        base_version=base_version,
        structured=draft.structured if section is None else None,
        notes=draft.notes,
        # A one-section regenerate keeps the findings of the whole draft it completes.
        findings=findings,
        finding_prints=prints,
    )
    return draft


def lost_blocks(previous: dict[str, Any] | None, new: dict[str, Any] | None) -> list[dict[str, Any]]:
    """One note per visual block the replaced version drew and the new one does not (#9979)."""
    before = previous if isinstance(previous, dict) else {}
    after = new if isinstance(new, dict) else {}
    return [doc_note("block_lost", block=key) for key in BLOCK_NAMES if before.get(key) and not after.get(key)]


__all__ = [
    "CONTINUATIONS_MAX",
    "DRAFT_BUDGET_MAX",
    "DRAFT_MAX_TOKENS",
    "SECTION_GUIDE",
    "TRANSIENT_RETRIES",
    "FleetDocDraft",
    "FleetDocDraftError",
    "build_messages",
    "build_structured",
    "describe_cron",
    "draft_context",
    "draft_fleet_documentation",
    "extract_sections",
    "is_transient",
    "load_draft_context",
    "names_of_bindings",
    "output_budget",
    "regenerate_fleet_documentation",
    "sections_in_parts",
    "tool_use_facts",
    "with_computed_facts",
]
