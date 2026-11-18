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

"""What the Memory Steward takes from the lower Concierge levels (#8258).

Identity, consent, audit and budget belong to level 1 (#8254), dry-run to
level 2 (#8255). The Steward reaches them through this module only.
"""

from __future__ import annotations

import logging
import uuid
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from sqlalchemy import select

from app.models.agent import Agent
from app.models.assignment import AssignmentRun
from app.services.health import consent, identity
from app.services.health.budget import BudgetRefusedError, Reservation, charge, release, remaining, reserve
from app.services.health.sandbox import dry_run

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)


class BudgetExhaustedError(Exception):
    """The Concierge may not start another model call (budget, licence, switch)."""


async def memory_lane_readable(db: AsyncSession, agent_id: uuid.UUID, user_id: uuid.UUID | None) -> bool:
    """Level 1's consent rule, on the rows: the agent in the supervision scope
    and opted in, the lane's user (if any) not opted out."""
    return await consent.content_readable(db, agent_id=agent_id, user_id=user_id)


async def readable_scope(db: AsyncSession) -> tuple[frozenset[uuid.UUID], frozenset[uuid.UUID]]:
    """The agents whose content the Concierge may read, and the users who
    opted out: a prefilter in SQL, before ``memory_lane_readable`` per lane."""
    return await consent.content_readable_agent_ids(db), await consent.opted_out_user_ids(db)


#: The key level 8's cycle runner sets on a Concierge run's trigger context
#: (#8261), after ``budget.open_cycle``.
CYCLE_KEY = "concierge_cycle_id"
#: How far up a delegation chain a run's cycle is looked for.
_MAX_PARENT_RUNS = 8


def cycle_id_of(trigger_context: object) -> uuid.UUID | None:
    """The cycle a run's trigger context names; ``None`` when it names none
    or the value is not a UUID."""
    if not isinstance(trigger_context, dict):
        return None
    value = trigger_context.get(CYCLE_KEY)
    if not isinstance(value, str):
        return None
    try:
        return uuid.UUID(value)
    except ValueError:
        return None


async def run_cycle_id(db: AsyncSession, run_id: uuid.UUID | None) -> uuid.UUID | None:
    """The cycle *run_id* works in: its own key, else its root run's (a
    delegated child run takes its parent's)."""
    seen: set[uuid.UUID] = set()
    while run_id is not None and run_id not in seen and len(seen) < _MAX_PARENT_RUNS:
        seen.add(run_id)
        row = (
            await db.execute(
                select(AssignmentRun.trigger_context, AssignmentRun.parent_run_id).where(AssignmentRun.id == run_id)
            )
        ).first()
        if row is None:
            return None
        cycle = cycle_id_of(row.trigger_context)
        if cycle is not None:
            return cycle
        run_id = row.parent_run_id
    return None


async def ensure_budget(db: AsyncSession, cycle_id: uuid.UUID | None) -> None:
    """Raise :class:`BudgetExhaustedError` when the Concierge may spend no more:
    its switch, the month's caps, the cycle's. The pass runs inside a run, so
    it asks what a run in flight asks (``budget.remaining``), never the AI
    gate the cycle passed when it opened."""
    refusal = (await remaining(db, cycle_id)).refusal
    if refusal is not None:
        logger.info("Memory Steward pass not started: %s", refusal)
        raise BudgetExhaustedError(refusal)


async def reserve_model_call(db: AsyncSession, cycle_id: uuid.UUID | None, *, tokens: int) -> Reservation | None:
    """Set the call's tokens aside on the cycle before it starts; no cycle, no
    reservation. Raises :class:`BudgetExhaustedError` when a cap would pass."""
    if cycle_id is None:
        return None
    try:
        return await reserve(db, cycle_id, tokens=tokens, cost_usd=Decimal(0))
    except BudgetRefusedError as e:
        logger.info("Memory Steward model call not started: %s", e)
        raise BudgetExhaustedError(str(e)) from e


async def release_model_call(db: AsyncSession, reservation: Reservation | None) -> None:
    if reservation is not None:
        await release(db, reservation)


async def book_usage(
    db: AsyncSession,
    usage: dict[str, Any] | None,
    *,
    model: str | None,
    cycle_id: uuid.UUID | None,
    reservation: Reservation | None,
) -> None:
    """Charge the pass's model call on its cycle's line, priced by the one
    pricing rule; a model with no price row is charged its tokens alone, a
    call that reports no usage what was reserved for it. No cycle (a run the
    cycle runner did not start): nothing is charged."""
    if cycle_id is None:
        logger.info("Memory Steward model call on %s outside a Concierge cycle: %s", model, usage)
        return
    from app.services.tracing.cost_calculator import input_output_tokens, price_usage, resolve_model_price_any

    tokens = sum(input_output_tokens(usage))
    if tokens == 0 and reservation is not None:
        # No usage reported is not a free call: the estimate stands for it.
        tokens = reservation.tokens
    costs = price_usage(usage, await resolve_model_price_any(db, model)) if model else None
    await charge(
        db,
        cycle_id,
        tokens=tokens,
        cost_usd=costs.total_cost if costs is not None else Decimal(0),
        reservation=reservation,
    )


async def _anchor(db: AsyncSession, agent_id: uuid.UUID, user_id: uuid.UUID | None) -> uuid.UUID | None:
    """Whom an audit row is about: the lane's user, else the agent's owner,
    else the company-managed owner."""
    if user_id is not None:
        return user_id
    owner = (await db.execute(select(Agent.user_id).where(Agent.id == agent_id))).scalar_one_or_none()
    if owner is not None:
        return owner
    from app.api.v1.helpers import resolve_company_managed_owner_id

    return await resolve_company_managed_owner_id(db)


async def ensure_auditable(db: AsyncSession, *, agent_id: uuid.UUID, user_id: uuid.UUID | None) -> None:
    """Raise ``consent.ConsentDeniedError`` when a write on the lane could not
    be audited: then it does not happen."""
    if await _anchor(db, agent_id, user_id) is None:
        logger.warning("Memory Steward write on agent %s refused: no user to anchor its audit on", agent_id)
        raise consent.ConsentDeniedError()


async def audit_lane_read(db: AsyncSession, *, agent_id: uuid.UUID, user_id: uuid.UUID | None) -> None:
    """Audit the pass's read of the lane's content; raises
    ``consent.ConsentDeniedError`` when it cannot be audited (then no read)."""
    await consent.audit_content_read(
        db, kind="memory", ref_id=f"{agent_id}:{user_id or 'agent'}", agent_id=agent_id, user_id=user_id
    )


async def audit(
    db: AsyncSession,
    *,
    action: str,
    agent_id: uuid.UUID,
    user_id: uuid.UUID | None,
    summary: str,
    details: dict[str, Any],
) -> None:
    """Audit one applied operation as ``system:concierge``. *details* holds
    ids and counts, never memory content."""
    from app.services.audit import log_system_audit

    anchor = await _anchor(db, agent_id, user_id)
    if anchor is None:
        # ``ensure_auditable`` ran before the write; raising rolls it back.
        raise consent.ConsentDeniedError()
    await log_system_audit(
        db,
        actor=identity.CONCIERGE_ACTOR,
        anchor_user_id=anchor,
        action=action,
        entity_type="memory_consolidation",
        entity_id=details.get("consolidation_id"),
        summary=summary,
        details={"agent_id": str(agent_id), "user_lane": str(user_id) if user_id else None, **details},
    )


async def dry_run_active(db: AsyncSession) -> bool:
    return await dry_run.dry_run_active(db)


async def record_intercepted_write(
    db: AsyncSession, *, agent_id: uuid.UUID, user_id: uuid.UUID | None, operation: str, details: dict[str, Any]
) -> None:
    """Record a write dry-run kept from happening (level 2), ids only."""
    await dry_run.record_dry_run_write(
        db,
        action=f"memory_{operation}",
        target_type="memory_lane",
        target_id=agent_id,
        details={"user_lane": str(user_id) if user_id else None, **details},
        anchor_user_id=await _anchor(db, agent_id, user_id),
    )
