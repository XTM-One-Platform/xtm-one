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

"""Shadow runs: a sandbox flow measured on an assignment's live triggers (#8260, B12).

A shadow (``models/flow_shadow.py``) pairs every live run of an assignment
with a flow run on the same event, in level 2's intercepted mode: the flow's
write nodes are never executed, their answers simulated. When the shadow ends:

* a ``measure`` shadow reports its verdict to the Experimenter and stops;
* a ``switch_over`` shadow (an approved ``agent_to_flow`` proposal) that
  *held* hands the trigger to the flow, then is watched
  (``flow_switch_over``).

The verdict is a pure function of counted figures (``settle_decision``), so it
is tested on fixed numbers; the scheduler step ``flow_shadow_tick`` only feeds
it. A shadow that could not gather enough pairs is ``incomplete``, never
``held``.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import exists, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.utils import format_exception_message
from app.models.assignment import Assignment, AssignmentRun
from app.models.flow import Flow
from app.models.flow_run import FlowRun
from app.models.flow_shadow import (
    PAIR_STATUS_COMPARED,
    PAIR_STATUS_INCOMPLETE,
    PAIR_STATUS_PENDING,
    PAIR_STATUS_SKIPPED,
    SHADOW_OPEN_STATUSES,
    SHADOW_PURPOSE_SWITCH_OVER,
    SHADOW_STATUS_CANCELLED,
    SHADOW_STATUS_HELD,
    SHADOW_STATUS_INCOMPLETE,
    SHADOW_STATUS_KEPT,
    SHADOW_STATUS_NOT_HELD,
    SHADOW_STATUS_RUNNING,
    SHADOW_STATUS_WATCHING,
    FlowShadow,
    FlowShadowPair,
)
from app.services.concierge.flow_comparison import RunComparison, compare_sides, run_side
from app.services.concierge.flow_lab import flow_trigger_for_assignment_run, trigger_takeover_refusal

logger = logging.getLogger(__name__)

DEFAULT_SHADOW_DAYS = 3
DEFAULT_WATCH_DAYS = 7
DEFAULT_MIN_PAIRS = 5

#: A shadow holds when the flow agrees with the assignment on this share of
#: the pairs where both completed...
MIN_AGREEMENT_SHARE = 0.8
#: ...and its success rate is not below the assignment's minus this margin.
SUCCESS_MARGIN = 0.05

#: A shadow that has not compared enough pairs when it ends keeps going (a
#: daily assignment gives three runs in three days), at most this long after
#: it started; then it is incomplete.
SHADOW_MAX_DAYS = 30

#: How long a shadow's copy is kept past its watch window.
COPY_RETENTION_MARGIN = timedelta(days=1)

#: Bounded work per scheduler tick, so one busy assignment cannot starve the others.
PAIRS_PER_TICK = 20
SHADOWS_PER_TICK = 50

#: At most this many live runs are paired per shadow (or twice the pairs its
#: verdict needs, when more): a busy assignment cannot spend without bound.
MAX_PAIRS_PER_SHADOW = 50
#: The budget work kind a shadow run's reservation is held under (its pair's id).
SHADOW_WORK_KIND = "flow_shadow_pair"

#: Tokens reserved for a shadow run whose live run recorded none (yet).
DEFAULT_RUN_TOKENS = 20_000
#: A shadow's cycle is rotated within the stale-cycle sweep's 24 hours.
SHADOW_CYCLE_SPAN = timedelta(hours=12)


@dataclass(frozen=True, slots=True)
class ShadowDurations:
    shadow: timedelta
    watch: timedelta
    min_pairs: int


async def shadow_durations(db: AsyncSession) -> ShadowDurations:
    """The shadow and watch durations: Settings > Agents and automations > Concierge, else B18's defaults."""
    shadow_days, watch_days, min_pairs = DEFAULT_SHADOW_DAYS, DEFAULT_WATCH_DAYS, DEFAULT_MIN_PAIRS
    try:
        from app.services.concierge.settings import load_concierge_settings

        settings = await load_concierge_settings(db)
        shadow_days = int(getattr(settings, "flow_shadow_days", shadow_days) or shadow_days)
        watch_days = int(getattr(settings, "flow_watch_days", watch_days) or watch_days)
        min_pairs = int(getattr(settings, "flow_shadow_min_pairs", min_pairs) or min_pairs)
    except ImportError:
        pass
    return ShadowDurations(
        shadow=timedelta(days=max(1, min(shadow_days, 30))),
        watch=timedelta(days=max(1, min(watch_days, 60))),
        min_pairs=max(1, min(min_pairs, 100)),
    )


# ── Pure decisions ─────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class ShadowVerdict:
    status: str  # held | not_held | incomplete
    pairs: int
    compared: int
    incomplete: int
    agreement_share: float | None
    assignment_success_rate: float | None
    flow_success_rate: float | None
    simulated_writes: int
    note: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "pairs": self.pairs,
            "compared": self.compared,
            "incomplete": self.incomplete,
            "agreement_share": self.agreement_share,
            "assignment_success_rate": self.assignment_success_rate,
            "flow_success_rate": self.flow_success_rate,
            "simulated_writes": self.simulated_writes,
        }


def _pct(value: float | None) -> str:
    return "n/a" if value is None else f"{round(value * 100)} %"


def settle_decision(comparisons: list[dict], *, incomplete: int, min_pairs: int) -> ShadowVerdict:
    """Whether a shadow held, from its compared pairs (``RunComparison.as_dict``).

    *comparisons* are the pairs whose two runs ended, ``left`` the assignment
    run and ``right`` the flow run; *incomplete* counts the pairs that never
    got a comparable flow run. Fewer than *min_pairs* comparisons is
    ``incomplete``: too little to say anything, and never shown as held.
    """
    compared = len(comparisons)
    simulated = sum(
        int((c.get(side) or {}).get("simulated_writes") or 0) for c in comparisons for side in ("left", "right")
    )
    if compared < min_pairs:
        return ShadowVerdict(
            SHADOW_STATUS_INCOMPLETE,
            compared + incomplete,
            compared,
            incomplete,
            None,
            None,
            None,
            simulated,
            f"Only {compared} comparable pairs, {min_pairs} needed: no verdict.",
        )

    def succeeded(c: dict, side: str) -> bool:
        return (c.get(side) or {}).get("status") == "completed"

    assignment_rate = sum(succeeded(c, "left") for c in comparisons) / compared
    flow_rate = sum(succeeded(c, "right") for c in comparisons) / compared
    both = [c for c in comparisons if succeeded(c, "left") and succeeded(c, "right")]
    agreement = (sum(1 for c in both if c.get("verdict") == "agree") / len(both)) if both else None
    reasons = []
    if flow_rate < assignment_rate - SUCCESS_MARGIN:
        reasons.append(f"the flow completed {_pct(flow_rate)} of the runs, the assignment {_pct(assignment_rate)}")
    if agreement is None or agreement < MIN_AGREEMENT_SHARE:
        reasons.append(
            f"the outputs agreed on {_pct(agreement)} of the runs both completed, "
            f"{_pct(MIN_AGREEMENT_SHARE)} needed"
        )
    status = SHADOW_STATUS_NOT_HELD if reasons else SHADOW_STATUS_HELD
    note = (
        "; ".join(reasons).capitalize() + "."
        if reasons
        else (
            f"The flow completed {_pct(flow_rate)} of {compared} runs (assignment {_pct(assignment_rate)}) "
            f"and agreed on {_pct(agreement)}."
        )
    )
    if simulated:
        note += f" {simulated} writes were simulated, not executed."
    return ShadowVerdict(status, compared + incomplete, compared, incomplete, agreement, assignment_rate, flow_rate, simulated, note)


# ── Lifecycle ──────────────────────────────────────────────────────────────


class ShadowError(Exception):
    """A shadow cannot start; the message is safe to show, the reason is logged."""


async def start_shadow(
    db: AsyncSession,
    *,
    assignment: Assignment,
    flow: Flow,
    purpose: str,
    proposal_id: uuid.UUID | None = None,
    started_by_user_id: uuid.UUID | None = None,
    now: datetime | None = None,
) -> FlowShadow:
    """Open a shadow of sandbox *flow* on *assignment* (flushed; the caller commits).

    The caller has checked access. *flow* is always a sandbox copy (level 2):
    only a copy's runs are intercepted, and for a switch-over the production
    flow is created from it at switch-over time, so nothing reaches production
    before the shadow held. Refused when the assignment already has an open
    shadow, has no live trigger, or, for a switch-over, triggers the flow
    cannot take over (``trigger_takeover_refusal``).
    """
    from app.services.concierge.sandbox.copies import is_sandbox_copy, retain_sandbox_copy, sandbox_copy_of

    if not is_sandbox_copy(flow):
        raise ShadowError("Only a sandbox copy can run in shadow.")
    if assignment.trigger_type == "manual":
        raise ShadowError("This assignment has no live trigger to shadow.")
    if purpose == SHADOW_PURPOSE_SWITCH_OVER:
        from app.models.agent import Agent

        refusal = trigger_takeover_refusal(assignment, await db.get(Agent, assignment.agent_id), flow.graph)
        if refusal:
            raise ShadowError(refusal[0].upper() + refusal[1:] + ".")
    open_shadow = await db.scalar(
        select(FlowShadow.id).where(
            FlowShadow.assignment_id == assignment.id, FlowShadow.status.in_(SHADOW_OPEN_STATUSES)
        )
    )
    if open_shadow is not None:
        raise ShadowError("This assignment already has a shadow running.")
    now = now or datetime.now(UTC)
    durations = await shadow_durations(db)
    shadow = FlowShadow(
        assignment_id=assignment.id,
        flow_id=flow.id,
        proposal_id=proposal_id,
        purpose=purpose,
        status=SHADOW_STATUS_RUNNING,
        started_at=now,
        ends_at=now + durations.shadow,
        cursor_at=now,
        started_by_user_id=started_by_user_id,
    )
    db.add(shadow)
    await db.flush()
    # The copy must outlive the shadow and its watch: level 2's purge would
    # otherwise collect it mid-way.
    copy = await sandbox_copy_of(db, flow_id=flow.id)
    if copy is not None:
        await retain_sandbox_copy(db, copy.id, until=shadow_deadline(shadow) + durations.watch + COPY_RETENTION_MARGIN)
    return shadow


def shadow_deadline(shadow: FlowShadow) -> datetime:
    """The latest a shadow may run to gather its pairs."""
    return max(shadow.ends_at, shadow.started_at + timedelta(days=SHADOW_MAX_DAYS))


def _is_live_run(run: AssignmentRun, trigger_type: str) -> bool:
    """A run the assignment's own trigger started: not a manual run, re-run, follow-up or tool call."""
    context = run.trigger_context if isinstance(run.trigger_context, dict) else {}
    return context.get("type") == trigger_type and run.parent_run_id is None


def _stored(reservation: Any) -> dict[str, Any]:
    return {
        "cycle_id": str(reservation.cycle_id),
        "tokens": reservation.tokens,
        "cost_usd": str(reservation.cost_usd),
        "pair_id": str(reservation.hold[1]) if reservation.hold else None,
    }


def _reservation(stored: dict[str, Any]) -> Any:
    from app.services.concierge.budget import Reservation

    pair_id = stored.get("pair_id")
    return Reservation(
        cycle_id=uuid.UUID(stored["cycle_id"]),
        tokens=int(stored["tokens"]),
        cost_usd=Decimal(stored["cost_usd"]),
        hold=(SHADOW_WORK_KIND, uuid.UUID(pair_id)) if pair_id else None,
    )


async def _shadow_cycle(db: AsyncSession, shadow: FlowShadow, *, now: datetime) -> uuid.UUID:
    """The open Concierge cycle the shadow's runs are reserved on; ``BudgetRefusedError`` when none may open.

    Rotated before the stale-cycle sweep would close it as abandoned: a
    shadow outlives any one cycle.
    """
    from app.models.concierge import ConciergeCycle
    from app.services.concierge.budget import close_cycle, open_cycle

    cycle = await db.get(ConciergeCycle, shadow.cycle_id) if shadow.cycle_id else None
    if cycle is not None and cycle.status == "open":
        if cycle.opened_at > now - SHADOW_CYCLE_SPAN:
            return cycle.id
        await close_cycle(db, cycle.id, status="completed")
    shadow.cycle_id = await open_cycle(db, specialist="flow_lab", trigger="schedule")
    return shadow.cycle_id


async def _reserve_run(
    db: AsyncSession, shadow: FlowShadow, run: AssignmentRun, pair_id: uuid.UUID, *, now: datetime
) -> dict[str, Any]:
    """Reserve one shadow run on the Concierge budget, sized on the live run it copies.

    Held by its pair: rotating the shadow's cycle keeps it until the run is charged.
    """
    from app.services.concierge.budget import reserve
    from app.services.concierge.flow_comparison import assignment_run_side

    side = await assignment_run_side(db, run)
    tokens = (side.input_tokens or 0) + (side.output_tokens or 0) or DEFAULT_RUN_TOKENS
    cost = Decimal(str(side.cost)) if side.cost is not None else Decimal(0)
    cycle_id = await _shadow_cycle(db, shadow, now=now)
    return _stored(await reserve(db, cycle_id, tokens=tokens, cost_usd=cost, hold=(SHADOW_WORK_KIND, pair_id)))


async def _charge_pair(db: AsyncSession, pair: FlowShadowPair, flow_side: Any) -> None:
    """Book what the pair's flow run spent, releasing its reservation; once per pair."""
    if not pair.reservation:
        return
    from app.services.concierge.budget import charge

    reservation = _reservation(pair.reservation)
    tokens = ((flow_side.input_tokens or 0) + (flow_side.output_tokens or 0)) if flow_side else 0
    cost = Decimal(str(flow_side.cost)) if flow_side and flow_side.cost is not None else Decimal(0)
    await charge(db, reservation.cycle_id, tokens=tokens, cost_usd=cost, reservation=reservation)
    pair.reservation = None


async def pair_new_runs(
    db: AsyncSession, shadow: FlowShadow, assignment: Assignment, flow: Flow, *, max_pairs: int, now: datetime
) -> int:
    """Create the shadow flow runs of the assignment's live runs since the cursor.

    Each run is reserved on the Concierge's budget first and charged when its
    pair settles; past *max_pairs* paired runs, later ones are passed over.
    """
    from app.services.concierge.availability import concierge_enabled
    from app.services.concierge.budget import BudgetRefusedError, budget_refusal, release
    from app.services.concierge.consent import content_readable
    from app.services.concierge.sandbox.copies import sandbox_copy_of
    from app.services.concierge.sandbox.runs import start_sandbox_flow_run

    # A shadow run is agentic work (agent nodes, simulated writes): it waits,
    # cursor kept, while the flag is off, the licence gate refuses or the
    # Concierge's budget is spent. The runs it could not pair in time end
    # ``incomplete``, never counted as agreement.
    if not concierge_enabled() or (refusal := await budget_refusal(db)) is not None:
        logger.debug("Shadow %s: pairing deferred (%s)", shadow.id, "flag off" if not concierge_enabled() else refusal)
        return 0
    from app.models.agent import Agent
    from app.services.trigger_integration_access import assignment_acting_user_id

    copy = await sandbox_copy_of(db, flow_id=flow.id)
    # A shadow run acts as the assignment's own runs do, never as anyone a
    # model named: the copy then reaches what the assignment reaches.
    run_as = assignment_acting_user_id(assignment, await db.get(Agent, assignment.agent_id))
    runs = list(
        (
            await db.execute(
                select(AssignmentRun)
                .where(
                    AssignmentRun.assignment_id == assignment.id,
                    # ``>=`` and the paired filter: runs sharing the cursor's
                    # instant past a page are read again, never skipped.
                    AssignmentRun.created_at >= shadow.cursor_at,
                    AssignmentRun.created_at <= shadow_deadline(shadow),
                    ~exists().where(
                        FlowShadowPair.shadow_id == shadow.id, FlowShadowPair.assignment_run_id == AssignmentRun.id
                    ),
                )
                .order_by(AssignmentRun.created_at, AssignmentRun.id)
                .limit(PAIRS_PER_TICK)
            )
        ).scalars()
    )
    if not runs:
        return 0
    already = await db.scalar(
        select(func.count())
        .select_from(FlowShadowPair)
        .where(FlowShadowPair.shadow_id == shadow.id, FlowShadowPair.status != PAIR_STATUS_SKIPPED)
    )
    paired = 0
    for run in runs:
        # Consent is the run's, read now: an opt-out since the start stops the copying.
        if (
            not _is_live_run(run, assignment.trigger_type)
            or already + paired >= max_pairs
            or not await content_readable(db, agent_id=assignment.agent_id, user_id=run.user_id)
        ):
            # Recorded as passed over, so the paired filter never reads it again.
            db.add(FlowShadowPair(shadow_id=shadow.id, assignment_run_id=run.id, status=PAIR_STATUS_SKIPPED))
            shadow.cursor_at = run.created_at
            continue
        mapped = flow_trigger_for_assignment_run(assignment.trigger_type, run.trigger_payload, run.trigger_context)
        flow_run_id = reservation = None
        pair_id = uuid.uuid4()
        if mapped is not None and copy is not None:
            try:
                reservation = await _reserve_run(db, shadow, run, pair_id, now=now)
            except BudgetRefusedError as exc:
                # The budget is spent: this run and the next wait, cursor kept.
                logger.info("Shadow %s: pairing stopped: %s", shadow.id, exc.message)
                break
            trigger_type, trigger_payload = mapped
            try:
                async with db.begin_nested():
                    flow_run = await start_sandbox_flow_run(
                        db,
                        copy,
                        trigger_type=trigger_type,
                        trigger_payload=trigger_payload,
                        case_ref=f"shadow:{shadow.id}:{run.id}",
                        purpose="shadow",
                        run_as_user_id=run_as,
                    )
                    flow_run_id = flow_run.id
            except Exception as exc:
                logger.warning(
                    "Shadow %s: no flow run for assignment run %s: %s",
                    shadow.id,
                    run.id,
                    format_exception_message(exc),
                )
                await release(db, _reservation(reservation))
                reservation = None
        shadow.cursor_at = run.created_at
        db.add(
            FlowShadowPair(
                id=pair_id,
                shadow_id=shadow.id,
                assignment_run_id=run.id,
                flow_run_id=flow_run_id,
                status=PAIR_STATUS_PENDING if flow_run_id else PAIR_STATUS_INCOMPLETE,
                reservation=reservation,
            )
        )
        paired += 1
    await db.flush()
    return paired


async def settle_shadow_budget(db: AsyncSession, shadow: FlowShadow) -> None:
    """Once the shadow stopped pairing: charge what its unsettled runs spent so far, close its cycle."""
    from app.services.concierge.budget import close_cycle

    held = list(
        (
            await db.execute(
                select(FlowShadowPair).where(FlowShadowPair.shadow_id == shadow.id, FlowShadowPair.reservation.is_not(None))
            )
        ).scalars()
    )
    for pair in held:
        flow_run = await db.get(FlowRun, pair.flow_run_id) if pair.flow_run_id else None
        await _charge_pair(db, pair, await run_side(db, "flow_run", flow_run))
    if shadow.cycle_id is not None:
        await close_cycle(db, shadow.cycle_id, status="completed")
    await db.flush()


async def compare_pending_pairs(db: AsyncSession, shadow: FlowShadow, *, final: bool = False) -> int:
    """Compare the pending pairs whose two runs ended; *final* closes the rest as incomplete."""
    pairs = list(
        (
            await db.execute(
                select(FlowShadowPair).where(
                    FlowShadowPair.shadow_id == shadow.id, FlowShadowPair.status == PAIR_STATUS_PENDING
                )
            )
        ).scalars()
    )
    done = 0
    for pair in pairs:
        assignment_run = await db.get(AssignmentRun, pair.assignment_run_id)
        flow_run = await db.get(FlowRun, pair.flow_run_id) if pair.flow_run_id else None
        comparison: RunComparison = compare_sides(
            await run_side(db, "assignment_run", assignment_run), await run_side(db, "flow_run", flow_run)
        )
        if comparison.verdict == "incomplete" and not final:
            continue
        pair.comparison = comparison.as_dict()
        pair.status = PAIR_STATUS_INCOMPLETE if comparison.verdict == "incomplete" else PAIR_STATUS_COMPARED
        if comparison.right is not None:
            pair.intercepted_writes = comparison.right.intercepted_writes
            pair.simulated_writes = comparison.right.simulated_writes
        await _charge_pair(db, pair, comparison.right)
        done += 1
    await db.flush()
    return done


async def shadow_verdict(db: AsyncSession, shadow: FlowShadow, *, min_pairs: int) -> ShadowVerdict:
    pairs = list(
        (await db.execute(select(FlowShadowPair).where(FlowShadowPair.shadow_id == shadow.id))).scalars()
    )
    compared = [p.comparison for p in pairs if p.status == PAIR_STATUS_COMPARED and p.comparison]
    incomplete = sum(1 for p in pairs if p.status in (PAIR_STATUS_PENDING, PAIR_STATUS_INCOMPLETE))
    return settle_decision(compared, incomplete=incomplete, min_pairs=min_pairs)


async def _set_proposal_status(db: AsyncSession, shadow: FlowShadow, status: str, note: str, **stamps: Any) -> None:
    if shadow.proposal_id is None or not status:
        return
    from app.models.improvement_proposal import ImprovementProposal
    from app.services.improvement_proposals import set_proposal_status

    proposal = await db.get(ImprovementProposal, shadow.proposal_id)
    if proposal is not None:
        await set_proposal_status(db, proposal, status, note, **stamps)


async def _close(db: AsyncSession, shadow: FlowShadow, status: str, note: str, proposal_status: str, *, now: datetime) -> str:
    shadow.status = status
    shadow.settled_at = now
    shadow.note = note[:1000]
    await _set_proposal_status(db, shadow, proposal_status, shadow.note)
    return shadow.status


async def _locked(db: AsyncSession, model: Any, row_id: uuid.UUID | None) -> Any:
    """*model*'s row *row_id* locked ``FOR UPDATE`` and re-read, ``None`` when there is none."""
    if row_id is None:
        return None
    return await db.get(model, row_id, with_for_update=True, populate_existing=True)


async def advance_shadow(db: AsyncSession, shadow: FlowShadow, *, now: datetime | None = None) -> str:
    """One step of *shadow*'s lifecycle (the caller commits); its status after it."""
    running = shadow.status == SHADOW_STATUS_RUNNING
    status = await _advance(db, shadow, now=now or datetime.now(UTC))
    if running and status != SHADOW_STATUS_RUNNING:
        await settle_shadow_budget(db, shadow)
    return status


async def _advance(db: AsyncSession, shadow: FlowShadow, *, now: datetime) -> str:
    # Locked before any decision, and re-read: an owner pausing or editing the
    # assignment meanwhile must not be undone by a take-over or a rollback.
    assignment = await _locked(db, Assignment, shadow.assignment_id)
    copy = await _locked(db, Flow, shadow.flow_id)
    if assignment is None:
        if shadow.production_flow_id is not None:
            # The switch-over went through: the flow stays and does the work.
            return await _close(
                db, shadow, SHADOW_STATUS_KEPT, "The assignment was deleted after the switch-over; the flow stays.", "kept", now=now
            )
        return await _close(db, shadow, SHADOW_STATUS_CANCELLED, "The assignment was deleted.", "failed", now=now)
    if copy is None and shadow.status == SHADOW_STATUS_RUNNING:
        return await _close(db, shadow, SHADOW_STATUS_CANCELLED, "The sandbox flow was deleted.", "failed", now=now)

    if shadow.status == SHADOW_STATUS_RUNNING:
        durations = await shadow_durations(db)
        await pair_new_runs(
            db, shadow, assignment, copy, max_pairs=max(MAX_PAIRS_PER_SHADOW, 2 * durations.min_pairs), now=now
        )
        final = now >= shadow_deadline(shadow)
        await compare_pending_pairs(db, shadow, final=final)
        if now < shadow.ends_at:
            return shadow.status
        verdict = await shadow_verdict(db, shadow, min_pairs=durations.min_pairs)
        if verdict.status == SHADOW_STATUS_INCOMPLETE and not final:
            # Too few pairs yet: keep shadowing until the deadline.
            return shadow.status
        shadow.verdict = {**(shadow.verdict or {}), **verdict.as_dict()}
        if shadow.purpose != SHADOW_PURPOSE_SWITCH_OVER:
            return await _close(db, shadow, verdict.status, verdict.note, "", now=now)
        if verdict.status != SHADOW_STATUS_HELD:
            # Nothing in production changed: no flow was created.
            return await _close(db, shadow, verdict.status, verdict.note, "failed", now=now)
        from app.services.concierge.availability import concierge_active

        if not await concierge_active(db):
            # Switched off, the Concierge changes nothing in production: the
            # take-over waits for it up to the deadline, then is given up.
            if not final:
                return shadow.status
            return await _close(
                db, shadow, SHADOW_STATUS_CANCELLED, "Not switched over: Platform Health is switched off.", "failed", now=now
            )
        from app.services.concierge.flow_switch_over import take_over

        return await take_over(db, shadow, assignment, copy, verdict, now=now)

    if shadow.status == SHADOW_STATUS_WATCHING:
        from app.services.concierge.flow_switch_over import advance_watch

        return await advance_watch(db, shadow, assignment, now=now)
    return shadow.status


@dataclass(frozen=True, slots=True)
class ShadowTickResult:
    advanced: int
    failed: int


async def flow_shadow_tick(*, limit: int = SHADOWS_PER_TICK) -> ShadowTickResult:
    """The scheduler step: advance every open shadow, one transaction each, SKIP LOCKED.

    Runs whatever the switches: with the Concierge off, pairing and the
    take-over wait, but a watch still rolls a regressing switch-over back.
    """
    from app.core.database import async_session

    async with async_session() as db:
        ids = list(
            (
                await db.execute(
                    select(FlowShadow.id)
                    .where(FlowShadow.status.in_(SHADOW_OPEN_STATUSES))
                    .order_by(FlowShadow.updated_at)
                    .limit(limit)
                )
            ).scalars()
        )
    advanced = failed = paired = 0
    for shadow_id in ids:
        async with async_session() as db:
            try:
                shadow = await db.scalar(
                    select(FlowShadow).where(FlowShadow.id == shadow_id).with_for_update(skip_locked=True)
                )
                if shadow is None or shadow.status not in SHADOW_OPEN_STATUSES:
                    continue
                before = shadow.cursor_at
                await advance_shadow(db, shadow)
                shadow.updated_at = datetime.now(UTC)
                await db.commit()
                advanced += 1
                paired += int(shadow.cursor_at != before)
            except Exception as exc:
                await db.rollback()
                failed += 1
                logger.error(
                    "Could not advance flow shadow %s: %s", shadow_id, format_exception_message(exc), exc_info=True
                )
    if paired:
        # Shadow runs were created: wake the engine that executes them.
        from app.services import orchestration_engine

        await orchestration_engine.notify_orchestration_wake()
    return ShadowTickResult(advanced, failed)
