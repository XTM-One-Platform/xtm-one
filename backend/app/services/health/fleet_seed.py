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

"""The seeded Platform Health fleet (#8261, PRD 6.1, decision B10).

Five company-managed agents - the Platform Health orchestrator, the Memory Steward,
the Quality Analyst, the Flow Lab and the Experimenter - their assignments, and
a company-managed fleet coordinating in a company-managed workspace.

The fleet is **built in** and read-only for everyone, administrators included,
in every mode (Sam, 19 Nov 2026; ``read_only``): every seed rewrites the whole
definition of each agent (name, description, icon, persona, tools, skills,
handovers, enabled), of each assignment (description, prompt, enabled, caps)
and of the fleet, keyed by slug, so a hand edit never survives a boot. The rows
keep their ids, so enrolments, cycles, findings and history stay with them. A
personal agent holding a fleet slug is still never adopted. The tool groups
that read as the run's user with no consent check
(:data:`NON_CONSENT_TOOL_GROUPS`) are never in the definition.

Every assignment is a ``manual`` one: the cycle runner (``cycles.py``) starts
it, after opening a budget cycle, so no Platform Health run ever starts outside a
cycle (invariant 1: the work is assignments on agents; the schedule lives in
Settings > Agents and automations > Platform Health). The rows keep a real owner (invariant 9), but a run
nobody triggered takes no user: it acts as the Platform Health identity, never as
that owner (``assignment_engine._execution_identity``).

Seeded on every deployment: Platform Health is built in (no feature flag since
slice S1 of #101). Switched off in Settings, nothing is deleted: the cycle
runner starts nothing until an administrator switches it on again.
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
from app.services.assignment_pause import clear_pause_provenance, is_emergency_stopped
from app.services.health.availability import health_enabled

logger = logging.getLogger(__name__)

#: What the cycle runner starts: one fleet-wide schedule per specialist, the
#: per-target investigation, and the weekly digest.
AssignmentKey = Literal["memory", "quality", "flows", "investigation", "digest", "module_analyst"]

#: Tool groups shipped by levels 3 and 8 (ids registered in ``builtin_tool_groups``).
IMPROVEMENTS_GROUP: Final = "improvement_proposals"
ORCHESTRATOR_GROUP: Final = "health_orchestrator"
#: Read traces and platform figures as the acting user, with no consent check:
#: never on a fleet agent (``health_diagnosis`` is the consent-aware read).
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
    #: Slugs the seed gave this agent before (the Concierge's, until S1 of #101):
    #: the next seed adopts the company-managed row still carrying one.
    former_slugs: tuple[str, ...] = ()

    @property
    def slugs(self) -> tuple[str, ...]:
        """The current slug first, then every former one."""
        return (self.slug, *self.former_slugs)


@dataclass(frozen=True)
class FleetAssignmentSpec:
    key: AssignmentKey
    agent_slug: str
    #: The seed's key with the agent. A rename keeps the former name in
    #: ``former_names``: the next seed renames the row still carrying it.
    name: str
    description: str
    prompt: str
    show_in_digest: bool = False
    timeout_seconds: int = 3600
    max_iterations: int = FLEET_MAX_ITERATIONS
    extra: dict = field(default_factory=dict)
    #: Names the seed gave this assignment before (Platform Health, until S1 of #101).
    former_names: tuple[str, ...] = ()

    @property
    def names(self) -> tuple[str, ...]:
        """The current name first, then every former one."""
        return (self.name, *self.former_names)


ORCHESTRATOR_SLUG: Final = "platform-health"
#: The tag every seeded member carries (``concierge`` until S1 of #101).
FLEET_AGENT_TAG: Final = "health"
FORMER_FLEET_AGENT_TAGS: Final = frozenset({"concierge"})

FLEET_AGENTS: Final[tuple[FleetAgentSpec, ...]] = (
    FleetAgentSpec(
        slug=ORCHESTRATOR_SLUG,
        role="orchestrator",
        name="Platform Health",
        description=(
            "Orchestrates the platform's improvement fleet: plans each cycle, hands the work to "
            "its specialists, reports every cycle, and answers administrators about agent health."
        ),
        icon="Compass",
        builtin_tool_ids=(ORCHESTRATOR_GROUP, IMPROVEMENTS_GROUP),
        disable_chat=False,
        handovers=(
            ("health-memory-steward", "delegate"),
            ("health-quality-analyst", "delegate"),
            ("health-flow-lab", "delegate"),
            ("health-experimenter", "delegate"),
        ),
        former_slugs=("concierge",),
    ),
    FleetAgentSpec(
        slug="health-memory-steward",
        former_slugs=("concierge-memory-steward",),
        role="memory_steward",
        name="Memory Steward",
        description="Consolidates agents' memory lanes, reversibly, within the managers' opt-in and users' opt-out.",
        icon="Brain",
        builtin_tool_ids=("memory_steward",),
        skill_slugs=("memory-steward",),
    ),
    FleetAgentSpec(
        slug="health-quality-analyst",
        former_slugs=("concierge-quality-analyst",),
        role="quality_analyst",
        name="Quality Analyst",
        description="Diagnoses agents from traces, ratings and analytics, and drafts the variants worth measuring.",
        icon="Stethoscope",
        builtin_tool_ids=("health_diagnosis", IMPROVEMENTS_GROUP),
        skill_slugs=("health-quality-analyst",),
        handovers=(("health-experimenter", "delegate"),),
    ),
    FleetAgentSpec(
        slug="health-flow-lab",
        former_slugs=("concierge-flow-lab",),
        role="flow_lab",
        name="Flow Lab",
        description="Diagnoses flows and authors repairs, optimizations and new flows on sandbox copies.",
        icon="FlaskConical",
        builtin_tool_ids=("flow_lab", IMPROVEMENTS_GROUP),
        skill_slugs=("health-flow-lab",),
        handovers=(("health-experimenter", "delegate"),),
    ),
    FleetAgentSpec(
        slug="health-experimenter",
        former_slugs=("concierge-experimenter",),
        role="experimenter",
        name="Experimenter",
        description="Measures variants on sandbox copies with evaluations and files the improvement proposals.",
        icon="Scale",
        builtin_tool_ids=("health_experiments", "health_sandbox", "flow_lab", IMPROVEMENTS_GROUP),
        skill_slugs=("health-experimenter",),
    ),
    # The Module Analyst (#101 S14, dev-docs/platform-health.md section 12): starts only
    # from its own triggers (section 12.2), so the orchestrator hands it nothing.
    FleetAgentSpec(
        slug="health-module-analyst",
        role="module_analyst",
        name="Module Analyst",
        description="Investigates each registered module read-only and attaches recommendations to its findings.",
        icon="Radar",
        builtin_tool_ids=("health_modules", IMPROVEMENTS_GROUP),
    ),
)

_REPORT = (
    "End with a short report: what was done, in counts, and every step that was skipped, refused or "
    "stopped by the budget, with its reason. Write it in the language the trigger names (`language`)."
)

FLEET_ASSIGNMENTS: Final[tuple[FleetAssignmentSpec, ...]] = (
    FleetAssignmentSpec(
        key="memory",
        agent_slug="health-memory-steward",
        name="Platform Health memory consolidation",
        former_names=("Concierge memory consolidation",),
        description="Consolidates every memory lane Platform Health may read. Started by its schedule in Settings > Agents and automations > Platform Health.",
        prompt=f"List the memory lanes you may consolidate, then consolidate each one. {_REPORT}",
    ),
    FleetAssignmentSpec(
        key="quality",
        agent_slug="health-quality-analyst",
        name="Platform Health quality review",
        former_names=("Concierge quality review",),
        description="Reviews the quality signals of the enrolled agents. Started by its schedule in Settings > Agents and automations > Platform Health.",
        prompt=(
            "Review the quality signals of the agents you supervise, record the findings the evidence "
            f"supports, and hand the variants worth measuring to the Experimenter. {_REPORT}"
        ),
    ),
    FleetAssignmentSpec(
        key="flows",
        agent_slug="health-flow-lab",
        name="Platform Health flow review",
        former_names=("Concierge flow review",),
        description="Reviews the enrolled flows and assignments. Started by its schedule in Settings > Agents and automations > Platform Health.",
        prompt=(
            "Diagnose the flows and assignments you supervise, author the candidates worth measuring on "
            f"sandbox copies, and hand them to the Experimenter. {_REPORT}"
        ),
    ),
    FleetAssignmentSpec(
        key="investigation",
        agent_slug=ORCHESTRATOR_SLUG,
        name="Platform Health investigation",
        former_names=("Concierge investigation",),
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
        name="Platform Health weekly digest",
        former_names=("Concierge weekly digest",),
        description="Summarizes the Platform Health cycles closed since the last digest. Started by its schedule in Settings > Agents and automations > Platform Health.",
        prompt=(
            "List the Platform Health cycles closed since `since` and summarize them for administrators: "
            "consolidations, findings, experiments, proposals filed, applied and rolled back, budget spent, "
            "and everything skipped with its reason. Use the cycles' own figures and statuses; a cycle that "
            "is not completed is never reported as completed. Write in the language the trigger names (`language`)."
        ),
        show_in_digest=True,
        timeout_seconds=900,
        max_iterations=15,
    ),
    # One cycle per registration (target type ``module``); started by the triggers of
    # section 12.2 (S14a-2b), never by a schedule of its own in ``cycles.SCHEDULE_KEYS``.
    FleetAssignmentSpec(
        key="module_analyst",
        agent_slug="health-module-analyst",
        name="Platform Health module investigation",
        description="Investigates one registered module after its findings changed, daily, or on demand.",
        prompt=(
            "Investigate the registered module the trigger names (`target_id`, a platform registration): read "
            "its open findings, read the module through your read tools, and attach a recommendation to each "
            "finding the evidence explains. Never write to the module. "
            f"{_REPORT}"
        ),
        timeout_seconds=1800,
        max_iterations=30,
    ),
)

ROLE_BY_SLUG: Final[dict[str, str]] = {spec.slug: spec.role for spec in FLEET_AGENTS}
#: Former seed slug -> current slug (the Concierge's agents, until S1 of #101).
RENAMED_FLEET_AGENT_SLUGS: Final[dict[str, str]] = {
    former: spec.slug for spec in FLEET_AGENTS for former in spec.former_slugs
}
ASSIGNMENT_BY_KEY: Final[dict[str, FleetAssignmentSpec]] = {spec.key: spec for spec in FLEET_ASSIGNMENTS}

_FLEET_DESCRIPTION: Final = "The platform's improvement fleet: memory, quality, flows, experiments and modules."


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
        is_builtin=True,
        slug=spec.slug,
        # The orchestrator goes by Platform Health's name (``naming``), every member by its own.
        name=name if spec.slug == ORCHESTRATOR_SLUG else spec.name,
        description=spec.description,
        icon=spec.icon,
        persona=_resolve_seed_content("agent_persona", spec.slug),
        output_format="markdown",
        disable_chat=spec.disable_chat,
        builtin_tool_ids=list(spec.builtin_tool_ids),
        available_channel_types=["web"],
        tags=[FLEET_AGENT_TAG],
    )
    db.add(agent)
    await db.flush()
    await seed_initial_agent_versions(db, agent=agent, author_id=None)
    logger.info("Seeded Platform Health agent: %s", spec.slug)
    return agent


async def _sync_agent(db: AsyncSession, agent: Agent, spec: FleetAgentSpec, name: str) -> None:
    """Rewrite *agent*'s seeded definition in place (same id), a version per changed field.

    The handovers and skills are written by :func:`seed_health_fleet`, which
    knows the other rows' ids.
    """
    from sqlalchemy.orm.attributes import flag_modified

    from app.agents.seed import _resolve_seed_content
    from app.services.agent_config_versioning import record_agent_version_changes, versioned_agent_values

    before = versioned_agent_values(agent)
    agent.is_builtin = True
    agent.enabled = True
    agent.name = name if spec.slug == ORCHESTRATOR_SLUG else spec.name
    agent.description = spec.description
    agent.icon = spec.icon
    agent.persona = _resolve_seed_content("agent_persona", spec.slug)
    agent.output_format = "markdown"
    agent.disable_chat = spec.disable_chat
    agent.builtin_tool_ids = list(spec.builtin_tool_ids)
    agent.available_channel_types = ["web"]
    agent.tags = [FLEET_AGENT_TAG]
    for attr in ("builtin_tool_ids", "available_channel_types", "tags"):
        flag_modified(agent, attr)
    await record_agent_version_changes(
        db, agent=agent, before=before, author_id=None, commit_message="Platform Health seed"
    )


def _adopt_former_slugs(rows: list[Agent]) -> dict[str, Agent]:
    """The fleet's rows by current slug, a row still on a former slug renamed in place.

    The row keeps its id, so its handovers, fleet membership, assignments,
    enrolments and history stay with it: no migration. A row already on the
    current slug wins; a former one beside it is left as it is (logged), never
    deleted. A personal agent is never renamed (the caller skips it).
    """
    by_slug: dict[str, Agent] = {}
    for agent in rows:
        if agent.slug and agent.slug not in RENAMED_FLEET_AGENT_SLUGS:
            by_slug[agent.slug] = agent
    for agent in rows:
        current = RENAMED_FLEET_AGENT_SLUGS.get(agent.slug or "")
        if current is None or not agent.is_company_managed:
            continue
        if current in by_slug:
            logger.warning(
                "Platform Health seed: %r and its former slug %r both exist; the former row is left as is",
                current,
                agent.slug,
            )
            continue
        logger.info("Platform Health seed: agent %s renamed from %r to %r", agent.id, agent.slug, current)
        agent.slug = current
        tags = [tag for tag in (agent.tags or []) if tag not in FORMER_FLEET_AGENT_TAGS]
        agent.tags = tags if FLEET_AGENT_TAG in tags else [*tags, FLEET_AGENT_TAG]
        by_slug[current] = agent
    return by_slug


async def _ensure_assignment(db: AsyncSession, spec: FleetAssignmentSpec, agent: Agent) -> bool:
    rows = (
        (await db.execute(select(Assignment).where(Assignment.agent_id == agent.id, Assignment.name.in_(spec.names))))
        .scalars()
        .all()
    )
    existing = next((row for row in rows if row.name == spec.name), None)
    if existing is None and rows:
        # Seeded under a former name: renamed in place, so its history stays with it.
        existing = rows[0]
        existing.name = spec.name
    if existing is not None:
        # Built in: the seeded definition is rewritten on every boot (the cycle
        # runner, never the row, decides when it runs).
        existing.description = spec.description
        existing.prompt = spec.prompt
        existing.trigger_type = "manual"
        existing.trigger_config = {}
        if is_emergency_stopped(existing):
            # An emergency stop outlives a restart, as for every built-in
            # assignment (agents/seed.py): only the stop's resume re-enables
            # it, a boot never does. The stamp stays so the resume finds it.
            logger.info("Platform Health seed: assignment %s stays paused by an emergency stop", existing.id)
        else:
            if not existing.enabled:
                clear_pause_provenance(existing)
            existing.enabled = True
        existing.show_in_digest = spec.show_in_digest
        existing.timeout_seconds = spec.timeout_seconds
        existing.max_iterations = spec.max_iterations
        existing.is_company_managed = True
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


async def _ensure_fleet(db: AsyncSession, agents: dict[str, Agent], owner_id: uuid.UUID | None, name: str) -> Fleet:
    """The company-managed fleet the orchestrator leads, created once, missing members added."""
    from app.models.workspace import Workspace
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
        logger.info("Seeded the Platform Health fleet")
    # Built in: read-only through the API and the tools (``read_only``), its
    # definition rewritten here; never swept as a stale ``BUILTIN_FLEETS`` entry
    # (``app.agents.fleet_seed._sweep_stale`` skips a fleet led by a fleet role).
    from app.services.health.read_only import HEALTH_FLEET_SLUG

    fleet.is_builtin = True
    fleet.slug = HEALTH_FLEET_SLUG
    fleet.name = name
    fleet.is_archived = False
    workspace_row = await db.get(Workspace, fleet.workspace_id)
    if workspace_row is not None and workspace_row.is_company_managed:
        workspace_row.name = name
    fleet.description = _FLEET_DESCRIPTION
    fleet.icon = "Compass"
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


async def seed_health_fleet(db: AsyncSession) -> int:
    """Create what is missing of the fleet; the number of agents created. Commits.

    Idempotent. Called by ``provision_builtin_content`` after the skill seed, so
    the skills of levels 5-7 exist when they are attached.
    """
    if not health_enabled():
        return 0
    from app.api.v1.helpers import resolve_company_managed_owner_id
    from app.services.health.identity import set_health_role
    from app.services.health.naming import DEFAULT_NAME

    owner_id = await resolve_company_managed_owner_id(db)
    # Built in: the rows carry the platform's name, never a saved or hand-made one.
    name = DEFAULT_NAME
    by_slug = _adopt_former_slugs(
        list(
            (
                await db.execute(
                    select(Agent).where(Agent.slug.in_([slug for spec in FLEET_AGENTS for slug in spec.slugs]))
                )
            ).scalars()
        )
    )
    created = 0
    for spec in FLEET_AGENTS:
        agent = by_slug.get(spec.slug)
        if agent is not None and not agent.is_company_managed:
            # A personal agent holding the slug is never adopted into the
            # fleet (it would act as the platform); it is left alone and the
            # fleet goes without that member until the slug is free.
            logger.warning("Platform Health seed: slug %r is held by a personal agent, member not seeded", spec.slug)
            by_slug.pop(spec.slug)
            continue
        if agent is None:
            agent = by_slug[spec.slug] = await _create_agent(db, spec, owner_id, name)
            created += 1
        else:
            await _sync_agent(db, agent, spec, name)
        if agent.health_role != spec.role:
            await set_health_role(db, agent, spec.role)

    skills = await _skill_ids_by_slug(db, {slug for spec in FLEET_AGENTS for slug in spec.skill_slugs})
    for spec in FLEET_AGENTS:
        agent = by_slug.get(spec.slug)
        if agent is None:
            continue
        wanted = [skills[slug] for slug in spec.skill_slugs if slug in skills]
        if [str(s) for s in (agent.skill_ids or [])] != wanted:
            agent.skill_ids = wanted
        if spec.slug == ORCHESTRATOR_SLUG:
            # Every fleet orchestrator advises on its fleet's health (#11700);
            # the skill list above was reset, so the grant is part of the definition.
            from app.services.fleets import grant_fleet_health_advisory

            await grant_fleet_health_advisory(db, agent)
        targets = [(by_slug[s], kind) for s, kind in spec.handovers if s in by_slug]
        handover_ids = [str(target.id) for target, _ in targets]
        handover_types = {str(target.id): kind for target, kind in targets}
        if list(agent.handover_agent_ids or []) != handover_ids or (agent.handover_types or {}) != handover_types:
            agent.handover_agent_ids = handover_ids
            agent.handover_types = handover_types

    for spec in FLEET_ASSIGNMENTS:
        agent = by_slug.get(spec.agent_slug)
        if agent is not None:
            await _ensure_assignment(db, spec, agent)

    if ORCHESTRATOR_SLUG in by_slug:
        await _ensure_fleet(db, by_slug, owner_id, name)
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
                Agent.health_role == role,
                Agent.is_company_managed.is_(True),
                Assignment.name.in_(spec.names),
            )
            .order_by((Assignment.name == spec.name).desc())
            .limit(1)
        )
    ).first()
    return (row[0], row[1]) if row is not None else None


async def fleet_agent_ids(db: AsyncSession) -> list[uuid.UUID]:
    """The ids of the agents holding a fleet role (the digest's "by Platform Health")."""
    from app.services.health.identity import HEALTH_ROLES

    return list(
        (
            await db.execute(
                select(Agent.id).where(
                    Agent.health_role.in_(sorted(HEALTH_ROLES)), Agent.is_company_managed.is_(True)
                )
            )
        ).scalars()
    )
