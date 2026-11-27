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

"""External task lifecycle: create, read, cancel — plus the stale-run reaper.

An external task does not carry its own state. It references the
``AssignmentRun`` that executes it and projects that run's status through
``protocol.run_status_to_state``. Two copies of "what state is this task in"
would eventually disagree, and the partner-visible copy is precisely the one
that must not drift.

Runs are created with ``assignment_id = NULL``. That is an established shape
here, not a workaround: the platform already creates assignment-less runs for
delegation chains started from a chat context.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.a2a import protocol as proto
from app.a2a.audit import audit_actor
from app.core.config import settings
from app.core.redaction import known_secrets_scoped
from app.core.utils import safe_uuid
from app.core.worker_shutdown import TurnWoundDown
from app.models.a2a import A2AExposure, A2ATask
from app.models.agent import Agent
from app.models.assignment import AssignmentRun

logger = logging.getLogger(__name__)

REJECT_CONTENT_TYPE = "content_type_not_supported"
REJECT_NO_CONTENT = "no_readable_content"

#: ``AssignmentRun.status`` values that mean "not finished". Named once so a
#: guard cannot silently disagree with the projection in ``protocol``.
_ACTIVE_RUN_STATUSES: tuple[str, ...] = ("pending", "delayed", "running")

#: The terminal write is the one write on the executor's path whose loss is
#: partner-visible — the answer already exists and has already been charged for,
#: and dropping it leaves the reaper to report "abandoned" a whole lease later.
#: Retried a bounded number of times before giving up to the reaper.
_TERMINAL_WRITE_RETRIES = 3
_TERMINAL_WRITE_BACKOFF_SECONDS = 0.5

#: Machine-readable marker prefixing ``AssignmentRun.error`` when an external
#: run failed because the run-as user's usage allowance was exhausted.
#: ``failure_detail`` projects it into the failed task's status message, and
#: ``execute_task_run`` routes the FR-036 audit entry on it.
QUOTA_EXHAUSTED_ERROR_PREFIX = "quota_exhausted"

#: How many times one task may ask its caller for a detail. Two agents can
#: ask each other for precision indefinitely, and neither gets bored: at the
#: bound the tool is simply not offered, so the agent answers with what it
#: has. Three is room for a genuine misunderstanding and not for a habit.
_MAX_CLARIFICATION_ROUNDS = 3

#: How an external task reaches the agent: a consult by the partner agent,
#: whose answer goes back to it (``format_handover_context_prompt``). The
#: partner is not named: its registration's name is the operator's label, and
#: nothing it says about itself is verified.
_PARTNER_HANDOVER_CONTEXT = {"type": "consult", "source_agent": "an external partner agent over A2A"}

#: The stored failure reason for a run that ran out of wall clock. Fixed text
#: because it reaches the partner verbatim (see
#: :data:`_PARTNER_VISIBLE_ERROR_PREFIXES`); the loop's own message is an
#: apology and its elapsed figure an internal budget.
TIMEOUT_ERROR_TEXT = "External task exceeded its execution budget"

#: The reason a task hits the total-lifetime bound rather than the lease. A task
#: waiting on delegated work is deliberately not reaped as abandoned, so this is
#: the only thing that bounds one whose work never finishes.
LIFETIME_ERROR_TEXT = "External task exceeded its maximum lifetime"

#: What a background task of the run reads when the partner cancelled the
#: A2A task: the operator sees who stopped it, not a deletion that never was.
PARTNER_CANCEL_TASK_REASON = (
    "Cancelled because the A2A partner that submitted its run cancelled the task."
)
#: The same, for a task stopped by the total-lifetime bound.
LIFETIME_CANCEL_TASK_REASON = (
    "Cancelled because its A2A task exceeded its maximum lifetime."
)

#: The reason a task fails with when a platform stop (a deployment, a
#: scale-in, a node drain, a worker recycle) cut its run. Fixed text,
#: read by the partner: the partner did not cancel it, so it is never
#: reported ``canceled``, and it is failed at the cut, not a lease later.
INTERRUPTED_ERROR_TEXT = "External task interrupted by a platform restart"

#: The reason of a task whose run met a pool checkout timeout (#2561): the
#: platform was under load, the task can be sent again as is. Never the
#: driver's QueuePool text, which names internal pool sizes.
BUSY_ERROR_TEXT = "External task busy: the platform is under load. Send the task again in a few seconds."


class TaskRejectedError(ValueError):
    """Submission refused before any run started."""

    def __init__(self, reason: str, message: str):
        super().__init__(message)
        self.reason = reason


def _dedup_key(client_id: str, message_id: str) -> str:
    return f"a2a:dedup:{client_id}:{message_id}"


async def _release_message_id(client_id: str, message_id: str) -> None:
    """Drop a claim taken for a submission that was rejected before running.

    The fallthrough in ``create_task`` copes without this - it re-checks the
    database when a claim fails - but leaving the key set would make every
    corrected retry pay a wasted round trip and keep a meaningless key alive
    for the whole dedup window.
    """
    from app.core.redis import redis_client

    try:
        redis = await redis_client()
        await redis.delete(_dedup_key(client_id, message_id))
    except Exception:  # a stale claim is harmless; the fallthrough covers it
        return


async def _claim_message_id(client_id: str, message_id: str) -> bool:
    """Atomically claim a message id. False means it was already claimed.

    ``SET NX`` rather than GET-then-SET: two concurrent retries of the same
    submission must not both pass the check and start two billable runs.

    A Redis failure returns True (claim granted) because the unique constraint
    on ``(client_id, message_id)`` is the real guarantee — this is the fast
    path, not the correctness boundary. Failing closed here would refuse
    legitimate first-time submissions during a Redis blip.
    """
    from app.core.redis import redis_client

    try:
        redis = await redis_client()
        return bool(
            await redis.set(
                _dedup_key(client_id, message_id),
                "1",
                nx=True,
                ex=settings.a2a_dedup_window_seconds,
            )
        )
    except Exception:  # any Redis failure defers to the unique constraint
        return True


def validate_submission(parts: list[dict[str, Any]]) -> str:
    """Validate inbound content, returning the flattened text. Raises TaskRejectedError.

    Refused BEFORE a run starts so the partner learns the shape is wrong
    without being charged for an agent run that was never going to work.
    """
    if not parts:
        raise TaskRejectedError(REJECT_NO_CONTENT, "Message carried no parts")

    chunks: list[str] = []
    for part in parts:
        kind, value = proto.read_part(part)
        if kind == "text":
            chunks.append(str(value))
        elif kind == "data":
            import json

            chunks.append(json.dumps(value, ensure_ascii=False))
        elif kind in ("file", "raw"):
            raise TaskRejectedError(
                REJECT_CONTENT_TYPE,
                "This agent accepts text and structured data only",
            )
        else:
            raise TaskRejectedError(REJECT_CONTENT_TYPE, "Unrecognised content part")

    if not chunks:
        raise TaskRejectedError(REJECT_NO_CONTENT, "Message carried no readable content")

    # Bounded here rather than only at the HTTP edge: this is the value that is
    # persisted verbatim in ``A2ATask.inbound_parts`` and handed to the executor
    # as the prompt, and it is the only cap a chunked request (which carries no
    # ``Content-Length`` for the edge check to read) has to clear. The rate
    # limits bound how MANY submissions a partner makes, never how large each
    # one is, so without this one registration can persist multi-MB rows and
    # push arbitrarily long prompts inside its normal budget.
    flattened = "\n\n".join(chunks)
    if len(flattened) > settings.a2a_max_message_chars:
        raise TaskRejectedError(
            REJECT_CONTENT_TYPE,
            f"Message exceeds the {settings.a2a_max_message_chars}-character limit",
        )
    return flattened


# Output shape is enforced in ``build_artifacts``, which emits a structured
# artifact only for an agent configured to produce one - the same fact the
# card advertises. There is nothing to check on the way in: the protocol
# carries no "give me structured output" flag for a partner to set.


async def find_existing_task(
    db: AsyncSession, client_id: str, message_id: str
) -> A2ATask | None:
    """Find a prior task for this message id, ignoring rejected ones.

    Rejected submissions are deliberately excluded. Deduplication exists to
    stop a partner being charged twice when a network failure leaves them
    unsure whether their submission landed - a case where they cannot know
    what happened. A content rejection is the opposite: the partner was told
    exactly what was wrong and is expected to fix it and re-send. Letting a
    rejection consume the message id would answer that corrected submission
    with the stale rejection, describing content the partner has already
    replaced.

    A *failed* task is still returned. That one is a real result for that
    message, and re-running it needs a new message id - the same contract
    idempotency keys have everywhere else.
    """
    return (
        await db.execute(
            select(A2ATask).where(
                A2ATask.client_id == client_id,
                A2ATask.message_id == message_id,
                A2ATask.rejected_reason.is_(None),
            )
        )
    ).scalar_one_or_none()


class TaskNotAskingError(Exception):
    """A continuation named a task that is not waiting on its caller."""


async def answer_question(
    db: AsyncSession,
    *,
    task: A2ATask,
    exposure,
    agent,
    client_id: str,
    provider: str | None,
    message_id: str,
    parts: list,
) -> AssignmentRun:
    """Continue *task* with the caller's answer. Returns the new run.

    A2A continues the SAME task: the client answered what was asked, and a
    second task would make one exchange look like two to everything that
    counts them. The run is new because the one that asked is over — nothing
    was parked, so there is nothing to resume — and ``run_id`` moves to it, so
    the projection follows the work.

    The question and the answer travel as history rather than being pasted
    into the message. An external run starts with none, so without this the
    agent would be answered without remembering what it asked.
    """
    if not task.caller_question:
        raise TaskNotAskingError("This task is not waiting on an answer.")

    answer = validate_submission(parts)
    if message_id != task.message_id and not await _claim_message_id(
        client_id, message_id
    ):
        # ``(client_id, message_id)`` is unique across live tasks, so reusing
        # one another task already holds would fail the flush below on the
        # constraint — a 500, with the session poisoned, on a surface where
        # every other partner-supplied value gets a protocol error. Claimed the
        # same way ``create_task`` claims it, so an answer and a submission
        # cannot take the same id either.
        raise TaskRejectedError(
            "duplicate_message_id",
            "That messageId belongs to another task; use a new one.",
        )
    from app.a2a.run_exchange import ANSWERED_BY_RUN_KEY, PREVIOUS_RUN_KEY

    asked = task.caller_question
    original = ""
    prior = None
    if task.run_id is not None:
        prior = (
            await db.execute(
                select(AssignmentRun).where(AssignmentRun.id == task.run_id)
            )
        ).scalar_one_or_none()
        if prior is not None:
            original = str((prior.trigger_payload or {}).get("message") or "")

    run = AssignmentRun(
        assignment_id=None,
        agent_id=agent.id,
        user_id=exposure.run_as_user_id,
        status="pending",
        trigger_payload={
            "a2a_message_id": message_id,
            "message": answer,
            # What the executor rebuilds the exchange from.
            "a2a_prior_message": original,
            "a2a_question": asked,
            # The run that asked: the run pages link the two halves of one
            # exchange (``run_exchange``), since ``run_id`` keeps only the last.
            **({PREVIOUS_RUN_KEY: str(prior.id)} if prior is not None else {}),
        },
        trigger_context={
            "source": "a2a",
            "client_id": client_id,
            **({"a2a_provider": provider} if provider else {}),
        },
    )
    db.add(run)
    await db.flush()

    # Cleared with the same flush that repoints the task: a poll between the
    # two would otherwise see a task still asking a question nobody will
    # answer again, or one answering with the run that asked.
    task.caller_question = None
    task.run_id = run.id
    task.message_id = message_id
    task.inbound_parts = parts
    # The run that asked marked the task reaped when it finalised — finished
    # work is not abandoned work. This one is live again, so the lease reaper
    # and the total lifetime bound have to be able to see it.
    task.reaped = False
    task.lease_heartbeat_at = datetime.now(UTC)
    await db.flush()
    if prior is not None:
        # The other direction of the same link, flushed after the task row so
        # the locks are taken task first, run second, as the reaper takes them.
        # Reassigned, not mutated: the column is plain JSON, which tracks no
        # in-place change.
        prior.trigger_payload = {
            **(prior.trigger_payload or {}),
            ANSWERED_BY_RUN_KEY: str(run.id),
        }
        await db.flush()
    return run


async def create_task(
    db: AsyncSession,
    *,
    exposure: A2AExposure,
    agent: Agent,
    client_id: str,
    message_id: str,
    parts: list[dict[str, Any]],
    provider: str | None = None,
    context: Any = None,
    version: str | None = None,
    queued: bool = False,
) -> tuple[A2ATask, bool]:
    """Create an external task. Returns ``(task, created)``.

    ``queued`` marks a follow-up submitted while its conversation still had a
    task open: the executor holds its run until every task ahead of it in the
    context is finished (``_wait_for_context_turn``).

    ``created is False`` means this was a duplicate submission and the caller
    is being handed the task the first one created — no second run starts.

    ``provider`` is the A2A provider whose credential form authenticated the
    submission, from ``a2a_server._authenticate``. It is stamped on the run
    because it cannot be recovered afterwards: a federated caller on a legacy
    exposure reuses that exposure's OAuth-shaped registration id deliberately
    (one partner, one budget), so the id's shape reports Foundry traffic as
    OAuth traffic. Optional, and ``None`` on the paths that do not know —
    callers reading it back must treat absence as "derive from the id", which
    is what any run carrying no stamped provider needs anyway.
    """
    existing = await find_existing_task(db, client_id, message_id)
    if existing is not None:
        return existing, False

    if not await _claim_message_id(client_id, message_id):
        existing = await find_existing_task(db, client_id, message_id)
        if existing is not None:
            return existing, False

    message_text = validate_submission(parts)

    run = AssignmentRun(
        assignment_id=None,  # assignment-less: an established shape here
        agent_id=agent.id,
        # The run belongs to the same identity that executes it, matching
        # ``assignment_engine``'s ``user_id=run_as_user_id or user_id``. Never
        # ``agent.user_id``: for a company-managed agent that is the seeding
        # administrator, and quota, run visibility and the activity feed would
        # all misattribute the work to them.
        user_id=exposure.run_as_user_id,
        status="pending",
        # The validated, flattened message travels on the run itself so the
        # executor needs no back-reference to the A2A layer to know what to do.
        trigger_payload={
            "a2a_message_id": message_id,
            "message": message_text,
            **({"a2a_queued": True} if queued else {}),
        },
        # ``client_id`` is the registration the call belongs to — its rate
        # budget, its task scope. ``a2a_provider`` is the credential form that
        # was actually presented, which the id cannot always express. Both are
        # recorded because they answer different questions and, on a legacy
        # exposure, genuinely differ.
        trigger_context={
            "source": "a2a",
            "client_id": client_id,
            **({"a2a_provider": provider} if provider else {}),
        },
    )
    db.add(run)
    await db.flush()

    if context is None:
        # A submission carrying no identifier starts a conversation. One
        # carrying an issued one arrives here with it already resolved — the
        # route resolves it, because refusing an unissued one is a protocol
        # answer rather than a creation decision.
        from app.a2a import contexts as ctx_svc

        context = await ctx_svc.mint(
            db,
            client_id=client_id,
            agent_id=agent.id,
            exposure_id=exposure.id,
        )

    task = A2ATask(
        exposure_id=exposure.id,
        client_id=client_id,
        run_id=run.id,
        message_id=message_id,
        inbound_parts=parts,
        lease_heartbeat_at=datetime.now(UTC),
        context_id=context.id,
        protocol_version=version,
    )
    db.add(task)
    try:
        await db.flush()
    except IntegrityError:
        # The unique (client_id, message_id) index fired: a concurrent
        # submission of the same message won the race. Reachable precisely
        # when Redis is unavailable (``_claim_message_id`` then returns True
        # for both callers and defers to this constraint), so it must resolve
        # to the winner's task - raising would fail the duplicate during
        # exactly the outage the constraint exists to cover.
        await db.rollback()
        # The rollback expired every instance in this session, and the caller
        # still reads ``exposure``/``agent`` attributes for its audit and
        # metrics — plain attribute access on an expired object in an async
        # session raises instead of lazy-loading, so reload both eagerly.
        await db.refresh(exposure)
        await db.refresh(agent)
        existing = await find_existing_task(db, client_id, message_id)
        if existing is not None:
            return existing, False
        raise
    return task, True


async def create_rejected_task(
    db: AsyncSession,
    *,
    exposure: A2AExposure,
    client_id: str,
    message_id: str,
    parts: list[dict[str, Any]],
    reason: str,
) -> A2ATask:
    """Record a submission refused before any run started.

    Gets a context like any other task: the foreign key admits no orphan, and a
    partner that fixes its payload and re-sends is continuing the same
    conversation.
    """
    from app.a2a import contexts as ctx_svc

    context = await ctx_svc.mint(
        db,
        client_id=client_id,
        agent_id=exposure.agent_id,
        exposure_id=exposure.id,
    )
    task = A2ATask(
        exposure_id=exposure.id,
        client_id=client_id,
        run_id=None,
        message_id=message_id,
        inbound_parts=parts,
        rejected_reason=reason,
        context_id=context.id,
    )
    db.add(task)
    await db.flush()
    # The partner is expected to fix the payload and re-send under the same
    # message id, so the claim must not outlive the rejection.
    await _release_message_id(client_id, message_id)
    return task


async def get_task(
    db: AsyncSession, task_id: uuid.UUID, client_id: str
) -> A2ATask | None:
    """Read a task, scoped to its creating client.

    The ``client_id`` predicate is in the QUERY rather than checked after
    loading, so there is no branch in which another client's row is in memory
    and could leak through a logging statement or an error message.
    """
    return (
        await db.execute(
            select(A2ATask).where(
                A2ATask.id == task_id, A2ATask.client_id == client_id
            )
        )
    ).scalar_one_or_none()


async def live_delegated_work(db: AsyncSession, run_id: uuid.UUID) -> bool:
    """Whether *run_id* still has background work running.

    The linkage is the run id on the child's ``metadata_json``, which is the
    only correlation between the two tables and the one
    ``mark_sub_agent_tasks_cancelled_for_runs`` already matches on. Both keys
    are checked for the same reason it checks both: a child records its own
    child run under ``assignment_run_id`` and the run that spawned it under
    ``parent_run_id``.

    Called on every ``tasks/get``, which is why the migration adds a partial
    index over exactly this expression and status set.
    """
    from sqlalchemy import or_

    from app.models.sub_agent_task import SubAgentTask

    rows = (
        await db.execute(
            select(SubAgentTask.id)
            .where(
                SubAgentTask.status.in_(("pending", "running")),
                or_(
                    SubAgentTask.metadata_json["parent_run_id"].astext
                    == str(run_id),
                    SubAgentTask.metadata_json["assignment_run_id"].astext
                    == str(run_id),
                ),
            )
            .limit(1)
        )
    ).scalars().all()
    return bool(rows)


async def resolve_state(db: AsyncSession, task: A2ATask) -> proto.A2AState:
    """Project the task's current A2A state from the run it mirrors.

    Folds in the work that run delegated: a run can finish while the work it
    handed to a peer, or to a copy of itself, is still going, and the partner
    holds only this task. Neither fact is stored — one source of truth per
    ``006-a2a-server-inbound``'s data model — so this reads both.

    The task row is read again, with its run, in one statement. The
    terminal write commits both rows together, and a task loaded before that
    commit interpreted with a run read after it answered ``completed`` with
    no ``respondingAgent``, or with no question for a run that ended by
    asking one. The caller's task is refreshed in place, so what it reads
    afterwards is what the state was decided on.
    """
    if task.rejected_reason:
        return proto.A2AState.REJECTED
    if task.run_id is None:
        return proto.A2AState.SUBMITTED
    row = (
        await db.execute(
            select(A2ATask, AssignmentRun)
            .outerjoin(AssignmentRun, AssignmentRun.id == A2ATask.run_id)
            .where(A2ATask.id == task.id)
            # ``populate_existing``, because the caller's session already holds
            # the task, and may hold this run — the send that created it does.
            # A plain re-SELECT returns the identity-mapped instances and
            # DISCARDS the fresher rows it just read, so a poll would keep
            # reporting what this session first saw, never the executor's.
            .execution_options(populate_existing=True)
        )
    ).one_or_none()
    run = row[1] if row is not None else None
    if run is None:
        # The run vanished, or the task with it. Fail closed rather than
        # reporting "working" forever for something nothing is executing.
        return proto.A2AState.FAILED
    if task.cancel_requested_at and run.status not in (
        "completed",
        "failed",
        "cancelled",
    ):
        return proto.A2AState.CANCELED
    state = proto.run_status_to_state(run.status)
    # A run that ended by asking its caller a question did not finish the
    # task; the task is blocked on the client, which is what A2A's
    # ``input-required`` means. Checked before delegated work, because a run
    # that asked has stopped and has nothing outstanding.
    if state in proto.TERMINAL_STATES and task.caller_question:
        # A cancel withdraws the question with the task. The run that asked is
        # already terminal, so the guard above cannot see this one — and a
        # question nobody will ever answer is not a live task.
        return (
            proto.A2AState.CANCELED
            if task.cancel_requested_at
            else proto.A2AState.INPUT_REQUIRED
        )
    # A terminal run whose delegated work is still running is not a terminal
    # task. Asked only when the run itself has finished: while it is running
    # the answer cannot change the state, and this is a polling method.
    if (
        state in proto.TERMINAL_STATES
        and not task.cancel_requested_at
        and await live_delegated_work(db, task.run_id)
    ):
        return proto.A2AState.WORKING
    return state


async def wait_until_terminal(db: AsyncSession, task: A2ATask) -> None:
    """Hold a blocking send until *task* stops moving, or time out.

    Polls the same projection ``tasks/get`` serves, so a blocking caller and
    a polling caller can never see different truths. The loop works across
    replicas — the run may be executing anywhere, and ``resolve_state``
    reloads the task and its run with ``populate_existing`` so each poll
    observes the executor's committed progress, and a cancel committed by
    another request, rather than the copy this session already holds. Only
    those two rows: the caller's ``agent`` and ``exposure`` stay loaded and
    usable after the wait.

    Each poll ends its transaction before sleeping, so the wait does not hold a
    pooled connection idle-in-transaction for its whole duration (at the create
    limit one partner would otherwise occupy the pool).

    It ends it with ``commit``, NOT ``rollback``, and the difference is not
    stylistic. The caller has ``agent`` and ``exposure`` loaded in this session
    and reads them after the wait to render the response; ``rollback`` expires
    every instance, and plain attribute access on an expired object in an async
    session raises rather than lazy-loading — so rolling back here turned every
    blocking send into a 500. ``commit`` on a session configured
    ``expire_on_commit=False`` releases the connection and expires nothing.

    On timeout it simply returns: the caller renders the task in whatever
    state it reached, which is exactly what an immediate return would have
    carried — a capped wait degrades to today's behaviour, never to an
    error. The run lease (``a2a_run_lease_seconds``) bounds the true worst
    case; this cap only bounds how long one HTTP connection is held open,
    and the create rate limit (10/min per registration) bounds how many can
    be held at once.

    The caller must have committed, or be willing to commit, its own work before
    calling: the loop commits between polls. The send handler commits the task
    row before it dispatches the run, so there is nothing pending by then.
    """
    import asyncio
    import time

    from app.core.config import settings

    deadline = time.monotonic() + settings.a2a_blocking_send_timeout_seconds
    while True:
        state = await resolve_state(db, task)
        if (
            state in proto.TERMINAL_STATES
            # A2A's blocking contract is "until an interrupted or terminal
            # state", and a task asking its caller is interrupted: nothing will
            # move it until the caller answers, so waiting out the cap only
            # delays the question. Worst for the client the rest of this module
            # reasons about — v1.0 blocks by default and does not poll.
            or state is proto.A2AState.INPUT_REQUIRED
            or time.monotonic() >= deadline
        ):
            return
        # End the transaction BEFORE sleeping, not after: the point is to not
        # hold a pooled connection idle-in-transaction across the sleep. See the
        # docstring for why this is a commit and must never become a rollback.
        await db.commit()
        await asyncio.sleep(settings.a2a_blocking_poll_interval_seconds)


async def build_artifacts(
    db: AsyncSession, task: A2ATask, agent: Agent, version: str
) -> list[dict[str, Any]]:
    """Project a completed run's output into A2A artifacts.

    Without this a partner receives a task that reaches COMPLETED carrying no
    result — technically a valid lifecycle, and useless.

    Structured data is emitted only for an agent configured to produce it,
    matching what the card advertises; synthesising it for an agent that is
    not would make the card a lie.

    A transferred answer carries ``metadata.respondingAgent``: otherwise the
    partner reads a peer's work as the answer of the agent whose card it
    validated against. The peer's name is disclosed deliberately - reaching
    that peer is the operator's own binding decision (``a2a.exposure``).
    """
    if task.run_id is None:
        return []
    run = (
        await db.execute(select(AssignmentRun).where(AssignmentRun.id == task.run_id))
    ).scalar_one_or_none()
    if run is None or not run.result_summary:
        return []

    produces_structured = (agent.output_format or "").lower() == "json"
    parts: list[dict[str, Any]] = []
    if produces_structured:
        import json

        try:
            parsed = json.loads(run.result_summary)
        except (ValueError, TypeError):
            parsed = None
        # A DataPart's value must be a JSON OBJECT in both wires (v0.3 types it
        # so; v1.0 parses it as a proto Struct), but a JSON-format agent may
        # legitimately return an array or a scalar root — the schema emitters
        # admit both. Parsing successfully is therefore not enough to emit
        # structured data: a non-object root takes the text fallback, or the
        # strict parser rejects the whole response and the answer never lands.
        if isinstance(parsed, dict):
            parts.append(proto.data_part(parsed, version))
        else:
            # Configured for JSON but did not produce an object. Return the text
            # rather than a malformed structured artifact.
            parts.append(proto.text_part(run.result_summary, version))
    else:
        parts.append(proto.text_part(run.result_summary, version))

    # ``artifactId``/``name``, not ``id``/``title``. Both protocol versions
    # agree here - v0.3's ``Artifact`` requires ``artifactId`` and labels with
    # ``name``, and v1.0's is the same shape - so this is one of the few
    # cross-wire values that needs no version branch. Any other key fails a
    # partner's deserialisation of the one payload on this surface that
    # carries the actual answer.
    artifact: dict[str, Any] = {
        "artifactId": str(uuid.uuid4()),
        "name": "result",
        "parts": parts,
    }
    # ``metadata`` is an optional free-form object on Artifact in both wires:
    # no per-version spelling, and a partner ignoring it is unaffected.
    artifacts = [artifact]
    # Work the run delegated is the partner's result too, and the partner holds
    # no identifier for a child. One artifact each, attributed to the agent that
    # ran it, so a reader can tell the pipeline's stages apart.
    artifacts.extend(await _delegated_artifacts(db, task, version))

    if task.responding_agent_id and task.responding_agent_id != agent.id:
        responder = (
            await db.execute(
                select(Agent.name).where(Agent.id == task.responding_agent_id)
            )
        ).scalar_one_or_none()
        artifact["metadata"] = {
            "respondingAgent": {
                "id": str(task.responding_agent_id),
                # Omitted for a deleted agent; the id still attributes it.
                **({"name": responder} if responder else {}),
            }
        }
    return artifacts


async def _delegated_artifacts(
    db: AsyncSession, task: A2ATask, version: str
) -> list[dict[str, Any]]:
    """One artifact per finished child of this task's run.

    Only finished children: an in-flight one has nothing to project, and
    ``resolve_state`` is keeping the task non-terminal on its behalf. A child
    that failed contributes nothing either — its own error is not a partner
    -visible string (the whitelist in ``failure_detail`` is), and the task's
    status message is where a failure belongs.
    """
    if task.run_id is None:
        return []
    from sqlalchemy import or_

    from app.models.sub_agent_task import SubAgentTask

    children = (
        await db.execute(
            select(SubAgentTask)
            .where(
                SubAgentTask.status == "completed",
                or_(
                    SubAgentTask.metadata_json["parent_run_id"].astext
                    == str(task.run_id),
                    SubAgentTask.metadata_json["assignment_run_id"].astext
                    == str(task.run_id),
                ),
            )
            .order_by(SubAgentTask.completed_at)
        )
    ).scalars().all()

    out: list[dict[str, Any]] = []
    for child in children:
        if not child.result:
            continue
        name = (
            await db.execute(
                select(Agent.name).where(Agent.id == child.target_agent_id)
            )
        ).scalar_one_or_none()
        out.append(
            {
                "artifactId": str(uuid.uuid4()),
                "name": "delegated-result",
                "parts": [proto.text_part(child.result, version)],
                "metadata": {
                    "respondingAgent": {
                        "id": str(child.target_agent_id),
                        **({"name": name} if name else {}),
                    }
                },
            }
        )
    return out


#: The only stored failure reasons projected verbatim to an external partner.
#: A WHITELIST, because ``run.error`` otherwise holds ``str(exc)`` from any
#: executor exception — sqlalchemy errors embed the SQL statement and its
#: parameters, httpx errors name internal hostnames — and the failed task's
#: status message is the one place on this surface where an uncurated internal
#: string could cross the trust boundary. Everything else maps to a generic
#: line; FR-036 only needs exhaustion to be machine-distinguishable.
_PARTNER_VISIBLE_ERROR_PREFIXES = (
    QUOTA_EXHAUSTED_ERROR_PREFIX,
    "External task abandoned",  # the reaper's own fixed text
    "External task exceeded",  # TIMEOUT_ERROR_TEXT, LIFETIME_ERROR_TEXT
    "External task interrupted",  # INTERRUPTED_ERROR_TEXT
    "Exposure revoked",  # the executor's revoked-mid-flight text
    "External task busy",  # BUSY_ERROR_TEXT (#2561)
)

#: The prefix the stale-run sweep writes on a run it fails
#: (``app.services.autonomous.STALE_ASSIGNMENT_CLEANUP_ERROR_PREFIX``, pinned
#: equal by a test). Its text names internal timings, so the partner gets
#: :data:`ORPHANED_ERROR_TEXT` instead of the generic line.
_STALE_RUN_CLEANUP_PREFIX = "Run marked as failed by stale-assignment-run cleanup."
#: What a partner reads when the run serving its task stopped (a restart or a
#: crash) and the stale-run sweep failed it.
ORPHANED_ERROR_TEXT = (
    "External task interrupted: the run serving it stopped (platform restart or "
    "crash) and was failed by the stale-run cleanup"
)


async def failure_detail(db: AsyncSession, task: A2ATask) -> str | None:
    """The stored failure reason for a FAILED task, curated, or ``None``.

    Projected into the task's ``status.message`` so a partner can tell quota
    exhaustion from a crash — FR-036 requires exhaustion specifically to be
    machine-distinguishable. Only reasons this feature wrote itself cross
    verbatim (see :data:`_PARTNER_VISIBLE_ERROR_PREFIXES`); an arbitrary
    exception string is reduced to a generic line.
    """
    if task.run_id is None:
        return None
    run = (
        await db.execute(select(AssignmentRun).where(AssignmentRun.id == task.run_id))
    ).scalar_one_or_none()
    if run is None:
        return "Execution record lost"
    if not run.error:
        return None
    if run.error.startswith(_PARTNER_VISIBLE_ERROR_PREFIXES):
        return run.error
    if run.error.startswith(_STALE_RUN_CLEANUP_PREFIX):
        return ORPHANED_ERROR_TEXT
    return "Task execution failed"


async def _audit_quota_exhaustion(
    db: AsyncSession,
    *,
    exposure: A2AExposure,
    agent_name: str,
    task_id: uuid.UUID,
    client_id: str,
    detail: str,
) -> None:
    """FR-036's audit half: exhaustion is logged naming the client responsible.

    Anchored on the run-as user (NOT NULL, and the account whose allowance was
    consumed) with the client id in the email slot and the details, matching
    the submission audit in ``a2a_server``. Best-effort: a failed audit write
    must not mask the task's own terminal state, which is already committed.
    """
    from app.services.audit import log_audit

    try:
        await log_audit(
            db,
            user=audit_actor(exposure.run_as_user_id, client_id),
            action="reject",
            entity_type="a2a_task",
            entity_id=task_id,
            entity_name=agent_name,
            summary=(
                f"External A2A task from client {client_id} on '{agent_name}' "
                f"failed: usage allowance of the run-as user exhausted"
            ),
            details={"client_id": client_id, "reason": detail[:500]},
        )
        await db.commit()
    except Exception:
        logger.warning(
            "Failed to audit A2A quota exhaustion for task %s", task_id, exc_info=True
        )


async def announce_state(db: AsyncSession, task: A2ATask) -> None:
    """Tell a partner the state its task now has.

    Called by every path that ends a task, and by the sweep that catches the
    one nothing was executing to see. Call AFTER the transition is committed,
    like every other publish here.

    The state is read rather than passed, so it cannot disagree with what
    ``tasks/get`` would answer a moment later — which is the whole reason a
    run's terminal write goes through here: a run can finish with delegated
    work still live, and the task is ``working`` until that finishes too.

    A terminal state is claimed before anything is sent, so two replicas that
    both notice the same transition send one notification rather than two.

    Never raises. A callback is an accelerator whose floor is the partner
    polling.
    """
    from app.a2a import push as push_svc

    try:
        resolved = await resolve_state(db, task)
        claimed = False
        if resolved in proto.TERMINAL_STATES:
            if not await _claim_announcement(db, task):
                return
            claimed = True
        # In the partner's own vocabulary: the two versions spell a state
        # differently, and one branching on it to decide whether to read the
        # task would never branch on the other's spelling.
        state = proto.state_wire(
            resolved, task.protocol_version or proto.PROTOCOL_VERSION_0_3
        )
        if not await push_svc.notify(db, task, state) and claimed:
            # Nothing took it and the budget is not spent, so the claim is
            # given back for the sweep to try again. Two replicas can then
            # both retry, which is the trade: a duplicate notification costs
            # the partner an extra read, and a dropped one costs it the answer.
            await _release_announcement(db, task)
    except Exception:
        logger.warning(
            "A2A state announcement failed for task %s", task.id, exc_info=True
        )


async def announce_state_by_id(task_id: uuid.UUID) -> None:
    """Announce a task's state in a session of its own.

    For the request paths. Delivery is bounded per callback and not per task,
    so awaiting it inline lets one partner's dead endpoint hold that partner's
    own response for every callback it registered — and holds the request's
    database connection for the same span. FR-021: a notification never
    blocks, delays or changes the task's outcome.

    Never raises, like ``announce_state`` itself, and for the same reason.
    """
    from app.core.database import async_session

    try:
        async with async_session() as own_db:
            task = (
                await own_db.execute(select(A2ATask).where(A2ATask.id == task_id))
            ).scalar_one_or_none()
            if task is not None:
                await announce_state(own_db, task)
    except Exception:
        logger.warning(
            "A2A detached state announcement failed for task %s",
            task_id,
            exc_info=True,
        )


async def _release_announcement(db: AsyncSession, task: A2ATask) -> None:
    """Give back a claim whose notification nobody took.

    Committed immediately, like the claim: ``announce_missed`` selects on this
    column from its own transaction.
    """
    await db.execute(
        update(A2ATask)
        .where(A2ATask.id == task.id)
        .values(terminal_announced_at=None)
    )
    await db.commit()


async def _claim_announcement(db: AsyncSession, task: A2ATask) -> bool:
    """Stamp the task unless someone already has. True when this caller won.

    Conditional on the stamp being absent, so the claim is the write rather
    than a read followed by one. Committed immediately: a peer deciding whether
    to announce reads this from its own transaction.
    """
    won = bool(
        (
            await db.execute(
                update(A2ATask)
                .where(
                    A2ATask.id == task.id,
                    A2ATask.terminal_announced_at.is_(None),
                )
                .values(terminal_announced_at=datetime.now(UTC))
            )
        ).rowcount
    )
    await db.commit()
    return won


async def announce_missed(db: AsyncSession, *, limit: int = 50) -> int:
    """Announce terminal states whose transition nothing was executing to see.

    A run can finish while work it delegated is still running: the task stays
    ``working``, and by the time the child finishes there is no execution left
    to notice. Nothing would ever announce that transition, so a partner that
    registered a callback would never be told its task had ended (FR-025).

    Candidates are filtered on the same delegated-work predicate
    ``resolve_state`` folds in, so a task whose child is STILL running is not
    picked up and re-announced every sweep.

    A task waiting on its caller is excluded for the same reason. Its run ends
    ``completed`` and ``input-required`` is not terminal, so nothing claims it
    and it would match on every pass — and each pass spends one of the
    callback's lifetime attempts, until the budget is gone and the terminal
    notification this sweep exists to deliver never arrives.
    """
    from sqlalchemy import String, cast, or_

    from app.models.sub_agent_task import SubAgentTask

    live_child = (
        select(SubAgentTask.id)
        .where(
            SubAgentTask.status.in_(("pending", "running")),
            or_(
                SubAgentTask.metadata_json["parent_run_id"].astext
                == cast(A2ATask.run_id, String),
                SubAgentTask.metadata_json["assignment_run_id"].astext
                == cast(A2ATask.run_id, String),
            ),
        )
        .exists()
    )
    tasks = (
        (
            await db.execute(
                select(A2ATask)
                .join(AssignmentRun, AssignmentRun.id == A2ATask.run_id)
                .where(
                    A2ATask.terminal_announced_at.is_(None),
                    A2ATask.caller_question.is_(None),
                    AssignmentRun.status.notin_(_ACTIVE_RUN_STATUSES),
                    ~live_child,
                )
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )
    for task in tasks:
        await announce_state(db, task)
    return len(tasks)


async def request_cancel(
    db: AsyncSession, task: A2ATask, *, cancelled_out: list | None = None
) -> bool:
    """Signal cancellation. Returns False when the task is already terminal.

    Written to the database rather than only firing an in-process event: the
    request cancelling a task rarely lands on the replica running it, so an
    asyncio event alone would silently do nothing.

    ``cancelled_out`` collects the runs whose partners ``cascade_cancel`` left
    for the caller to tell, after its commit.
    """
    state = await resolve_state(db, task)
    if state in proto.TERMINAL_STATES:
        return False
    # A task waiting on its caller has a run that already finalised, so the
    # guarded UPDATE below would match nothing and report "already terminal"
    # for a task this surface reports as cancelable. Nothing is executing;
    # withdrawing the question is the whole of the cancel.
    asking = bool(task.caller_question)
    if task.run_id is not None and not asking:
        # The task row before the run row: ``_finalise_run``, its retry, the
        # reaper and the claim in ``execute_task_run`` all take them in that
        # order, and a cancel holding the run while it waits for the task
        # deadlocks with a run finalising at that moment. Locked rather than
        # stamped: ``get_db`` commits on teardown, so a stamp written here would
        # persist on the refused path below and leave a terminal task marked
        # cancel-requested. ``NO KEY`` is the mode the stamp's own UPDATE
        # takes, so the lock conflicts with exactly what the stamp would.
        await db.execute(
            select(A2ATask.id)
            .where(A2ATask.id == task.id)
            .with_for_update(key_share=True)
        )
        # Conditional on the run still being active — the same guard
        # ``_finalise_run`` uses, for the same reason: the state read above and
        # this write are not atomic across replicas, and the executor can
        # commit ``completed`` (with the result) in the gap. A cancel must
        # never overwrite that, or the partner loses the answer it paid for.
        result = await db.execute(
            update(AssignmentRun)
            .where(
                AssignmentRun.id == task.run_id,
                AssignmentRun.status.in_(("pending", "delayed", "running")),
            )
            .values(status="cancelled", completed_at=datetime.now(UTC))
        )
        # No row matched: the run went terminal between the read and the
        # write. The other writer's outcome stands, and this cancel reports
        # what every other too-late cancel reports - unless the run ended by
        # asking its caller. Read again, the task row held since the lock
        # above, that task is ``input-required``, and a cancel withdraws its
        # question like any other.
        if (
            not result.rowcount
            and await resolve_state(db, task) is not proto.A2AState.INPUT_REQUIRED
        ):
            return False
    task.cancel_requested_at = datetime.now(UTC)
    reached = await cascade_cancel(
        db, [task.run_id], reason=PARTNER_CANCEL_TASK_REASON
    )
    if cancelled_out is not None:
        cancelled_out.extend(reached)
    await db.flush()
    return True


async def lock_tasks_before_deletion(
    db: AsyncSession,
    *,
    run_ids: Any = None,
    agent_ids: Any = None,
    user_id: uuid.UUID | None = None,
) -> None:
    """Lock the task rows a deletion will write, before it writes any run.

    Deleting a run nulls its task's ``run_id`` (``ON DELETE SET NULL``), and
    deleting an agent or a user deletes its exposures, contexts and tasks, both
    inside the DELETE of the parent row: the run first, the task second. Every
    other writer of the pair takes the task row first (``request_cancel``, the
    claim in ``execute_task_run``, ``_finalise_run``, the reaper), so a deletion
    that did not would deadlock with one of them on the same live task. Called
    before the deletion's first write, it makes the deletion queue instead; the
    rows are taken in primary-key order, so two deletions queue behind each
    other too.

    *run_ids* and *agent_ids* are a list of ids or a SELECT of them; *user_id*
    covers what deleting that user cascades onto: its runs, its agents and the
    exposures that run as it. Nothing is issued when there is nothing to lock.
    """
    from sqlalchemy import or_

    from app.models.a2a import A2AContext

    def _given(ids: Any) -> Any:
        if isinstance(ids, (list, tuple, set, frozenset)):
            return [i for i in ids if i is not None] or None
        return ids

    runs = _given(run_ids)
    agents = _given(agent_ids)
    conditions = []
    if runs is not None:
        conditions.append(A2ATask.run_id.in_(runs))
    if user_id is not None:
        conditions.append(
            A2ATask.run_id.in_(
                select(AssignmentRun.id).where(AssignmentRun.user_id == user_id)
            )
        )
        conditions.append(
            A2ATask.exposure_id.in_(
                select(A2AExposure.id).where(A2AExposure.run_as_user_id == user_id)
            )
        )
        if agents is None:
            agents = select(Agent.id).where(Agent.user_id == user_id)
    if agents is not None:
        conditions.append(
            A2ATask.run_id.in_(
                select(AssignmentRun.id).where(AssignmentRun.agent_id.in_(agents))
            )
        )
        conditions.append(
            A2ATask.exposure_id.in_(
                select(A2AExposure.id).where(A2AExposure.agent_id.in_(agents))
            )
        )
        # A context answered by a deleted agent goes with it, and its tasks with
        # it, whichever agent the tasks were addressed to.
        conditions.append(
            A2ATask.context_id.in_(
                select(A2AContext.id).where(A2AContext.responsible_agent_id.in_(agents))
            )
        )
    if not conditions:
        return
    await db.execute(
        select(A2ATask.id)
        .where(or_(*conditions))
        .order_by(A2ATask.id)
        .with_for_update(key_share=True)
    )


async def lock_delegated_work(db: AsyncSession, run_ids) -> None:
    """Lock the background tasks the run tree under *run_ids* delegated.

    Called before a cancel writes any ``assignment_runs`` row, so it takes the
    ``sub_agent_tasks`` rows in the order their executor does (task, then run)
    and queues behind a task that is starting rather than deadlocking with it.
    A read of the tree and row locks only; nothing is written.
    """
    from app.services.orchestration_engine import find_child_runs
    from app.services.sub_agent import lock_sub_agent_tasks_for_runs

    roots = [rid for rid in run_ids if rid is not None]
    if not roots:
        return
    await lock_sub_agent_tasks_for_runs(db, roots + await find_child_runs(db, roots))


async def cascade_cancel(
    db: AsyncSession, run_ids, *, reason: str | None = None
) -> list[uuid.UUID]:
    """Stop the local half of everything *run_ids* handed off, and say what.

    ``reason`` is what each background task it stops reads as its error: the
    caller's own cause (a partner's cancel, the lifetime bound, an emergency
    stop), a plain "its run was cancelled" without one.

    Reaching them is what makes a reported CANCELED true. A run hands off three
    ways: a transfer child run, a local sub-agent, and another deployment over
    A2A. The first two are rows in this transaction and end here; the third is
    a network call the caller makes with ``spawn_run_cancel`` after its commit,
    because a cancel sent to a partner cannot be recalled and a rollback here
    would leave their work stopped and this side unaware it ever asked.

    Child runs go first: the other two arms are scoped by run id, so a child's
    own sub-agents and delegations are unreachable until the tree is known.

    The partner half is DECIDED here even though it is sent later: every
    outstanding delegation in the tree is marked as owed a cancel in this
    same transaction, so the decision and the obligation commit together and
    a hard kill before ``spawn_run_cancel`` finishes leaves the client sweep
    something to re-send.

    Returns the whole tree — the roots and every descendant.
    """
    from app.a2a.client.delegation import mark_cancel_owed
    from app.services.orchestration_engine import cancel_child_runs
    from app.services.sub_agent import (
        RUN_CANCELLED_TASK_REASON,
        mark_sub_agent_tasks_cancelled_for_runs,
    )

    roots = [rid for rid in run_ids if rid is not None]
    if not roots:
        return []
    # The background tasks' rows before the child runs'. The root run
    # a caller may have written already is not one an executor writes after
    # its task row: a background task marks its own run, a descendant.
    await lock_delegated_work(db, roots)
    tree = roots + await cancel_child_runs(db, roots)
    await mark_sub_agent_tasks_cancelled_for_runs(
        db, tree, reason=reason or RUN_CANCELLED_TASK_REASON
    )
    await mark_cancel_owed(db, run_refs=tree)
    return tree


async def reap_stale_tasks(
    db: AsyncSession,
    *,
    limit: int = 50,
    reaped_out: list[A2ATask] | None = None,
    cancelled_out: list | None = None,
) -> int:
    """Drive abandoned external runs to a terminal state. Returns the count.

    A run whose replica died mid-execution leaves its status at ``running``
    with a stale heartbeat, and the task stays pollable forever — which the
    specification forbids outright.

    Claims with ``FOR UPDATE SKIP LOCKED`` so concurrent reapers on different
    replicas take disjoint work rather than fighting over the same rows.

    Each reaped task is appended to *reaped_out*, so the caller can announce
    them once it has committed. Handed out rather than announced here: this
    only flushes, and a frame must follow the transition it reports.
    """
    from sqlalchemy import String, and_, cast, or_

    from app.models.sub_agent_task import SubAgentTask

    now = datetime.now(UTC)
    cutoff = now - timedelta(seconds=settings.a2a_run_lease_seconds)
    age_cutoff = now - timedelta(seconds=settings.a2a_task_max_lifetime_seconds)
    live_child = (
        select(SubAgentTask.id)
        .where(
            SubAgentTask.status.in_(("pending", "running")),
            or_(
                SubAgentTask.metadata_json["parent_run_id"].astext
                == cast(A2ATask.run_id, String),
                SubAgentTask.metadata_json["assignment_run_id"].astext
                == cast(A2ATask.run_id, String),
            ),
        )
        .exists()
    )
    rows = (
        await db.execute(
            select(A2ATask)
            .where(
                A2ATask.run_id.isnot(None),
                or_(
                    and_(
                        A2ATask.reaped.is_(False),
                        or_(
                            and_(
                                A2ATask.lease_heartbeat_at.isnot(None),
                                A2ATask.lease_heartbeat_at < cutoff,
                            ),
                            # The total bound. A task waiting on delegated work
                            # is not reaped for a stale lease (below), so
                            # without this one whose work never finishes stays
                            # pollable forever.
                            A2ATask.created_at < age_cutoff,
                        ),
                    ),
                    # The case the bound exists for: the run finished — marking
                    # the task reaped — while the work it delegated runs on.
                    # ``resolve_state`` folds that child in regardless, so the
                    # task answers ``working`` forever and the disjunct above,
                    # which the same flag excludes, can never see it again.
                    and_(A2ATask.created_at < age_cutoff, live_child),
                    # And the same shape with a question in place of a child: a
                    # partner that asks and walks away leaves a task no run is
                    # executing, no lease covers, and no cancel had reached.
                    and_(
                        A2ATask.created_at < age_cutoff,
                        A2ATask.caller_question.isnot(None),
                    ),
                ),
            )
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
    ).scalars().all()

    if not rows:
        return 0

    from app.a2a import metrics as a2a_metrics

    # One query for every claimed task's run, not one per task: the sweep runs
    # on every worker and holds all the claimed task rows locked while it works.
    runs = {
        run.id: run
        for run in (
            await db.execute(
                select(AssignmentRun).where(
                    AssignmentRun.id.in_([t.run_id for t in rows])
                )
            )
        )
        .scalars()
        .all()
    }

    reaped = 0
    parked: list[A2ATask] = []
    over_age_run_ids: list[uuid.UUID] = []
    for task in rows:
        run = runs.get(task.run_id)
        over_age = task.created_at < age_cutoff
        # Already reaped means the run finalised normally and only its
        # delegated work is holding the task open. There is nothing to write
        # for it — stopping that work is the whole job — so it is announced
        # below, once the stop has actually taken.
        already_reaped = task.reaped
        if over_age and task.run_id is not None:
            over_age_run_ids.append(task.run_id)
        if over_age and task.caller_question and not task.cancel_requested_at:
            # Withdrawn rather than left asking. CANCELED rather than failed:
            # the run did finish, and what expired is the wait for an answer.
            task.cancel_requested_at = datetime.now(UTC)
        # A stale lease does not mean abandoned. Work delegated to a peer or
        # to a copy of this agent runs on a worker, and nothing in this process
        # heartbeats for it, so a parked task's lease goes stale while its work
        # is demonstrably alive. Reporting that as abandoned is the untruth this
        # path exists to remove — only the total bound above overrides it.
        if not over_age and await live_delegated_work(db, task.run_id):
            continue
        if run is not None and run.status in _ACTIVE_RUN_STATUSES:
            # Conditional, not a blind write: the status is re-tested in the
            # UPDATE itself, so an executor that finalised this run since the
            # SELECT above keeps its terminal state instead of being driven back
            # out of one. Taking the run's row lock here also stops any further
            # finalize landing between this write and the flush below.
            error = (
                LIFETIME_ERROR_TEXT
                if over_age
                else "External task abandoned — execution lease expired"
            )
            completed_at = datetime.now(UTC)
            updated = (
                await db.execute(
                    update(AssignmentRun)
                    .where(
                        AssignmentRun.id == run.id,
                        AssignmentRun.status.in_(_ACTIVE_RUN_STATUSES),
                    )
                    .values(
                        status="failed", error=error, completed_at=completed_at
                    )
                )
            ).rowcount
            if updated:
                # Keep the in-session copy consistent with the row just written,
                # so a caller reading it does not see the pre-reap status.
                run.status = "failed"
                run.error = error
                run.completed_at = completed_at
                a2a_metrics.record_terminal("failed")
        task.reaped = True
        if already_reaped:
            parked.append(task)
            continue
        reaped += 1
        if reaped_out is not None:
            reaped_out.append(task)

    if over_age_run_ids:
        # The lifetime bound only holds if the work holding the task open
        # actually stops, and a partner working for an over-age task is folded
        # into nothing that would notice. Unguarded: the cascade shares this
        # session, so a database failure aborts the reap either way — the loop
        # retries next tick rather than losing the batch in silence.
        reached = await cascade_cancel(
            db, over_age_run_ids, reason=LIFETIME_CANCEL_TASK_REASON
        )
        if cancelled_out is not None:
            cancelled_out.extend(reached)

    for task in parked:
        # Announced only once the work has stopped. A non-terminal state is
        # published unclaimed, and every delivery spends one of the callback's
        # lifetime attempts — a row still reporting ``working`` would exhaust
        # them and the terminal notification would never land.
        if not await live_delegated_work(db, task.run_id):
            reaped += 1
            if reaped_out is not None:
                reaped_out.append(task)
    await db.flush()
    return reaped


def platform_interruption(cancel_event: asyncio.Event | None) -> str | None:
    """The failure reason of a run a platform stop cut, or ``None`` when the cut is not the platform's.

    A run whose partner (or an exposure revoke) asked to stop has its
    ``cancel_event`` set: that cut stays ``cancelled``. Otherwise a
    cancellation while this process records a stop (``worker_shutdown``:
    a stop signal or the worker's own recycle) is the platform's, and the
    task fails with :data:`INTERRUPTED_ERROR_TEXT` so the partner reads why
    and may send it again. An unknown cause keeps ``cancelled``.
    """
    if cancel_event is not None and cancel_event.is_set():
        return None
    from app.core import worker_shutdown

    cause = worker_shutdown.cut_cause()
    if cause == worker_shutdown.STOP:
        why = "a deployment, a scale-in or a node drain"
    elif cause == worker_shutdown.RECYCLE:
        why = "a worker recycle"
    else:
        return None
    return f"{INTERRUPTED_ERROR_TEXT} ({why}) before it finished; send the task again"


def _resumes_of(trigger_context: dict[str, Any] | None) -> list[dict[str, Any]]:
    from app.services import background_checkpoint

    return background_checkpoint.resumes_of(trigger_context)


def _handed_back(trigger_context: dict[str, Any] | None) -> dict[str, Any] | None:
    """The hand-back a stopping worker recorded on an external run, else ``None``."""
    from app.services.background_checkpoint import HANDED_BACK_KEY

    back = (trigger_context or {}).get(HANDED_BACK_KEY)
    return back if isinstance(back, dict) else None


def _attempt_of(trigger_context: dict[str, Any] | None) -> int:
    """The attempt an execution of an external run is: the hand-back's own when it resumes one.

    A hand-back gives its attempt back, so the resume is read as graceful
    (``background_checkpoint._hand_back_is_current``); otherwise one more than
    the resumes already made.
    """
    back = _handed_back(trigger_context)
    recorded = (back or {}).get("attempt")
    if isinstance(recorded, int) and not isinstance(recorded, bool) and recorded > 0:
        return recorded
    return len(_resumes_of(trigger_context)) + 1


def _record_resume(trigger_context: dict[str, Any] | None, *, attempt: int, replay: Any) -> dict[str, Any]:
    """*trigger_context* with this resume recorded and its hand-back cleared."""
    from app.services import background_checkpoint

    context = dict(trigger_context or {})
    updated = background_checkpoint.record_resume(
        context,
        attempt=attempt,
        cause=background_checkpoint.lost_cause(context, attempt=attempt),
        replayed_calls=replay.replayed_calls,
        resumed_from=replay.turn_id,
        graceful=True,
        completed_calls=replay.completed_calls,
    )
    updated.pop(background_checkpoint.HANDED_BACK_KEY, None)
    return updated


async def _hand_back(task_id: uuid.UUID, run_id: uuid.UUID, checkpoint: Any, *, attempt: int) -> bool:
    """Hand an external run its stopping worker cut back for the next worker; whether it was.

    The checkpoint is flushed first, every completed round in it; then the run
    goes back to ``pending`` with the hand-back recorded and the task's lease
    refreshed, so the reaper does not read it abandoned and the next live
    worker's sweep (:func:`resume_handed_back_tasks`) resumes it under the same
    task id. Conditional on the run still ``running``: a cancel or a terminal
    write that landed first stands. Shielded: the drain that cut the run does
    not cut its hand-back. Any failure answers ``False`` and the caller fails
    the task with the platform-restart reason, as before.
    """
    from app.core import worker_shutdown
    from app.core.database import async_session
    from app.services import background_checkpoint

    request = worker_shutdown.shutdown_request()
    signal = request.detail if request is not None else "a stop request"

    async def _write() -> bool:
        await checkpoint.aclose(flush=True)
        async with async_session() as db:
            # The task row before the run row, as every writer of the pair.
            await db.execute(select(A2ATask).where(A2ATask.id == task_id).with_for_update())
            run = (
                await db.execute(select(AssignmentRun).where(AssignmentRun.id == run_id).with_for_update())
            ).scalar_one_or_none()
            if run is None or run.status != "running":
                return False
            now = datetime.now(UTC)
            await db.execute(update(A2ATask).where(A2ATask.id == task_id).values(lease_heartbeat_at=now))
            await db.execute(
                update(AssignmentRun)
                .where(AssignmentRun.id == run_id, AssignmentRun.status == "running")
                .values(
                    status="pending",
                    trigger_context=background_checkpoint.handed_back(
                        run.trigger_context, attempt=attempt, signal=signal, now=now
                    ),
                )
            )
            await db.commit()
        return True

    try:
        handed = await asyncio.shield(_write())
    except BaseException as exc:
        # A failed hand-back falls back to the failure the caller writes.
        logger.warning("A2A task %s (run %s) could not be handed back: %s", task_id, run_id, exc)
        return False
    if handed:
        logger.warning(
            "A2A task %s (run %s) handed back at attempt %s: its worker is stopping; "
            "the next worker resumes it from its checkpoint under the same task id",
            task_id,
            run_id,
            attempt,
        )
    return handed


#: Seconds a hand-back is left to its own worker's drain before another worker resumes it.
HAND_BACK_SETTLE_SECONDS = 5


async def resume_handed_back_tasks(db: AsyncSession, *, limit: int = 20) -> list[uuid.UUID]:
    """The external tasks a stopping worker handed back, to resume on this worker.

    Never on a worker that is itself stopping. Each task is dispatched once per
    sweep; the executor's conditional claim (``pending`` to ``running``) keeps a
    second worker's dispatch of the same task from executing it twice.
    """
    from app.core import worker_shutdown
    from app.services.background_checkpoint import HANDED_BACK_KEY

    if worker_shutdown.shutdown_request() is not None:
        return []
    settled = datetime.now(UTC) - timedelta(seconds=HAND_BACK_SETTLE_SECONDS)
    rows = (
        await db.execute(
            select(A2ATask.id, AssignmentRun.trigger_context)
            .join(AssignmentRun, AssignmentRun.id == A2ATask.run_id)
            .where(
                AssignmentRun.status == "pending",
                AssignmentRun.trigger_context[HANDED_BACK_KEY].isnot(None),
                A2ATask.cancel_requested_at.is_(None),
            )
            .limit(limit)
        )
    ).all()
    due: list[uuid.UUID] = []
    for task_id, context in rows:
        back = _handed_back(context)
        at = (back or {}).get("at")
        try:
            moment = datetime.fromisoformat(at) if isinstance(at, str) else None
        except ValueError:
            moment = None
        if moment is None or moment <= settled:
            due.append(task_id)
    return due


def _executing_user_id(exposure: A2AExposure) -> str:
    """The identity an external run executes as.

    Always the exposure's recorded run-as user. Resolved once when the agent
    became reachable - its owner (see ``exposure.resolve_run_as``), or, on a
    legacy row, an administrator's explicit nomination - so nothing is
    inferred here.

    Never derive it from ``agent.user_id`` instead. That column is preserved
    when an agent becomes company-managed (see ``models/agent.py``), so
    reading it here silently runs external work as whoever seeded the agent,
    with that person's personal and group-shared data in reach.
    """
    return str(exposure.run_as_user_id)


#: How often the lease is refreshed: a quarter of it, so a run heartbeats
#: several times before the reaper could consider it abandoned. The poll the
#: beating loop runs on is bounded at startup to stay under this.
def _supervise_interval() -> int:
    return settings.a2a_run_lease_seconds // 4


async def _supervise_run(task_id: uuid.UUID, cancel_event: asyncio.Event) -> None:
    """Keep a running task's lease fresh and relay a cancel request into it.

    Both halves are load-bearing, on their own cadences.

    Nothing else refreshes ``lease_heartbeat_at`` between the start of a run
    and its end, so without the heartbeat any run outlasting
    ``a2a_run_lease_seconds`` is reaped to ``failed`` mid-flight while still
    executing - and the executor's own final write then flips it back, making
    the task leave and re-enter a terminal state (FAILED, then COMPLETED).

    And ``tasks/cancel`` only writes ``cancel_requested_at``; this poll is the
    one thing that reads it. Without it the run continues to completion, is
    billed in full, and the final write clobbers the cancellation (CANCELED,
    then COMPLETED). Polled on ``a2a_cancel_poll_seconds``: that delay is what
    a user who says stop sits through, which the lease does not measure.

    Revocation is read with it. ``execute_task_run`` checks the exposure once,
    before it claims the task, and no revocation path signals a run that is
    already executing — so this poll is also what stops an already-claimed run
    whose boundary has since been withdrawn (an owner transfer, a sharing
    change, an admin revoke). Otherwise the run keeps calling the previous
    run-as user's integrations under their identity, and charging their quota,
    until it finishes on its own.

    Uses its own session: the executor's is busy inside ``run_agent_chat``.
    """
    from app.core.database import async_session

    poll = settings.a2a_cancel_poll_seconds
    beat_every = timedelta(seconds=_supervise_interval())
    # Wall time, not a tick count. Ticks drift under load, and a run of failed
    # ticks would advance a counter while no heartbeat landed — the lease then
    # goes stale and the reaper takes a run whose supervisor is alive.
    last_beat = datetime.now(UTC)
    while True:
        await asyncio.sleep(poll)
        try:
            async with async_session() as db:
                row = (
                    await db.execute(
                        select(A2ATask.cancel_requested_at, A2AExposure.revoked_at)
                        .join(A2AExposure, A2AExposure.id == A2ATask.exposure_id)
                        .where(A2ATask.id == task_id)
                    )
                ).first()
                if row is not None and (row[0] is not None or row[1] is not None):
                    cancel_event.set()
                    return
                now = datetime.now(UTC)
                if now - last_beat >= beat_every:
                    await db.execute(
                        update(A2ATask)
                        .where(A2ATask.id == task_id)
                        .values(lease_heartbeat_at=now)
                    )
                    await db.commit()
                    # Moved only by a write that landed, so a failed tick is
                    # retried at the next poll rather than a lease later.
                    last_beat = now
        except Exception:
            # A transient database blip must not kill the run it supervises;
            # the next tick retries, and the reaper is the backstop if the
            # lease genuinely goes stale. Logged rather than swallowed silently
            # so a persistently failing supervisor is visible.
            logger.warning(
                "A2A supervisor tick failed for task %s", task_id, exc_info=True
            )
            continue


async def _write_terminal_by_id(
    db: AsyncSession,
    *,
    task_id: uuid.UUID,
    run_id: uuid.UUID,
    values: dict[str, Any],
    responding_agent_id: uuid.UUID | None = None,
) -> int:
    """The terminal write addressed by id only. Returns the run's rowcount.

    Deliberately touches no ORM instance: this runs on a FRESH session during
    retry, while the caller's session may be the very thing that failed. Task
    row before run row, the same lock order every other writer here takes.
    """
    task_values: dict[str, Any] = {
        "lease_heartbeat_at": datetime.now(UTC),
        "reaped": True,
    }
    if responding_agent_id is not None:
        task_values["responding_agent_id"] = responding_agent_id
    await db.execute(
        update(A2ATask).where(A2ATask.id == task_id).values(**task_values)
    )
    result = await db.execute(
        update(AssignmentRun)
        .where(AssignmentRun.id == run_id, AssignmentRun.status == "running")
        .values(**values)
    )
    await db.commit()
    return result.rowcount


async def _retry_terminal_write(
    *,
    task_id: uuid.UUID,
    run_id: uuid.UUID,
    values: dict[str, Any],
    responding_agent_id: uuid.UUID | None = None,
) -> int:
    """Re-attempt the terminal write on fresh sessions. 0 if it never lands."""
    from app.core.database import async_session

    for attempt in range(1, _TERMINAL_WRITE_RETRIES + 1):
        await asyncio.sleep(_TERMINAL_WRITE_BACKOFF_SECONDS * attempt)
        try:
            async with async_session() as retry_db:
                return await _write_terminal_by_id(
                    retry_db,
                    task_id=task_id,
                    run_id=run_id,
                    values=values,
                    responding_agent_id=responding_agent_id,
                )
        except SQLAlchemyError:
            logger.warning(
                "A2A terminal write retry %s/%s failed for task %s",
                attempt,
                _TERMINAL_WRITE_RETRIES,
                task_id,
                exc_info=True,
            )
    logger.error(
        "A2A terminal write never landed for task %s; the reaper will report it "
        "abandoned and the run's result is lost",
        task_id,
    )
    return 0


async def _finalise_run(
    db: AsyncSession,
    task: A2ATask,
    run_id: uuid.UUID,
    status: str,
    summary: str | None,
    error: str | None,
    *,
    responding_agent_id: uuid.UUID | None = None,
    conversation: list | None = None,
) -> None:
    """Write the terminal state, but never over another writer's.

    The UPDATE is conditional on the run still being ``running``. A reaper that
    already drove this run terminal, or a cancellation, must win: committing a
    stale in-session object over their decision would let a task leave a
    terminal state, which the specification forbids outright.
    """
    from app.a2a import metrics as a2a_metrics

    # Captured before the rollback below, which would expire the instance and
    # make even the primary key unreadable without IO.
    task_id = task.id

    # ``run_agent_chat`` shares this session; a database error escaping it
    # leaves the transaction aborted, and the terminal write must not die with
    # it. ``is_active`` is False exactly in that partial-rollback state, so a
    # healthy session pays nothing here.
    if not db.is_active:
        await db.rollback()

    values: dict[str, Any] = {
        "status": status,
        # Stamped like every other executor's finalize, so the runs list can
        # show a duration and the run does not read as still in flight.
        "completed_at": datetime.now(UTC),
    }
    if summary is not None:
        values["result_summary"] = summary
    if error is not None:
        values["error"] = error
    if conversation:
        # Serialised like every other executor's trace, so the run-detail
        # endpoint reads it without knowing where the run came from.
        try:
            from app.services.builtin_tools import serialize_messages

            values["messages"] = serialize_messages(conversation)
        except Exception:
            # Never at the cost of the terminal write below.
            logger.warning(
                "Could not serialise the conversation for task %s",
                task_id,
                exc_info=True,
            )

    # The task row is written FIRST, and the order is the point. The reaper
    # claims a2a_tasks with ``FOR UPDATE SKIP LOCKED`` and only then writes the
    # run; taking these two locks in the opposite order here would be the
    # classic ABBA deadlock, and the transaction Postgres aborts to break it
    # could be this one — throwing away a completed result that is already paid
    # for. Same order on both paths means the reaper simply skips this task, or
    # waits behind it, instead of deadlocking with it.
    task.lease_heartbeat_at = datetime.now(UTC)
    # Finished work is not abandoned work: without this every normally-completed
    # task stays a reaper candidate and gets swept once more a whole lease later,
    # purely to be marked.
    task.reaped = True
    if responding_agent_id is not None:
        task.responding_agent_id = responding_agent_id
    try:
        await db.flush()

        result = await db.execute(
            update(AssignmentRun)
            .where(AssignmentRun.id == run_id, AssignmentRun.status == "running")
            .values(**values)
        )
        await db.commit()
        rowcount = result.rowcount
        session_usable = True
    except SQLAlchemyError:
        session_usable = False
        # The run has already produced its answer and the run-as user has
        # already been charged for it, so losing this write is not the same kind
        # of failure as losing a read: the reaper would report "abandoned" a
        # lease later and the result would be gone. Retried on fresh sessions
        # rather than this one, which is plausibly what just failed.
        logger.warning(
            "A2A terminal write failed for task %s; retrying on a fresh session",
            task_id,
            exc_info=True,
        )
        rowcount = await _retry_terminal_write(
            task_id=task_id,
            run_id=run_id,
            values=values,
            responding_agent_id=responding_agent_id,
        )

    if rowcount:
        a2a_metrics.record_terminal(status)

    # This reads state and reaches outside the process, so it needs a working
    # session — and if the write landed on the retry path, this one is
    # plausibly the thing that failed. Skipped rather than attempted through
    # it: a callback is an accelerator whose documented floor is the partner
    # polling, whereas a second failure here would be raised out of a function
    # whose whole job is not losing the write above.
    if rowcount and session_usable:
        # After the terminal write, never before: a notification is an
        # invitation to read the task, so it must not arrive before there is
        # something to read. Never raises — a partner that misses a
        # notification polls, which is what every partner does today, whereas a
        # failure escaping here would cost the terminal write already committed
        # above.
        # The TASK's state, not the run's. A run can finish with delegated
        # work still live, and the task is ``working`` until that finishes too,
        # so a partner notified of the run's status would be invited to read a
        # task that then answers ``working``. The sweep announces the terminal
        # transition when it truly arrives, which is what the stamp is for.
        await announce_state(db, task)


def _conversational_target(handovers: list[dict[str, Any]]) -> uuid.UUID | None:
    """Which agent takes over the conversation, or ``None`` if none did.

    A ``mandatory_transfer`` never does, and that distinction is the whole
    function. It is an automatic hand-off the operator wired — a pipeline stage
    — so the chain is how one message gets answered, not a change of who the
    partner is talking to. Its stages are typically unexposed, so recording one
    as responsible refuses every follow-up in that context forever, with a
    message claiming the agent is "no longer available" when it never was.

    A model-chosen transfer does take over: that is the tool's contract, and
    the agent it names is one the same partner reaches by the operator's own
    binding. The LAST such hop wins — a conversational transfer followed by the
    target's own pipeline leaves the target responsible, not its terminal stage.

    Consultation and background delegation appear here too and are ignored:
    neither answers the partner's next message.
    """
    target: uuid.UUID | None = None
    for hop in handovers:
        if hop.get("type") != "transfer" or hop.get("mandatory"):
            continue
        parsed = safe_uuid(hop.get("target_agent_id"))
        if parsed is not None:
            target = parsed
    return target


#: How often a queued follow-up checks whether the tasks ahead of it in its
#: context are finished. Each check also renews its lease, so the reaper does
#: not take a task that is waiting its turn for an abandoned one.
_CONTEXT_QUEUE_POLL_SECONDS = 2.0


async def _wait_for_context_turn(db: AsyncSession, task: A2ATask, run_id: uuid.UUID) -> bool:
    """Hold a queued follow-up until every task ahead of it in its context is done.

    Returns False when the run went terminal while it waited (a cancel, the
    reaper's lifetime bound): nothing is left to execute. The wait is bounded
    by the tasks ahead, which are themselves bounded by the lease and the task
    lifetime, so a queued task never outlives the one it waits for by more than
    a poll.
    """
    from app.a2a import contexts as ctx_svc

    while await ctx_svc.has_open_task_ahead(db, task):
        status = (await db.execute(select(AssignmentRun.status).where(AssignmentRun.id == run_id))).scalar_one_or_none()
        if status is None or status not in ("pending", "delayed"):
            return False
        await db.execute(update(A2ATask).where(A2ATask.id == task.id).values(lease_heartbeat_at=datetime.now(UTC)))
        # Committed each turn: the read above must see the tasks ahead finish,
        # and an open transaction would hold the task row from a cancel.
        await db.commit()
        await asyncio.sleep(_CONTEXT_QUEUE_POLL_SECONDS)
    return True


@known_secrets_scoped("task_id")
async def execute_task_run(task_id: uuid.UUID) -> None:
    """Execute an external task's run to completion.

    Dispatched in the background after ``create_task`` commits. Everything it
    needs travels on the run and the exposure, so it opens its own session
    rather than borrowing the request's.

    One bound applies, and it is an identity rather than a tool list: the run
    executes as the exposure's run-as user, whatever the agent's tier
    (``_executing_user_id``). Two things follow, both deliberate:

    - **no session scope.** Omitting the ``session_*_ids`` arguments is the
      mechanism — a non-None value REPLACES what ``run_agent_chat`` resolves,
      so passing none runs the agent's own configuration, per-user integration
      bindings included;
    - **full handover** over every configured peer, each hop inheriting the
      same run-as user. ``mandatory_transfer`` included: an exposed agent that
      is the entry to a pipeline answers with its terminal stage's output, and
      ``build_artifacts`` names which agent produced it.

    So a partner reaches what the run-as user reaches through this agent
    interactively, plus its handover peers to the depth cap
    (``a2a.exposure``), except the platform-builder surface: the run sits in
    a partner-task scope (``a2a.partner_scope``), so no hop wires trace
    inspection and the agent builder keeps its read half, acting as an
    anonymous caller. Which agent answered is recorded on the task: after a
    transfer the artifact is not the addressed agent's work.
    """
    from app.a2a.partner_scope import PartnerPrincipal, partner_task_scope
    from app.core.database import async_session
    from app.services.agent_chat import QUOTA_REFUSAL_ERROR_CODES, run_agent_chat

    async with async_session() as db:
        task = (
            await db.execute(select(A2ATask).where(A2ATask.id == task_id))
        ).scalar_one_or_none()
        if task is None or task.run_id is None:
            return

        run = (
            await db.execute(
                select(AssignmentRun).where(AssignmentRun.id == task.run_id)
            )
        ).scalar_one_or_none()
        # Terminal-state guard: a retry and a reaper reclaim can both dispatch,
        # and replaying a finished run would replay its side effects.
        if run is None or run.status in ("completed", "failed", "cancelled"):
            return

        exposure = (
            await db.execute(
                select(A2AExposure).where(A2AExposure.id == task.exposure_id)
            )
        ).scalar_one_or_none()
        agent = (
            await db.execute(select(Agent).where(Agent.id == run.agent_id))
        ).scalar_one_or_none()
        if exposure is None or agent is None or exposure.revoked_at is not None:
            # Revoked mid-flight: the revocation wins — but never over a
            # concurrent writer's terminal decision (same guard as below).
            await db.execute(
                update(AssignmentRun)
                .where(
                    AssignmentRun.id == run.id,
                    AssignmentRun.status.in_(("pending", "delayed", "running")),
                )
                .values(
                    status="failed",
                    error="Exposure revoked before execution",
                    completed_at=datetime.now(UTC),
                )
            )
            await db.commit()
            return

        # A follow-up sent while its conversation was busy waits its turn here
        # rather than being refused at submission: the order of submission is
        # the order of execution within one context.
        if (run.trigger_payload or {}).get("a2a_queued") and not await _wait_for_context_turn(db, task, run.id):
            return

        # A run its stopping worker handed back resumes here from its
        # checkpoint, the same task id answering the partner: the bounds of the
        # assignment runs apply, past them the task fails with their reason.
        from app.services import assignment_run_resume

        resume_replay = None
        attempt = _attempt_of(run.trigger_context)
        if _handed_back(run.trigger_context) is not None:
            from app.services.builtin_tool_aliases import runtime_builtin_tool_ids
            from app.services.code_interpreter import code_interpreter_callable

            # The resume note names attachments/ only to a run that can call the code interpreter (#2677).
            resume_replay = await assignment_run_resume.load_replay(
                run.id,
                code_interpreter=code_interpreter_callable(
                    runtime_builtin_tool_ids(agent.builtin_tool_ids, getattr(agent, "builtin_allowed_tools", None)),
                    agent.tool_deny_list,
                ),
            )
            refused = assignment_run_resume.resume_refused(
                run.trigger_context,
                attempt=attempt,
                completed_calls=resume_replay.completed_calls,
            )
            if refused is not None:
                await assignment_run_resume.discard(run.id)
                await _finalise_run(db, task, run.id, "failed", None, refused[:2000])
                return
            resumed_context = _record_resume(run.trigger_context, attempt=attempt, replay=resume_replay)
            attempt = len(_resumes_of(resumed_context)) + 1
        else:
            resumed_context = None

        # Claim the run with a conditional UPDATE, never a plain ORM write:
        # the terminal-state read above and this transition are not atomic
        # across replicas, and a cancel committed in the gap must win — an
        # unconditional write here overwrote ``cancelled`` with ``running``,
        # reopening the CANCELED→COMPLETED sequence the guarded finalize and
        # cancel exist to forbid.
        now = datetime.now(UTC)
        payload = run.trigger_payload or {}
        message = payload.get("message", "")
        # A continuation carries the exchange that produced it. An external
        # run starts with no history, so without this the agent is answered
        # without remembering what it asked.
        prior_history: list[Any] = []
        if payload.get("a2a_question"):
            from app.llm.base import ChatMessage

            prior_history = [
                ChatMessage(
                    role="user", content=str(payload.get("a2a_prior_message") or "")
                ),
                ChatMessage(role="assistant", content=str(payload["a2a_question"])),
            ]
        if resume_replay is not None and resume_replay.history:
            prior_history.extend(assignment_run_resume.restore_history(resume_replay.history))
        # The task row before the run row, the order every writer in this
        # module takes them in (see ``_finalise_run``), so a cancel racing the
        # claim waits for it instead of deadlocking with it.
        task.lease_heartbeat_at = now
        await db.flush()
        claimed = await db.execute(
            update(AssignmentRun)
            .where(
                AssignmentRun.id == run.id,
                AssignmentRun.status.in_(("pending", "delayed")),
            )
            # ``started_at`` stamped like every other executor, so the runs
            # list shows a real start time and duration for external work.
            # The prompt goes in at the claim rather than at the finalize: it
            # is the run's input, and a run still in flight is when someone
            # most wants to read it. The finalize replaces it with the full
            # conversation.
            .values(
                status="running",
                # A resume keeps the first start: the task's wall time spans its resumes.
                started_at=(run.started_at if resumed_context is not None else None) or now,
                messages=[{"role": "user", "content": message}],
                # The resume recorded with the claim, never by a blind write before it.
                **({"trigger_context": resumed_context} if resumed_context is not None else {}),
            )
        )
        await db.commit()
        if not claimed.rowcount:
            # Lost the claim: another writer drove the run terminal between
            # the read and this UPDATE. Their decision stands.
            return

        run_id = run.id
        client_id = str((run.trigger_context or {}).get("client_id", ""))

        # A cancel request lands on whichever replica serves the RPC, almost
        # never this one, so it arrives through the database. The supervisor
        # below polls for it and trips this event, which ``run_agent_chat``
        # already honours between iterations.
        cancel_event = asyncio.Event()
        supervisor = asyncio.create_task(_supervise_run(task_id, cancel_event))
        # Its completed tool rounds are checkpointed at every iteration
        # boundary, so a worker asked to stop winds the run down there and
        # hands it back (``_hand_back``) instead of failing the task.
        checkpoint = assignment_run_resume.checkpoint_for(
            run_id, replay=resume_replay, attempt=attempt, user_id=_executing_user_id(exposure)
        )
        handed_back = False

        status, summary, error = "failed", None, None
        # Set only by a run that ended by asking; see the branch below.
        question: str | None = None
        # Bound before the try: the ``finally`` reads them, so an exception from
        # ``run_agent_chat`` would otherwise raise NameError out of the handler.
        transferred_to: str | None = None
        # Each hop the run took, in order. Needed because "who produced this
        # answer" and "who answers next" are different questions with different
        # answers — see ``_conversational_target``.
        handovers: list[dict[str, Any]] = []
        # ``run_agent_chat`` does not RAISE on quota exhaustion — it returns
        # the human-readable refusal as the reply text and reports the machine
        # cause through ``error_out``. That cause must become a FAILED task,
        # not a COMPLETED one carrying the refusal prose as its artifact
        # (FR-036).
        error_out: dict[str, Any] = {}
        conversation: list[Any] = []
        # Offered to the agent only while rounds remain: past the bound it
        # has to answer with what it has, which is what a caller that keeps
        # being asked would eventually insist on anyway.
        asked: dict[str, Any] | None = (
            {}
            if (task.clarification_count or 0) < _MAX_CLARIFICATION_ROUNDS
            else None
        )
        try:
            # Inside the try: the supervisor is already running, and a failure
            # before the ``finally`` would leave it heartbeating a run nobody
            # finalises.
            # The partner rides with the scope: the run history it may read
            # is its own tasks' (``a2a.partner_runs``), never the run-as
            # identity's.
            partner = (
                PartnerPrincipal(exposure_id=exposure.id, client_id=client_id)
                if client_id and getattr(exposure, "id", None)
                else None
            )
            with partner_task_scope(principal=partner):
                response_text, transferred_to = await run_agent_chat(
                    db=db,
                    user_id=_executing_user_id(exposure),
                    agent=agent,
                    history=prior_history,
                    user_message=message,
                    # The partner is an agent asking, and the answer goes back
                    # to it: the consult framing says so, and the run-as user
                    # is the one the run acts for, never the one it talks to.
                    handover_context=dict(_PARTNER_HANDOVER_CONTEXT),
                    # No narrowing of any kind - not by peer, not by type.
                    allow_handover=True,
                    parent_run_id=str(run_id),
                    handovers_out=handovers,
                    messages_out=conversation,
                    ask_caller_out=asked,
                    meter_quota=True,
                    cancel_event=cancel_event,
                    error_out=error_out,
                    on_checkpoint=checkpoint.offer,
                )
            if cancel_event.is_set():
                status = "cancelled"
            elif error_out.get("error_code") == "loop_timeout":
                # The loop answers a timeout with an apology rather than
                # raising, so the decision reads its signal, not its reply.
                status, error = "failed", TIMEOUT_ERROR_TEXT
            elif error_out.get("error_code") in QUOTA_REFUSAL_ERROR_CODES:
                # ``QUOTA_EXHAUSTED_ERROR_PREFIX`` is the machine-readable
                # marker ``_render_task`` projects into the failed task's
                # status message, and what routes the audit entry below.
                status = "failed"
                error = f"{QUOTA_EXHAUSTED_ERROR_PREFIX}: {error_out.get('error')}"
            elif asked and asked.get("question"):
                # The run is over and the task is not: it is blocked on the
                # client, which is what ``input-required`` means. The run
                # completes — nothing is executing it — and the question on
                # the task is what ``resolve_state`` reads to say so.
                status = "completed"
                question = str(asked["question"])
            else:
                status, summary = "completed", response_text
        except TurnWoundDown:
            # Its worker is stopping: the loop stopped at an iteration
            # boundary, every call answered and checkpointed. Handed back,
            # resumed by the next live worker under the same task id.
            handed_back = await _hand_back(task_id, run_id, checkpoint, attempt=attempt)
            if not handed_back:
                status, error = "failed", platform_interruption(None) or INTERRUPTED_ERROR_TEXT
        except asyncio.CancelledError:
            # Cut by a platform stop, not by its partner: handed back with
            # the rounds its checkpoint holds, the call in flight named to
            # the resumed turn; failed with a reason the partner reads only
            # when the hand-back cannot be written.
            interrupted = platform_interruption(cancel_event)
            if interrupted is not None:
                handed_back = await _hand_back(task_id, run_id, checkpoint, attempt=attempt)
                if not handed_back:
                    status, error = "failed", interrupted
                    logger.warning(
                        "A2A task %s (run %s) cut by a platform stop: failed with a reason its partner reads",
                        task_id,
                        run_id,
                    )
            else:
                status = "cancelled"
            raise
        except Exception as exc:
            # Any failure becomes a terminal FAILED task carrying a reason.
            # Leaving it non-terminal would strand the partner polling.
            from app.core.db_availability import is_pool_timeout

            if is_pool_timeout(exc):
                # Transient load (#2561): a partner-visible busy reason to send
                # the task again, never the raw QueuePool text.
                logger.warning("A2A task %s (run %s) failed on a pool timeout: answered busy", task_id, run_id)
                status, error = "failed", BUSY_ERROR_TEXT
            else:
                status, error = "failed", str(exc)[:2000]
        finally:
            supervisor.cancel()
            with suppress(asyncio.CancelledError):
                await supervisor
            # A handed-back run is left running for the next worker: no
            # terminal write, its checkpoint kept for the resume.
            if not handed_back:
                await checkpoint.aclose(flush=False)
                await assignment_run_resume.discard(run_id)
                # Who produced this answer, for the artifact's attribution: the
                # last agent to run, whatever got it there.
                responding = safe_uuid(transferred_to)
                # Who answers the partner's NEXT message, which is a different
                # question — see ``_conversational_target``.
                next_responsible = _conversational_target(handovers)
                if next_responsible is not None:
                    from app.a2a import contexts as ctx_svc
                    from app.models.a2a import A2AContext

                    context = (
                        await db.execute(select(A2AContext).where(A2AContext.id == task.context_id))
                    ).scalar_one_or_none()
                    # Only an agent this partner's own binding covers. Handover
                    # spans every configured peer, exposed or not, so the model can
                    # land the conversation on one the partner cannot reach —
                    # recorded, that refuses every follow-up in this context
                    # forever. The agent it addressed stays responsible instead.
                    reachable = await ctx_svc.reachable_agent_ids(db, task.client_id)
                    if context is not None and next_responsible in reachable:
                        await ctx_svc.set_responsible(db, context, next_responsible)
                # Before the terminal write, so a poll can never see the run
                # completed with no question on the task it is meant to explain.
                if question:
                    task.caller_question = question
                    task.clarification_count = (task.clarification_count or 0) + 1
                await _finalise_run(
                    db,
                    task,
                    run_id,
                    status,
                    summary,
                    error,
                    # None unless a transfer happened.
                    responding_agent_id=responding,
                    conversation=conversation,
                )
                if error and error.startswith(QUOTA_EXHAUSTED_ERROR_PREFIX):
                    # FR-036: exhaustion is audit-logged naming the external
                    # client responsible, so an operator can attribute the
                    # consumption to the partner integration rather than to the
                    # run-as user's own work.
                    await _audit_quota_exhaustion(
                        db,
                        exposure=exposure,
                        agent_name=agent.name,
                        task_id=task_id,
                        client_id=client_id,
                        detail=error,
                    )
