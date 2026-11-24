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

"""Fleet documentation (#9174, slice 5): a structured, persistent explanation of a fleet.

Stored in ``fleets.documentation`` (JSON, migration ``d9174fleetdocs1``)::

    {
        "sections": {"purpose": "...markdown...", "members": "...", ...},
        "version": 3,
        "updated_at": "2026-11-06T23:20:00+00:00",
        "updated_by": "<user id>" | "seed" | "fork",
        "fingerprint": "<16 hex>",
        "structured": {"purpose_line": "...", "triggers": [...], ...},  # optional, redesign S1
        "history": [{"version": 2, "sections": {...}, "structured": {...}, ...}, ...],
    }

Rules:

* **Sections.** A fixed, ordered set of keys (:data:`DOC_SECTIONS`), each a
  markdown text of at most :data:`SECTION_MAX_CHARS`. An unknown key is
  refused; an empty section is dropped.
* **Who writes.** The seed on a built-in fleet (read only through the API, the
  same rule as the rest of a built-in fleet); the owner or a ``config`` grantee
  on a custom fleet; a fork copies its source's documentation as its version 1.
* **Versions.** Every save is a new version; the previous one is kept in
  ``history`` (the newest :data:`HISTORY_KEPT`). A save naming a stale
  ``base_version`` is refused (409) so two editors never overwrite each other.
  :func:`documentation_history` lists the kept versions and
  :func:`restore_documentation_version` brings one back as the next version.
* **Structured parts** (About page redesign, slice S1 of 8644e340): an
  optional ``structured`` object (:class:`StructuredDoc`) the About page draws
  as visuals (trigger cards, a stepper, tables, a live checklist, a quota
  range). Every field is optional; the page falls back to the markdown
  section when a field is absent, so the sections stay the contract the
  editor and the AI draft write. Versioned with the sections. ``automations``
  and ``integrations`` (#9768 S1) are computed from the fleet's configuration
  by the generator (:mod:`app.services.fleet_doc_generation`), never typed.
* **Out of date.** ``fingerprint`` hashes what the documentation explains: the
  fleet's structure (``app.services.fleet_fork.fleet_structure``) and its member
  configuration (fleet defaults, each member's own context inclusion,
  collaboration instruction and organization scope). A change of either after
  the last save makes :func:`documentation_view` answer ``out_of_date``; the
  page offers a regenerate, never an automatic rewrite.
* **Built-in fleets** (#1956): the seed owns their structure and their
  documentation. Nobody regenerates or restores a version there
  (:func:`refuse_builtin_rewrite`), the view is never editable nor out of date,
  and a boot replaces a local version left by an earlier build with the
  shipped one (history kept).

Nothing here starts a run or calls a model: reading and saving are plain rows.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from functools import lru_cache
from typing import Annotated, Any, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.agent import Agent
from app.models.fleet import Fleet, FleetMember
from app.services.fleet_doc_findings import (
    clean_findings,
    dismissal_print,
    dismissal_record,
    facet_key,
    holding_dismissals,
    is_dismissed,
    lapsed_points,
    member_key,
    settled_dismissals,
    visible_findings,
    with_lapsed,
    with_restored,
    without_dismissals,
)
from app.services.fleets import FleetActionRefusedError, fleet_members

#: The documentation's sections, in reading order.
DOC_SECTIONS: tuple[str, ...] = (
    "purpose",
    "use_case",
    "triggers",
    "flow",
    "objects_created",
    "prerequisites",
)
#: Prose sections the About page no longer shows (#10123): their detail lives in another tab
#: (Members, ROI, Schedule) or in the live KPI tiles, and the control bar pauses the fleet. A
#: stored version or a seed that still carries one is read without it, never refused.
RETIRED_SECTIONS: tuple[str, ...] = (
    "members",
    "approvals",
    "outcomes",
    "roi_units",
    "quota_estimate",
    "switch_off",
)
SECTION_MAX_CHARS = 20_000
HISTORY_KEPT = 10

#: The modules that are not an integration provider; the page draws an icon for each.
BASE_MODULES: tuple[str, ...] = (
    "opencti",
    "openaev",
    "opencrq",
    "schedule",
    "work_item",
    "platform",
    "webhook",
    "manual",
)
#: Kept for the callers that list the base modules; :func:`doc_modules` is the full set.
DOC_MODULES: tuple[str, ...] = BASE_MODULES


@lru_cache(maxsize=1)
def doc_modules() -> frozenset[str]:
    """Every module a trigger, a gate or an object may name (#9979): the base modules and each integration provider.

    A corporate fleet's triggers and objects live in Outlook, Teams, GitHub,
    Notion...: each provider of the integration catalog is a module, drawn with
    its logo. Nothing is remapped: a value outside this set is refused.
    """
    from app.api.v1.integration_catalog import PROVIDER_CATALOG

    return frozenset(BASE_MODULES) | frozenset(str(k) for k in PROVIDER_CATALOG)


def _module(value: str) -> str:
    value = (value or "").strip().lower()
    if value not in doc_modules():
        raise ValueError(f"unknown module '{value}' (a base module or an integration provider key)")
    return value


#: Where a trigger, a gate, an action or an object lives; the page draws the module's mark.
DocModule = Annotated[str, AfterValidator(_module)]
#: The prerequisite checks :func:`app.services.fleet_about.evaluate_prerequisites` knows; besides them,
#: ``integration:<provider>`` for every integration provider of the catalog (:func:`prerequisite_check_known`).
PREREQUISITE_CHECKS: tuple[str, ...] = (
    "integration:opencti",
    "integration:openaev",
    "integration:opencrq",
    "profile:sector",
    "profile:regions",
    "profile:crown_jewels",
    "profile:software",
    "profile:ai_targets",
    "profile:tracked_organizations",
    # At least one of the fleet's automations is switched on, read live (#10094).
    "automations:enabled",
    "manual",
)
#: The live check of the "switch on the automations" prerequisite.
AUTOMATIONS_CHECK = "automations:enabled"


def prerequisite_check_known(check: str) -> bool:
    """Whether *check* is a prerequisite check the page evaluates (UX r1 B2 on #9988).

    The named checks, and ``integration:<provider>`` for every integration
    provider of the catalog: a corporate fleet's members use Outlook, GitHub,
    Notion..., each a live "connected" check for the reader.
    """
    if check in PREREQUISITE_CHECKS:
        return True
    kind, _, provider = check.partition(":")
    return kind == "integration" and provider in doc_modules() and provider not in BASE_MODULES


class _Part(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class DocTrigger(_Part):
    """What starts the fleet: one event card."""

    module: DocModule
    name: str = Field(min_length=1, max_length=120)
    #: The filter in plain words (never a raw key; a key goes in ``detail``).
    filter: str = Field(default="", max_length=300)
    #: A technical detail shown in a tooltip (a work item key, a cron).
    detail: str = Field(default="", max_length=200)
    #: The fleet automation this trigger belongs to, when it has one.
    automation_slug: str = Field(default="", max_length=120)
    #: False when the automation behind it is switched off (UX r1 B1 on #9988): drawn with its state, never hidden.
    enabled: bool = True


class DocMemberRole(_Part):
    """One member's role in one line (#10123): the About page's member card, matched to the live member by name."""

    name: str = Field(min_length=1, max_length=120)
    role: str = Field(default="", max_length=120)


#: The longest step body (#10327): 25 to 40 words of markdown with member names in bold and a short list fit.
STEP_DETAIL_MAX = 800


class DocStep(_Part):
    """One step of how it works; ``gate`` when a person approves before it acts.

    ``detail`` is the step's body in markdown (#10327): who acts, what starts the
    step, what it produces and where it goes next. The About page renders it with
    the sanitized markdown renderer (no raw HTML, no image, no heading); an older
    version without one reads its title only.
    """

    title: str = Field(min_length=1, max_length=120)
    detail: str = Field(default="", max_length=STEP_DETAIL_MAX)
    gate: bool = False
    #: The key of the approval gate this step stands for (#1074). When the fleet's
    #: catalog holds it, the About response derives ``gate`` from whether that gate
    #: waits at the fleet's live level and pins, so the step badge and the Approval
    #: tab never disagree; ``gate`` stays the fallback of a step without one.
    gate_key: str = Field(default="", max_length=120)


class DocGateTool(_Part):
    """One tool call a gate covers (#9769 S1b): the tool, and the argument values that make it the gated action.

    ``when`` maps an argument to the values that match (any of them); an empty
    ``when`` matches every call of the tool. The platform pauses a matching call
    for a person's approval while the fleet's autonomy level gates the action.
    """

    name: str = Field(min_length=1, max_length=120, pattern=r"^[a-z0-9_]+$")
    when: dict[str, list[str]] = Field(default_factory=dict, max_length=5)


class DocGateGrant(_Part):
    """What one approval of the gate covers (#1339): one decision per validation, not per call.

    A call a person approved covers the later calls of the same gate that name
    an object it named or created (``objects``: the arguments holding those ids)
    and give none of ``fresh`` (a new target, a new schedule): those ask again.
    """

    objects: list[str] = Field(default_factory=list, max_length=10)
    fresh: list[str] = Field(default_factory=list, max_length=20)


#: The gates one fleet's catalog holds at most (``StructuredDoc.gates``, the
#: ``PUT /fleets/{id}/approval-gates`` body). ``fleet_gate_proposals.propose_gates``
#: never returns more, so "Accept all" always fits (UX round 4 B1 on #10034:
#: a bound of 20 refused the 25 proposals of a corporate fleet).
GATES_MAX = 200


class DocGate(_Part):
    """What needs a person, where, and who approves."""

    action: str = Field(min_length=1, max_length=200)
    module: DocModule = "platform"
    approver: str = Field(default="", max_length=120)
    #: A stable handle of the gated action (#9769), unique in the fleet.
    key: str = Field(default="", max_length=60, pattern=r"^([a-z0-9][a-z0-9_.-]*)?$")
    #: The impact class that decides at which autonomy level it stops waiting
    #: (``app.services.fleet_autonomy.GATE_IMPACTS``); ``high`` when unsaid.
    impact: Literal["low", "high", "destructive"] = "high"
    #: The tool calls that are this action (#9769 S1b): enforced in code from
    #: the fleet's autonomy level; none = the gate is stated to the agent only.
    #: Bound: ``fleet_gate_proposals.GATE_TOOLS_MAX`` (one proposed gate names
    #: every write tool of an integration at one impact, #10034 UX round 2 B1).
    tools: list[DocGateTool] = Field(default_factory=list, max_length=200)
    #: What one approval covers (#1339); none = each matching call asks on its own.
    grant: DocGateGrant | None = None
    #: The integration a proposed gate writes to (#10034), so the page draws its
    #: mark and name; empty for a declared gate, which the module places.
    provider: str = Field(default="", max_length=60, pattern=r"^([a-z0-9][a-z0-9_-]*)?$")


class DocObjects(_Part):
    """The objects the fleet creates in one module."""

    module: DocModule
    objects: list[str] = Field(min_length=1, max_length=12)


class DocPrerequisite(_Part):
    """One line of the live checklist; ``check`` is evaluated for the reader, ``manual`` is not."""

    check: str = Field(min_length=1, max_length=60)
    label: str = Field(min_length=1, max_length=200)
    #: The page that fixes it (a route of the app, never an external URL).
    fix_route: str = Field(default="", max_length=200, pattern=r"^(/[\w\-/?=&.]*)?$")


class DocQuota(_Part):
    """An estimate of runs per month for one trigger, with its basis."""

    trigger: str = Field(min_length=1, max_length=120)
    basis: str = Field(default="", max_length=200)
    runs_min: int = Field(ge=0, le=100_000)
    runs_max: int = Field(ge=0, le=100_000)
    #: True when the trigger is event-driven with no fixed top (#10108): the range is a floor, the page says so.
    open_ended: bool = False
    #: The trigger kinds the row covers, a grouped row all of them (UX r1 B1 on #10112): the page words it from them.
    kinds: list[Literal["schedule", "event", "manual"]] = Field(default_factory=list, max_length=3)
    #: True when runs_max is the count observed over the last 30 days, not an estimate (UX r1 B3 on #10112).
    observed: bool = False


class DocAutomation(_Part):
    """One automation (assignment) of the fleet, as the Automations table reads it."""

    name: str = Field(min_length=1, max_length=200)
    member: str = Field(default="", max_length=200)
    #: The trigger kind in plain words ("Schedule", "Event", "Work item", "Webhook", "Manual").
    trigger: str = Field(min_length=1, max_length=60)
    #: When it runs, in plain words ("Every Monday at 08:00 (Europe/Paris)"), else empty.
    schedule: str = Field(default="", max_length=200)
    #: The event and its filter in plain words, else empty.
    event: str = Field(default="", max_length=300)
    enabled: bool = True
    #: Whose identity it runs as, in plain words ("its owner", "every user"); never an id.
    run_as: str = Field(default="", max_length=120)


class DocToolUse(_Part):
    """What one member uses: integrations, MCP servers, built-in tool groups and skills, by name."""

    member: str = Field(min_length=1, max_length=200)
    integrations: list[str] = Field(default_factory=list, max_length=20)
    mcp_servers: list[str] = Field(default_factory=list, max_length=20)
    tool_groups: list[str] = Field(default_factory=list, max_length=40)
    skills: list[str] = Field(default_factory=list, max_length=20)


class StructuredDoc(_Part):
    """The structured parts of a fleet's documentation (every field optional)."""

    purpose_line: str = Field(default="", max_length=300)
    outcome_line: str = Field(default="", max_length=300)
    #: Who the fleet serves, as short chips (#10123).
    serves: list[str] = Field(default_factory=list, max_length=6)
    #: One line per member (#10123): the member cards' role.
    members: list[DocMemberRole] = Field(default_factory=list, max_length=60)
    triggers: list[DocTrigger] = Field(default_factory=list, max_length=20)
    steps: list[DocStep] = Field(default_factory=list, max_length=20)
    #: GATES_MAX: every gate the proposals can return must be storable (UX round 4 B1 on #10034).
    gates: list[DocGate] = Field(default_factory=list, max_length=GATES_MAX)
    objects: list[DocObjects] = Field(default_factory=list, max_length=10)
    # 40 (UX r2 B2 on #9988): a corporate fleet's members use about 30 providers, each a live check.
    prerequisites: list[DocPrerequisite] = Field(default_factory=list, max_length=40)
    quota: list[DocQuota] = Field(default_factory=list, max_length=20)
    # 120 (#10108): a corporate fleet's home members carry more than 60 automations; the list matches the header count.
    automations: list[DocAutomation] = Field(default_factory=list, max_length=120)
    integrations: list[DocToolUse] = Field(default_factory=list, max_length=40)


#: The structured fields, in the order the generator and the page read them.
STRUCTURED_FIELDS: tuple[str, ...] = tuple(StructuredDoc.model_fields)


def field_limit(name: str) -> int | None:
    """The most items the list field *name* of :class:`StructuredDoc` holds; None for a scalar field."""
    info = StructuredDoc.model_fields.get(name)
    for meta in getattr(info, "metadata", ()) or ():
        limit = getattr(meta, "max_length", None)
        if isinstance(limit, int):
            return limit
    return None


def clean_structured_fields(structured: dict[str, Any] | None, notes: list[dict[str, Any]]) -> dict[str, Any] | None:
    """*structured* validated FIELD BY FIELD (#9979): a wrong field costs that field only, named in *notes*.

    A list longer than its field holds is cut to it, with a note; the whole
    block is never dropped for one field, so the computed facts always survive.
    """
    kept: dict[str, Any] = {}
    for key, value in (structured or {}).items():
        limit = field_limit(key)
        if limit is not None and isinstance(value, list) and len(value) > limit:
            notes.append(doc_note("part_cut", block=key, kept=limit, total=len(value)))
            value = value[:limit]
        try:
            cleaned = clean_structured({key: value})
        except FleetActionRefusedError:
            notes.append(doc_note("part_left_out", block=key))
            continue
        if cleaned and key in cleaned:
            kept[key] = cleaned[key]
    return kept or None


def clean_structured(structured: dict[str, Any] | None) -> dict[str, Any] | None:
    """*structured* validated, empty fields dropped; ``None`` when nothing is left. Refused (422) when invalid."""
    if not structured:
        return None
    try:
        model = StructuredDoc.model_validate(structured)
    except ValidationError as exc:
        first = exc.errors()[0]
        where = ".".join(str(part) for part in first.get("loc", ())) or "structured"
        raise FleetActionRefusedError(422, f"structured.{where}: {first.get('msg', 'invalid value')}") from None
    for prerequisite in model.prerequisites:
        if not prerequisite_check_known(prerequisite.check):
            raise FleetActionRefusedError(
                422,
                f"structured.prerequisites: unknown check '{prerequisite.check}' (one of {', '.join(PREREQUISITE_CHECKS)}).",
            )
    for estimate in model.quota:
        if estimate.runs_max < estimate.runs_min:
            raise FleetActionRefusedError(422, f"structured.quota: '{estimate.trigger}' has runs_max below runs_min.")
    cleaned = model.model_dump(exclude_defaults=True)
    return cleaned or None


@dataclass
class DocumentationView:
    """What the About page reads (:func:`documentation_view`)."""

    sections: dict[str, str] = field(default_factory=dict)
    version: int = 0
    updated_at: str | None = None
    updated_by: str | None = None
    editable: bool = False
    #: None when nothing is written yet; else whether the fleet moved since the last save.
    out_of_date: bool | None = None
    #: The structured parts (:class:`StructuredDoc`), None when the fleet has none.
    structured: dict[str, Any] | None = None
    #: A built-in fleet whose documentation was regenerated locally (the shipped one can be restored).
    local: bool = False
    #: Set when this version is a restore: ``{"version": N | None, "author": "seed" | "fork" | "ai"}``.
    restored_from: dict[str, Any] | None = None
    #: What the AI found wrong with the configuration (#10246), :func:`fleet_doc_findings.visible_findings`;
    #: only for a reader who manages the fleet, dismissed ones left out while their member is unchanged.
    findings: list[dict[str, Any]] = field(default_factory=list)
    #: The dismissals that hold (#10536), newest first: who, when, why; for a manager only.
    dismissed_findings: list[dict[str, Any]] = field(default_factory=list)


def clean_sections(sections: dict[str, str]) -> dict[str, str]:
    """*sections* in :data:`DOC_SECTIONS` order, empty and retired ones dropped; refused on an unknown key or a long text."""
    unknown = sorted(set(sections) - set(DOC_SECTIONS) - set(RETIRED_SECTIONS))
    if unknown:
        raise FleetActionRefusedError(422, f"Unknown documentation section(s): {', '.join(unknown)}.")
    cleaned: dict[str, str] = {}
    for key in DOC_SECTIONS:
        text = (sections.get(key) or "").strip()
        if not text:
            continue
        if len(text) > SECTION_MAX_CHARS:
            raise FleetActionRefusedError(422, f"The section '{key}' is longer than {SECTION_MAX_CHARS} characters.")
        cleaned[key] = text
    return cleaned


def documentation_fingerprint(structure: dict[str, Any], fleet: Fleet, rows: list[FleetMember]) -> str:
    """A short hash of what the documentation explains: *structure* and the member configuration."""
    config = {
        "default_context_inclusion": fleet.default_context_inclusion or "none",
        "default_collaboration_instruction": fleet.default_collaboration_instruction or "",
        "organization_scope": fleet.organization_scope,
        "members": sorted(
            (
                {
                    "agent": str(r.agent_id or r.target_fleet_id),
                    "role": r.role,
                    "context_inclusion": r.context_inclusion,
                    "collaboration_instruction": r.collaboration_instruction,
                    "organization_scope": r.organization_scope,
                }
                for r in rows
            ),
            key=lambda m: json.dumps(m, sort_keys=True, default=str),
        ),
    }
    body = json.dumps({"structure": structure, "config": config}, sort_keys=True, default=str)
    return hashlib.sha256(body.encode()).hexdigest()[:16]


def _print(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()[:16]


def member_fingerprints(
    fleet: Fleet, rows: list[FleetMember], agents: dict[uuid.UUID, Agent], assignments: list[Any]
) -> dict[str, str]:
    """A short hash per home member of what a finding about it concerns (#10536), keyed by :func:`member_key`.

    Each member's agent (name, description, persona, tools, integrations, skills, handovers), its fleet
    configuration and its automations (name, trigger, its configuration, enabled); a member is keyed by its
    name and by its slug. ``""`` is the fleet itself (its card and defaults), for a finding naming no member.
    A dismissal holds while the print of the member it names is unchanged.
    """
    by_agent: dict[Any, list[Any]] = {}
    for a in assignments:
        by_agent.setdefault(a.agent_id, []).append(
            [a.name, a.trigger_type, getattr(a, "trigger_config", None), bool(a.enabled)]
        )
    fleet_card = [fleet.name, fleet.description or ""]
    fleet_tools = [fleet.skills, str(fleet.orchestrator_agent_id)]
    fleet_rest = [fleet.default_context_inclusion, fleet.default_collaboration_instruction]
    prints = {"": _print([*fleet_card, fleet.skills, *fleet_rest, str(fleet.orchestrator_agent_id)])}
    # The print of each part a finding is about (#12099): a dismissal lapses only when its part changes.
    prints.update(
        {
            facet_key("", "card"): _print(fleet_card),
            facet_key("", "tools"): _print(fleet_tools),
            facet_key("", "config"): _print([*fleet_card, *fleet_tools, *fleet_rest]),
        }
    )
    for row in rows:
        if row.agent_id is None or row.role != "member":
            continue
        agent = agents.get(row.agent_id)
        if agent is None:
            continue
        value = _print(
            {
                "agent": [
                    agent.name,
                    getattr(agent, "description", None),
                    getattr(agent, "persona", None),
                    getattr(agent, "builtin_tool_ids", None),
                    getattr(agent, "integration_bindings", None),
                    getattr(agent, "mcp_server_ids", None),
                    getattr(agent, "skill_ids", None),
                    getattr(agent, "handover_agent_ids", None),
                    getattr(agent, "human_in_the_loop", None),
                    getattr(agent, "approve_tool_calls", None),
                ],
                "member": [
                    row.role_label,
                    str(row.parent_member_id),
                    row.is_exposed,
                    row.context_inclusion,
                    row.collaboration_instruction,
                    row.organization_scope,
                ],
                "automations": sorted(by_agent.get(row.agent_id, []), key=lambda x: json.dumps(x, default=str)),
            }
        )
        card = [agent.name, getattr(agent, "description", None), getattr(agent, "persona", None)]
        tools = {
            "agent": [
                getattr(agent, "builtin_tool_ids", None),
                getattr(agent, "integration_bindings", None),
                getattr(agent, "mcp_server_ids", None),
                getattr(agent, "skill_ids", None),
                getattr(agent, "handover_agent_ids", None),
                getattr(agent, "human_in_the_loop", None),
                getattr(agent, "approve_tool_calls", None),
            ],
            "automations": sorted(by_agent.get(row.agent_id, []), key=lambda x: json.dumps(x, default=str)),
        }
        # Everything but the role label: a label edit lapses no dismissal (#12099).
        rest = [
            str(row.parent_member_id),
            row.is_exposed,
            row.context_inclusion,
            row.collaboration_instruction,
            row.organization_scope,
        ]
        facets = {"card": _print(card), "tools": _print(tools), "config": _print([card, tools, rest])}
        for name in (agent.name, getattr(agent, "slug", None)):
            if name:
                prints[member_key(name)] = value
                prints.update({facet_key(name, f): p for f, p in facets.items()})
    return prints


#: Automations read per fleet to fingerprint its members.
PRINT_ASSIGNMENTS_MAX = 500


async def settle_dismissals(db: AsyncSession, fleet: Fleet) -> bool:
    """Move *fleet*'s holding dismissals of the former shape to their facet print, before a member edit (#12181).

    A dismissal recorded before #12099 compares the member's whole print, role label, parent and exposure
    included: settled while it still holds, a role label, parent or exposure edit no longer lapses it.
    Answers whether the documentation changed; the caller commits. No model call.
    """
    doc = getattr(fleet, "documentation", None)
    stored = doc if isinstance(doc, dict) and isinstance(doc.get("sections"), dict) else None
    records = [d for d in (stored or {}).get("dismissed_findings") or [] if isinstance(d, dict)]
    if stored is None or not any(d.get("id") and not d.get("print_facet") for d in records):
        return False
    settled, changed = settled_dismissals(records, await current_member_fingerprints(db, fleet))
    if changed:
        # A new dict so the JSON column is seen as changed.
        fleet.documentation = {**stored, "dismissed_findings": settled}
    return changed


async def current_member_fingerprints(db: AsyncSession, fleet: Fleet) -> dict[str, str]:
    """:func:`member_fingerprints` of *fleet* as stored now."""
    from app.models.assignment import Assignment

    rows = await fleet_members(db, fleet.id)
    home = [r.agent_id for r in rows if r.agent_id is not None and r.role == "member"]
    agents: dict[uuid.UUID, Agent] = {}
    assignments: list[Any] = []
    if home:
        agents = {a.id: a for a in (await db.execute(select(Agent).where(Agent.id.in_(home)))).scalars().all()}
        assignments = list(
            (await db.execute(select(Assignment).where(Assignment.agent_id.in_(home)).limit(PRINT_ASSIGNMENTS_MAX)))
            .scalars()
            .all()
        )
    return member_fingerprints(fleet, rows, agents, assignments)


async def current_fingerprint(db: AsyncSession, fleet: Fleet) -> str:
    """:func:`documentation_fingerprint` of *fleet* as stored now."""
    from app.services.fleet_fork import fleet_structure

    rows = await fleet_members(db, fleet.id)
    ids = [r.agent_id for r in rows if r.agent_id is not None]
    agents: dict[uuid.UUID, Agent] = {}
    if ids:
        agents = {a.id: a for a in (await db.execute(select(Agent).where(Agent.id.in_(ids)))).scalars().all()}
    return documentation_fingerprint(fleet_structure(fleet, rows, agents), fleet, rows)


def _stored(fleet: Fleet) -> dict[str, Any] | None:
    doc = fleet.documentation
    return doc if isinstance(doc, dict) and isinstance(doc.get("sections"), dict) else None


def refuse_builtin_rewrite(fleet: Fleet) -> None:
    """Refuse a regenerate or a version restore on a built-in fleet (#1956): the seed writes its documentation."""
    if fleet.is_builtin:
        raise FleetActionRefusedError(
            422,
            "A built-in fleet shows the documentation shipped with the platform, kept up to date at each update: "
            "it is never regenerated or restored.",
        )


async def documentation_view(db: AsyncSession, fleet: Fleet, *, can_edit: bool) -> DocumentationView:
    """The documentation of *fleet* for a reader; *can_edit* is whether that reader manages the fleet.

    ``editable`` means the reader may REGENERATE it; nobody edits it by hand
    (#9768 S2). A built-in fleet is never editable nor out of date: the seed
    owns its structure and rewrites its documentation at each boot (#1956).
    """
    editable = can_edit and not fleet.is_builtin
    doc = _stored(fleet)
    if doc is None:
        return DocumentationView(editable=editable)
    fingerprint = doc.get("fingerprint")
    if fleet.is_builtin:
        out_of_date: bool | None = False if fingerprint else None
    else:
        now = await current_fingerprint(db, fleet) if fingerprint else None
        out_of_date = None if not fingerprint else fingerprint != now
    findings: list[dict[str, Any]] = []
    dismissed: list[dict[str, Any]] = []
    # A manager of a built-in fleet still reads its suggestions (integrations and gates stay editable, #11948).
    if can_edit:
        shown, held, _prints = await manager_suggestions(db, fleet)
        findings, dismissed = shown, [dismissed_view(d) for d in held]
    return DocumentationView(
        sections={k: doc["sections"][k] for k in DOC_SECTIONS if doc["sections"].get(k)},
        version=int(doc.get("version") or 0),
        updated_at=doc.get("updated_at"),
        updated_by=doc.get("updated_by"),
        editable=editable,
        out_of_date=out_of_date,
        structured=doc.get("structured") or None,
        local=bool(fleet.is_builtin and doc.get("local")),
        restored_from=_restored_from(doc),
        findings=findings,
        dismissed_findings=dismissed,
    )


async def manager_suggestions(
    db: AsyncSession, fleet: Fleet
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, str]]:
    """What the About page chip counts for a manager of *fleet*: ``(open, holding dismissals, member prints)``.

    *open* is the stored findings plus the good-practice suggestions computed from the configuration
    (#11948 S2, #11972), each without a holding dismissal, plus the finding of every lapsed dismissal
    (:func:`fleet_doc_findings.lapsed_points`, #12099). The page and the fleet tool both read it, so
    the tool's count is the chip's (#12080). No model call; nothing for a fleet with no documentation.
    """
    doc = _stored(fleet)
    if doc is None:
        return [], [], {}
    from app.services.fleet_practice_suggestions import practice_suggestions, suggestion_editable

    practice = await practice_suggestions(db, fleet)
    prints: dict[str, str] = {}
    if doc.get("findings") or doc.get("dismissed_findings") or practice:
        prints = await current_member_fingerprints(db, fleet)
    shown = [*visible_findings(doc, prints), *visible_practice(doc, prints, practice)]
    # A lapsed dismissal is a live suggestion again (#12099), its finding stored or not.
    shown = with_lapsed(doc, prints, shown)
    # Only what the reader can change (#1510): on a built-in fleet a stored or lapsed finding on locked
    # configuration is dropped too, so the chip, the list, get_fleet and the fleet health count the same.
    shown = [s for s in shown if suggestion_editable(fleet, s)]
    return shown, holding_dismissals(doc, prints), prints


async def open_suggestion_count(db: AsyncSession, fleet: Fleet) -> int:
    """How many suggestions a manager of *fleet* has open: the About page chip, the fleet tool's header,
    ``get_fleet`` and the fleet health all read this one count (#12099)."""
    shown, _held, _prints = await manager_suggestions(db, fleet)
    return len(shown)


def documentation_text(fleet: Fleet, member: str = "") -> list[tuple[str, str]]:
    """The stored documentation of *fleet* as ``(section, text)`` pairs, in reading order (#12181).

    With *member*, only the lines naming it (case-insensitive), the sections without one left out: what
    a regeneration wrote about a member, read without the About page. Empty with no documentation.
    """
    doc = _stored(fleet)
    if doc is None:
        return []
    wanted = " ".join(str(member or "").split()).lower()
    out: list[tuple[str, str]] = []
    for key in DOC_SECTIONS:
        text = str(doc["sections"].get(key) or "").strip()
        if text and wanted:
            text = "\n".join(line for line in text.splitlines() if wanted in line.lower()).strip()
        if text:
            out.append((key, text))
    return out


def visible_practice(
    doc: dict[str, Any] | None, prints: dict[str, str], practice: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """The *practice* suggestions (#11948 S2) a holding dismissal of *doc* does not hide."""
    holding = holding_dismissals(doc, prints)
    return [p for p in practice if not is_dismissed(p, holding)]


def dismissed_view(record: dict[str, Any]) -> dict[str, Any]:
    """A dismissal as the page and the tool show it (#10536): the finding, who, when, why; no fingerprint.

    ``source`` is ``practice`` for a good-practice suggestion (#11948, read from its kind, so a dismissal
    recorded earlier qualifies too): the page words it in the reader's language like the open row; empty for
    a generated finding.
    """
    from app.services.fleet_practice_suggestions import is_practice_kind

    view = {
        k: record.get(k) or "" for k in ("id", "kind", "member", "subject", "finding", "at", "by", "by_name", "reason")
    }
    view["source"] = "practice" if is_practice_kind(record.get("kind")) else ""
    return view


def _restored_from(doc: dict[str, Any]) -> dict[str, Any] | None:
    """The ``restored_from`` record of a stored version, ``None`` when it is not a restore."""
    record = doc.get("restored_from")
    if not isinstance(record, dict) or record.get("author") not in RESTORED_AUTHORS:
        return None
    version = record.get("version")
    return {"version": int(version) if version else None, "author": record["author"]}


#: Who wrote the text a restore brings back: Filigran (shipped), the original fleet (fork) or the AI generator.
RESTORED_AUTHORS = ("seed", "fork", "ai")


def _source_author(doc: dict[str, Any]) -> str:
    """Who wrote the text of the stored version *doc*: a restore keeps the author of the text it brought back."""
    restored = _restored_from(doc)
    if restored is not None:
        return str(restored["author"])
    author = doc.get("updated_by")
    return author if author in ("seed", "fork") else "ai"


def _write(
    fleet: Fleet,
    sections: dict[str, str],
    fingerprint: str,
    author: str,
    *,
    version: int,
    structured: dict[str, Any] | None = None,
    restored_from: dict[str, Any] | None = None,
    notes: list[dict[str, Any]] | None = None,
    findings: list[dict[str, Any]] | None = None,
    finding_prints: dict[str, str] | None = None,
) -> None:
    previous = _stored(fleet)
    history: list[dict[str, Any]] = []
    if previous is not None and previous.get("sections"):
        history = [
            {
                k: previous.get(k)
                for k in ("version", "sections", "structured", "updated_at", "updated_by", "restored_from", "notes")
                if k not in ("restored_from", "notes") or previous.get(k)
            },
            *(previous.get("history") or []),
        ][:HISTORY_KEPT]
    fleet.documentation = {
        "sections": sections,
        "version": version,
        "updated_at": datetime.now(UTC).isoformat(),
        "updated_by": author,
        "fingerprint": fingerprint,
        "history": history,
    }
    if structured:
        fleet.documentation["structured"] = structured
    if restored_from:
        fleet.documentation["restored_from"] = restored_from
    if notes:
        # What the generator left out or derived (#9979): kept on the version for its history entry
        # (Version history, fleet managers only), never on the About page (#10246).
        fleet.documentation["notes"] = [stored_note(n) for n in notes][:NOTES_KEPT]
    kept_findings = (previous or {}).get("findings") if findings is None else findings
    if kept_findings:
        fleet.documentation["findings"] = clean_findings(kept_findings)
    # The member prints the findings were drafted against (#10546 retest): the next whole regeneration
    # keeps a finding while its member's print is unchanged (:func:`fleet_doc_findings.stable_findings`).
    kept_prints = (previous or {}).get("finding_prints") if finding_prints is None else finding_prints
    if kept_prints:
        fleet.documentation["finding_prints"] = kept_prints
    if (previous or {}).get("dismissed_findings"):
        # A dismissal holds across versions while its member is unchanged (#10536).
        fleet.documentation["dismissed_findings"] = previous["dismissed_findings"]


#: Version notes kept on a generated version.
NOTES_KEPT = 30

#: The visual blocks of the About page, by structured field, as a version note names them in English.
BLOCK_NAMES: dict[str, str] = {
    "purpose_line": "header purpose line",
    "outcome_line": "header outcome line",
    "serves": "who it serves",
    "members": "member roles",
    "triggers": "trigger cards",
    "steps": "numbered steps",
    "gates": "approval gates",
    "objects": "objects by module",
    "prerequisites": "prerequisites",
    "quota": "quota rows",
    "automations": "automations table",
    "integrations": "integrations and tools",
}

#: The version notes (#9979; UX r1 B6 on #9988): a code and its params, never an English sentence
#: with internal keys. The About page renders each code in the reader's language (``FleetAboutView``);
#: :func:`note_text` renders it in English for the fleet tool's answer. ``block`` is a structured field.
NOTE_TEXTS: dict[str, str] = {
    "section_left_out": "a section the AI was not asked for was left out",
    "section_cut": "a section was cut to {max} characters",
    "sections_not_drafted": "{count} section(s) not written by the AI",
    "part_not_model": "a block the AI does not write was left out of its answer",
    "part_cut": "{block}: {kept} of {total} kept, the most this block holds",
    "part_item_left_out": "{block}: an entry the AI wrote was invalid and left out",
    "part_left_out": "{block}: invalid, left out",
    "part_derived": "{block} come from the fleet's configuration: the AI wrote none",
    "part_kept": "{block} kept from the version this one replaces: neither the AI nor the configuration gave them",
    "block_lost": "this version has no {block}: the version it replaces had them",
    "quota_grouped": "quota estimate: {grouped} triggers grouped in one line past {shown} lines, the total is unchanged",
    "prerequisites_grouped": "prerequisites: {grouped} integrations grouped in one line past {shown} lines",
    "second_call_asked": "the AI answered without the structured blocks: they were asked for in a second call",
    "structured_cut": "the AI's answer for the structured blocks was cut: they come from the configuration",
    "second_call_empty": "the second call answered no structured blocks: they come from the configuration",
    "text": "{text}",
}


def doc_note(code: str, **params: str | int) -> dict[str, Any]:
    """One version note: a :data:`NOTE_TEXTS` code and its params (strings and numbers only)."""
    if code not in NOTE_TEXTS:
        raise ValueError(f"unknown documentation note code '{code}'")
    return {"code": code, "params": params}


def stored_note(note: object) -> dict[str, Any]:
    """*note* as stored and served: a known code with scalar params; a legacy sentence becomes a ``text`` note."""
    if isinstance(note, dict) and note.get("code") in NOTE_TEXTS:
        raw = note.get("params")
        params = {
            str(k): (v if isinstance(v, int) and not isinstance(v, bool) else str(v)[:120])
            for k, v in (raw.items() if isinstance(raw, dict) else ())
        }
        return {"code": note["code"], "params": params}
    return {"code": "text", "params": {"text": str(note)[:300]}}


def note_text(note: object) -> str:
    """*note* in English (the fleet tool's answer); a block reads by its name, never its key."""
    clean = stored_note(note)
    params = dict(clean["params"])
    if "block" in params:
        params["block"] = BLOCK_NAMES.get(str(params["block"]), "a block")
    try:
        return NOTE_TEXTS[clean["code"]].format(**params)
    except (KeyError, IndexError, ValueError):
        return NOTE_TEXTS[clean["code"]]


async def save_documentation(
    db: AsyncSession,
    fleet: Fleet,
    sections: dict[str, str],
    *,
    author: uuid.UUID,
    base_version: int | None,
    structured: dict[str, Any] | None = None,
    notes: list[dict[str, Any]] | None = None,
    findings: list[dict[str, Any]] | None = None,
    finding_prints: dict[str, str] | None = None,
) -> None:
    """Save a GENERATED documentation as the next version; the caller audits and commits (#9768 S2).

    *findings* replaces the stored configuration findings (#10246); ``None`` keeps them.
    *finding_prints* are the member prints those findings were drafted against; ``None`` keeps them.

    The one caller is the generator route and tool: nobody types the
    documentation by hand. *structured* replaces the stored structured
    parts; ``None`` keeps them (a one-section regenerate), ``{}`` clears them.
    Refused: a built-in fleet (422, the seed writes it, #1956), a stale
    *base_version* (409), an unknown section, a text too long or an invalid
    structured part (422).
    """
    refuse_builtin_rewrite(fleet)
    stored = _stored(fleet) or {}
    current = int(stored.get("version") or 0)
    if base_version is not None and base_version != current:
        raise FleetActionRefusedError(
            409, f"The documentation changed since you opened it (version {current}): reload it, then try again."
        )
    cleaned = clean_sections(sections)
    kept = stored.get("structured") if structured is None else clean_structured(structured)
    _write(
        fleet,
        cleaned,
        await current_fingerprint(db, fleet),
        str(author),
        version=current + 1,
        structured=kept,
        notes=notes,
        findings=findings,
        finding_prints=finding_prints,
    )


async def restore_shipped_documentation(db: AsyncSession, fleet: Fleet, *, author: uuid.UUID) -> None:
    """Bring back a built-in fleet's shipped documentation as its next version (#9768 S2).

    Refused: a custom fleet (422, it has no shipped version) or a built-in
    fleet whose shipped copy is not stored yet (409, the next boot stores it).
    """
    if not fleet.is_builtin:
        raise FleetActionRefusedError(422, "Only a built-in fleet has a shipped documentation to restore.")
    stored = _stored(fleet) or {}
    shipped = stored.get("shipped")
    if not isinstance(shipped, dict) or not shipped.get("sections"):
        raise FleetActionRefusedError(
            409, "The shipped documentation is not available yet: try again after the next update."
        )
    _write(
        fleet,
        dict(shipped["sections"]),
        await current_fingerprint(db, fleet),
        str(author),
        version=int(stored.get("version") or 0) + 1,
        structured=shipped.get("structured"),
        restored_from={"version": None, "author": "seed"},
    )
    fleet.documentation["shipped"] = shipped


@dataclass
class HistoryEntry:
    """One kept version of a fleet's documentation, as the version history lists it."""

    version: int
    updated_at: str | None
    #: A user id, ``seed`` (built-in) or ``fork`` (copied at the fork).
    updated_by: str | None
    #: The sections written in that version, in reading order.
    section_keys: list[str] = field(default_factory=list)
    current: bool = False
    #: Set when that version is a restore (see :class:`DocumentationView`).
    restored_from: dict[str, Any] | None = None
    #: What the generator left out or derived for that version (#9979, #10246): :func:`doc_note` codes.
    notes: list[dict[str, Any]] = field(default_factory=list)
    #: That version lost a visual block or misses a section: the history offers Regenerate (#10246).
    needs_regenerate: bool = False


#: Generator notes that make a version worth regenerating (#10246).
REGENERATE_NOTES = frozenset({"block_lost", "sections_not_drafted"})


def _version_notes(doc: dict[str, Any]) -> tuple[list[dict[str, Any]], bool]:
    notes = [stored_note(n) for n in doc.get("notes") or []]
    return notes, any(n["code"] in REGENERATE_NOTES for n in notes)


def documentation_history(fleet: Fleet, *, with_notes: bool = False) -> list[HistoryEntry]:
    """The current version then the kept ones (newest first), without their texts (#9768 S2).

    *with_notes*: each entry carries its generator notes and the Regenerate flag, for a reader
    who manages the fleet (#10246).
    """
    stored = _stored(fleet)
    if stored is None or not stored.get("sections"):
        return []
    entries: list[HistoryEntry] = []
    for doc, current in [(stored, True), *((kept, False) for kept in stored.get("history") or [])]:
        if not isinstance(doc, dict) or not isinstance(doc.get("sections"), dict) or not doc["sections"]:
            continue
        notes, flagged = _version_notes(doc) if with_notes else ([], False)
        entries.append(
            HistoryEntry(
                version=int(doc.get("version") or 0),
                updated_at=doc.get("updated_at"),
                updated_by=doc.get("updated_by"),
                section_keys=[k for k in DOC_SECTIONS if doc["sections"].get(k)],
                current=current,
                restored_from=_restored_from(doc),
                notes=notes,
                needs_regenerate=flagged,
            )
        )
    return entries


async def dismiss_findings(
    db: AsyncSession,
    fleet: Fleet,
    ids: list[str] | None,
    *,
    by: str,
    by_name: str = "",
    reason: str = "",
) -> list[dict[str, Any]]:
    """Hide the stored findings *ids* (every visible one when ``None``) while their member is unchanged (#10536).

    No new version. Refused (404) when an id names no stored finding. Answers the findings dismissed;
    the caller audits and commits.
    """
    from app.services.fleet_practice_suggestions import practice_suggestions

    stored = _stored(fleet)
    every = clean_findings((stored or {}).get("findings"))
    practice = await practice_suggestions(db, fleet) if stored is not None else []
    prints = await current_member_fingerprints(db, fleet)
    # A lapsed suggestion is dismissed by the id the list shows, its finding stored or not (#12099).
    lapsed = lapsed_points(stored, prints, [*every, *practice])
    by_id = {f["id"]: f for f in [*every, *practice, *lapsed]}
    if ids is None:
        chosen = [*visible_findings(stored, prints), *visible_practice(stored, prints, practice), *lapsed]
    else:
        unknown = [i for i in ids if i not in by_id]
        if stored is None or unknown or not ids:
            raise FleetActionRefusedError(404, "No such suggestion on this fleet's documentation.")
        chosen = [by_id[i] for i in dict.fromkeys(ids)]
    if stored is None or not chosen:
        return []
    # Former records are moved to their facet print first (#12181), so this write never keeps a whole print.
    records, _ = settled_dismissals(list(stored.get("dismissed_findings") or []), prints)
    for f in chosen:
        facet, member_print = dismissal_print(f, prints)
        records = dismissal_record(
            {"dismissed_findings": records},
            {k: v for k, v in f.items() if k != "lapsed"},
            member_print,
            by=by,
            by_name=by_name,
            reason=reason,
            facet=facet,
        )
    # A new dict so the JSON column is seen as changed.
    fleet.documentation = {**stored, "dismissed_findings": records}
    return chosen


async def dismiss_finding(
    db: AsyncSession, fleet: Fleet, fid: str, *, by: str = "", by_name: str = "", reason: str = ""
) -> None:
    """Hide the stored finding *fid* while its member is unchanged (#10246, #10536); 404 when unknown."""
    await dismiss_findings(db, fleet, [fid], by=by, by_name=by_name, reason=reason)


def restore_findings(fleet: Fleet, ids: list[str] | None) -> int:
    """Bring back the dismissed findings *ids* (every one when ``None``), answering how many (#10536).

    Refused (404) when an id names no dismissal. A restored finding a regeneration had dropped from the
    stored list is put back from its dismissal record, same id (#10546 retest): it is active again, never
    in neither list. The caller audits and commits.
    """
    stored = _stored(fleet)
    if stored is None:
        raise FleetActionRefusedError(404, "No such dismissed suggestion on this fleet's documentation.")
    if ids is not None:
        known = {d.get("id") for d in stored.get("dismissed_findings") or [] if isinstance(d, dict)}
        if not ids or any(i not in known for i in ids):
            raise FleetActionRefusedError(404, "No such dismissed suggestion on this fleet's documentation.")
    kept, removed = without_dismissals(stored, set(ids) if ids is not None else None)
    if removed:
        from app.services.fleet_practice_suggestions import is_practice_kind

        # A practice suggestion (#11948 S2) is computed on read: restoring it never stores it.
        back = [
            d
            for d in stored.get("dismissed_findings") or []
            if isinstance(d, dict) and d not in kept and not is_practice_kind(d.get("kind"))
        ]
        findings = with_restored(clean_findings(stored.get("findings")), back)
        fleet.documentation = {**stored, "dismissed_findings": kept, "findings": findings}
    return removed


async def restore_documentation_version(
    db: AsyncSession, fleet: Fleet, version: int, *, author: uuid.UUID, base_version: int | None
) -> None:
    """Bring a kept version back as the next version, like the shipped restore; no model call (#9768 S2).

    The version shown now moves into the history, so a restore is undone by
    restoring again. Refused: a built-in fleet (422, #1956), a stale
    *base_version* (409), a version that is not kept or is the current one
    (404 / 422).
    """
    refuse_builtin_rewrite(fleet)
    stored = _stored(fleet) or {}
    current = int(stored.get("version") or 0)
    if base_version is not None and base_version != current:
        raise FleetActionRefusedError(
            409, f"The documentation changed since you opened it (version {current}): reload it, then try again."
        )
    if version == current:
        raise FleetActionRefusedError(422, f"Version {version} is the version shown now.")
    kept = next(
        (
            h
            for h in stored.get("history") or []
            if isinstance(h, dict) and int(h.get("version") or 0) == version and isinstance(h.get("sections"), dict)
        ),
        None,
    )
    if kept is None or not kept["sections"]:
        raise FleetActionRefusedError(404, f"Version {version} of this documentation is not kept any more.")
    _write(
        fleet,
        {k: v for k, v in kept["sections"].items() if k in DOC_SECTIONS},
        await current_fingerprint(db, fleet),
        str(author),
        version=current + 1,
        structured=kept.get("structured") or None,
        restored_from={"version": version, "author": _source_author(kept)},
    )


async def seed_documentation(
    db: AsyncSession, fleet: Fleet, sections: dict[str, str] | None, structured: dict[str, Any] | None = None
) -> None:
    """Write a built-in fleet's documentation from its seed entry; nothing when unchanged or absent.

    The shipped copy is always stored (``shipped``). A version the seed did not
    write (``local: true`` from a regenerate, or a restore, that an earlier
    build allowed on a built-in fleet) is replaced by the shipped one as the
    next version, kept in the history (#1956): a built-in fleet always shows
    its shipped text, written by the seed.
    """
    if not sections:
        return
    cleaned = clean_sections(sections)
    parts = clean_structured(structured)
    shipped = {"sections": cleaned, "structured": parts}
    stored = _stored(fleet)
    fingerprint = await current_fingerprint(db, fleet)
    seeded = stored is not None and stored.get("updated_by") == "seed" and not stored.get("local")
    if (
        stored is not None
        and seeded
        and stored.get("sections") == cleaned
        and (stored.get("structured") or None) == parts
    ):
        # Same text: the seed also defines the structure, so the docs follow it.
        if stored.get("fingerprint") != fingerprint or stored.get("shipped") != shipped:
            fleet.documentation = {**stored, "fingerprint": fingerprint, "shipped": shipped}
        return
    _write(fleet, cleaned, fingerprint, "seed", version=int((stored or {}).get("version") or 0) + 1, structured=parts)
    fleet.documentation["shipped"] = shipped


async def copy_documentation(db: AsyncSession, source: Fleet, fork: Fleet) -> None:
    """Give *fork* its *source*'s documentation as version 1, out of date only once the fork moves."""
    stored = _stored(source)
    if stored is None or not stored.get("sections"):
        return
    fork.documentation = None
    structured = stored.get("structured")
    _write(
        fork,
        dict(stored["sections"]),
        await current_fingerprint(db, fork),
        "fork",
        version=1,
        structured=dict(structured) if structured else None,
    )
