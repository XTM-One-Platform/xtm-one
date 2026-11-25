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

"""Reset a built-in fleet to its factory state (#1508).

"Back to not set up" (:func:`app.services.fleet_control.unset_fleet`) only
switches the automations off. Everything else a person sets on a built-in
fleet survives it and every boot by design (the customer layer, #914 / #916):
the autonomy level once moved, approval pins, customer gates, the run-as of
the automations, the member defaults, a local documentation version, and the
customer layer of every home member agent (integration bindings and their
scoping, MCP servers, knowledge bases, skills, custom tools, organization
notes, model pin, approval switches).

:func:`reset_builtin_fleet` puts all of it back to what a fresh install seeds:

* every automation off and ``created_disabled`` (``unset_fleet``), its run-as
  and schedule anchor cleared;
* the autonomy level back to ``SEEDED_FLEET_AUTONOMY_LEVEL``; ``approval_pins``
  and ``customer_gates`` emptied; every fleet-level customer setting back to
  its column default; a local documentation version replaced by the shipped one;
* every home member agent's customer layer cleared, the bindings the seed
  itself makes (built-in integrations, knowledge bases, skills) kept, and its
  definition rewritten from the seed spec now (``_sync_agent_fields``).

Kept: ids, run history, cases and findings, the audit trail, the workspace,
and the open work items of the members (decision items included): the dry run
and the audit entry name how many (``FleetResetOutcome.before``, #2134).
A dry run answers the counts the confirmation dialog shows, and the automations
an emergency stop holds. Refused during a platform emergency stop, while a stop
holds any automation of the fleet, and on a custom fleet. Idempotent: a second call
changes nothing and the next boot changes nothing. The caller owns the commit.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from fastapi import Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.agent import Agent
from app.models.fleet import FLEET_ROLE_CONNECTED, FLEET_ROLE_MEMBER, Fleet
from app.models.user import User
from app.services.fleets import FleetActionRefusedError, fleet_members

RESET_REFUSED_CUSTOM = "Only a built-in fleet can be reset to its factory state; a custom fleet has no factory state."
RESET_REFUSED_DURING_STOP = "An emergency stop is in effect. Lift it (profile menu or banner) before resetting a fleet."
RESET_REFUSED_HELD_BY_STOP = (
    "An emergency stop holds automations of this fleet ({names}). Resume them before resetting the fleet."
)

#: Fleet columns a person may set on a built-in fleet, with the value a fresh seed leaves.
_FLEET_FACTORY_VALUES: dict[str, Any] = {
    "approval_pins": None,
    "customer_gates": None,
    "enforce_gates_at_tool_level": False,
    "instructions_position": None,
    "instructions_position_announced": None,
    "prerequisite_confirmations": None,
    "health_dismissals": None,
    "health_check": None,
    "health_enrolled": False,
    "hide_activity_from_non_admins": False,
    "budget_config": None,
    "max_iterations": None,
    "run_budget": None,
    "default_context_inclusion": "none",
    "default_collaboration_instruction": "",
}

#: Member row columns a person may set, with their seed value.
_MEMBER_FACTORY_VALUES: dict[str, Any] = {"context_inclusion": None, "organization_scope": None}

#: Customer-layer agent fields reset by value (the seeded bindings are recomputed apart).
_AGENT_FACTORY_VALUES: dict[str, Any] = {
    "integration_lending": None,
    "integration_allowed_tools": None,
    "integration_element_bindings": None,
    "integration_element_enforcement": None,
    "integration_read_only": None,
    "integration_signatures": None,
    "integration_live_writes": None,
    "mcp_server_ids": [],
    "mcp_allowed_tools": None,
    "knowledge_base_permissions": None,
    "skill_load_modes": None,
    "custom_tool_ids": [],
    "customer_notes": None,
    "customer_fields": None,
    "enabled": True,
    "approve_tool_calls": None,
    "hide_activity_from_non_admins": False,
    "llm_provider": None,
    "llm_provider_id": None,
}


@dataclass
class FleetResetOutcome:
    """What a reset changes (or, on a dry run, would change)."""

    automations: int = 0
    run_as_cleared: int = 0
    bindings: int = 0
    pins: int = 0
    gates: int = 0
    autonomy_level: str | None = None
    agents: int = 0
    settings: list[str] = field(default_factory=list)
    documentation: bool = False
    held_by_stop: tuple[str, ...] = ()
    #: The fleet as it stood before the reset (#2134): the level that held, its members, pins and
    #: added gates, and the open work the reset keeps (open decision items and open items of its
    #: home members). What the audit entry records under ``before``.
    before: dict[str, Any] = field(default_factory=dict)

    @property
    def changed(self) -> bool:
        return bool(
            self.automations
            or self.run_as_cleared
            or self.bindings
            or self.pins
            or self.gates
            or self.autonomy_level
            or self.agents
            or self.settings
            or self.documentation
        )

    def audit_details(self) -> dict[str, Any]:
        """What the reset changed (the audit entry's ``changes``; ``before`` is :attr:`before`)."""
        return {
            "automations": self.automations,
            "run_as_cleared": self.run_as_cleared,
            "bindings": self.bindings,
            "pins": self.pins,
            "gates": self.gates,
            "autonomy_level": self.autonomy_level,
            "agents": self.agents,
            "settings": list(self.settings),
            "documentation": self.documentation,
            "held_by_stop": list(self.held_by_stop),
        }


def _differs(current: Any, factory: Any) -> bool:
    """Whether *current* is not the factory value (an empty value equals any other empty one)."""
    if not factory:
        return bool(current)
    return bool(current != factory)


def _seed_entry(slug: str) -> dict[str, Any] | None:
    from app.agents.fleet_seed import fleets_for_mode
    from app.services.ai_state import content_mode

    return next((f for f in fleets_for_mode(content_mode()) if f["slug"] == slug), None)


def _agent_specs() -> dict[str, dict[str, Any]]:
    from app.agents.seed import _seeded_builtin_agents

    return {a["slug"]: a for a in _seeded_builtin_agents()}


async def _seeded_bindings(db: AsyncSession, slug: str) -> tuple[dict[str, list[str]], set[str], set[str]]:
    """The integration bindings, knowledge base ids and skill ids the seed itself gives *slug*."""
    from app.agents.builtin_agents import BUILTIN_INTEGRATIONS, BUILTIN_KNOWLEDGE_BASES, BUILTIN_SKILLS
    from app.agents.seed import _seed_slug_tag
    from app.models.agent_skill import AgentSkill
    from app.models.integration import Integration
    from app.models.knowledge_base import KnowledgeBase

    integrations = {
        (i["provider"], i["name"]) for i in BUILTIN_INTEGRATIONS if slug in i.get("bind_to_agent_slugs", [])
    }
    bindings: dict[str, list[str]] = {}
    if integrations:
        rows = await db.execute(
            # access_restricted: bypass - a reset of a built-in fleet reads the built-in integrations by key.
            select(Integration.id, Integration.provider, Integration.name).where(Integration.is_builtin.is_(True))
        )
        for iid, provider, name in rows.all():
            if (provider, name) in integrations:
                bindings.setdefault(provider, []).append(str(iid))
    kb_slugs = {kb["slug"] for kb in BUILTIN_KNOWLEDGE_BASES if slug in kb.get("bind_to_agent_slugs", [])}
    kb_ids: set[str] = set()
    if kb_slugs:
        rows = await db.execute(select(KnowledgeBase.id, KnowledgeBase.tags).where(KnowledgeBase.is_builtin.is_(True)))
        kb_ids = {str(kid) for kid, tags in rows.all() if _seed_slug_tag(tags) in kb_slugs}
    skill_slugs = {s["slug"] for s in BUILTIN_SKILLS if slug in s.get("bind_to_agent_slugs", [])}
    skill_ids: set[str] = set()
    if skill_slugs:
        rows = await db.execute(select(AgentSkill.id, AgentSkill.tags).where(AgentSkill.is_company_managed.is_(True)))
        skill_ids = {str(sid) for sid, tags in rows.all() if _seed_slug_tag(tags) in skill_slugs}
    return bindings, kb_ids, skill_ids


def _binding_count(value: Any) -> int:
    if isinstance(value, dict):
        return sum(len(v or []) for v in value.values())
    return len(value or [])


async def _reset_agent(
    db: AsyncSession, agent: Agent, spec: dict[str, Any] | None, *, apply: bool, anthropic_enabled: bool
) -> tuple[bool, int]:
    """Reset *agent*'s customer layer and rewrite it from *spec*; (changed, bindings removed)."""
    from sqlalchemy.orm.attributes import flag_modified

    seeded_bindings, kb_ids, skill_ids = await _seeded_bindings(db, agent.slug or "")
    removed = 0
    changed = False
    current = {p: [str(i) for i in ids or []] for p, ids in (agent.integration_bindings or {}).items()}
    kept_seeded = {p: [i for i in ids if i in set(seeded_bindings.get(p, []))] for p, ids in current.items()}
    removed += _binding_count(current) - _binding_count(kept_seeded)
    target = dict(kept_seeded)
    # Every seeded binding is present after a seed; one a person removed comes back.
    for provider, ids in seeded_bindings.items():
        kept = target.setdefault(provider, [])
        kept.extend(i for i in ids if i not in kept)
    target = {p: ids for p, ids in target.items() if ids}
    if target != current:
        changed = True
        if apply:
            agent.integration_bindings = target
    removed += _binding_count((agent.integration_lending or {}).get("integration_ids"))
    kbs = [str(k) for k in agent.knowledge_base_ids or []]
    kept_kbs = [k for k in kbs if k in kb_ids] + sorted(kb_ids - set(kbs))
    skills = [str(s) for s in agent.skill_ids or []]
    kept_skills = [s for s in skills if s in skill_ids] + sorted(skill_ids - set(skills))
    removed += (
        len(set(kbs) - kb_ids)
        + len(set(skills) - skill_ids)
        + len(agent.mcp_server_ids or [])
        + len(agent.custom_tool_ids or [])
    )
    if kept_kbs != kbs or kept_skills != skills:
        changed = True
        if apply:
            agent.knowledge_base_ids = kept_kbs
            agent.skill_ids = kept_skills
    for name, factory in _AGENT_FACTORY_VALUES.items():
        if _differs(getattr(agent, name, None), factory):
            changed = True
            if apply:
                setattr(agent, name, list(factory) if isinstance(factory, list) else factory)
    if spec is not None:
        from app.agents.seed import _resolve_llm_model

        model = _resolve_llm_model(spec, anthropic_enabled=anthropic_enabled)
        if agent.llm_model != model or agent.llm_model_source != spec.get("llm_model_source"):
            changed = True
        if (agent.integration_template_bindings or []) != (spec.get("integration_template_bindings") or []):
            changed = True
        if (agent.integration_template_read_only or None) != (spec.get("integration_template_read_only") or None):
            changed = True
    if apply and changed:
        from app.agents.seed import _sync_agent_fields
        from app.services.agent_config_versioning import record_agent_version_changes, versioned_agent_values

        before = versioned_agent_values(agent)
        if spec is not None:
            # customer_fields is empty now: the seed writes the model and the template defaults.
            _sync_agent_fields(agent, spec, anthropic_enabled=anthropic_enabled)
        for attr in ("integration_bindings", "knowledge_base_ids", "skill_ids", "mcp_server_ids", "custom_tool_ids"):
            flag_modified(agent, attr)
        await record_agent_version_changes(
            db, agent=agent, before=before, author_id=None, commit_message="Fleet reset to factory state"
        )
    return changed, removed


async def reset_builtin_fleet(
    db: AsyncSession,
    fleet: Fleet,
    *,
    actor: User,
    dry_run: bool = False,
    request: Request | None = None,
) -> FleetResetOutcome:
    """Put built-in *fleet* back to its factory state; on *dry_run* only count what would change."""
    from app.agents.fleet_seed import SEEDED_FLEET_AUTONOMY_LEVEL
    from app.agents.seed import _is_anthropic_enabled
    from app.services.fleet_autonomy import effective_level
    from app.services.fleet_control import _fleet_automations, unset_fleet, unsettable_count
    from app.services.panic import platform_stop_open

    if not getattr(fleet, "is_builtin", False):
        raise FleetActionRefusedError(422, RESET_REFUSED_CUSTOM)
    if await platform_stop_open(db):
        raise FleetActionRefusedError(409, RESET_REFUSED_DURING_STOP)

    outcome = FleetResetOutcome()
    unsettable, held = await unsettable_count(db, fleet)
    outcome.held_by_stop = held
    # A stop of one person's or one fleet's scope holds automations a reset cannot touch (only
    # the stop's own resume lifts them), so the fleet would not end in its factory state: the
    # dry run names them for the dialog, the reset itself is refused (#1523 UX round 2 B1).
    if held and not dry_run:
        raise FleetActionRefusedError(409, RESET_REFUSED_HELD_BY_STOP.format(names=", ".join(held)))
    automations = await _fleet_automations(db, fleet, lock=not dry_run)
    outcome.run_as_cleared = sum(1 for a in automations if a.run_as_user_id is not None)
    outcome.automations = unsettable
    outcome.pins = len(fleet.approval_pins or {})
    outcome.gates = len(fleet.customer_gates or [])
    if effective_level(fleet.autonomy_level) != SEEDED_FLEET_AUTONOMY_LEVEL:
        outcome.autonomy_level = SEEDED_FLEET_AUTONOMY_LEVEL
    outcome.settings = sorted(
        name
        for name, factory in _FLEET_FACTORY_VALUES.items()
        if name not in ("approval_pins", "customer_gates") and _differs(getattr(fleet, name, None), factory)
    )
    if (fleet.organization_scope or {"mode": "primary"}) != {"mode": "primary"}:
        outcome.settings.append("organization_scope")
    stored_docs = fleet.documentation if isinstance(fleet.documentation, dict) else None
    outcome.documentation = bool(stored_docs and stored_docs.get("local"))

    rows = await fleet_members(db, fleet.id)
    home = [r for r in rows if r.role == FLEET_ROLE_MEMBER and r.agent_id is not None]
    open_gates, open_items = await _open_member_work(db, [r.agent_id for r in home])
    outcome.before = {
        "autonomy_level": effective_level(fleet.autonomy_level),
        "home_members": len(home),
        "connected_agents": sum(1 for r in rows if r.role == FLEET_ROLE_CONNECTED and r.agent_id is not None),
        "approval_pins": dict(fleet.approval_pins or {}),
        "customer_gates": outcome.gates,
        "open_gates": open_gates,
        "open_items": open_items,
    }
    member_settings = [
        r for r in home if any(_differs(getattr(r, n, None), v) for n, v in _MEMBER_FACTORY_VALUES.items())
    ]
    if member_settings:
        outcome.settings.append("member_settings")
    specs = _agent_specs()
    anthropic_enabled = await _is_anthropic_enabled(db)
    agents = [a for a in [await db.get(Agent, r.agent_id) for r in home] if a is not None and a.is_builtin]
    for agent in agents:
        changed, removed = await _reset_agent(
            db, agent, specs.get(agent.slug or ""), apply=not dry_run, anthropic_enabled=anthropic_enabled
        )
        outcome.agents += int(changed)
        outcome.bindings += removed
    if dry_run:
        return outcome

    await unset_fleet(db, fleet, actor=actor, request=request)
    for assignment in automations:
        assignment.run_as_user_id = None
        assignment.schedule_resumed_at = None
    fleet.autonomy_level = SEEDED_FLEET_AUTONOMY_LEVEL
    for name, factory in _FLEET_FACTORY_VALUES.items():
        setattr(fleet, name, factory)
    fleet.organization_scope = {"mode": "primary"}
    for row in member_settings:
        for name, factory in _MEMBER_FACTORY_VALUES.items():
            setattr(row, name, factory)
    if outcome.documentation:
        await _restore_shipped_documentation(db, fleet)
    if fleet.orchestrator_agent_id is not None:
        from app.services.fleets import ensure_fleet_health_check

        lead = await db.get(Agent, fleet.orchestrator_agent_id)
        if lead is not None:
            await ensure_fleet_health_check(db, fleet, lead)
    await db.flush()
    return outcome


async def _open_member_work(db: AsyncSession, agent_ids: list[Any]) -> tuple[int, int]:
    """``(open decision items, open items)`` owned by *agent_ids*: the work a reset keeps.

    A decision item is an open item with ``needs_human`` set (a gate waiting for a person).
    """
    from sqlalchemy import func

    from app.models.work_item import WORK_ITEM_TERMINAL_STATUSES, WorkItem

    if not agent_ids:
        return 0, 0
    row = (
        await db.execute(
            # access_restricted: bypass - an administrator's reset counts its own fleet members' items.
            select(
                func.count().filter(WorkItem.needs_human.is_(True)),
                func.count(),
            ).where(
                WorkItem.owner_agent_id.in_(agent_ids),
                WorkItem.status.notin_(WORK_ITEM_TERMINAL_STATUSES),
            )
        )
    ).one()
    return int(row[0] or 0), int(row[1] or 0)


async def _restore_shipped_documentation(db: AsyncSession, fleet: Fleet) -> None:
    """Drop a local documentation version so the shipped one is written, as a fresh seed does."""
    from app.agents.builtin_fleet_docs import builtin_fleet_documentation, builtin_fleet_structured
    from app.services.fleet_docs import seed_documentation

    entry = _seed_entry(fleet.slug)
    if entry is None:
        return
    stored = dict(fleet.documentation or {})
    stored.pop("local", None)
    fleet.documentation = stored
    await seed_documentation(db, fleet, builtin_fleet_documentation(entry), builtin_fleet_structured(entry))
