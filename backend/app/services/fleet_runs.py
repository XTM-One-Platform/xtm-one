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

"""What a fleet means to a run: defaults, trace attribution, cost.

Fleets phase 6 (decisions of 31 Oct 2026). The HOME fleet of an agent
(``FleetMember.role == "member"``, at most one per agent) is the only fleet a
run answers to:

* **Run defaults.** ``Fleet.budget_config`` (the agent Budget tab's partial
  override shape) and ``Fleet.max_iterations`` sit between the agent and the
  platform: the agent's own value wins, per budget dimension, then the
  fleet's, then the platform's. Both are written under the agents API's
  authority rule (the platform ceiling binds a non-admin), and the platform
  ceilings still clamp at run time, so a fleet default can only ever narrow
  what the platform allows. A flow's Budget node is still bounded by the
  result (``budget_policy.bound_by``). A connected member is not billed to,
  nor bounded by, the fleet that consumes it: it runs on its own home
  fleet's defaults.
* **Trace attribution.** Every chat and assignment run trace carries
  ``fleet_id``, the running agent's home fleet. A run another agent caused
  (a handover, a consult, a delegated task) whose caller's home fleet is a
  different one also carries ``caller_fleet_id`` and ``target_fleet_id``, so
  cross-fleet work is filterable in the Trace Explorer and Langfuse.
* **Cost by fleet.** :func:`fleet_cost` sums the traces stamped with a
  ``fleet_id``: the cost of a connected member's run lands on its home fleet,
  never on the consumer's.

An agent in no fleet (the playground) resolves exactly as before: no
default, no ``fleet_id`` key.

A BUILT-IN fleet counts as a home fleet only while it is active: its
orchestrator holds at least one enabled assignment (#9160). Seeded fleets
are opt-in, so until an administrator enables one of its automations its
members (the CTEM dashboard analysts among them) run exactly as playground
agents: no organization block (``organization_context.fleet_run_context``),
no fleet run defaults, no fleet trace keys.

The built-in agents of 1.261102.0 folded into a seeded fleet as members
(``fleet_opt_in.PRE_EXISTING_FLEET_MEMBERS``) keep their behaviour once the
fleet is on (#9188): never the fleet's budget or iteration defaults, and the
organization block only on a run the fleet started (a run its own members
caused, the orchestrator's handover among them), never on a run of the
allowlisted dashboard and decay automations. Their traces still carry the
fleet keys.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import exists, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.utils import format_exception_message
from app.models.fleet import FLEET_ROLE_MEMBER, Fleet, FleetMember
from app.services.budget_policy import BudgetOverrides, merge_overrides, parse_overrides

logger = logging.getLogger(__name__)

#: Trace metadata keys this module writes; the Trace Explorer filters on them.
FLEET_ID_KEY = "fleet_id"
CALLER_FLEET_ID_KEY = "caller_fleet_id"
TARGET_FLEET_ID_KEY = "target_fleet_id"


@dataclass(frozen=True)
class FleetRunDefaults:
    """The home fleet of one agent and the run defaults it carries."""

    fleet_id: uuid.UUID | None = None
    budget_config: dict | None = None
    max_iterations: int | None = None
    #: The member's effective organization scope (its row's, else the fleet's;
    #: ``services.fleets.effective_member_settings``). ``None`` reads the primary.
    organization_scope: dict | None = None


NO_FLEET = FleetRunDefaults()


def _as_uuid(value: object) -> uuid.UUID | None:
    if value is None or value == "":
        return None
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError):
        return None


def builtin_fleet_active_clause() -> Any:
    """SQL: the built-in :class:`Fleet` of the outer query has an enabled automation on a lead.

    A lead is its orchestrator, or a home member whose slug a seeded fleet
    declares in ``leads`` (CTEM-31): the generic CTEM loop stays active when an
    automation of a fleet it folded in (posture, a module desk) is switched on.
    """
    from app.agents.builtin_fleets import BUILTIN_FLEET_LEAD_SLUGS
    from app.models.agent import Agent
    from app.models.assignment import Assignment

    lead_members = (
        select(FleetMember.agent_id)
        .join(Agent, Agent.id == FleetMember.agent_id)
        .where(
            FleetMember.fleet_id == Fleet.id,
            FleetMember.role == FLEET_ROLE_MEMBER,
            Agent.slug.in_(sorted(BUILTIN_FLEET_LEAD_SLUGS)),
        )
    )
    return exists().where(
        Assignment.enabled.is_(True),
        or_(Assignment.agent_id == Fleet.orchestrator_agent_id, Assignment.agent_id.in_(lead_members)),
    )


async def home_fleet_defaults_of(
    db: AsyncSession, agent_ids: Iterable[object]
) -> dict[uuid.UUID, FleetRunDefaults]:
    """The home fleet and its run defaults of each agent that has one, in one query.

    A built-in fleet with no enabled assignment on its orchestrator is no
    home fleet (opt-in, see the module docstring).
    """
    ids = list({u for u in (_as_uuid(a) for a in agent_ids) if u is not None})
    if not ids:
        return {}
    builtin_active = builtin_fleet_active_clause()
    rows = (
        await db.execute(
            select(
                FleetMember.agent_id,
                Fleet.id,
                Fleet.budget_config,
                Fleet.max_iterations,
                Fleet.organization_scope,
                FleetMember.organization_scope,
            )
            .join(Fleet, Fleet.id == FleetMember.fleet_id)
            .where(
                FleetMember.role == FLEET_ROLE_MEMBER,
                FleetMember.agent_id.in_(ids),
                or_(Fleet.is_builtin.is_(False), builtin_active),
            )
        )
    ).all()
    from app.services.fleets import resolve_organization_scope

    return {
        agent_id: FleetRunDefaults(
            fleet_id=fleet_id,
            budget_config=budget_config,
            max_iterations=max_iterations,
            organization_scope=resolve_organization_scope(fleet_scope, member_scope),
        )
        for agent_id, fleet_id, budget_config, max_iterations, fleet_scope, member_scope in rows
    }


def home_fleet_id_subquery(agent_id: Any) -> Any:
    """The home fleet id of *agent_id* (a column or a scalar subquery) as a scalar subquery.

    The rule of :func:`home_fleet_defaults_of` in SQL, for a writer that stamps
    the fleet in the same statement (``roi_chat_records.record_deliverable``, #12077).
    """
    builtin_active = builtin_fleet_active_clause()
    return (
        select(FleetMember.fleet_id)
        .join(Fleet, Fleet.id == FleetMember.fleet_id)
        .where(
            FleetMember.role == FLEET_ROLE_MEMBER,
            FleetMember.agent_id == agent_id,
            or_(Fleet.is_builtin.is_(False), builtin_active),
        )
        .limit(1)
        .scalar_subquery()
    )


def active_handover_target_clause() -> Any:
    """SQL filter on ``Agent``: false for the orchestrator of an inactive built-in fleet.

    The seed gives the CTEM Assistant a handover to every built-in fleet's
    orchestrator (``entry_handovers``); while that fleet has no enabled
    assignment the handover is not offered, so the Assistant's chat
    behaves as before the fleets existed (#9160).
    """
    from app.models.agent import Agent
    from app.models.assignment import Assignment

    enabled = exists().where(Assignment.agent_id == Agent.id, Assignment.enabled.is_(True))
    builtin_orchestrator = exists().where(
        Fleet.orchestrator_agent_id == Agent.id, Fleet.is_builtin.is_(True)
    )
    return or_(~builtin_orchestrator, enabled)


async def home_fleet_defaults(db: AsyncSession, agent_id: object) -> FleetRunDefaults:
    """The home fleet of one agent, :data:`NO_FLEET` when it has none or the read fails.

    A failed read never fails the run: it runs as a playground agent would.
    """
    agent_uuid = _as_uuid(agent_id)
    if agent_uuid is None:
        return NO_FLEET
    try:
        return (await home_fleet_defaults_of(db, [agent_uuid])).get(agent_uuid, NO_FLEET)
    except Exception as exc:  # attribution and defaults are best effort
        logger.warning("Home fleet of agent %s unreadable: %s", agent_uuid, format_exception_message(exc))
        return NO_FLEET


def is_pre_existing_fleet_member(agent: Any) -> bool:
    """Whether *agent* is a built-in agent of 1.261102.0 a seeded fleet folded in as a member."""
    from app.agents.fleet_opt_in import PRE_EXISTING_FLEET_MEMBERS

    return bool(getattr(agent, "is_builtin", False)) and getattr(agent, "slug", None) in PRE_EXISTING_FLEET_MEMBERS


async def fleet_run_defaults(db: AsyncSession, agent: Any) -> FleetRunDefaults:
    """The home fleet whose budget and iteration defaults a run of *agent* takes.

    :data:`NO_FLEET` for a pre-existing member: its runs keep the budget and
    iteration cap of 1.261102.0 whatever the fleet state.
    """
    if is_pre_existing_fleet_member(agent):
        return NO_FLEET
    return await home_fleet_defaults(db, getattr(agent, "id", None))


async def assignment_fleet_context(
    db: AsyncSession, agent: Any, assignment: Any, caller_agent_id: object | None = None
) -> FleetRunDefaults:
    """The home fleet whose organization block an assignment run of *agent* carries.

    :data:`NO_FLEET` on a run of the 1.261102.0 allowlisted automations, and
    on a run of a pre-existing member the fleet did not start: its caller
    (*caller_agent_id*, the agent of the parent run) is not in the same fleet.
    """
    from app.agents.fleet_opt_in import ENABLED_BY_DEFAULT_ALLOWLIST

    if (
        bool(getattr(agent, "is_builtin", False))
        and (getattr(agent, "slug", None), getattr(assignment, "name", None)) in ENABLED_BY_DEFAULT_ALLOWLIST
    ):
        return NO_FLEET
    home = await home_fleet_defaults(db, getattr(agent, "id", None))
    if home.fleet_id is None or not is_pre_existing_fleet_member(agent):
        return home
    caller = await home_fleet_defaults(db, caller_agent_id) if caller_agent_id is not None else NO_FLEET
    return home if caller.fleet_id == home.fleet_id else NO_FLEET


def member_budget_overrides(agent_budget_config: Any, fleet: FleetRunDefaults | None) -> BudgetOverrides:
    """The agent's own budget, its unset dimensions filled from its home fleet's."""
    own = parse_overrides(agent_budget_config)
    if fleet is None or not fleet.budget_config:
        return own
    return merge_overrides(own, parse_overrides(fleet.budget_config))


def member_max_iterations(agent_max_iterations: int | None, fleet: FleetRunDefaults | None) -> int | None:
    """The agent's own iteration cap, else its home fleet's, else ``None`` (the platform's)."""
    if agent_max_iterations is not None:
        return agent_max_iterations
    return fleet.max_iterations if fleet is not None else None


def fleet_trace_metadata(target: FleetRunDefaults | None, caller: FleetRunDefaults | None = None) -> dict[str, str]:
    """The fleet keys of a run trace: ``fleet_id``, plus the pair when it crosses fleets.

    ``target`` is the running agent's home fleet, ``caller`` the home fleet of
    the agent that caused the run (``None`` for a run nobody delegated). The
    pair is written only when both sides have a home fleet and they differ: a
    call from the playground into a fleet, or within one fleet, is not a
    cross-fleet run.
    """
    out: dict[str, str] = {}
    target_id = target.fleet_id if target is not None else None
    if target_id is not None:
        out[FLEET_ID_KEY] = str(target_id)
    caller_id = caller.fleet_id if caller is not None else None
    if target_id is not None and caller_id is not None and caller_id != target_id:
        out[CALLER_FLEET_ID_KEY] = str(caller_id)
        out[TARGET_FLEET_ID_KEY] = str(target_id)
    return out


async def run_fleet_trace_metadata(
    db: AsyncSession,
    agent_id: object,
    caller_agent_id: object | None = None,
    *,
    target: FleetRunDefaults | None = None,
) -> dict[str, str]:
    """:func:`fleet_trace_metadata` for a run of ``agent_id`` caused by ``caller_agent_id``.

    ``target`` skips the read when the caller already resolved the agent's
    home fleet (the run defaults are read from the same row). Best effort: a
    failed read stamps nothing.
    """
    try:
        caller_uuid = _as_uuid(caller_agent_id)
        agent_uuid = _as_uuid(agent_id)
        wanted = [a for a in (None if target is not None else agent_uuid, caller_uuid) if a is not None]
        found = await home_fleet_defaults_of(db, wanted) if wanted else {}
        if target is None:
            target = found.get(agent_uuid, NO_FLEET) if agent_uuid is not None else NO_FLEET
        caller = found.get(caller_uuid) if caller_uuid is not None and caller_uuid != agent_uuid else None
        return fleet_trace_metadata(target, caller)
    except Exception as exc:  # attribution is best effort
        logger.warning("Fleet trace metadata of agent %s unreadable: %s", agent_id, format_exception_message(exc))
        return {}


async def parent_run_agent_id(db: AsyncSession, parent_run_id: object | None) -> uuid.UUID | None:
    """The agent of the run that delegated this one, ``None`` for a run nobody delegated."""
    run_uuid = _as_uuid(parent_run_id)
    if run_uuid is None:
        return None
    from app.models.assignment import AssignmentRun

    try:
        return (
            await db.execute(select(AssignmentRun.agent_id).where(AssignmentRun.id == run_uuid))
        ).scalar_one_or_none()
    except Exception as exc:  # attribution is best effort
        logger.warning("Agent of parent run %s unreadable: %s", run_uuid, format_exception_message(exc))
        return None


def chat_caller_agent_id(visited_agents: list[str] | None) -> str | None:
    """The agent that handed this chat turn over: the chain's last ancestor, if any."""
    chain = list(visited_agents or [])
    return chain[-2] if len(chain) >= 2 else None


@dataclass(frozen=True)
class FleetAgentCost:
    agent_id: uuid.UUID | None
    agent_name: str
    trace_count: int
    total_cost: float
    total_tokens: int


async def fleet_cost(db: AsyncSession, fleet_id: uuid.UUID, *, since: datetime) -> list[FleetAgentCost]:
    """The cost of the runs attributed to ``fleet_id`` since ``since``, by agent, costliest first.

    Read from the local trace store (``llm_traces``): an install on another
    tracing store reads an empty list.
    """
    from app.models.llm_trace import LLMTrace

    fleet_key = LLMTrace.metadata_json[FLEET_ID_KEY].astext
    rows = (
        await db.execute(
            select(
                LLMTrace.agent_id,
                func.max(LLMTrace.agent_name_snapshot),
                func.count(LLMTrace.id),
                func.coalesce(func.sum(LLMTrace.total_cost), 0),
                func.coalesce(func.sum(LLMTrace.total_tokens), 0),
            )
            .where(fleet_key == str(fleet_id), LLMTrace.started_at >= since)
            .group_by(LLMTrace.agent_id)
        )
    ).all()
    costs = [
        FleetAgentCost(
            agent_id=agent_id,
            agent_name=name or "",
            trace_count=int(count or 0),
            total_cost=float(cost or 0),
            total_tokens=int(tokens or 0),
        )
        for agent_id, name, count, cost, tokens in rows
    ]
    return sorted(costs, key=lambda c: (-c.total_cost, c.agent_name))
