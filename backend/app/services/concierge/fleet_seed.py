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

"""The seeded Concierge fleet (#8261, PRD 6.1, decision B10).

Five company-managed agents - the Concierge orchestrator, the Memory Steward,
the Quality Analyst, the Flow Lab and the Experimenter - their assignments, and
a company-managed fleet coordinating in a company-managed workspace.

The agents are **not** built-in: the PRD makes them "editable like any agent
(persona, model, skills)", and built-in agents are read-only in XTM One mode
(``helpers.writable_agent_error``). So the seed creates each one once, keyed by
its slug, and afterwards only keeps what nothing else may write: its fleet
role (``identity.set_concierge_role``, the seed's call alone), its fleet
membership and the skills the levels below ship. A persona, model or tool an
administrator changed stays changed, except the tool groups that read as the
run's user with no consent check (:data:`NON_CONSENT_TOOL_GROUPS`): every seed
takes them off the fleet.

Every assignment is a ``manual`` one: the cycle runner (``cycles.py``) starts
it, after opening a budget cycle, so no Concierge run ever starts outside a
cycle (invariant 1: the work is assignments on agents; the schedule lives in
Settings > Agents and automations > Concierge). The rows keep a real owner (invariant 9), but a run
nobody triggered takes no user: it acts as the Concierge identity, never as
that owner (``assignment_engine._execution_identity``).

Seeded only while the ``CONCIERGE`` flag is on. With the flag off nothing is
created and nothing is deleted: the agents' runs stop acting as the Concierge
(``identity.concierge_principal_for``) and the cycle runner starts nothing.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from typing import Final, Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.agent import Agent
from app.models.assignment import Assignment
from app.models.fleet import Fleet, FleetMember
from app.services.concierge.availability import concierge_enabled

logger = logging.getLogger(__name__)

#: What the cycle runner starts: one fleet-wide schedule per specialist, the
#: per-target investigation, and the weekly digest.
AssignmentKey = Literal["memory", "quality", "flows", "investigation", "digest"]

#: Tool groups shipped by levels 3 and 8 (ids registered in ``builtin_tool_groups``).
IMPROVEMENTS_GROUP: Final = "improvement_proposals"
ORCHESTRATOR_GROUP: Final = "concierge_orchestrator"
#: Read traces and platform figures as the acting user, with no consent check:
#: never on a fleet agent (``concierge_diagnosis`` is the consent-aware read).
NON_CONSENT_TOOL_GROUPS: Final = frozenset(
    {"observability", "platform_insights", "inspect_my_traces", "inspect_agent_traces"}
)
#: The agentic loop's cap on a seeded assignment, never the platform default.
FLEET_MAX_ITERATIONS: Final = 50


@dataclass(frozen=True)
class FleetAgentSpec:
    slug: str
    role: str
    name: str
    description: str
    icon: str
    builtin_tool_ids: tuple[str, ...]
    #: Seeded skills (``BUILTIN_SKILLS`` slugs of levels 5-7) the agent carries.
    skill_slugs: tuple[str, ...] = ()
    disable_chat: bool = True
    #: ``(agent slug, handover type)`` of the members it may hand work to.
    handovers: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class FleetAssignmentSpec:
    key: AssignmentKey
    agent_slug: str
    #: The seed's key with the agent: never rename it.
    name: str
    description: str
    prompt: str
    show_in_digest: bool = False
    timeout_seconds: int = 3600
    max_iterations: int = FLEET_MAX_ITERATIONS
    extra: dict = field(default_factory=dict)


ORCHESTRATOR_SLUG: Final = "concierge"

FLEET_AGENTS: Final[tuple[FleetAgentSpec, ...]] = (
    FleetAgentSpec(
        slug=ORCHESTRATOR_SLUG,
        role="orchestrator",
        name="Concierge",
        description=(
            "Orchestrates the platform's improvement fleet: plans each cycle, hands the work to "
            "its specialists, reports every cycle, and answers administrators about agent health."
        ),
        icon="Compass",
        builtin_tool_ids=(ORCHESTRATOR_GROUP, IMPROVEMENTS_GROUP),
        disable_chat=False,
        handovers=(
            ("concierge-memory-steward", "delegate"),
            ("concierge-quality-analyst", "delegate"),
            ("concierge-flow-lab", "delegate"),
            ("concierge-experimenter", "delegate"),
        ),
    ),
    FleetAgentSpec(
        slug="concierge-memory-steward",
        role="memory_steward",
        name="Memory Steward",
        description="Consolidates agents' memory lanes, reversibly, within the managers' opt-in and users' opt-out.",
        icon="Brain",
        builtin_tool_ids=("memory_steward",),
        skill_slugs=("memory-steward",),
    ),
    FleetAgentSpec(
        slug="concierge-quality-analyst",
        role="quality_analyst",
        name="Quality Analyst",
        description="Diagnoses agents from traces, ratings and analytics, and drafts the variants worth measuring.",
        icon="Stethoscope",
        builtin_tool_ids=("concierge_diagnosis", IMPROVEMENTS_GROUP),
        skill_slugs=("concierge-quality-analyst",),
        handovers=(("concierge-experimenter", "delegate"),),
    ),
    FleetAgentSpec(
        slug="concierge-flow-lab",
        role="flow_lab",
        name="Flow Lab",
        description="Diagnoses flows and authors repairs, optimizations and new flows on sandbox copies.",
        icon="FlaskConical",
        builtin_tool_ids=("flow_lab", IMPROVEMENTS_GROUP),
        skill_slugs=("concierge-flow-lab",),
        handovers=(("concierge-experimenter", "delegate"),),
    ),
    FleetAgentSpec(
        slug="concierge-experimenter",
        role="experimenter",
        name="Experimenter",
        description="Measures variants on sandbox copies with evaluations and files the improvement proposals.",
        icon="Scale",
        builtin_tool_ids=("concierge_experiments", "concierge_sandbox", "flow_lab", IMPROVEMENTS_GROUP),
        skill_slugs=("concierge-experimenter",),
    ),
)

_REPORT = (
    "End with a short report: what was done, in counts, and every step that was skipped, refused or "
    "stopped by the budget, with its reason. Write it in the language the trigger names (`language`)."
)

FLEET_ASSIGNMENTS: Final[tuple[FleetAssignmentSpec, ...]] = (
    FleetAssignmentSpec(
        key="memory",
        agent_slug="concierge-memory-steward",
        name="Concierge memory consolidation",
        description="Consolidates every memory lane the Concierge may read. Started by its schedule in Settings > Agents and automations > Concierge.",
        prompt=f"List the memory lanes you may consolidate, then consolidate each one. {_REPORT}",
    ),
    FleetAssignmentSpec(
        key="quality",
        agent_slug="concierge-quality-analyst",
        name="Concierge quality review",
        description="Reviews the quality signals of the supervised agents. Started by its schedule in Settings > Agents and automations > Concierge.",
        prompt=(
            "Review the quality signals of the agents you supervise, record the findings the evidence "
            f"supports, and hand the variants worth measuring to the Experimenter. {_REPORT}"
        ),
    ),
    FleetAssignmentSpec(
        key="flows",
        agent_slug="concierge-flow-lab",
        name="Concierge flow review",
        description="Reviews the supervised flows and assignments. Started by its schedule in Settings > Agents and automations > Concierge.",
        prompt=(
            "Diagnose the flows and assignments you supervise, author the candidates worth measuring on "
            f"sandbox copies, and hand them to the Experimenter. {_REPORT}"
        ),
    ),
    FleetAssignmentSpec(
        key="investigation",
        agent_slug=ORCHESTRATOR_SLUG,
        name="Concierge investigation",
        description="Investigates one agent or flow after a signal or an on-demand request.",
        prompt=(
            "Investigate the target the trigger names (`target_type`, `target_id`) and, for a signal, the "
            "threshold it crossed (`signal`). Delegate the diagnosis to the Quality Analyst (an agent or an "
            f"assignment) or the Flow Lab (a flow). {_REPORT}"
        ),
    ),
    FleetAssignmentSpec(
        key="digest",
        agent_slug=ORCHESTRATOR_SLUG,
        name="Concierge weekly digest",
        description="Summarizes the Concierge cycles closed since the last digest. Started by its schedule in Settings > Agents and automations > Concierge.",
        prompt=(
            "List the Concierge cycles closed since `since` and summarize them for administrators: "
            "consolidations, findings, experiments, proposals filed, applied and rolled back, budget spent, "
            "and everything skipped with its reason. Use the cycles' own figures and statuses; a cycle that "
            "is not completed is never reported as completed. Write in the language the trigger names (`language`)."
        ),
        show_in_digest=True,
        timeout_seconds=900,
        max_iterations=15,
    ),
)

ROLE_BY_SLUG: Final[dict[str, str]] = {spec.slug: spec.role for spec in FLEET_AGENTS}
ASSIGNMENT_BY_KEY: Final[dict[str, FleetAssignmentSpec]] = {spec.key: spec for spec in FLEET_ASSIGNMENTS}

_FLEET_DESCRIPTION: Final = "The platform's improvement fleet: memory, quality, flows and experiments."


async def _skill_ids_by_slug(db: AsyncSession, slugs: set[str]) -> dict[str, str]:
    """The seeded skills' ids by seed slug (the ``seed:<slug>`` tag the skill seed writes)."""
    if not slugs:
        return {}
    from app.agents.seed import _seed_slug_tag
    from app.models.agent_skill import AgentSkill

    rows = await db.execute(select(AgentSkill.id, AgentSkill.tags).where(AgentSkill.is_company_managed.is_(True)))
    found: dict[str, str] = {}
    for skill_id, tags in rows.all():
        slug = _seed_slug_tag(tags)
        if slug in slugs:
            found[slug] = str(skill_id)
    return found


async def _create_agent(db: AsyncSession, spec: FleetAgentSpec, owner_id: uuid.UUID | None, name: str) -> Agent:
    from app.agents.seed import _resolve_seed_content
    from app.services.agent_config_versioning import seed_initial_agent_versions

    agent = Agent(
        user_id=owner_id,
        is_company_managed=True,
        is_builtin=False,
        slug=spec.slug,
        # The orchestrator goes by the Concierge's name (``naming``), every member by its own.
        name=name if spec.slug == ORCHESTRATOR_SLUG else spec.name,
        description=spec.description,
        icon=spec.icon,
        persona=_resolve_seed_content("agent_persona", spec.slug),
        output_format="markdown",
        disable_chat=spec.disable_chat,
        builtin_tool_ids=list(spec.builtin_tool_ids),
        available_channel_types=["web"],
        tags=["concierge"],
    )
    db.add(agent)
    await db.flush()
    await seed_initial_agent_versions(db, agent=agent, author_id=None)
    logger.info("Seeded Concierge agent: %s", spec.slug)
    return agent


async def _ensure_assignment(db: AsyncSession, spec: FleetAssignmentSpec, agent: Agent) -> bool:
    existing = (
        await db.execute(select(Assignment).where(Assignment.agent_id == agent.id, Assignment.name == spec.name))
    ).scalar_one_or_none()
    if existing is not None:
        # Seeded before the cap existed: it would inherit the platform default.
        if existing.max_iterations is None:
            existing.max_iterations = spec.max_iterations
        return False
    db.add(
        Assignment(
            agent_id=agent.id,
            user_id=agent.user_id,
            is_company_managed=True,
            name=spec.name,
            description=spec.description,
            trigger_type="manual",
            trigger_config={},
            prompt=spec.prompt,
            enabled=True,
            show_in_digest=spec.show_in_digest,
            timeout_seconds=spec.timeout_seconds,
            max_iterations=spec.max_iterations,
            run_as_all_users=False,
        )
    )
    return True


async def _ensure_fleet(
    db: AsyncSession, agents: dict[str, Agent], owner_id: uuid.UUID | None, name: str
) -> Fleet:
    """The company-managed fleet the orchestrator leads, created once, missing members added."""
    from app.services.fleets import add_home_member, create_fleet, home_fleets_of, set_orchestrator
    from app.services.workspaces import create_workspace

    orchestrator = agents[ORCHESTRATOR_SLUG]
    fleet = (
        await db.execute(
            select(Fleet).where(Fleet.orchestrator_agent_id == orchestrator.id, Fleet.is_company_managed.is_(True))
        )
    ).scalar_one_or_none()
    if fleet is None:
        workspace = await create_workspace(
            db, user_id=owner_id, name=name, description=_FLEET_DESCRIPTION, is_company_managed=True
        )
        fleet = await create_fleet(
            db,
            user_id=owner_id,
            name=name,
            workspace_id=workspace.id,
            description=_FLEET_DESCRIPTION,
            icon="Compass",
            is_company_managed=True,
        )
        logger.info("Seeded the Concierge fleet")
    homes = await home_fleets_of(db, [agent.id for agent in agents.values()])
    for agent in agents.values():
        if agent.id not in homes:
            await add_home_member(db, fleet, agent)
    member = (
        await db.execute(
            select(FleetMember.id).where(FleetMember.fleet_id == fleet.id, FleetMember.agent_id == orchestrator.id)
        )
    ).scalar_one_or_none()
    if member is not None and fleet.orchestrator_agent_id != orchestrator.id:
        await set_orchestrator(db, fleet, orchestrator.id)
    return fleet


async def seed_concierge_fleet(db: AsyncSession) -> int:
    """Create what is missing of the fleet; the number of agents created. Commits.

    Idempotent. Called by ``provision_builtin_content`` after the skill seed, so
    the skills of levels 5-7 exist when they are attached.
    """
    if not concierge_enabled():
        return 0
    from app.api.v1.helpers import resolve_company_managed_owner_id
    from app.services.concierge.identity import set_concierge_role
    from app.services.concierge.naming import DEFAULT_NAMES, load_concierge_name, rename_concierge

    owner_id = await resolve_company_managed_owner_id(db)
    name = await load_concierge_name(db)
    by_slug = {
        agent.slug: agent
        for agent in (
            await db.execute(select(Agent).where(Agent.slug.in_([spec.slug for spec in FLEET_AGENTS])))
        ).scalars()
    }
    created = 0
    for spec in FLEET_AGENTS:
        agent = by_slug.get(spec.slug)
        if agent is not None and not agent.is_company_managed:
            # A personal agent holding the slug is never adopted into the
            # fleet (it would act as the platform); it is left alone and the
            # fleet goes without that member until the slug is free.
            logger.warning("Concierge seed: slug %r is held by a personal agent, member not seeded", spec.slug)
            by_slug.pop(spec.slug)
            continue
        if agent is None:
            agent = by_slug[spec.slug] = await _create_agent(db, spec, owner_id, name)
            created += 1
        if agent.concierge_role != spec.role:
            await set_concierge_role(db, agent, spec.role)
        kept = [tool for tool in agent.builtin_tool_ids or [] if tool not in NON_CONSENT_TOOL_GROUPS]
        if kept != list(agent.builtin_tool_ids or []):
            agent.builtin_tool_ids = kept

    skills = await _skill_ids_by_slug(db, {slug for spec in FLEET_AGENTS for slug in spec.skill_slugs})
    for spec in FLEET_AGENTS:
        agent = by_slug.get(spec.slug)
        if agent is None:
            continue
        wanted = [skills[slug] for slug in spec.skill_slugs if slug in skills]
        current = [str(s) for s in (agent.skill_ids or [])]
        missing = [sid for sid in wanted if sid not in current]
        if missing:
            agent.skill_ids = current + missing
        if not agent.handover_agent_ids and spec.handovers:
            targets = [(by_slug[s], kind) for s, kind in spec.handovers if s in by_slug]
            agent.handover_agent_ids = [str(target.id) for target, _ in targets]
            agent.handover_types = {str(target.id): kind for target, kind in targets}

    for spec in FLEET_ASSIGNMENTS:
        agent = by_slug.get(spec.agent_slug)
        if agent is not None:
            await _ensure_assignment(db, spec, agent)

    if ORCHESTRATOR_SLUG in by_slug:
        await _ensure_fleet(db, by_slug, owner_id, name)
        # A deployment that changed mode, or seeded before the Concierge had a
        # name: rows still carrying a default name take this one's.
        await rename_concierge(db, previous=DEFAULT_NAMES, name=name)
    await db.commit()
    return created


async def fleet_assignment(db: AsyncSession, key: AssignmentKey) -> tuple[Assignment, Agent] | None:
    """The seeded assignment *key* and its agent, read by role (never by a name an admin may edit)."""
    spec = ASSIGNMENT_BY_KEY[key]
    role = ROLE_BY_SLUG[spec.agent_slug]
    row = (
        await db.execute(
            select(Assignment, Agent)
            .join(Agent, Agent.id == Assignment.agent_id)
            .where(
                Agent.concierge_role == role,
                Agent.is_company_managed.is_(True),
                Assignment.name == spec.name,
            )
            .limit(1)
        )
    ).first()
    return (row[0], row[1]) if row is not None else None


async def fleet_agent_ids(db: AsyncSession) -> list[uuid.UUID]:
    """The ids of the agents holding a fleet role (the digest's "by the Concierge")."""
    from app.services.concierge.identity import CONCIERGE_ROLES

    return list(
        (
            await db.execute(
                select(Agent.id).where(
                    Agent.concierge_role.in_(sorted(CONCIERGE_ROLES)), Agent.is_company_managed.is_(True)
                )
            )
        ).scalars()
    )
