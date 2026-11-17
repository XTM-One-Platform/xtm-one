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

"""Tell the conversation that triggered an assignment run how it ended.

A chat turn that triggers a run (the agent builder's ``trigger_assignment``)
owes that run to its conversation: ``trigger_assignment(owed_conversation_id=)``
writes an ``AssignmentRunConversationNotice`` in the run's own transaction.
The conversation is then told once, whichever comes first:

* the agent asked - a wait watching the run (``builtin_tools/wait``) or a
  ``get_run`` read it ended, in that conversation (:func:`mark_told`);
* nobody asked - when the worker is done with the run, :func:`schedule` waits
  :data:`WAKE_GRACE_SECONDS` (a wait still watching the run polls it every two
  seconds and reports first), claims the notice and resumes the conversation
  through the task wake's turn (``parent_wake.wake_conversation``): a hidden
  platform message carrying the run id, status, duration and result, one
  turn, folded into a turn already running, never after a Stop.

A wake runs in the worker that ended the run: one stopped during the grace
takes it along, and :func:`sweep_lost_wakes` resumes the conversation later.
A cancelled run is nobody's news, as for a cancelled background task or Video
Studio job: its notice is claimed and nothing is said.

The run ids a wait watches are read by ``builtin_tools.wait.watched_run_statuses``.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.orm import load_only

from app.core.utils import format_exception_message, safe_uuid
from app.models.assignment import Assignment, AssignmentRun, AssignmentRunConversationNotice

logger = logging.getLogger(__name__)

#: Names a run wake in the log and the activity trail.
WAKE_LABEL = "assignment-run"
#: Seconds between the end of a run and the wake of its conversation: longer
#: than a wait's poll (``wait_policy.WAIT_POLL_INTERVAL``), so a turn still
#: waiting on the run tells the agent and the wake finds nothing owed.
WAKE_GRACE_SECONDS = 15.0
#: How long after a run ended its conversation must still be owed it for the
#: sweep to resume it: well past :data:`WAKE_GRACE_SECONDS`, so the worker's
#: own wake always goes first.
SWEEP_AFTER_SECONDS = 120.0
#: A run that ended longer ago than this is not told any more: its news has aged.
SWEEP_WINDOW = timedelta(hours=24)
#: How often one worker sweeps.
SWEEP_INTERVAL_SECONDS = 300.0
#: Runs resumed by one sweep at most.
SWEEP_MAX_RUNS = 20
#: A told notice is kept this long, an untold one :data:`UNTOLD_RETENTION`.
TOLD_RETENTION = timedelta(days=1)
UNTOLD_RETENTION = timedelta(days=30)
#: The run's result is carried in the wake up to this many characters.
WAKE_RESULT_CHARS = 32_000

#: The ends a conversation is told about; a cancelled run is claimed silently.
_WAKE_STATUSES = frozenset({"completed", "failed", "skipped"})
#: What the wake prompt reads of a run.
_WAKE_FIELDS = (
    AssignmentRun.id,
    AssignmentRun.status,
    AssignmentRun.created_at,
    AssignmentRun.started_at,
    AssignmentRun.completed_at,
    AssignmentRun.result_summary,
    AssignmentRun.error,
)

# Strong references: asyncio keeps only weak ones to a task.
_WAKES: set[asyncio.Task] = set()


def _terminal_statuses() -> tuple[str, ...]:
    from app.services.assignment_engine import TERMINAL_RUN_STATUSES

    return TERMINAL_RUN_STATUSES


def _parse(value: object) -> uuid.UUID | None:
    """*value* as a UUID, given as text or as a UUID: ``safe_uuid`` reads text only."""
    return safe_uuid(str(value)) if value is not None else None


def _uuids(ids: Iterable[object]) -> dict[str, uuid.UUID]:
    """``{input id: UUID}`` for the ids that parse, input order kept."""
    out: dict[str, uuid.UUID] = {}
    for raw in ids:
        text = str(raw).strip()
        parsed = safe_uuid(text)
        if parsed is not None:
            out[text] = parsed
    return out


# ── Told / owed ───────────────────────────────────────────────────────────


async def mark_told(run_ids: Iterable[str], conversation_id: object) -> None:
    """The ended runs of *run_ids* owed to *conversation_id* are told: the caller reports them to it.

    Only a run owed to that very conversation: one another conversation
    triggered, read here, is still owed to its own. Never raises: a failure
    costs a second telling, never the read that called it.
    """
    from app.core.database import async_session

    conversation = _parse(conversation_id)
    ids = list(_uuids(run_ids).values())
    if conversation is None or not ids:
        return
    try:
        async with async_session() as db:
            await db.execute(
                update(AssignmentRunConversationNotice)
                .where(
                    AssignmentRunConversationNotice.run_id.in_(ids),
                    AssignmentRunConversationNotice.conversation_id == conversation,
                    AssignmentRunConversationNotice.told_at.is_(None),
                )
                .values(told_at=datetime.now(UTC))
                .execution_options(synchronize_session=False)
            )
            await db.commit()
    except Exception as exc:
        logger.warning(
            "Could not record that conversation %s was told about run(s) %s: %s",
            conversation,
            ", ".join(str(i) for i in ids),
            format_exception_message(exc),
        )


async def _claim(run_id: uuid.UUID) -> tuple[uuid.UUID, uuid.UUID, AssignmentRun, str | None] | None:
    """Take the notice of the ended run *run_id*: ``(conversation, user, run, assignment name)``.

    One conditional update, so a wake and a sweep racing never both claim it;
    nothing while the run is still active or once anything told it.
    """
    from app.core.database import async_session

    async with async_session() as db:
        claimed = (
            await db.execute(
                update(AssignmentRunConversationNotice)
                .where(
                    AssignmentRunConversationNotice.run_id == run_id,
                    AssignmentRunConversationNotice.told_at.is_(None),
                    AssignmentRun.id == AssignmentRunConversationNotice.run_id,
                    AssignmentRun.status.in_(_terminal_statuses()),
                )
                .values(told_at=datetime.now(UTC))
                .returning(
                    AssignmentRunConversationNotice.conversation_id,
                    AssignmentRunConversationNotice.user_id,
                )
                .execution_options(synchronize_session=False)
            )
        ).first()
        if claimed is None:
            await db.rollback()
            return None
        await db.commit()
        row = (
            await db.execute(
                select(AssignmentRun, Assignment.name)
                .options(load_only(*_WAKE_FIELDS))
                .outerjoin(Assignment, Assignment.id == AssignmentRun.assignment_id)
                .where(AssignmentRun.id == run_id)
            )
        ).first()
    if row is None:
        return None
    run, name = row
    return claimed.conversation_id, claimed.user_id, run, name


# ── The wake ──────────────────────────────────────────────────────────────


def schedule(run_id: object) -> None:
    """Resume, after the grace, the conversation the ended run *run_id* is owed to; never raises."""
    parsed = _parse(run_id)
    if parsed is None:
        return
    try:
        task = asyncio.get_running_loop().create_task(_wake_after(parsed, WAKE_GRACE_SECONDS))
    except RuntimeError:
        return
    _WAKES.add(task)
    task.add_done_callback(_WAKES.discard)


async def _wake_after(run_id: uuid.UUID, delay: float) -> None:
    await asyncio.sleep(delay)
    await _wake_safely(run_id)


async def _wake_safely(run_id: uuid.UUID) -> None:
    try:
        await _wake(run_id)
    except Exception as exc:
        logger.warning(
            "Could not resume the conversation of assignment run %s: %s",
            run_id,
            format_exception_message(exc),
            exc_info=True,
        )


async def wake_owed_conversation(run_id: object) -> bool:
    """Tell the conversation the ended run *run_id* is owed to how it ended; ``False`` when nothing was told."""
    parsed = _parse(run_id)
    if parsed is None:
        return False
    return await _wake(parsed)


async def _wake(run_id: uuid.UUID) -> bool:
    claimed = await _claim(run_id)
    if claimed is None:
        return False
    conversation_id, user_id, run, assignment_name = claimed
    if run.status not in _WAKE_STATUSES:
        return False
    from app.services.parent_wake import wake_conversation

    logger.info(
        "Resuming conversation %s with the end of assignment run %s (%s)",
        conversation_id,
        run.id,
        run.status,
    )
    await wake_conversation(
        conversation_id=str(conversation_id),
        user_id=str(user_id),
        wake_text=format_wake_prompt(run, assignment_name),
        label=WAKE_LABEL,
        delegated_at=run.created_at,
    )
    return True


async def sweep_lost_wakes(now: datetime | None = None) -> int:
    """Resume the conversations still owed runs that ended a while ago; how many were resumed.

    A wake runs in the worker that ended the run, after the grace: a worker
    stopped meanwhile takes it along, and the conversation would wait for
    ever. The claim is atomic, so a sweep and a late wake never both tell the
    same run, and every worker may sweep. Each wake runs in a task of its own.
    Notices told a day ago, or never told in a month, are deleted.
    """
    from app.core.database import async_session

    now = now or datetime.now(UTC)
    ended_at = func.coalesce(AssignmentRun.completed_at, AssignmentRun.created_at)
    async with async_session() as db:
        await db.execute(
            delete(AssignmentRunConversationNotice).where(
                or_(
                    AssignmentRunConversationNotice.told_at < now - TOLD_RETENTION,
                    AssignmentRunConversationNotice.created_at < now - UNTOLD_RETENTION,
                )
            )
        )
        await db.commit()
        owed = (
            (
                await db.execute(
                    select(AssignmentRunConversationNotice.run_id)
                    .join(AssignmentRun, AssignmentRun.id == AssignmentRunConversationNotice.run_id)
                    .where(
                        AssignmentRunConversationNotice.told_at.is_(None),
                        AssignmentRun.status.in_(_terminal_statuses()),
                        ended_at <= now - timedelta(seconds=SWEEP_AFTER_SECONDS),
                        ended_at > now - SWEEP_WINDOW,
                    )
                    .limit(SWEEP_MAX_RUNS)
                )
            )
            .scalars()
            .all()
        )
    for run_id in owed:
        task = asyncio.get_running_loop().create_task(_wake_safely(run_id))
        _WAKES.add(task)
        task.add_done_callback(_WAKES.discard)
    if owed:
        logger.warning(
            "Resuming %d conversation(s) whose assignment run wake was lost with its worker",
            len(owed),
        )
    return len(owed)


def _duration(run: AssignmentRun) -> str | None:
    from app.tools.wait_policy import format_wait_duration

    start = run.started_at or run.created_at
    if start is None or run.completed_at is None:
        return None
    return format_wait_duration((run.completed_at - start).total_seconds())


def format_wake_prompt(run: AssignmentRun, assignment_name: str | None) -> str:
    """The hidden per-turn text that resumes the conversation. Not system-prompt text."""
    what = f"Assignment run {run.id}" + (f" ({assignment_name})" if assignment_name else "")
    ended = "was skipped" if run.status == "skipped" else run.status
    duration = _duration(run)
    parts = [
        f"[Platform] {what}, which you triggered in this conversation, {ended}"
        + (f" after {duration}" if duration else "")
        + ". Nobody has told the user yet.",
        "Answer the user now from the run's outcome below: what it did and found, or why it did not finish, "
        "and carry on with their request if a step of it is still to do. Do not paste this platform notice.",
    ]
    if run.status == "completed":
        heading, body = "Result summary:", (run.result_summary or "").strip()
    else:
        heading, body = "Reason:", (run.error or run.result_summary or "").strip()
    if not body:
        parts.append("The run left no result summary.")
        return "\n\n".join(parts)
    if len(body) > WAKE_RESULT_CHARS:
        body = body[:WAKE_RESULT_CHARS].rstrip()
        parts.append(
            f"The text below was cut at {WAKE_RESULT_CHARS} characters; the run holds the rest "
            f"(get_run({run.id}))."
        )
    parts.extend([heading, body])
    return "\n\n".join(parts)
