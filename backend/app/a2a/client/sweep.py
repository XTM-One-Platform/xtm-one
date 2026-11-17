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

"""Periodic re-verification of partner cards.

An OPTIMISATION, not the binding control. A cycle that never runs widens
nothing, because a delegation re-verifies for itself once the recorded verdict
is older than the staleness threshold (FR-050). Saying that plainly matters:
it is what makes it safe for this to be best-effort background work rather
than something a delegation has to wait on.

What the sweep buys is a registration that reports the truth BEFORE anyone
delegates — an operator opening the integrations page sees that a partner
changed its card, rather than discovering it when an agent's work is refused.

Claims rows with ``FOR UPDATE SKIP LOCKED``, mirroring ``a2a.tasks``'s reaper.
Concurrent workers on different replicas divide the work rather than
duplicating it, and idempotency is nearly free because the sweep writes a
verdict rather than performing a transition.

No row lock across network work. A claim's lock lasts until the transaction
ends, and each claimed row is followed by up to forty seconds of network work
against a partner that may not answer; a delegation re-verifying the same
agent would wait on that lock. So the claim is committed (released) as soon
as the row is chosen, discovery and the card fetch run unlocked, and the
verdict write takes the row lock only from its flush to the commit that
follows. ``SKIP LOCKED`` still keeps two replicas from choosing one card in
the same instant, and a row another verifier refreshed meanwhile is skipped.
"""

from __future__ import annotations

import logging
import time
import uuid
from collections.abc import Collection
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select

from app.core.config import settings as app_settings
from app.models.a2a import A2AClientTarget

logger = logging.getLogger(__name__)


async def due_targets(
    db, *, limit: int, exclude: Collection[uuid.UUID] = ()
) -> list:
    """Partner AGENTS whose verdict has aged past the threshold.

    One row per card, not per registration: a partner exposing three agents
    has three verdicts that age independently, and sweeping the registration
    could only ever refresh one of them.

    Inbound registrations are excluded by construction rather than by a check
    afterwards — they own no targets, having no card to verify, so they never
    enter this query however many partner credentials a deployment holds for
    the other direction.

    ``exclude`` names rows this cycle already handled: a verification that
    raised stamped nothing, so the row is still due.
    """
    threshold = datetime.now(UTC) - timedelta(
        seconds=app_settings.a2a_client_verdict_staleness_seconds
    )
    stmt = select(A2AClientTarget).where(
        (A2AClientTarget.last_verified_at.is_(None))
        | (A2AClientTarget.last_verified_at < threshold),
    )
    if exclude:
        stmt = stmt.where(A2AClientTarget.id.not_in(list(exclude)))
    stmt = (
        stmt.order_by(A2AClientTarget.last_verified_at.asc().nulls_first())
        .limit(limit)
        .with_for_update(skip_locked=True)
    )
    return list((await db.execute(stmt)).scalars().all())


async def sweep_once(db, *, limit: int = 25) -> int:
    """Re-verify up to *limit* cards, one transaction each. Returns how many.

    Bounded rather than exhaustive: a deployment with many partners spreads
    the work across cycles. The ordering means the stalest is always served
    first, so no registration can be starved by a busier neighbour. One row
    per transaction — see the module docstring.
    """
    from app.a2a.client.registration import (
        discover_partner_agents,
        verify_registration,
    )
    from app.api.v1.platform import is_chat_enabled
    from app.models.integration import Integration

    # A2A is offered exactly while agentic chat is. Otherwise no outbound
    # request leaves this platform — including re-verification of targets
    # created while it was. Checked here so every caller of a sweep cycle is
    # covered.
    if not await is_chat_enabled(db):
        return 0

    verified = 0
    discovered: set = set()
    handled: set[uuid.UUID] = set()
    for _ in range(limit):
        rows = await due_targets(db, limit=1, exclude=handled)
        if not rows:
            break
        row_id, integration_id = rows[0].id, rows[0].integration_id
        handled.add(row_id)
        # Releases the claim before any network work: held across the card
        # fetch, it would make every other writer of this row wait.
        await db.commit()
        try:
            integration = (
                await db.execute(
                    select(Integration).where(Integration.id == integration_id)
                )
            ).scalar_one_or_none()
            if integration is not None:
                # Once per integration per cycle, before its targets are
                # re-verified: an agent the partner bound since the last sweep
                # becomes reachable without anyone pressing anything.
                if integration.id not in discovered:
                    discovered.add(integration.id)
                    await discover_partner_agents(db, integration)
                # Re-read unlocked: discovery may have removed the row, and
                # another verifier may have refreshed it since the claim.
                row = (
                    await db.execute(select(A2AClientTarget).where(A2AClientTarget.id == row_id))
                ).scalar_one_or_none()
                if row is not None and _still_due(row):
                    await verify_registration(db, integration, row, trigger="sweep")
                    verified += 1
            # else: the cascade should have removed this row with its
            # integration; re-verifying without credentials is only noise.
        except Exception:
            # One partner must not stop the batch. A verification that raises
            # is itself a bug — ``verify_registration`` is documented never to
            # — so it is logged loudly rather than swallowed quietly, and
            # whatever it half-wrote is rolled back with the claim.
            logger.exception(
                "a2a.client: sweep failed to verify integration %s",
                integration_id,
            )
            await db.rollback()
            continue
        # The verdict, and the row lock its flush took.
        await db.commit()
    return verified


def _still_due(row: A2AClientTarget) -> bool:
    """Whether *row* is still past the threshold :func:`due_targets` applies."""
    last: datetime | None = row.last_verified_at
    if last is None:
        return True
    if last.tzinfo is None:
        last = last.replace(tzinfo=UTC)
    staleness = float(app_settings.a2a_client_verdict_staleness_seconds)
    return bool(last < datetime.now(UTC) - timedelta(seconds=staleness))


def owed_cancel_cutoff():
    """The age at which an owed cancel is presumed lost rather than in flight.

    The fast path (``spawn_run_cancel``, the conversation cascade) tells the
    partner seconds after the decision commits, on whichever replica decided.
    Re-sending sooner than one delegation ceiling would race that live path;
    anything older than it is a cancel no process is still sending. Derived
    rather than configured, like ``callbacks.claim_cutoff``: it bounds the same
    network work it protects.
    """
    return datetime.now(UTC) - timedelta(
        seconds=app_settings.a2a_client_delegation_ceiling_seconds
    )


def owed_cancel_cycle_budget() -> float:
    """How long one re-send cycle may keep claiming rows, in seconds.

    Half the cycle's own interval: a backlog is drained in one cycle unless it
    would take longer than that, and a cycle then never runs into the next
    tick. What is left is still owed and still eligible, so the next cycle
    takes it first - the oldest stamp is always claimed first.
    """
    return app_settings.a2a_client_sweep_interval_seconds / 2


# How many owed rows one read of the backlog returns; each is claimed alone.
_OWED_CANCEL_BATCH = 25


async def reissue_owed_cancels(
    db, *, limit: int | None = None, budget_seconds: float | None = None
) -> int:
    """Tell partners about cancels a hard crash interrupted. Returns how many.

    ``cascade_cancel`` and the conversation cascade commit ``cancel_pending_at``
    before any partner is called, and telling the partner concludes the row,
    so a row still OUTSTANDING with it past ``owed_cancel_cutoff`` is a cancel
    that a killed process never sent. Re-sending goes through the
    ordinary ``cancel_outstanding_for``, narrowed to that one row: told or
    concluded locally, the row ends ``cancelled``, so nothing is ever re-sent
    twice.

    Kept apart from ``collect_overdue`` on purpose: abandonment cancels nothing
    by design, and an owed cancel is the opposite obligation.

    Not gated on chat availability, unlike the rest of the sweep: this is the
    teardown ``delegation.cancel`` sends whatever the licence or the AI toggle
    says, and a crash must not change whether the partner is told.

    Run by its own loop (``main._a2a_owed_cancel_loop``) on a fixed cadence of
    ``a2a_client_sweep_interval_seconds``, whatever the rest of the sweep
    costs, so a cancel still inside its grace when the process comes back -
    the usual quick restart - goes out with the next cycle: at most one
    delegation ceiling plus one interval after the decision.

    Drains the backlog rather than a fixed batch: rows are claimed until none
    is left, or *limit* rows were handled, or *budget_seconds* (default
    ``owed_cancel_cycle_budget()``) ran out - checked before each claim after
    the first, so a cycle can overrun it by one partner call at most. The
    bound above holds whenever the backlog ahead of a stamp fits in that
    budget; what does not is the next cycle's first work.

    The backlog is read ``_OWED_CANCEL_BATCH`` ids at a time, oldest stamp
    first: nothing indexes the columns it filters on, so a read per row would
    scan the delegation history once per cancel. With nothing owed, a cycle
    is that one read.

    One row per transaction, claimed by id with ``FOR UPDATE SKIP LOCKED``
    like ``sweep_once``: each claim is followed by a partner call, and two
    replicas must not tell one partner twice. The claim re-tests the row, so
    one another replica took or concluded since the read is skipped.
    """
    from app.a2a.client.delegation import cancel_outstanding_for
    from app.models.a2a import A2ADelegation

    cutoff = owed_cancel_cutoff()
    owed = (
        A2ADelegation.outcome.is_(None),
        A2ADelegation.partner_task_id.isnot(None),
        A2ADelegation.cancel_pending_at.isnot(None),
        A2ADelegation.cancel_pending_at < cutoff,
    )
    budget = owed_cancel_cycle_budget() if budget_seconds is None else budget_seconds
    deadline = time.monotonic() + budget
    reissued = 0
    attempts = 0
    # Only the rows a read must not return again this cycle: a re-send that
    # failed and rolled back, one that concluded nothing, or one another
    # replica holds. A row that went through is concluded and leaves the
    # query by itself, so excluding it too would grow the NOT IN list with
    # the whole backlog.
    failed: set[uuid.UUID] = set()
    batch: list[uuid.UUID] = []
    while limit is None or attempts < limit:
        if attempts and time.monotonic() >= deadline:
            logger.warning(
                "a2a.client: owed-cancel re-send stopped after %d row(s), its "
                "%.0f s budget spent; the rest goes first next cycle",
                attempts,
                budget,
            )
            break
        if not batch:
            stmt = select(A2ADelegation.id).where(*owed)
            if failed:
                stmt = stmt.where(A2ADelegation.id.not_in(list(failed)))
            stmt = stmt.order_by(A2ADelegation.cancel_pending_at.asc()).limit(
                _OWED_CANCEL_BATCH
            )
            batch = list((await db.execute(stmt)).scalars().all())
            if not batch:
                break
        row_id = batch.pop(0)
        attempts += 1
        row = (
            (
                await db.execute(
                    select(A2ADelegation)
                    .where(A2ADelegation.id == row_id, *owed)
                    .with_for_update(skip_locked=True)
                )
            )
            .scalars()
            .first()
        )
        if row is None:
            # Another replica holds it, or it concluded since the read.
            failed.add(row_id)
            continue
        try:
            outcome = await cancel_outstanding_for(db, delegation_id=row_id)
        except Exception:
            logger.exception(
                "a2a.client: could not re-send the owed cancel for delegation %s",
                row_id,
            )
            await db.rollback()
            failed.add(row_id)
            continue
        await db.commit()
        done = outcome.cancelled + outcome.concluded
        if not done:
            # Nothing concluded it (its scope found no row), so it is still
            # eligible: never pick it again in this cycle.
            failed.add(row_id)
        reissued += done
    return reissued


async def collect_overdue(db, *, limit: int = 25) -> int:
    """Ask about outstanding partner tasks nobody has asked about lately.

    FR-018: the floor beneath every other mechanism. A partner bounds its
    delivery attempts and then gives up — this platform's own sender stops
    after five and writes an audit entry — so a delegation whose callback never
    arrives would otherwise stay outstanding until something else cancelled it.
    A calling agent that never collects leaves the same gap.

    Collecting through the ordinary path means the answer reaches whoever was
    waiting by the ordinary route too: the same conclusion, the same
    resumption, the same claim deciding which of two replicas acts.
    """
    from app.a2a.client import callbacks as callback_svc
    from app.a2a.client.callbacks import abandoned_cutoff, unclaimed_or_stale
    from app.api.v1.platform import is_chat_enabled
    from app.models.a2a import MECHANISM_ASKED, A2ADelegation

    if not await is_chat_enabled(db):
        return 0

    cutoff = datetime.now(UTC) - timedelta(
        seconds=app_settings.a2a_client_floor_interval_seconds
    )
    # Measured from when this delegation last started being outstanding: its
    # submission, or the re-open that revived it. Not from the last time the
    # partner said something — a collect that comes back "working" resets
    # nothing, because a row that never concludes is what has to end however
    # talkative the partner is. The re-open counts because a revived row
    # carries a ``submitted_at`` older than any cutoff, and would otherwise be
    # re-abandoned on the next cycle.
    heard_from = func.coalesce(
        A2ADelegation.reopened_at, A2ADelegation.submitted_at
    )
    rows = (
        (
            await db.execute(
                select(A2ADelegation)
                .where(
                    A2ADelegation.outcome.is_(None),
                    A2ADelegation.partner_task_id.isnot(None),
                    # A row owed a cancel is not outstanding work to collect:
                    # its caller was cancelled, so resuming it would be wrong,
                    # and abandoning it would cancel nothing at the partner.
                    # ``reissue_owed_cancels`` is what ends it.
                    A2ADelegation.cancel_pending_at.is_(None),
                    heard_from < cutoff,
                    # Not merely unclaimed: a claim whose holder died would
                    # otherwise hide the row from the sweep forever, which is
                    # the one case the floor exists to catch.
                    unclaimed_or_stale(),
                )
                .order_by(A2ADelegation.submitted_at)
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )
    collected = 0
    abandoned = abandoned_cutoff()
    for row in rows:
        try:
            if (row.reopened_at or row.submitted_at) < abandoned:
                # Past the point where "still running" is a claim about the
                # partner rather than about this row's age — counted from when
                # it last started being outstanding, which for a revived row is
                # the re-open. Ended here, or the floor re-collects it every
                # cycle for as long as it lives and its caller waits on an
                # answer nothing will bring.
                await callback_svc.conclude_abandoned(
                    db, row, mechanism=MECHANISM_ASKED
                )
                collected += 1
                continue
            # The floor asks the partner; nothing called back, whatever this
            # delegation once hoped for. Bounded well under the delegation
            # ceiling: this is a question, not a wait. A row still running at
            # the ceiling would cost the whole cycle, and a full window of them
            # costs more than the interval between cycles — pushing the card
            # re-verification that shares this coroutine out behind it.
            await callback_svc.on_partner_finished(
                db,
                row,
                mechanism=MECHANISM_ASKED,
                ceiling_seconds=app_settings.a2a_client_grace_seconds,
            )
            collected += 1
        except Exception:
            logger.warning(
                "a2a.client: floor could not collect delegation %s", row.id,
                exc_info=True,
            )
    return collected
