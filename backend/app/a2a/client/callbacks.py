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

"""Being told a partner's task finished, rather than asking.

The mirror of :mod:`app.a2a.push`, which is what this platform sends when it is
the server. What it sends is what a partner should send us, so the shape here
is that shape read back: an identity and a state, no result. A callback is an
invitation to read the task, never the answer — the answer comes from reading
it, over an authenticated channel, which is the only reason an unauthenticated
route may exist at all.

Three rules, and each is a refusal:

* a callback is registered only where the partner declares support AND this
  deployment is reachable, because registering one tells the partner where
  this deployment lives;
* a callback authenticates against a secret minted for one delegation, so a
  leaked one exposes one exchange;
* the delegation row is the claim, so a callback and the polling floor racing
  on different replicas conclude it once.
"""

from __future__ import annotations

import logging
import secrets
import uuid

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.a2a.client.destinations import assert_allowed_async
from app.core.config import settings as app_settings
from app.core.security import decrypt_value, encrypt_value, timing_safe_str_eq
from app.models.a2a import MECHANISM_CALLED_BACK, A2ADelegation

logger = logging.getLogger(__name__)

#: Length of a minted callback secret, in bytes before encoding. 32 bytes is
#: the same order as every other secret this platform mints.
_SECRET_BYTES = 32


async def deployment_is_reachable() -> bool:
    """Whether a partner can reach this deployment to call back (FR-010).

    The operator's explicit setting decides it, and a refusal is final: that
    setting exists to withhold this deployment's whereabouts, so a configured
    callback address does not overrule it. Absent an explicit setting, such an
    address settles it — naming one asserts a partner can reach it. Absent
    both, the configured public address decides, and one the destination guard
    would refuse to *send* to is one no partner can reach either, so the same
    check answers both directions: a dev deployment on loopback, or a
    self-hosted one on a private address, reads as unreachable without anyone
    having to say so.

    A configured address is a default, not evidence: a deployment can advertise
    one no partner can actually reach. The cost of that mistake is a partner
    exhausting its delivery attempts while the polling floor concludes the
    delegation regardless.
    """
    explicit = app_settings.a2a_client_callbacks_reachable
    if explicit is not None:
        return explicit
    if app_settings.a2a_client_callback_base_url:
        return True
    try:
        await assert_allowed_async(app_settings.base_url)
    except Exception:
        return False
    return True


def mint_secret() -> str:
    """A secret for one delegation, and one only."""
    return secrets.token_urlsafe(_SECRET_BYTES)


def callback_url(delegation_id: uuid.UUID) -> str:
    """Where the partner should post. The delegation is named in the path so a
    callback can be routed before it is authenticated.

    Built on the callback base where one is configured, because the address a
    partner reaches is not always the one a browser does.
    """
    base = app_settings.a2a_client_callback_base_url or app_settings.base_url
    return f"{base.rstrip('/')}/a2a/callbacks/{delegation_id}"


def store_secret(secret: str) -> str:
    """Encrypted at rest, like every other partner credential here."""
    return encrypt_value(secret)


def presented_secret_matches(row: A2ADelegation, presented: str | None) -> bool:
    """Constant time, and false for anything unreadable.

    An unreadable secret — a rotated encryption key — refuses rather than
    letting a callback through, on the same fail-closed terms the sending half
    uses when it declines to send ciphertext as a credential. A presented
    value no minted secret could be (non-ASCII, a lone surrogate from the JSON
    body) is a mismatch too, never an error.
    """
    if not presented or not row.callback_secret:
        return False
    try:
        expected = decrypt_value(row.callback_secret)
    except Exception:
        logger.warning(
            "a2a.client: callback secret for delegation %s is unreadable", row.id
        )
        return False
    return timing_safe_str_eq(presented, expected)


async def addressed_delegation(db, delegation_id: str) -> A2ADelegation | None:
    """The delegation this callback may still be acted on for, or ``None``.

    ``None`` covers a malformed id, one this deployment never issued, and one
    already concluded. They share an answer so a partner cannot learn which
    delegations exist by asking.

    Abandonment is the only exception. Every other conclusion followed
    something that happened — the partner failed, the credential was rejected,
    the ceiling ran out and CANCELLED the task at the partner — so a callback
    after it contradicts a decision. Abandonment cancels nothing, so the work
    may still be running and the answer that arrives is the one the caller was
    promised.

    Keyed on ``abandoned_at == terminal_at``, not on the stamp alone: the
    stamp survives a real collection, so on its own it would admit a row that
    something else has since ended — including one this side cancelled at the
    ceiling, which writes ``timeout`` over the same stamped row.

    The equality holds exactly when the conclusion IS the abandonment.
    ``conclude_abandoned`` is the only writer of ``abandoned_at`` and writes
    both from one ``now``; every other conclusion writes a fresh
    ``terminal_at`` and leaves the stamp.

    And bounded in time: the secret is checked either way, but an unbounded
    window would make a one-shot credential indefinitely replayable. A partner
    silent for a further whole window is not coming back.

    The refusal is unchanged for a stranger: an id nobody holds still matches
    nothing, and the secret is checked after this either way.
    """
    from sqlalchemy import and_, or_

    from app.models.a2a import OUTCOME_TIMEOUT

    try:
        parsed = uuid.UUID(str(delegation_id))
    except (ValueError, AttributeError, TypeError):
        # Partner-supplied and unparsed until here; an exception would be a
        # 500 where a not-found belongs.
        return None
    return (
        await db.execute(
            select(A2ADelegation).where(
                A2ADelegation.id == parsed,
                or_(
                    # Not a row owed a cancel: its caller was cancelled, and
                    # a collect would resume it.
                    and_(
                        A2ADelegation.outcome.is_(None),
                        A2ADelegation.cancel_pending_at.is_(None),
                    ),
                    and_(
                        A2ADelegation.outcome == OUTCOME_TIMEOUT,
                        A2ADelegation.abandoned_at == A2ADelegation.terminal_at,
                        A2ADelegation.abandoned_at > abandoned_cutoff(),
                    ),
                ),
            )
        )
    ).scalar_one_or_none()


def claim_cutoff():
    """The age at which a claim stops meaning "someone is working on this".

    A claim covers one collect, which goes through the ordinary delegation path
    and is bounded by the per-attempt ceiling. A claim older than that cannot
    still be in progress: either the replica holding it died, or its read of
    the partner's task timed out — and in both cases nobody is acting on it.

    Derived rather than configured, because it is not an independent choice:
    it is the bound on the work the claim covers.
    """
    from datetime import UTC, datetime, timedelta

    return datetime.now(UTC) - timedelta(
        seconds=app_settings.a2a_client_delegation_ceiling_seconds
    )


def unclaimed_or_stale():
    """The condition for a delegation nobody is currently acting on.

    Shared by the claim and by the floor that reclaims: the two must agree on
    when a claim has lapsed, or the floor hands a row to a caller whose claim
    then fails and the row is skipped by both.
    """
    from sqlalchemy import or_

    return or_(
        A2ADelegation.callback_claimed_at.is_(None),
        A2ADelegation.callback_claimed_at < claim_cutoff(),
    )


async def claim(db, row: A2ADelegation) -> bool:
    """Take responsibility for acting on this callback. True when this won.

    FR-019. Every replica can receive the same partner's callback, and the
    polling floor runs underneath regardless, so acting must happen once — two
    replicas resuming one run is the race this loses on purpose. The claim is
    the write rather than a read followed by one.

    A LAPSED claim can be taken again, and that is what stops a claim
    stranding the delegation it was meant to protect. A replica that claims and
    then dies leaves a row no partner will call about again — the partner got
    its 200 — so if the claim were permanent the row would stay outstanding
    forever, invisible to the floor. Deferring to a claim is politeness toward
    a replica that is still working, not a lock nobody can break.
    """
    from datetime import UTC, datetime

    won = bool(
        (
            await db.execute(
                update(A2ADelegation)
                .where(
                    A2ADelegation.id == row.id,
                    unclaimed_or_stale(),
                    # Nobody acts on a row this side decided to cancel, owed
                    # or already told: its caller was cancelled, and a collect
                    # would resume it. In the statement, so a callback
                    # admitted just before the cancel committed still loses
                    # here.
                    A2ADelegation.cancel_pending_at.is_(None),
                )
                .values(callback_claimed_at=datetime.now(UTC))
            )
        ).rowcount
    )
    await db.commit()
    return won


async def act_on_callback(delegation_id: uuid.UUID, *, mechanism: str) -> None:
    """Act on a partner's callback in a session of its own.

    For the route. Acting means an ordinary collect, bounded by the delegation
    ceiling — the partner's task is not terminal simply because it said so —
    and awaiting that on the request path would hold the partner's own response,
    and the request's database connection, for the whole of it.

    The row is re-read rather than carried over, so the window between
    answering the partner and acting cannot resurrect one the floor concluded
    in the meantime. ``claim`` still decides who acts.

    Never raises: the partner has its 200 and there is no caller left to hand a
    failure to.
    """
    from app.core.database import async_session

    try:
        async with async_session() as own_db:
            row = await addressed_delegation(own_db, str(delegation_id))
            if row is None:
                return
            await on_partner_finished(own_db, row, mechanism=mechanism)
    except Exception:
        logger.warning(
            "a2a.client: could not act on callback for delegation %s",
            delegation_id,
            exc_info=True,
        )


#: Kept referenced: asyncio holds only weak references, so a bare
#: ``create_task`` can be collected before it has acted.
BACKGROUND_CALLBACKS: set = set()


def spawn_partner_finished(delegation_id) -> None:
    """Act on an authenticated callback, detached from the request."""
    import asyncio

    _bg = asyncio.create_task(
        act_on_callback(delegation_id, mechanism=MECHANISM_CALLED_BACK)
    )
    BACKGROUND_CALLBACKS.add(_bg)
    _bg.add_done_callback(BACKGROUND_CALLBACKS.discard)


async def on_partner_finished(
    db, row: A2ADelegation, *, mechanism: str, ceiling_seconds: float | None = None
) -> None:
    """Read the partner's task, then wake whatever was waiting for it.

    The callback said a state; this reads the task, because a body arriving on
    an unauthenticated route is a reason to look and never the answer itself.
    The read is an ordinary collect, so it goes through the credentials, the
    pre-flight and the ownership rule every other delegation does.

    ``mechanism`` is the caller's to state and has no default: this path is
    reached both by a partner calling back and by the floor asking, and the
    row is where an operator reads which of the two actually reported it.

    Resumption is each run path's own: an assignment run has a durable
    execution to re-arm, and a conversation has the wake that already brings it
    back when background work finishes. Neither is invented here.
    """
    if not await claim(db, row):
        # Another replica has it, the floor already concluded, or the attempt
        # that published this row is still polling and holds the claim.
        #
        # Only a PARTNER's call is recorded here. It has its 200 and will not
        # call again, so that it called has to survive the loss: whoever
        # releases the claim collects there rather than leaving an answer that
        # already exists until the floor's next cycle. The floor arriving
        # second is not that — nothing called, and writing it would make the
        # column answer "when the floor lost a race" and send the claim-holder
        # to collect something nobody announced.
        if mechanism != MECHANISM_CALLED_BACK:
            return
        from datetime import UTC, datetime

        await db.execute(
            update(A2ADelegation)
            .where(
                A2ADelegation.id == row.id,
                A2ADelegation.callback_seen_at.is_(None),
            )
            .values(callback_seen_at=datetime.now(UTC))
        )
        await db.commit()
        # The claim above committed on its own, and so did this stamp: nothing
        # held a lock across the two, so the holder may have stopped and
        # released in between — reading no stamp, and releasing for the last
        # time. The stamp says somebody must look at this row; a claim that is
        # suddenly free says nobody else will.
        if not await claim(db, row):
            return
        await db.execute(
            update(A2ADelegation)
            .where(A2ADelegation.id == row.id)
            .values(callback_seen_at=None)
        )
        await db.commit()

    if row.outcome is not None:
        # Re-opened rather than collected as it stands: ``_record`` concludes
        # the row it finds still OPEN, so collecting on a concluded one writes
        # a second row instead of ending this one.
        #
        # The statement carries its own precondition rather than trusting the
        # read above — the claim makes that redundant only while nothing
        # reaches here by another route.
        #
        # ``reopened_at`` restarts the row's clock: ``submitted_at`` is older
        # than the abandonment cutoff by definition for any row that was
        # abandoned, so the next cycle would re-abandon what this re-opened
        # seconds ago, once per delivery retry. ``abandoned_at`` stays —
        # how this ended is not whether we had given up on it before it did.
        from datetime import UTC, datetime

        from app.models.a2a import OUTCOME_TIMEOUT

        reopened = bool(
            (
                await db.execute(
                    update(A2ADelegation)
                    .where(
                        A2ADelegation.id == row.id,
                        A2ADelegation.outcome == OUTCOME_TIMEOUT,
                        A2ADelegation.abandoned_at == A2ADelegation.terminal_at,
                    )
                    .values(
                        outcome=None,
                        terminal_at=None,
                        reopened_at=datetime.now(UTC),
                    )
                )
            ).rowcount
        )
        await db.commit()
        if not reopened:
            # Somebody decided in the meantime, and that is the case to leave
            # alone. ``act_on_callback`` re-reads the row, so the window is
            # narrow — that read to winning the claim — but it is reachable:
            # another replica claims, collects for longer than
            # ``claim_cutoff``, its claim lapses, this one wins the re-claim,
            # and the other concludes. Carrying on with a stale ``row.outcome``
            # is what would hurt: ``_record`` finds no open row and inserts a
            # SECOND one for this exchange, which is the defect the re-open
            # exists to avoid, and resumes the caller on top of it.
            return
        row.outcome = None

    from app.a2a.client.delegation import CallingRun, delegate
    from app.models.integration import Integration

    integration = (
        await db.execute(
            select(Integration).where(Integration.id == row.integration_id)
        )
    ).scalar_one_or_none()
    target = await _target_of(db, row.target_id)
    if integration is None or target is None:
        # The registration, or the partner agent it was sent to, is gone —
        # ``sync_targets`` deletes a target the partner stopped listing and
        # ``target_id`` is SET NULL, so the row outlives what it was collected
        # through. Concluded here rather than returned from: left outstanding
        # it is a row the floor re-claims and re-abandons every cycle, for a
        # caller that was told the answer would reach it.
        await _conclude_unreachable(db, row, target, mechanism=mechanism)
        return

    result = await delegate(
        db,
        integration,
        target,
        continue_task_id=row.partner_task_id,
        ceiling_seconds=ceiling_seconds,
        calling=CallingRun(
            agent_id=row.agent_id,
            user_id=row.user_id,
            run_ref=row.run_ref,
            conversation_ref=row.conversation_ref,
        ),
    )
    await db.execute(
        update(A2ADelegation)
        .where(A2ADelegation.id == row.id)
        .values(mechanism=mechanism)
    )
    await db.commit()

    if result.still_running:
        # The partner said it finished and its task says otherwise. Believe the
        # task: the floor will conclude this when it really is done.
        return
    await _resume_caller(db, row, result, partner_name=_declared_name(target))


async def _conclude_unreachable(
    db, row: A2ADelegation, target, *, mechanism: str
) -> None:
    """End a delegation whose partner can no longer be reached at all.

    ``unreachable`` rather than a failure of the partner's: nothing was asked
    of it. The caller is still resumed, because a run parked on an answer that
    can never arrive is the one outcome this feature must not produce.
    """
    from datetime import UTC, datetime

    from app.models.a2a import OUTCOME_UNREACHABLE

    await db.execute(
        update(A2ADelegation)
        .where(A2ADelegation.id == row.id)
        .values(
            outcome=OUTCOME_UNREACHABLE,
            terminal_at=datetime.now(UTC),
            mechanism=mechanism,
        )
    )
    await db.commit()
    await _resume_caller(
        db,
        row,
        _Unreachable(),
        partner_name=_declared_name(target),
    )


async def conclude_abandoned(db, row: A2ADelegation, *, mechanism: str) -> None:
    """End a delegation that has gone far too long without concluding.

    A bounded collect hands back rather than cancelling, which is right — the
    clock running out says nothing about the partner. But nothing else ends
    such a row, and the floor's own predicate has a minimum age and no maximum,
    so it would be re-collected every cycle for as long as it existed while its
    caller waited on an answer that will never arrive. That last part is the
    one outcome this feature must not produce.

    ``timeout`` because that is what it is from here: a whole window with no
    terminal state, which is the measure rather than the partner's silence.
    The caller is resumed, as it is for every other end.

    ``abandoned_at`` alongside it because this timeout is not the other one:
    the ceiling's cancelled the task at the partner, this one cancels nothing.
    The callback route reads the column to tell them apart.
    """
    from datetime import UTC, datetime

    from app.models.a2a import OUTCOME_TIMEOUT

    now = datetime.now(UTC)
    # The write is the arbitration, against both other actors on this row.
    #
    # ``outcome IS NULL`` settles floor against floor: ``collect_overdue``
    # filters on ``unclaimed_or_stale()`` in its SELECT and comes here
    # directly, and a filter in a SELECT is not a claim. Resuming is not a
    # bookkeeping write — the chat path posts a turn into the conversation and
    # dedups nothing — so only the replica that transitioned the row may do
    # it. That also makes this idempotent.
    #
    # ``unclaimed_or_stale()`` settles floor against a collect, and belongs in
    # the statement rather than inherited from that SELECT: the SELECT tests it
    # once for a batch of up to 25 rows that are then polled one at a time, so
    # for a row late in the batch it is minutes stale. A live claim means
    # somebody better placed is already ending this row.
    won = bool(
        (
            await db.execute(
                update(A2ADelegation)
                .where(
                    A2ADelegation.id == row.id,
                    A2ADelegation.outcome.is_(None),
                    unclaimed_or_stale(),
                    # The floor's SELECT filtered this, but a cancel can commit
                    # while a batch of 25 is being walked: an owed cancel is
                    # ended by ``reissue_owed_cancels``, never abandoned.
                    A2ADelegation.cancel_pending_at.is_(None),
                )
                .values(
                    outcome=OUTCOME_TIMEOUT,
                    terminal_at=now,
                    abandoned_at=now,
                    mechanism=mechanism,
                )
            )
        ).rowcount
    )
    await db.commit()
    if not won:
        # Another replica's floor concluded it, or a late callback re-opened
        # and collected it. Either way there is nothing here to resume.
        return
    await _resume_caller(
        db,
        row,
        _Abandoned(),
        partner_name=_declared_name(await _target_of(db, row.target_id)),
    )


class _Abandoned:
    """What ``_resume_caller`` reads from a partner that never answered."""

    succeeded = False
    still_running = False
    result_text = ""
    outcome = "timeout"
    detail = "The partner never reported a terminal state."


def abandoned_cutoff():
    """The age at which an outstanding delegation stops being "still running".

    It measures how long a delegation may go without reaching a terminal
    state — not how long the partner has been silent. A partner that answers
    every floor collect with "working" is bounded by this exactly like one
    that went dark: a row that never concludes is what has to end, because
    nothing else ends it and its caller waits until it does.

    Configured, unlike ``claim_cutoff``, which is derived because it bounds
    the same work it protects — a claim covers one collect, and one collect is
    bounded by the per-attempt ceiling. This bounds something else, and the
    hand-back and the callback exist precisely because the work outlasts the
    attempt. Its default is the longest a task may live on the surface this
    platform itself serves; sooner would have the two halves of this feature
    disagreeing about the same piece of work.
    """
    from datetime import UTC, datetime, timedelta

    return datetime.now(UTC) - timedelta(
        seconds=app_settings.a2a_client_delegation_abandoned_seconds
    )


class _Unreachable:
    """What ``_resume_caller`` reads from a collect that could not happen."""

    succeeded = False
    still_running = False
    result_text = ""
    outcome = "unreachable"
    detail = "The partner this was sent to is no longer registered."


async def _target_of(db: AsyncSession, target_id: uuid.UUID | None) -> object | None:
    from app.models.a2a import A2AClientTarget

    if target_id is None:
        return None
    return (
        await db.execute(
            select(A2AClientTarget).where(A2AClientTarget.id == target_id)
        )
    ).scalar_one_or_none()


async def _resume_caller(
    db, row: A2ADelegation, result, *, partner_name: str
) -> None:
    """Hand the answer back to the run or the conversation that asked.

    Two mechanisms because two run paths genuinely resume differently, and
    inventing a third is the parallel infrastructure this feature exists to
    avoid. A run whose id is recorded has a durable execution; a delegation
    made from a chat turn has only the conversation, which outlives the turn.

    Never for a caller this side decided to cancel. A collect already under
    way when the cancel committed still concludes the row, and the stamp is
    re-read here, from the database rather than the stale row - it outlives
    the partner being told - so the answer is recorded without resurrecting
    the run or waking the conversation the user abandoned. A cancel
    that lands after the answer was recorded finds no outstanding row to
    stamp: a run is then held by its own terminal status, which
    ``execute_assignment_run`` refuses to re-execute, and a conversation by
    the Stop the cancel cascade records on it, which the wake compares with
    ``submitted_at`` before delivering.
    """
    owed = (
        await db.execute(
            select(A2ADelegation.cancel_pending_at).where(A2ADelegation.id == row.id)
        )
    ).scalar_one_or_none()
    if owed is not None:
        logger.info(
            "a2a.client: not resuming the caller of delegation %s - it was "
            "cancelled while the partner's answer was being collected",
            row.id,
        )
        return
    if row.run_ref is not None:
        from app.models.assignment import AssignmentRun
        from app.services.assignment_engine import (
            TERMINAL_RUN_STATUSES,
            resume_assignment_run,
        )

        run = (
            await db.execute(
                select(AssignmentRun).where(AssignmentRun.id == row.run_ref)
            )
        ).scalar_one_or_none()
        if run is not None and run.status not in TERMINAL_RUN_STATUSES:
            await resume_assignment_run(db, run)
            return
        # The run that asked has ended (#12079): ``execute_assignment_run``
        # refuses a terminal run, so resuming it would drop the answer. The
        # answer is raised as a platform event instead, for the agent's
        # ``platform_event`` assignments; a later run of that agent and user
        # may also collect the task (``delegation._scoped_to_caller``).
        await _publish_delegation_completed(row, result, partner_name=partner_name, run=run)
        if row.conversation_ref is None:
            return

    if row.conversation_ref is not None:
        from app.services.parent_wake import schedule_partner_wake

        schedule_partner_wake(
            conversation_id=str(row.conversation_ref),
            user_id=str(row.user_id),
            partner_name=partner_name,
            result_text=result.result_text,
            error_text=None if result.succeeded else (result.detail or result.outcome),
            delegated_at=row.submitted_at,
        )


async def _publish_delegation_completed(
    row: A2ADelegation, result: object, *, partner_name: str, run: object | None
) -> None:
    """Raise ``a2a.delegation.completed`` for a delegation whose run has ended.

    Ids, the outcome and a bounded, enveloped preview of the answer: the run
    the event starts reads it quarantined, and collects the whole answer with
    ``continue_task_id`` and no message. Never raises.
    """
    from app.a2a.client.tool import guard_partner_text
    from app.services import platform_events

    try:
        text = getattr(result, "result_text", None)
        preview = guard_partner_text(text, partner_name=partner_name) if text else ""
        await platform_events.publish(
            platform_events.A2A_DELEGATION_COMPLETED,
            platform_events.a2a_delegation_data(
                delegation_id=row.id,
                agent_id=row.agent_id,
                user_id=row.user_id,
                run_id=row.run_ref,
                assignment_id=getattr(run, "assignment_id", None),
                partner=partner_name,
                partner_task_id=row.partner_task_id,
                outcome=getattr(result, "outcome", None),
                succeeded=bool(getattr(result, "succeeded", False)),
                detail=None if getattr(result, "succeeded", False) else getattr(result, "detail", None),
                result_preview=preview,
            ),
            # One event per delegation: a callback and the floor racing to
            # conclude it start one run.
            event_id=f"a2a-delegation:{row.id}",
        )
    except Exception:
        logger.warning(
            "a2a.client: could not raise a2a.delegation.completed for delegation %s",
            row.id,
            exc_info=True,
        )


def _declared_name(target) -> str:
    """What the partner's card called it, for the wake the caller reads."""
    return getattr(target, "declared_name", None) or "an external partner"


# --- Ticket status pushes (#2276) -------------------------------------------
#
# A partner XTM One tells every callback of the task that filed a ticket when
# that ticket changes status (``app.a2a.ticket_push``, the sending half). The
# delegation that filed it has long concluded by then, so the delegation path
# above refuses it. This half authenticates the push against the same
# per-delegation secret, checks it names that delegation's partner task, and
# raises a platform event: ids and statuses only, never acted on as an answer.

#: The discriminator the sending half writes; a task push carries none.
TICKET_PUSH_KIND = "work_item.status_changed"

#: The statuses a ticket push may announce (the sender's ``NOTIFIED_STATUSES``).
TICKET_STATUSES = frozenset({"open", "in_progress", "blocked", "done", "cancelled"})


def is_ticket_push(body: object) -> bool:
    """Whether *body* is a ticket status push rather than a task callback. Pure."""
    return isinstance(body, dict) and body.get("kind") == TICKET_PUSH_KIND


def ticket_push_fields(body: object, row: A2ADelegation) -> dict[str, str | None] | None:
    """The work item id and statuses of a ticket push for *row*, else ``None``. Pure.

    Nothing in the body is trusted beyond what is checked: the task it names
    must be the partner task this delegation submitted, the work item id must
    parse as one, and the status must be one the sender announces.
    """
    if not isinstance(body, dict) or not is_ticket_push(body) or not row.partner_task_id:
        return None
    if str(body.get("taskId") or "") != str(row.partner_task_id):
        return None
    try:
        work_item_id = str(uuid.UUID(str(body.get("workItemId"))))
    except (TypeError, ValueError, AttributeError):
        return None
    status = body.get("status")
    previous = body.get("previousStatus")
    if status not in TICKET_STATUSES:
        return None
    if previous not in TICKET_STATUSES and previous != "claimed":
        previous = None
    return {"work_item_id": work_item_id, "status": status, "previous_status": previous}


async def ticket_push_delegation(db: AsyncSession, delegation_id: str) -> A2ADelegation | None:
    """The delegation a ticket push may name, whatever its outcome, or ``None``.

    Unlike :func:`addressed_delegation`, a concluded delegation is admitted:
    a ticket outlives the task that filed it. The secret check that follows is
    the same, and nothing is resumed, so the one-shot property the delegation
    path protects is not at stake: a replayed push raises the same deduped
    event and discloses nothing.
    """
    try:
        parsed = uuid.UUID(str(delegation_id))
    except (ValueError, AttributeError, TypeError):
        return None
    found: A2ADelegation | None = (
        await db.execute(select(A2ADelegation).where(A2ADelegation.id == parsed))
    ).scalar_one_or_none()
    return found


async def publish_ticket_status(db: AsyncSession, row: A2ADelegation, fields: dict[str, str | None]) -> str | None:
    """Raise ``a2a.ticket.status_changed`` for an authenticated push. Never raises."""
    from app.services import platform_events

    try:
        partner = _declared_name(await _target_of(db, row.target_id))
        return await platform_events.publish(
            platform_events.A2A_TICKET_STATUS_CHANGED,
            platform_events.a2a_ticket_status_data(
                delegation_id=row.id,
                agent_id=row.agent_id,
                user_id=row.user_id,
                run_id=row.run_ref,
                partner=partner,
                partner_task_id=row.partner_task_id,
                work_item_id=str(fields["work_item_id"]),
                status=str(fields["status"]),
                previous_status=fields.get("previous_status"),
            ),
            # The sender pushes once per change; a retry or a replay of the same
            # change starts one run.
            event_id=f"a2a-ticket:{row.id}:{fields['work_item_id']}:{fields['status']}",
        )
    except Exception:
        logger.warning(
            "a2a.client: could not raise a2a.ticket.status_changed for delegation %s",
            row.id,
            exc_info=True,
        )
        return None
