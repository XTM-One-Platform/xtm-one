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

"""Fork a fleet: an editable personal copy of any fleet, a built-in one included (#9174).

A built-in fleet is read-only and rewritten by the seed on every boot
(:data:`app.services.fleets.BUILTIN_FLEET_WRITABLE_FIELDS`); a fork is how a
person adapts one. The copy is a normal personal fleet the seed never reads:

* **Members.** ``members="copy"`` (the default) makes each home member agent an
  editable personal copy through the agent duplicate path
  (``app.api.v1.agents.copy_agent_for_user``: the shared copy manifest, v1
  versions, bindings the forker cannot bind left out and counted), the copies'
  intra-fleet handovers re-pointed at the copies. ``members="keep"`` copies the
  orchestrator only (a fleet needs a home entry agent and an agent has one home
  fleet) and links every other member as a connected agent of the fork,
  carrying its role label as the collaboration instruction.
* **Structure.** Parents, exposure, role labels, the orchestrator, connected
  agents and fleets, the card skills, accepted handover types and the run
  defaults (budget lowered to the forker's ceiling, iteration cap clamped).
* **Nothing runs.** Every copied assignment is created switched off with the
  ``created_disabled`` stamp, whatever the source's state, and loses the
  source's OpenCTI / OpenAEV provisioning state; enabling one goes through the
  opt-in flow. A fork starts no run and meters no quota.
* **Provenance.** ``fleets.forked_from`` records the source (id, slug, name,
  built-in flag), :func:`structure_fingerprint` of its structure at fork time
  and that structure (:func:`structure_snapshot`), so a later change of the
  source is detectable (:func:`source_changed`) and explainable as a
  structural diff (:func:`source_changes`).

The caller checked visibility of the source and audits; this module flushes,
never commits.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.utils import fit_copy_name
from app.models.agent import Agent
from app.models.fleet import FLEET_DEFAULT_ICON, FLEET_ROLE_CONNECTED, FLEET_ROLE_MEMBER, Fleet, FleetMember
from app.models.user import User
from app.models.workspace import Workspace
from app.services.fleet_autonomy import customer_gates_of
from app.services.fleets import (
    FleetActionRefusedError,
    add_connected,
    add_home_member,
    create_fleet,
    ensure_workspace_can_host,
    fleet_members,
    resolve_organization_scope,
    set_orchestrator,
)

ForkMembers = Literal["copy", "keep"]
FORK_MEMBER_MODES: tuple[str, ...] = ("copy", "keep")


@dataclass
class FleetForkResult:
    """What a fork wrote: the new fleet, the agents copied, what was left out."""

    fleet: Fleet
    copied_agents: dict[uuid.UUID, Agent] = field(default_factory=dict)
    kept_agents: list[uuid.UUID] = field(default_factory=list)
    assignments_copied: int = 0
    warnings: list[str] = field(default_factory=list)


def fleet_structure(fleet: Fleet, rows: list[FleetMember], agents: dict[uuid.UUID, Agent]) -> dict:
    """What a fork copies from *fleet*: its structure, not its state.

    Covers the fleet's card fields, each home member (agent slug, role label,
    parent, exposure, persona, skill ids and handovers by slug) and each
    connected link. Archiving, the run defaults and timestamps are left out:
    they do not change what the fork holds.
    """
    slug_of = {a.id: a.slug for a in agents.values()}
    member_slug = {r.id: slug_of.get(r.agent_id) for r in rows if r.agent_id is not None}
    members = []
    for row in rows:
        if row.role == FLEET_ROLE_MEMBER and row.agent_id is not None:
            agent = agents.get(row.agent_id)
            members.append(
                {
                    "slug": slug_of.get(row.agent_id, str(row.agent_id)),
                    "role_label": row.role_label or "",
                    "parent": member_slug.get(row.parent_member_id) if row.parent_member_id else None,
                    "exposed": bool(row.is_exposed),
                    "persona": (agent.persona or "") if agent is not None else "",
                    "skill_ids": sorted(str(s) for s in (agent.skill_ids or [])) if agent is not None else [],
                    "handovers": sorted(
                        f"{slug_of.get(_uuid(t), t)}:{(agent.handover_types or {}).get(str(t)) or 'delegate'}"
                        for t in (agent.handover_agent_ids or [])
                    )
                    if agent is not None
                    else [],
                }
            )
        elif row.role == FLEET_ROLE_CONNECTED:
            members.append(
                {
                    "connected": str(row.agent_id or row.target_fleet_id),
                    "instruction": row.collaboration_instruction or "",
                    "inclusion": row.context_inclusion or "none",
                }
            )
    return {
        "name": fleet.name,
        "description": fleet.description or "",
        "skills": fleet.skills,
        "accepted_handover_types": fleet.accepted_handover_types,
        "orchestrator": slug_of.get(fleet.orchestrator_agent_id) if fleet.orchestrator_agent_id else None,
        "members": sorted(members, key=lambda m: json.dumps(m, sort_keys=True)),
    }


def structure_fingerprint(structure: dict) -> str:
    """A short hash of a :func:`fleet_structure`."""
    return hashlib.sha256(json.dumps(structure, sort_keys=True, default=str).encode()).hexdigest()[:16]


def structure_snapshot(structure: dict) -> dict:
    """*structure* as kept in ``forked_from``: each persona replaced by a short digest.

    A persona can be long; the diff only needs to know that it changed.
    """
    members = []
    for member in structure.get("members") or []:
        if "persona" in member:
            member = {**member, "persona": hashlib.sha256(member["persona"].encode()).hexdigest()[:12]}
        members.append(member)
    return {**structure, "members": members}


async def source_structure(db: AsyncSession, fleet: Fleet) -> dict:
    """:func:`fleet_structure` of *fleet* as stored now."""
    rows = await fleet_members(db, fleet.id)
    ids = [r.agent_id for r in rows if r.agent_id is not None]
    agents: dict[uuid.UUID, Agent] = {}
    if ids:
        agents = {a.id: a for a in (await db.execute(select(Agent).where(Agent.id.in_(ids)))).scalars().all()}
    return fleet_structure(fleet, rows, agents)


async def source_fingerprint(db: AsyncSession, fleet: Fleet) -> str:
    """:func:`structure_fingerprint` of *fleet* as stored now."""
    return structure_fingerprint(await source_structure(db, fleet))


async def source_changed(db: AsyncSession, fork: Fleet) -> bool | None:
    """Whether the source of *fork* changed since the fork; None when unknown.

    None: *fork* is not a fork, or its source no longer exists.
    """
    provenance = fork.forked_from if isinstance(fork.forked_from, dict) else None
    if not provenance:
        return None
    source = await db.get(Fleet, _uuid(provenance.get("fleet_id")))
    if source is None:
        return None
    return await source_fingerprint(db, source) != provenance.get("fingerprint")


@dataclass
class SourceChanges:
    """How the source of a fork moved since the fork (:func:`source_changes`)."""

    is_fork: bool
    source_exists: bool = False
    #: None when unknown: not a fork, or the source is gone.
    changed: bool | None = None
    #: False on a fork made before the structure was kept: changed is known, the diff is not.
    diff_available: bool = False
    changes: list[dict] = field(default_factory=list)
    #: The seeded fleet that took over a retired built-in source (CTEM-31): {fleet_id, slug, name}.
    successor: dict | None = None


async def retired_source_successor(db: AsyncSession, provenance: dict) -> dict | None:
    """The seeded fleet that took over the retired built-in source of a fork, else None."""
    from app.agents.builtin_fleets import RETIRED_FLEET_SUCCESSORS

    if not provenance.get("is_builtin"):
        return None
    slug = RETIRED_FLEET_SUCCESSORS.get(str(provenance.get("slug") or ""))
    if slug is None:
        return None
    row = (
        await db.execute(select(Fleet).where(Fleet.is_builtin.is_(True), Fleet.slug == slug).limit(1))
    ).scalar_one_or_none()
    if row is None:
        return None
    return {"fleet_id": str(row.id), "slug": row.slug, "name": row.name}


_CARD_FIELDS = ("name", "description", "skills", "accepted_handover_types", "orchestrator")
_MEMBER_FIELDS = ("role_label", "parent", "exposed", "persona", "skill_ids", "handovers")
_CONNECTED_FIELDS = ("instruction", "inclusion")


def diff_structures(before: dict, after: dict) -> list[dict]:
    """The structural changes from *before* to *after* (two :func:`structure_snapshot`).

    One entry per object changed: ``{object, name, action, fields}``, object
    ``fleet``, ``member`` (by agent slug) or ``connected`` (by agent or fleet
    id), action ``added``, ``removed`` or ``changed`` with the fields that moved.
    """
    changes: list[dict] = []
    card = [f for f in _CARD_FIELDS if before.get(f) != after.get(f)]
    if card:
        changes.append({"object": "fleet", "name": after.get("name") or "", "action": "changed", "fields": card})

    def _index(structure: dict, key: str) -> dict[str, dict]:
        return {m[key]: m for m in structure.get("members") or [] if key in m}

    for kind, key, fields in (("member", "slug", _MEMBER_FIELDS), ("connected", "connected", _CONNECTED_FIELDS)):
        old, new = _index(before, key), _index(after, key)
        for name in sorted(new.keys() - old.keys()):
            changes.append({"object": kind, "name": name, "action": "added", "fields": []})
        for name in sorted(old.keys() - new.keys()):
            changes.append({"object": kind, "name": name, "action": "removed", "fields": []})
        for name in sorted(old.keys() & new.keys()):
            moved = [f for f in fields if old[name].get(f) != new[name].get(f)]
            if moved:
                changes.append({"object": kind, "name": name, "action": "changed", "fields": moved})
    return changes


async def source_changes(db: AsyncSession, fork: Fleet) -> SourceChanges:
    """Whether, and how, the source of *fork* changed since the fork.

    Reads the source as stored, whatever the caller may see of it: the answer
    carries slugs, ids and field names, never a persona or a description.
    """
    provenance = fork.forked_from if isinstance(fork.forked_from, dict) else None
    if not provenance:
        return SourceChanges(is_fork=False)
    source = await db.get(Fleet, _uuid(provenance.get("fleet_id")))
    if source is None:
        return SourceChanges(is_fork=True, successor=await retired_source_successor(db, provenance))
    live = await source_structure(db, source)
    result = SourceChanges(
        is_fork=True,
        source_exists=True,
        changed=structure_fingerprint(live) != provenance.get("fingerprint"),
    )
    if source.is_builtin and source.slug != provenance.get("slug"):
        # CTEM-31: the built-in source was adopted under a new slug (the generic CTEM loop).
        result.successor = {"fleet_id": str(source.id), "slug": source.slug, "name": source.name}
    kept = provenance.get("structure")
    if isinstance(kept, dict):
        result.diff_available = True
        if result.changed:
            result.changes = diff_structures(kept, structure_snapshot(live))
    return result


async def fork_fleet(
    db: AsyncSession,
    source: Fleet,
    user: User,
    user_group_ids: list[str],
    *,
    name: str | None = None,
    workspace_id: uuid.UUID | None = None,
    members: ForkMembers = "copy",
) -> FleetForkResult:
    """Write a personal fork of *source* for *user*; the caller audits and commits.

    Refused (:class:`FleetActionRefusedError`): the built-in Platform Health
    fleet (403, read-only for everyone), an unknown *members* mode
    (422), a fleet without an orchestrator in ``keep`` mode (400), a
    workspace the caller cannot manage or that cannot host a personal fleet.
    """
    from app.api.v1.agents import copy_agent_for_user
    from app.api.v1.helpers import lower_budget_to_ceiling, user_visible_filter
    from app.models.general_assistant_config import clamp_max_iterations
    from app.services.fleet_docs import copy_documentation
    from app.services.health.read_only import HEALTH_READ_ONLY_REFUSAL, is_health_fleet
    from app.services.llm_provider_deletion import lock_provider_configs

    if is_health_fleet(source):
        # The Platform Health fleet ships with the platform and is never forked (#101).
        raise FleetActionRefusedError(403, HEALTH_READ_ONLY_REFUSAL)
    if members not in FORK_MEMBER_MODES:
        raise FleetActionRefusedError(422, f"members is one of {', '.join(FORK_MEMBER_MODES)}.")

    rows = await fleet_members(db, source.id)
    homes = [r for r in rows if r.role == FLEET_ROLE_MEMBER and r.agent_id is not None]
    member_ids = [r.agent_id for r in rows if r.agent_id is not None]
    visible: dict[uuid.UUID, Agent] = {}
    if member_ids:
        found = await db.execute(
            select(Agent).where(
                Agent.id.in_(member_ids),
                user_visible_filter(Agent, user.id, user_group_ids, is_admin=user.is_admin),
            )
        )
        visible = {a.id: a for a in found.scalars().all()}
    all_agents: dict[uuid.UUID, Agent] = dict(visible)
    hidden = [a for a in member_ids if a not in visible]
    if hidden:
        all_agents.update(
            {a.id: a for a in (await db.execute(select(Agent).where(Agent.id.in_(hidden)))).scalars().all()}
        )
    structure = fleet_structure(source, rows, all_agents)

    workspace = await _fork_workspace(db, user, user_group_ids, workspace_id, name or source.name)
    fork_name = (name or "").strip()[:200] or fit_copy_name(source.name, Fleet.__table__.c.name, prefix="Copy of ")
    fork = await create_fleet(
        db,
        user_id=user.id,
        name=fork_name,
        workspace_id=workspace.id,
        description=source.description or "",
        icon=source.icon or FLEET_DEFAULT_ICON,
        color=source.color or "",
        skills=list(source.skills) if source.skills else None,
        accepted_handover_types=list(source.accepted_handover_types) if source.accepted_handover_types else None,
        budget_config=lower_budget_to_ceiling(source.budget_config, user=user) if source.budget_config else None,
        max_iterations=clamp_max_iterations(source.max_iterations) if source.max_iterations is not None else None,
        # The approval gate catalog is configuration (#10000): the fork gates what its source gates.
        # Gates a person added to a built-in source (#916) join the fork's own catalog.
        approval_gates=(
            [dict(g) for g in source.approval_gates]
            + [dict(g) for g in customer_gates_of(getattr(source, "customer_gates", None))]
            if isinstance(source.approval_gates, list)
            else None
        ),
        # The fork keeps its source's pins, and with them whether the computed catalog is on (#10153);
        # its level is a new fleet's (create_fleet), never raised from the source's.
        approval_pins=dict(source.approval_pins) if isinstance(source.approval_pins, dict) else None,
        forked_from={
            "fleet_id": str(source.id),
            "slug": source.slug,
            "name": source.name,
            "is_builtin": bool(source.is_builtin),
            "fingerprint": structure_fingerprint(structure),
            "structure": structure_snapshot(structure),
            "members": members,
            "forked_at": datetime.now(UTC).isoformat(),
        },
    )
    result = FleetForkResult(fleet=fork)

    to_copy = [r for r in homes if members == "copy" or r.agent_id == source.orchestrator_agent_id]
    if members == "keep" and source.orchestrator_agent_id is None:
        raise FleetActionRefusedError(
            400, "The fleet has no orchestrator: fork it with members copy, or set its orchestrator first."
        )
    if to_copy:
        await lock_provider_configs(db, shared=True)
    for row in to_copy:
        agent = visible.get(row.agent_id)  # type: ignore[arg-type]
        if agent is None:
            result.warnings.append(f"a member you cannot see ({row.agent_id}) was left out")
            continue
        copy, count, dropped = await copy_agent_for_user(db, agent, user, user_group_ids)
        result.copied_agents[agent.id] = copy
        result.assignments_copied += count
        lost = sum(v for v in dropped.values() if v)
        if lost:
            result.warnings.append(f"'{agent.name}': {lost} binding(s) you cannot use were not copied")

    _repoint_handovers(result.copied_agents)

    # Home members: the copies, in the source's order, then their places.
    member_row_of: dict[uuid.UUID, FleetMember] = {}
    for row in homes:
        copy = result.copied_agents.get(row.agent_id)  # type: ignore[arg-type]
        if copy is None:
            continue
        member_row_of[row.id] = await add_home_member(db, fork, copy, is_exposed=bool(row.is_exposed))
        member_row_of[row.id].role_label = row.role_label or ""
    for row in homes:
        new_row = member_row_of.get(row.id)
        if new_row is not None and row.parent_member_id in member_row_of:
            new_row.parent_member_id = member_row_of[row.parent_member_id].id
    orchestrator_copy = result.copied_agents.get(source.orchestrator_agent_id) if source.orchestrator_agent_id else None
    if orchestrator_copy is not None:
        await set_orchestrator(db, fork, orchestrator_copy.id)

    # The member configuration as it is (#9162 R3, scope 4): the fleet
    # defaults, and on every copied row its own overrides, NULL kept NULL so
    # an inherited setting still inherits in the fork.
    fork.default_context_inclusion = source.default_context_inclusion or "none"
    fork.default_collaboration_instruction = source.default_collaboration_instruction or ""
    fork.organization_scope = resolve_organization_scope(source.organization_scope, None)
    for row in homes:
        new_row = member_row_of.get(row.id)
        if new_row is not None:
            new_row.organization_scope = row.organization_scope

    # Kept members: connected agents of the fork, their role as the instruction.
    if members == "keep":
        for row in homes:
            if row.agent_id in result.copied_agents:
                continue
            agent = visible.get(row.agent_id)  # type: ignore[arg-type]
            if agent is None:
                result.warnings.append(f"a member you cannot see ({row.agent_id}) was left out")
                continue
            await add_connected(
                db,
                fork,
                agent=agent,
                collaboration_instruction=row.role_label or "",
                context_inclusion="none",
            )
            result.kept_agents.append(agent.id)

    # The source's connected agents and fleets, as far as the forker sees them.
    target_ids = [r.target_fleet_id for r in rows if r.role == FLEET_ROLE_CONNECTED and r.target_fleet_id]
    targets: dict[uuid.UUID, Fleet] = {}
    if target_ids:
        found_fleets = await db.execute(
            select(Fleet).where(
                Fleet.id.in_(target_ids),
                user_visible_filter(Fleet, user.id, user_group_ids, is_admin=user.is_admin),
            )
        )
        targets = {f.id: f for f in found_fleets.scalars().all()}
    for row in rows:
        if row.role != FLEET_ROLE_CONNECTED:
            continue
        agent = visible.get(row.agent_id) if row.agent_id is not None else None
        target = targets.get(row.target_fleet_id) if row.target_fleet_id is not None else None
        if agent is None and target is None:
            result.warnings.append("a connected agent or fleet you cannot see was left out")
            continue
        if agent is not None and (agent.id in result.kept_agents or agent.id in result.copied_agents):
            continue
        link = await add_connected(
            db,
            fork,
            agent=agent,
            target_fleet=target,
            collaboration_instruction=row.collaboration_instruction,
            context_inclusion=row.context_inclusion,
        )
        link.organization_scope = row.organization_scope
    await db.flush()
    # The documentation, as the fork's version 1 (#9174 slice 5).
    await copy_documentation(db, source, fork)
    return result


def _repoint_handovers(copies: dict[uuid.UUID, Agent]) -> None:
    """Point each copy's handovers to a source member at that member's copy.

    A handover to an agent outside the copied set (a connected agent, a kept
    member) is left as it is: the copy hands over to the same agent the source
    did, as an agent duplicate does.
    """
    by_source = {str(src): str(copy.id) for src, copy in copies.items()}
    for copy in copies.values():
        ids = [str(t) for t in (copy.handover_agent_ids or [])]
        if not any(t in by_source for t in ids):
            continue
        copy.handover_agent_ids = [by_source.get(t, t) for t in ids]
        for name in ("handover_types", "handover_conditions"):
            value = getattr(copy, name)
            if isinstance(value, dict) and value:
                setattr(copy, name, {by_source.get(str(k), str(k)): v for k, v in value.items()})


async def _fork_workspace(
    db: AsyncSession,
    user: User,
    user_group_ids: list[str],
    workspace_id: uuid.UUID | None,
    fleet_name: str,
) -> Workspace:
    """The workspace named by id (managed by the caller), else a new personal one."""
    from app.api.v1.helpers import can_manage
    from app.services.workspaces import create_workspace, lock_workspace_for_hosting

    if workspace_id is not None:
        workspace = await db.get(Workspace, workspace_id)
        if workspace is None or not can_manage(user, workspace, user_group_ids):
            raise FleetActionRefusedError(404, f"Workspace {workspace_id} not found or you cannot manage it.")
        await lock_workspace_for_hosting(db, workspace)
        ensure_workspace_can_host(False, workspace)
        return workspace
    return await create_workspace(
        db,
        user_id=user.id,
        name=fleet_name[:200],
        description=f"Where the fleet {fleet_name} works.",
        is_company_managed=False,
    )


def _uuid(value: object) -> uuid.UUID | None:
    try:
        return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))
    except (TypeError, ValueError):
        return None


__all__ = [
    "FORK_MEMBER_MODES",
    "FleetForkResult",
    "SourceChanges",
    "diff_structures",
    "fleet_structure",
    "fork_fleet",
    "source_changed",
    "source_changes",
    "source_fingerprint",
    "source_structure",
    "structure_fingerprint",
    "structure_snapshot",
]
