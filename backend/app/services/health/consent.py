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

"""THE consent rule of the Platform Health (#8254): what it may read.

    readable = supervision scope ∩ opted-in targets (for content) − opted-out users

PRD ``dev-docs/platform-concierge.md`` 6.2. Every Platform Health read path - trace
tools, memory tools, dataset building, flow run journals - applies this module,
never a re-spelling of it:

* **Metrics** (rollups, counts, costs, ratings without comment text): the
  supervision scope alone, :func:`in_supervision_scope`.
* **Enrolment** (Platform Health S1, ``dev-docs/platform-health.md`` 1.2): the
  supervision scope only ever holds ENROLLED targets, an agent or flow whose
  managers opted it in (``health_enrolled``). ``scope_mode = all``
  no longer means every agent: it means every enrolled one, and ``selected``
  narrows that set further. Nothing is supervised on upgrade but what an owner
  already chose.
* **Content** (trace payloads, memories, conversation excerpts, a flow run's
  journal): the target is in scope AND opted in by its managers
  (``agents.health_enrolled``, ``flows.health_enrolled``),
  AND no user whose lane or conversation it is opted out
  (``users.health_content_opt_out``). :func:`content_readable` is the Python
  predicate, :func:`consent_filter` the same rule as a SQL clause for
  PostgreSQL tables, :class:`ConsentScope` the same rule as sets for a store
  that is not PostgreSQL (ClickHouse traces).
* Nothing is cached. Each call reads the rows, so a revoked opt-in or a new
  opt-out applies to the next read, including one by work already in flight.
  Content is checked again right before it goes to a model
  (:func:`require_content_read`), which also audits it.
* The Platform Health fleet is never in its own scope; nothing is readable while
  Platform Health is off (flag or Settings), which fails closed.
* Content needs a target: a memory or a conversation tied to no agent or flow
  has nobody to have opted it in, and is not readable.

A revocation (an opt-out, a revoked opt-in, a narrowed scope) also removes what
was already derived from the content - dataset entries, memory consolidations,
proposal evidence. Those belong to later levels, which register a handler here
(:func:`register_revocation_handler`, its module listed in
:func:`register_revocation_handlers`); :func:`apply_revocation` runs them in
the transaction that records the revocation.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from typing import Any, Literal

from sqlalchemy import and_, exists, false, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from app.models.agent import Agent
from app.models.flow import Flow
from app.models.user import User
from app.services.health.availability import health_enabled
from app.services.health.settings import HealthSettings, load_health_settings

logger = logging.getLogger(__name__)

TargetType = Literal["agent", "flow"]
ContentKind = Literal["trace", "memory", "dataset_entry", "flow_run"]

#: What a caller is told when a read is refused: never why (the reason is logged).
DENIED_MESSAGE = "This content is not available to Platform Health."


class ConsentDeniedError(Exception):
    """A Platform Health read the consent rule refuses. Its message is generic."""

    def __init__(self) -> None:
        super().__init__(DENIED_MESSAGE)


def _uuid(value: object) -> uuid.UUID | None:
    if value is None or isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError, AttributeError):
        return None


async def _active_settings(db: AsyncSession) -> HealthSettings | None:
    """The settings when Platform Health is on (flag and Settings), else ``None``."""
    if not health_enabled():
        return None
    current = await load_health_settings(db)
    return current if current.enabled else None


# ── The snapshot ──────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ConsentScope:
    """The rule as sets, read once: for a store the SQL filter cannot reach.

    Read it at the start of each read, never keep it across reads.
    ``agent_ids`` / ``flow_ids`` ``None`` = every target but the fleet.
    """

    active: bool
    agent_ids: frozenset[uuid.UUID] | None
    flow_ids: frozenset[uuid.UUID] | None
    fleet_agent_ids: frozenset[uuid.UUID]
    content_agent_ids: frozenset[uuid.UUID]
    content_flow_ids: frozenset[uuid.UUID]
    opted_out_user_ids: frozenset[uuid.UUID]

    def allows_metrics(self, *, agent_id: object = None, flow_id: object = None) -> bool:
        if not self.active:
            return False
        agent, flow = _uuid(agent_id), _uuid(flow_id)
        if agent is None and flow is None:
            return False
        if agent is not None and (
            agent in self.fleet_agent_ids or (self.agent_ids is not None and agent not in self.agent_ids)
        ):
            return False
        return flow is None or self.flow_ids is None or flow in self.flow_ids

    def allows_content(
        self, *, agent_id: object = None, flow_id: object = None, user_ids: Iterable[object] = ()
    ) -> bool:
        if not self.allows_metrics(agent_id=agent_id, flow_id=flow_id):
            return False
        agent, flow = _uuid(agent_id), _uuid(flow_id)
        if agent is not None and agent not in self.content_agent_ids:
            return False
        if flow is not None and flow not in self.content_flow_ids:
            return False
        return not any(_uuid(u) in self.opted_out_user_ids for u in user_ids if u is not None)


_INACTIVE = ConsentScope(
    active=False,
    agent_ids=frozenset(),
    flow_ids=frozenset(),
    fleet_agent_ids=frozenset(),
    content_agent_ids=frozenset(),
    content_flow_ids=frozenset(),
    opted_out_user_ids=frozenset(),
)


async def load_consent_scope(db: AsyncSession) -> ConsentScope:
    """The rule as sets, read now."""
    current = await _active_settings(db)
    if current is None:
        return _INACTIVE
    selected = current.scope_mode == "selected"
    fleet = frozenset((await db.execute(select(Agent.id).where(Agent.health_role.is_not(None)))).scalars())
    agent_q = select(Agent.id).where(Agent.health_enrolled.is_(True), Agent.health_role.is_(None))
    flow_q = select(Flow.id).where(Flow.health_enrolled.is_(True))
    if selected:
        agent_q = agent_q.where(Agent.id.in_(current.scope_agent_ids))
        flow_q = flow_q.where(Flow.id.in_(current.scope_flow_ids))
    # Enrolled targets only, in both modes: the scope is never "every agent".
    enrolled_agents = frozenset((await db.execute(agent_q)).scalars())
    enrolled_flows = frozenset((await db.execute(flow_q)).scalars())
    return ConsentScope(
        active=True,
        agent_ids=enrolled_agents,
        flow_ids=enrolled_flows,
        fleet_agent_ids=fleet,
        content_agent_ids=enrolled_agents,
        content_flow_ids=enrolled_flows,
        opted_out_user_ids=await opted_out_user_ids(db),
    )


async def opted_out_user_ids(db: AsyncSession) -> frozenset[uuid.UUID]:
    """Every user who opted out."""
    return frozenset((await db.execute(select(User.id).where(User.health_content_opt_out.is_(True)))).scalars())


async def content_readable_agent_ids(db: AsyncSession) -> frozenset[uuid.UUID]:
    """The agents whose content Platform Health may read now: in scope and opted in."""
    return (await load_consent_scope(db)).content_agent_ids


# ── The predicate ─────────────────────────────────────────────────────────


async def in_supervision_scope(db: AsyncSession, target_type: TargetType, target_id: object) -> bool:
    """Whether Platform Health may read *target*'s metrics (no content)."""
    current = await _active_settings(db)
    target = _uuid(target_id)
    if current is None or target is None:
        return False
    # Enrolled targets only (module docstring): an agent or flow its managers opted in.
    if target_type == "agent":
        row = (
            await db.execute(select(Agent.health_role, Agent.health_enrolled).where(Agent.id == target))
        ).one_or_none()
        if row is None or row[0] is not None or row[1] is not True:
            return False
        return current.scope_mode == "all" or target in current.scope_agent_ids
    if target_type == "flow":
        opted = (await db.execute(select(Flow.health_enrolled).where(Flow.id == target))).scalar_one_or_none()
        if opted is not True:
            return False
        return current.scope_mode == "all" or target in current.scope_flow_ids
    return False


async def content_readable(
    db: AsyncSession,
    *,
    agent_id: object = None,
    flow_id: object = None,
    user_id: object = None,
    user_ids: Iterable[object] = (),
) -> bool:
    """THE predicate: may Platform Health read this content now?

    *agent_id* / *flow_id*: the target the content belongs to (at least one;
    both when an agent's trace was recorded inside a flow run, and then both
    must allow it). *user_id* / *user_ids*: every user whose lane or
    conversation it is (a ``None`` is a run with no user).
    """
    agent, flow = _uuid(agent_id), _uuid(flow_id)
    if (agent_id is not None and agent is None) or (flow_id is not None and flow is None):
        return False
    if agent is None and flow is None:
        return False
    users = [user_id, *user_ids]
    if any(x is not None and _uuid(x) is None for x in users):
        return False
    current = await _active_settings(db)
    if current is None:
        return False
    selected = current.scope_mode == "selected"
    if agent is not None:
        if selected and agent not in current.scope_agent_ids:
            return False
        row = (
            await db.execute(select(Agent.health_role, Agent.health_enrolled).where(Agent.id == agent))
        ).one_or_none()
        if row is None or row[0] is not None or row[1] is not True:
            return False
    if flow is not None:
        if selected and flow not in current.scope_flow_ids:
            return False
        opted = (await db.execute(select(Flow.health_enrolled).where(Flow.id == flow))).scalar_one_or_none()
        if opted is not True:
            return False
    wanted = {_uuid(x) for x in users if x is not None}
    if not wanted:
        return True
    opted_out = await db.execute(
        select(User.id).where(User.id.in_(wanted), User.health_content_opt_out.is_(True)).limit(1)
    )
    return opted_out.first() is None


# ── The SQL filter ────────────────────────────────────────────────────────


async def consent_filter(
    db: AsyncSession,
    *,
    agent_id_col: Any = None,
    flow_id_col: Any = None,
    user_id_cols: Iterable[Any] = (),
) -> ColumnElement[bool]:
    """The same rule as :func:`content_readable`, as a WHERE clause on a PostgreSQL table.

    *agent_id_col* / *flow_id_col*: the columns naming the content's target
    (UUID columns); a row is readable when at least one is set and every one
    set is allowed. *user_id_cols*: every UUID column naming a user whose
    content it is (a NULL one names nobody). Opt-ins and opt-outs are
    EXISTS / NOT EXISTS on their rows, evaluated by the reading query itself;
    the scope is bound from the settings read now.
    """
    if agent_id_col is None and flow_id_col is None:
        raise ValueError("consent_filter needs the column of the content's target")
    current = await _active_settings(db)
    if current is None:
        return false()
    selected = current.scope_mode == "selected"
    set_targets = []
    checks = []
    if agent_id_col is not None:
        agent_ok = exists(
            select(Agent.id).where(
                Agent.id == agent_id_col,
                Agent.health_enrolled.is_(True),
                Agent.health_role.is_(None),
            )
        )
        if selected:
            agent_ok = and_(agent_id_col.in_(current.scope_agent_ids), agent_ok)
        set_targets.append(agent_id_col.is_not(None))
        checks.append(or_(agent_id_col.is_(None), agent_ok))
    if flow_id_col is not None:
        flow_ok = exists(select(Flow.id).where(Flow.id == flow_id_col, Flow.health_enrolled.is_(True)))
        if selected:
            flow_ok = and_(flow_id_col.in_(current.scope_flow_ids), flow_ok)
        set_targets.append(flow_id_col.is_not(None))
        checks.append(or_(flow_id_col.is_(None), flow_ok))
    for col in user_id_cols:
        checks.append(
            or_(col.is_(None), ~exists(select(User.id).where(User.id == col, User.health_content_opt_out.is_(True))))
        )
    return and_(or_(*set_targets), *checks)


# ── Audit ─────────────────────────────────────────────────────────────────


async def _anchor_user_id(
    db: AsyncSession, *, user_id: uuid.UUID | None, agent_id: uuid.UUID | None, flow_id: uuid.UUID | None
) -> uuid.UUID | None:
    """Whom an audit row is about: the user whose content it is, else the target's owner."""
    if (
        user_id is not None
        and (await db.execute(select(User.id).where(User.id == user_id))).scalar_one_or_none() is not None
    ):
        return user_id
    owner = None
    if agent_id is not None:
        owner = (await db.execute(select(Agent.user_id).where(Agent.id == agent_id))).scalar_one_or_none()
    if owner is None and flow_id is not None:
        owner = (await db.execute(select(Flow.user_id).where(Flow.id == flow_id))).scalar_one_or_none()
    if owner is not None:
        return owner
    from app.api.v1.helpers import resolve_company_managed_owner_id

    return await resolve_company_managed_owner_id(db)


async def audit_content_read(
    db: AsyncSession,
    *,
    kind: ContentKind,
    ref_id: object,
    agent_id: object = None,
    flow_id: object = None,
    user_id: object = None,
) -> None:
    """Record one Platform Health read of content: who (``system:concierge``), what
    (*kind*, *ref_id*), whose (target, user lane). Flushed, not committed.

    Raises :class:`ConsentDeniedError` when the row cannot be written (no user
    to anchor it on): a read that cannot be audited does not happen.
    """
    from app.services.audit import log_system_audit
    from app.services.health.identity import HEALTH_ACTOR, current_health_principal

    agent, flow, user = _uuid(agent_id), _uuid(flow_id), _uuid(user_id)
    anchor = await _anchor_user_id(db, user_id=user, agent_id=agent, flow_id=flow)
    if anchor is None:
        logger.warning("Concierge read of %s %.80r refused: no user to anchor its audit row on", kind, ref_id)
        raise ConsentDeniedError()
    principal = current_health_principal()
    ref = _uuid(ref_id)
    details: dict[str, Any] = {
        "agent_id": str(agent) if agent else None,
        "flow_id": str(flow) if flow else None,
        "user_lane": str(user) if user else None,
        "via_agent_id": str(principal.agent_id) if principal else None,
    }
    if ref is None:
        details["ref"] = str(ref_id)[:200]
    await log_system_audit(
        db,
        actor=HEALTH_ACTOR,
        anchor_user_id=anchor,
        action="concierge_content_read",
        entity_type=kind,
        entity_id=ref,
        summary=f"The Concierge read a {kind.replace('_', ' ')}",
        details=details,
    )


async def require_content_read(
    db: AsyncSession,
    *,
    kind: ContentKind,
    ref_id: object,
    agent_id: object = None,
    flow_id: object = None,
    user_id: object = None,
    user_ids: Iterable[object] = (),
) -> None:
    """Check and audit one content read, right before the content goes to a model.

    Raises :class:`ConsentDeniedError` (generic message) when the rule refuses;
    the reason is logged.
    """
    others = list(user_ids)
    if not await content_readable(db, agent_id=agent_id, flow_id=flow_id, user_id=user_id, user_ids=others):
        logger.info(
            "Concierge read of %s %.80r refused by consent (agent %.80r, flow %.80r)", kind, ref_id, agent_id, flow_id
        )
        raise ConsentDeniedError()
    await audit_content_read(db, kind=kind, ref_id=ref_id, agent_id=agent_id, flow_id=flow_id, user_id=user_id)


# ── Revocation ────────────────────────────────────────────────────────────

RevocationKind = Literal["user_opt_out", "agent_opt_in_revoked", "flow_opt_in_revoked", "scope_narrowed"]


@dataclass(frozen=True, slots=True)
class Revocation:
    """Consent withdrawn: what derived content must go.

    ``scope_narrowed`` carries no id: a handler drops what the scope read now
    (:func:`load_consent_scope`) no longer covers.
    """

    kind: RevocationKind
    user_id: uuid.UUID | None = None
    agent_id: uuid.UUID | None = None
    flow_id: uuid.UUID | None = None


RevocationHandler = Callable[[AsyncSession, Revocation], Awaitable[None]]
_HANDLERS: list[RevocationHandler] = []


def register_revocation_handler(handler: RevocationHandler) -> RevocationHandler:
    """Run *handler* on every revocation, in the revoking transaction. Usable as a decorator."""
    if handler not in _HANDLERS:
        _HANDLERS.append(handler)
    return handler


def register_revocation_handlers() -> None:
    """Import every module that registers a handler. Called at start-up by the
    API and the worker: those modules are otherwise imported lazily, and a
    revocation before the first import would leave their derived content."""
    from app.services.health import datasets, memory_history  # noqa: F401


async def apply_revocation(db: AsyncSession, revocation: Revocation) -> None:
    """Run every handler in the caller's transaction.

    A handler that fails fails the revocation: the caller rolls back and
    reports an error, never a revocation that half applied.
    """
    for handler in list(_HANDLERS):
        await handler(db, revocation)
