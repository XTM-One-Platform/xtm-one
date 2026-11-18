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

"""Who may read what of the Quality Analyst's and the Experimenter's work (#8259).

One rule for every surface - the agent tools, the platform MCP server
(``/mcp/xtm``), later the Improvements inbox - read on the database rows at
every call, never cached across calls:

* **The Concierge** (a call inside a Concierge run: level 1's
  ``identity.current_concierge()``) reads an agent's rollups, counts, costs
  and ratings without comment while the agent is in its supervision scope,
  and its conversation content (trace ids, rating comments, content-derived
  hypotheses) only while the agent is opted in; nothing of an opted-out user.
  That is level 1's consent scope (``services/health/consent.py``); no
  ``agent_tool_admin_bypass``.
* **A person** reads the findings and signals of an agent whose feedback they
  may read (its managers: ``annotations.load_feedback_agent``), and their
  content only if they may also read its chats (``may_read_agent_traces``).
  The users' opt-out applies to them too: what the Concierge derived from an
  opted-out user is dropped whoever reads it.
* **Writes** are the Concierge's only, and each is held by one role read on
  the principal: recording a finding and drafting a variant by the
  ``quality_analyst``, running an experiment and filing its proposal by the
  ``experimenter``.

An agent the caller may not observe answers the same generic refusal whether
it exists or not.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol

from app.core.utils import format_exception_message

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.models.agent import Agent
    from app.models.user import User

logger = logging.getLogger(__name__)

#: The one refusal of an agent out of reach, existing or not.
NOT_IN_REACH = "Error: this agent is not in your reach."
WRITE_REFUSED = "Error: only the Concierge records findings and runs experiments."


class ConsentScope(Protocol):
    """Level 1's ``consent.ConsentScope``: one snapshot of the rows, per call."""

    opted_out_user_ids: frozenset[uuid.UUID]

    def allows_metrics(self, *, agent_id: uuid.UUID | None = None, flow_id: uuid.UUID | None = None) -> bool: ...

    def allows_content(
        self,
        *,
        agent_id: uuid.UUID | None = None,
        flow_id: uuid.UUID | None = None,
        user_ids: Any = (),
    ) -> bool: ...


@dataclass(frozen=True, slots=True)
class AgentAccess:
    #: ``None`` for a target that is no agent's (a flow, through a registered rule).
    agent: Agent | None
    #: Trace ids, rating comments, hypotheses drawn from them (rollups, counts
    #: and ratings without comment are what every granted access reads).
    content: bool


@dataclass(frozen=True, slots=True)
class Reader:
    """The caller of a Concierge tool, resolved once per call."""

    #: The person calling (platform MCP, a chat), or the run's admin; ``None`` in a scheduled Concierge run.
    user: User | None
    #: Level 1's ``HealthPrincipal`` when the call runs as the Concierge.
    principal: Any | None
    scope: ConsentScope
    #: The Concierge cycle the run works in, which books what an experiment spends; ``None`` outside one.
    cycle_id: uuid.UUID | None = None

    @property
    def is_concierge(self) -> bool:
        return self.principal is not None

    @property
    def opted_out_user_ids(self) -> frozenset[uuid.UUID]:
        return frozenset(self.scope.opted_out_user_ids or ())

    def write_refusal(self, role: str) -> str | None:
        """Why this caller may not run a write reserved to the Concierge's *role*, or ``None``."""
        if self.principal is None or getattr(self.principal, "role", None) != role:
            return WRITE_REFUSED
        return None

    async def agent_access(self, db: AsyncSession, agent_id: uuid.UUID) -> AgentAccess | None:
        """What the caller may read of *agent_id*, or ``None`` when it is out of reach."""
        return await _agent_access(db, self, agent_id)


# The seams on level 1, kept in one place so the tests stub them there.


def _current_principal(agent_id: str | None) -> Any | None:
    """The Concierge principal when *agent_id*, the agent calling the tool, is its own.

    A principal stays bound over what its run executes inline (a sandbox
    copy's turn, a consulted agent): those agents read as their runner.
    """
    from app.services.builtin_tools.improvements import calling_concierge

    return calling_concierge(agent_id)


async def _load_scope(db: AsyncSession) -> ConsentScope:
    from app.services.health.consent import load_consent_scope

    return await load_consent_scope(db)


async def _run_cycle(db: AsyncSession, run_id: str | uuid.UUID | None) -> uuid.UUID | None:
    """The Concierge cycle *run_id* works in: the key level 8's cycle runner sets
    on the run's trigger context, read up its delegation chain (level 5's
    ``run_cycle_id``, the one reading); ``None`` outside a cycle."""
    from app.services.health.memory_steward_deps import run_cycle_id

    try:
        run = uuid.UUID(str(run_id)) if run_id else None
    except (ValueError, TypeError):
        return None
    return await run_cycle_id(db, run) if run is not None else None


async def load_reader(
    db: AsyncSession,
    user_id: str | uuid.UUID | None,
    *,
    run_id: str | uuid.UUID | None = None,
    agent_id: str | None = None,
) -> Reader | None:
    """The caller of a Concierge tool, or ``None``: nobody to read for, or the consent rows unreadable.

    *agent_id* is the agent calling the tool: the call is the Concierge's only
    when it is the bound principal's own agent. *run_id* is the assignment run
    the tool is called in: a Concierge run's cycle is read from it, never from
    an argument.
    """
    from app.models.user import User

    principal = _current_principal(agent_id)
    user: User | None = None
    if user_id and user_id != "anonymous":
        try:
            user = await db.get(User, uuid.UUID(str(user_id)))
        except (ValueError, TypeError):
            user = None
    if principal is None and user is None:
        return None
    try:
        scope = await _load_scope(db)
    except Exception as exc:
        # Fail closed: without the consent rows nobody reads Concierge data.
        logger.warning("Concierge consent scope unreadable: %s", format_exception_message(exc))
        return None
    cycle_id = await _run_cycle(db, run_id) if principal is not None else None
    return Reader(user=user, principal=principal, scope=scope, cycle_id=cycle_id)


async def _agent_access(db: AsyncSession, reader: Reader, agent_id: uuid.UUID) -> AgentAccess | None:
    from app.models.agent import Agent

    if reader.is_concierge:
        # No admin bypass: the supervision scope alone (the fleet is never in it).
        if not reader.scope.allows_metrics(agent_id=agent_id):
            return None
        agent = await db.get(Agent, agent_id)
        if agent is None:
            return None
        return AgentAccess(agent=agent, content=bool(reader.scope.allows_content(agent_id=agent_id)))

    if reader.user is None:
        return None
    from fastapi import HTTPException

    from app.services import annotations as annotation_service
    from app.services.builtin_tools.traces import may_read_agent_traces

    try:
        agent = await annotation_service.load_feedback_agent(db, reader.user, str(agent_id))
    except (HTTPException, annotation_service.AgentFeedbackForbiddenError):
        return None
    content = await may_read_agent_traces(db, reader.user, agent)
    return AgentAccess(agent=agent, content=bool(content))


async def agent_of_target(db: AsyncSession, target_type: str, target_id: uuid.UUID) -> uuid.UUID | None:
    """The agent whose access rule a finding's target follows (``None``: gone or not an agent's)."""
    if target_type == "agent":
        return target_id
    if target_type == "assignment":
        from app.models.assignment import Assignment

        assignment = await db.get(Assignment, target_id)
        return assignment.agent_id if assignment is not None else None
    return None


#: The read rule of a finding target that is no agent's: level 7 registers ``flow``.
TargetAccessRule = Callable[["AsyncSession", Reader, uuid.UUID], Awaitable[AgentAccess | None]]
_TARGET_RULES: dict[str, TargetAccessRule] = {}


def register_target_access(target_type: str, rule: TargetAccessRule) -> None:
    """Make findings and experiments on *target_type* readable through *rule* (same rule on every surface)."""
    _TARGET_RULES[target_type] = rule


async def target_access(db: AsyncSession, reader: Reader, target_type: str, target_id: uuid.UUID) -> AgentAccess | None:
    """What *reader* may read of a finding's target, or ``None``: the one gate of every finding read."""
    agent_id = await agent_of_target(db, target_type, target_id)
    if agent_id is not None:
        return await reader.agent_access(db, agent_id)
    rule = _TARGET_RULES.get(target_type)
    return await rule(db, reader, target_id) if rule is not None else None


def redact_finding(payload: dict[str, Any], *, content: bool) -> dict[str, Any]:
    """*payload* (one finding as served) as a reader may see it.

    Without content access a content-derived finding keeps its pattern and
    counts and loses its trace ids and hypothesis (PRD edge case: a revoked
    opt-in redacts the content evidence).
    """
    if content or not payload.get("content_evidence"):
        return payload
    redacted = dict(payload)
    evidence = dict(redacted.get("evidence") or {})
    evidence["trace_ids"] = []
    evidence["rating_ids"] = []
    redacted["evidence"] = evidence
    redacted["hypothesis"] = ""
    redacted["suggested_experiment"] = ""
    redacted["redacted"] = True
    return redacted
