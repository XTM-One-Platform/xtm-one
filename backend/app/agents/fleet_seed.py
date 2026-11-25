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

"""Seed the built-in fleets of ``BUILTIN_FLEETS`` (#8988, CTEM seeded fleets S1).

The fleet counterpart of :func:`app.agents.seed.seed_builtin_agents`, called by
``provision_builtin_content`` after the agents, assignments and skills it
references:

* only in xtm_one and dev modes, and there only the entries whose
  ``platform_targets`` match (:func:`seed_matches_platform`); copilot mode
  seeds none and sweeps any it finds;
* company-managed, ``is_builtin``, owned by the company-managed owner
  (:func:`resolve_company_managed_owner_id`), upserted by slug on every boot;
  a user's own fleet holding the slug is moved aside to a free one, never
  adopted;
* a company-managed workspace created per fleet, named after it;
* home members resolved by built-in agent slug, with parents, exposure and role
  labels; the orchestrator is the root and always exposed; a member whose
  agent is not seeded (feature-gated, other mode) is left out and its reports
  move up to its nearest seeded ancestor; an agent already home in another
  fleet stays there (logged), never stolen;
* connected agents and the entry handovers (an agent outside the fleet, the
  CTEM Assistant, given a handover to the orchestrator) added when missing;
* stale built-in fleets (slug no longer seeded) deleted: their members go back
  to the playground, the workspace stays, like a REST delete;
* the autonomy level (#10005): a built-in fleet starts at
  :data:`SEEDED_FLEET_AUTONOMY_LEVEL` (``low``: only destructive actions wait).
  An existing one still at the stored default that no person ever moved (no
  ``autonomy_level_change`` audit row) is moved there at the next boot, with a
  ``system:fleet-seed`` audit row; a level a person set is never overwritten.
  The level changes nothing until someone enables the fleet.

The API and the ``fleets`` tools keep a built-in fleet read-only
(:func:`app.services.fleets.ensure_builtin_fleet_writable`); this seed is the
only writer of its definition.
"""

from __future__ import annotations

import logging
import uuid

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.builtin_fleets import SEEDED_BUILTIN_FLEETS
from app.models.agent import Agent
from app.models.audit_log import AuditLog
from app.models.fleet import FLEET_DEFAULT_ICON, FLEET_ROLE_CONNECTED, FLEET_ROLE_MEMBER, Fleet, FleetMember

logger = logging.getLogger(__name__)

#: The modes a built-in fleet can exist in, whatever its ``platform_targets``.
FLEET_SEED_MODES: frozenset[str] = frozenset({"xtm_one", "dev"})

#: The autonomy level a built-in fleet ships at (#10005): ``low``, the last
#: level before full autonomy, where only ``destructive`` gates wait for a
#: person. Custom fleets keep the model default (``full_gates``).
SEEDED_FLEET_AUTONOMY_LEVEL = "low"
#: ``AuditLog.action`` of the seed moving a fleet to its shipped level.
SEED_DEFAULT_AUDIT_ACTION = "autonomy_level_seed_default"
#: ``system:<actor>`` of that row: the seed, never a user.
SEED_AUDIT_ACTOR = "fleet-seed"


def fleets_for_mode(platform_mode: str, fleets: list[dict] | None = None) -> list[dict]:
    """The entries of *fleets* (default ``SEEDED_BUILTIN_FLEETS``) seeded in *platform_mode*."""
    from app.agents.seed import seed_matches_platform

    if platform_mode not in FLEET_SEED_MODES:
        return []
    return [f for f in (SEEDED_BUILTIN_FLEETS if fleets is None else fleets) if seed_matches_platform(f, platform_mode)]


def resolve_parent(agent_slug: str, parents: dict[str, str | None], present: set[str]) -> str | None:
    """The nearest ancestor of *agent_slug* that is seeded (in *present*), else ``None``.

    A loop in the declared parents ends the walk at the top level.
    """
    seen = {agent_slug}
    cursor = parents.get(agent_slug)
    while cursor is not None and cursor not in seen:
        if cursor in present:
            return cursor
        seen.add(cursor)
        cursor = parents.get(cursor)
    return None


async def _sweep_stale(db: AsyncSession, current_slugs: set[str]) -> int:
    # The Platform Health fleet is built in but seeded by its own seed
    # (``services.health.fleet_seed``): led by an agent holding a fleet role.
    health_led = select(Agent.id).where(Agent.health_role.is_not(None))
    stale = (
        (
            await db.execute(
                select(Fleet).where(
                    Fleet.is_builtin.is_(True),
                    Fleet.slug.notin_(current_slugs or {""}),
                    or_(Fleet.orchestrator_agent_id.is_(None), Fleet.orchestrator_agent_id.notin_(health_led)),
                )
            )
        )
        .scalars()
        .all()
    )
    removed = 0
    for fleet in stale:
        if not fleet.is_company_managed:
            # Never the company's to delete: handed back as an ordinary fleet.
            fleet.is_builtin = False
            continue
        logger.info("Removing stale built-in fleet: %s", fleet.slug)
        await db.delete(fleet)
        removed += 1
    if stale:
        await db.flush()
    return removed


async def _retired_pins(db: AsyncSession, entries: list[dict]) -> dict[str, dict]:
    """The approval pins of the retired fleets each entry ``absorbs``, read before the sweep (CTEM-31).

    Pins are a person's choice: the generic fleet that folds a retired fleet in
    keeps them, its own pin winning on the same gate key.
    """
    absorbed = {old: entry["slug"] for entry in entries for old in entry.get("absorbs") or []}
    if not absorbed:
        return {}
    rows = (
        await db.execute(
            select(Fleet.slug, Fleet.approval_pins).where(Fleet.is_builtin.is_(True), Fleet.slug.in_(absorbed))
        )
    ).all()
    carried: dict[str, dict] = {}
    for old_slug, pins in rows:
        if isinstance(pins, dict) and pins:
            carried.setdefault(absorbed[old_slug], {}).update(pins)
    return carried


def _carry_pins(fleet: Fleet, pins: dict | None) -> None:
    """Add the retired fleets' *pins* to *fleet*; a pin it already holds wins."""
    if not pins:
        return
    current = fleet.approval_pins if isinstance(fleet.approval_pins, dict) else {}
    merged = {**pins, **current}
    if merged != current:
        fleet.approval_pins = merged
        logger.info("Built-in fleet %s: kept %d pin(s) of the fleets it absorbs", fleet.slug, len(pins))


async def _upsert_fleet(db: AsyncSession, data: dict, owner_id: uuid.UUID) -> tuple[Fleet, bool]:
    from app.services.fleets import unique_fleet_slug
    from app.services.workspaces import create_workspace

    slug = data["slug"]
    fleet = (
        await db.execute(select(Fleet).where(Fleet.is_builtin.is_(True), Fleet.slug == slug).limit(1))
    ).scalar_one_or_none()
    if fleet is None and data.get("previous_slugs"):
        # CTEM-31: adopt the row seeded under a former slug, so its id, workspace,
        # level, pins and the forks pointing at it carry over.
        fleet = (
            await db.execute(
                select(Fleet).where(Fleet.is_builtin.is_(True), Fleet.slug.in_(list(data["previous_slugs"]))).limit(1)
            )
        ).scalar_one_or_none()
        if fleet is not None:
            logger.info("Built-in fleet '%s' renamed to '%s'", fleet.slug, slug)
            fleet.slug = slug
            await db.flush()
    created = False
    if fleet is None:
        squatter = (
            await db.execute(select(Fleet).where(Fleet.user_id == owner_id, Fleet.slug == slug))
        ).scalar_one_or_none()
        if squatter is not None:
            squatter.slug = await unique_fleet_slug(db, owner_id, slug)
            logger.warning(
                "Built-in fleet slug '%s' was held by fleet %s; moved it to '%s'.", slug, squatter.id, squatter.slug
            )
            await db.flush()
        workspace = await create_workspace(
            db,
            user_id=owner_id,
            name=data["name"],
            description=f"Where the fleet {data['name']} works.",
            is_company_managed=True,
        )
        fleet = Fleet(
            user_id=owner_id,
            slug=slug,
            name=data["name"],
            workspace_id=workspace.id,
            is_company_managed=True,
            is_builtin=True,
        )
        db.add(fleet)
        created = True
        logger.info("Seeded built-in fleet: %s", slug)
    fleet.name = data["name"]
    fleet.description = data.get("description", "")
    fleet.icon = data.get("icon") or FLEET_DEFAULT_ICON
    fleet.is_company_managed = True
    await db.flush()
    return fleet, created


async def _apply_seed_autonomy(db: AsyncSession, fleet: Fleet, owner_id: uuid.UUID) -> bool:
    """Move *fleet* to :data:`SEEDED_FLEET_AUTONOMY_LEVEL` unless a person set its level; whether it moved.

    A person's choice is read from the audit log: any ``autonomy_level_change``
    row on the fleet (the PATCH and the bulk action write one) keeps the stored
    level, whatever it is. Only a fleet still at the model default moves, so a
    second boot changes nothing.
    """
    from app.services.audit import log_system_audit
    from app.services.fleet_autonomy import DEFAULT_AUTONOMY_LEVEL, effective_level

    previous = effective_level(fleet.autonomy_level)
    if previous != DEFAULT_AUTONOMY_LEVEL or previous == SEEDED_FLEET_AUTONOMY_LEVEL:
        return False
    if fleet.id is not None:
        person_set = (
            await db.execute(
                select(AuditLog.id)
                .where(
                    AuditLog.entity_type == "fleet",
                    AuditLog.entity_id == fleet.id,
                    AuditLog.action == "autonomy_level_change",
                )
                .limit(1)
            )
        ).first()
        if person_set is not None:
            return False
    fleet.autonomy_level = SEEDED_FLEET_AUTONOMY_LEVEL
    await db.flush()
    await log_system_audit(
        db,
        actor=SEED_AUDIT_ACTOR,
        anchor_user_id=owner_id,
        action=SEED_DEFAULT_AUDIT_ACTION,
        entity_type="fleet",
        entity_id=fleet.id,
        entity_name=fleet.name,
        summary=f"Seed default: autonomy level of fleet '{fleet.name}' set to {SEEDED_FLEET_AUTONOMY_LEVEL}",
        details={"from": previous, "to": SEEDED_FLEET_AUTONOMY_LEVEL, "reason": "seed default"},
    )
    logger.info("Built-in fleet %s: autonomy level seeded to %s", fleet.slug, SEEDED_FLEET_AUTONOMY_LEVEL)
    return True


async def _agents_by_slug(db: AsyncSession, slugs: set[str]) -> dict[str, Agent]:
    if not slugs:
        return {}
    rows = (
        await db.execute(
            select(Agent).where(Agent.slug.in_(slugs), Agent.is_builtin.is_(True), Agent.is_company_managed.is_(True))
        )
    ).scalars()
    return {a.slug: a for a in rows}


async def _sync_members(db: AsyncSession, fleet: Fleet, data: dict, agents: dict[str, Agent]) -> None:
    members = [m for m in data.get("members") or [] if m.get("agent")]
    orchestrator = data.get("orchestrator")
    present = {m["agent"] for m in members if m["agent"] in agents}
    parents = {m["agent"]: m.get("parent") for m in members}
    if orchestrator:
        parents[orchestrator] = None

    # Agents already home in ANOTHER fleet stay there.
    homes = {
        agent_id: fleet_id
        for agent_id, fleet_id in (
            await db.execute(
                select(FleetMember.agent_id, FleetMember.fleet_id).where(
                    FleetMember.role == FLEET_ROLE_MEMBER,
                    FleetMember.agent_id.in_([agents[s].id for s in present] or [None]),
                )
            )
        ).all()
    }
    for slug in sorted(present):
        if homes.get(agents[slug].id, fleet.id) != fleet.id:
            logger.warning("Built-in fleet '%s': agent '%s' is home in another fleet, left there.", fleet.slug, slug)
            present.discard(slug)
    wanted_ids = {agents[s].id: s for s in present}

    rows = (await db.execute(select(FleetMember).where(FleetMember.fleet_id == fleet.id))).scalars().all()
    by_agent: dict[uuid.UUID, FleetMember] = {}
    for row in rows:
        if row.agent_id in wanted_ids and row.role == FLEET_ROLE_CONNECTED:
            # CTEM-31 (#1048): an agent connected to the adopted row becomes a home
            # member; the row is promoted in place, (fleet, agent) being unique.
            row.role = FLEET_ROLE_MEMBER
            row.collaboration_instruction = None
            by_agent[row.agent_id] = row
        elif row.role == FLEET_ROLE_MEMBER and row.agent_id in wanted_ids:
            by_agent[row.agent_id] = row
        elif row.role == FLEET_ROLE_MEMBER:
            if fleet.orchestrator_agent_id == row.agent_id:
                fleet.orchestrator_agent_id = None
            await db.delete(row)
    await db.flush()

    spec = {m["agent"]: m for m in members}
    for slug in sorted(present):
        agent = agents[slug]
        row = by_agent.get(agent.id)
        if row is None:
            row = by_agent[agent.id] = FleetMember(fleet_id=fleet.id, agent_id=agent.id, role=FLEET_ROLE_MEMBER)
            db.add(row)
        row.role_label = str(spec[slug].get("role_label") or "")
        row.is_exposed = bool(spec[slug].get("exposed", False)) or slug == orchestrator
    await db.flush()
    for slug in present:
        parent = None if slug == orchestrator else resolve_parent(slug, parents, present)
        by_agent[agents[slug].id].parent_member_id = by_agent[agents[parent].id].id if parent else None
    fleet.orchestrator_agent_id = agents[orchestrator].id if orchestrator in present else None
    if orchestrator in present:
        from app.services.fleets import grant_fleet_health_advisory, grant_program_status

        # Every seeded orchestrator reads its programs live (#10979) and advises
        # on its fleet's health (#11700); the agent seed rewrites builtin_tool_ids
        # each boot, so this runs each boot too.
        lead = agents[str(orchestrator)]
        grant_program_status(lead)
        await grant_fleet_health_advisory(db, lead)
    await db.flush()


async def _sync_connected(db: AsyncSession, fleet: Fleet, data: dict, agents: dict[str, Agent]) -> None:
    wanted = {c["agent"]: c for c in data.get("connected") or [] if c.get("agent") in agents}
    home_ids = set(
        (
            await db.execute(
                select(FleetMember.agent_id).where(
                    FleetMember.fleet_id == fleet.id, FleetMember.role == FLEET_ROLE_MEMBER
                )
            )
        ).scalars()
    )
    rows = (
        (
            await db.execute(
                select(FleetMember).where(FleetMember.fleet_id == fleet.id, FleetMember.role == FLEET_ROLE_CONNECTED)
            )
        )
        .scalars()
        .all()
    )
    by_agent = {row.agent_id: row for row in rows if row.agent_id is not None}
    wanted_ids = {agents[s].id for s in wanted}
    for row in rows:
        if row.agent_id not in wanted_ids:
            await db.delete(row)
    for slug, entry in wanted.items():
        agent = agents[slug]
        if agent.id in home_ids:
            continue
        row = by_agent.get(agent.id)
        if row is None:
            row = FleetMember(fleet_id=fleet.id, agent_id=agent.id, role=FLEET_ROLE_CONNECTED)
            db.add(row)
        row.collaboration_instruction = str(entry.get("collaboration_instruction") or "")
    await db.flush()


def _add_entry_handovers(fleet: Fleet, data: dict, agents: dict[str, Agent]) -> None:
    if fleet.orchestrator_agent_id is None:
        return
    target = str(fleet.orchestrator_agent_id)
    for entry in data.get("entry_handovers") or []:
        source = agents.get(entry.get("from") or "")
        if source is None or source.id == fleet.orchestrator_agent_id:
            continue
        ids = [str(i) for i in source.handover_agent_ids or []]
        if target not in ids:
            source.handover_agent_ids = [*ids, target]
        types = dict(source.handover_types or {})
        if target not in types:
            types[target] = entry.get("type") or "delegate"
            source.handover_types = types


async def _add_member_back_consults(db: AsyncSession, fleet: Fleet) -> None:
    """Give each home member of a built-in fleet a Consult handover to its orchestrator (#11948).

    The suggested two-way coordination of the skill "Fleet design and
    operations": a member can ask its orchestrator a route, scope or context
    question. A handover the member already holds to the orchestrator keeps
    its type. The agent seed rewrites built-in handovers each boot, so this
    runs each boot too, after it.
    """
    if fleet.orchestrator_agent_id is None:
        return
    target = str(fleet.orchestrator_agent_id)
    member_ids = (
        await db.execute(
            select(FleetMember.agent_id).where(
                FleetMember.fleet_id == fleet.id,
                FleetMember.role == FLEET_ROLE_MEMBER,
                FleetMember.agent_id != fleet.orchestrator_agent_id,
            )
        )
    ).scalars()
    for agent_id in member_ids:
        source = await db.get(Agent, agent_id)
        if source is None:
            continue
        ids = [str(i) for i in source.handover_agent_ids or []]
        if target not in ids:
            source.handover_agent_ids = [*ids, target]
        types = dict(source.handover_types or {})
        if target not in types:
            types[target] = "consult"
            source.handover_types = types


async def _with_computed_facts(db: AsyncSession, fleet: Fleet, structured: dict | None) -> dict | None:
    """The shipped structured parts with the facts the generator computes on top (#9768 S5).

    The Automations table, the Integrations and tools block and the schedule
    quota of a built-in fleet come from the same code as a regenerated one
    (``fleet_doc_generation.with_computed_facts``), so a duplicated built-in
    fleet regenerated by AI renders the same blocks as the seeded one. A read
    that fails keeps the shipped parts as they are: the seed never stops on it.
    """
    if not structured:
        return structured
    from app.services.fleet_doc_generation import load_draft_context, with_computed_facts

    try:
        # A savepoint: a failed read never aborts the seed's transaction.
        async with db.begin_nested():
            context = await load_draft_context(db, fleet)
    except Exception as exc:  # the shipped parts stay valid without the computed facts
        logger.warning("Built-in fleet %s: computed documentation facts unreadable: %s", fleet.slug, exc)
        return structured
    return with_computed_facts(structured, context)


def _seed_approval_gates(fleet: Fleet, structured: dict | None) -> None:
    """Store the built-in fleet's declared gates as its approval gate catalog (#10000).

    Configuration, written from code on every boot: a documentation regenerate
    never touches it, so the gates the level enforces (their tools included)
    survive a regenerated local version. Validated like the documentation's.
    """
    from app.services.fleet_docs import clean_structured

    gates = (structured or {}).get("gates") or []
    cleaned = (clean_structured({"gates": gates}) or {}).get("gates") or []
    if fleet.approval_gates != cleaned:
        fleet.approval_gates = cleaned


async def seed_builtin_fleets(db: AsyncSession, *, platform_mode: str, fleets: list[dict] | None = None) -> int:
    """Upsert the built-in fleets of *platform_mode*; the number created. Commits.

    *fleets* overrides ``SEEDED_BUILTIN_FLEETS`` (tests). A defined fleet left out
    of the seed (``SEEDED_BUILTIN_FLEET_SLUGS``) is swept like a retired one: its
    members go back to the playground, a person's fork is kept. Idempotent: a second boot
    with the same entries creates and changes nothing.
    """
    from app.agents.builtin_fleet_docs import builtin_fleet_documentation, builtin_fleet_structured
    from app.api.v1.helpers import resolve_company_managed_owner_id
    from app.services.fleet_docs import seed_documentation

    entries = fleets_for_mode(platform_mode, fleets)
    pins_by_slug = await _retired_pins(db, entries)
    # A former slug of a current entry is adopted by _upsert_fleet, never swept.
    kept = {f["slug"] for f in entries} | {s for f in entries for s in f.get("previous_slugs") or []}
    removed = await _sweep_stale(db, kept)
    if removed:
        # The swept fleets' former leads keep no check gated on them (#1400).
        from app.services.fleets import repair_orphaned_fleet_health_checks

        await repair_orphaned_fleet_health_checks(db)
    if not entries:
        await db.commit()
        if removed:
            logger.info("Removed %d stale built-in fleet(s)", removed)
        return 0
    owner_id = await resolve_company_managed_owner_id(db)
    if owner_id is None:
        logger.warning("No company-managed owner yet: built-in fleets not seeded on this boot.")
        await db.commit()
        return 0
    slugs = {
        s
        for f in entries
        for s in (
            [f.get("orchestrator")]
            + [m.get("agent") for m in f.get("members") or []]
            + [c.get("agent") for c in f.get("connected") or []]
            + [h.get("from") for h in f.get("entry_handovers") or []]
        )
        if s
    }
    agents = await _agents_by_slug(db, slugs)
    created = 0
    for data in entries:
        fleet, was_created = await _upsert_fleet(db, data, owner_id)
        created += int(was_created)
        await _apply_seed_autonomy(db, fleet, owner_id)
        # A seeded fleet never pauses a tool call, on any deployment (#1675).
        fleet.enforce_gates_at_tool_level = False
        _carry_pins(fleet, pins_by_slug.get(data["slug"]))
        await _sync_members(db, fleet, data, agents)
        await _sync_connected(db, fleet, data, agents)
        _add_entry_handovers(fleet, data, agents)
        await _add_member_back_consults(db, fleet)
        _seed_approval_gates(fleet, builtin_fleet_structured(data))
        # The CTEM cases it runs (#1966): configuration written from code on every boot.
        ownership = data.get("case_ownership")
        fleet.case_ownership = dict(ownership) if ownership else None
        structured = await _with_computed_facts(db, fleet, builtin_fleet_structured(data))
        await seed_documentation(db, fleet, builtin_fleet_documentation(data), structured)
    await db.commit()
    if removed:
        logger.info("Removed %d stale built-in fleet(s)", removed)
    return created
