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

"""Resume an idle parent chat turn when a background task becomes terminal.

The implicit wait and the wait-budget hand-off keep the parent loop alive
*while the same turn is still running*; they never wake an idle parent when a
child becomes terminal.

This module does. When a chat-originated
``SubAgentTask`` reaches ``completed`` or ``failed`` and the originating
conversation is no longer in an agent loop, we persist a hidden platform
user message (crash-safe for ``acquire_or_fold``) and start one parent
continuation turn. If a parent turn is already in flight, the wake is
folded into that turn as steering instead of starting a second loop.

Cancelled / delivery-suppressed / incomplete (wait-budget) tasks are
never woken: those have their own continuation or the user aborted.

The same turn resumes a conversation for work it started elsewhere: an A2A
partner's answer (:func:`schedule_partner_wake`) and a Video Studio
job (:func:`wake_conversation`).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import sys
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.redaction import known_secrets_scoped
from app.core.shutdown_finalizers import before_cancel, track_background_turn
from app.core.utils import format_exception_message, is_graceful_shutdown_cancellation, safe_uuid
from app.models.agent import Agent
from app.models.chat import Conversation
from app.models.sub_agent_task import SubAgentTask
from app.services import chat_turn_stream
from app.services.conversation_files import (
    conversation_file_refs,
    register_conversation_files,
)
from app.services.integration_credentials import (
    UNAVAILABLE_INTEGRATIONS_META_KEY,
    clean_unavailable_integrations,
)
from app.services.mention_dispatch import (
    DISPATCH_BUDGET_SECONDS,
    MENTIONED_AGENT_HANDOVER_TYPES,
    MentionReply,
    TurnMetering,
    TurnRefusedError,
    answer_folded_mentions,
    compose_channel_reply,
    conversation_agent_target,
    folded_mention_history,
    folded_mentions,
    quota_refused,
    reply_metadata,
    run_on_own_session,
)
from app.services.sub_agent_files import (
    attach_rows_to_message,
    format_result_files_section,
    load_attachment_rows,
    load_result_file_refs,
    register_context_files,
    task_result_file_ids,
)
from app.tools.agent_loop import SteeringQueue

logger = logging.getLogger(__name__)

PARENT_WAKE_SOURCE = "background_task_wake"
#: The metadata a wake's user message is stored with, never rewritten: what
#: tells a hidden wake apart, in SQL and in Python (``chat_messages``).
PARENT_WAKE_METADATA = json.dumps({"hidden": True, "source": PARENT_WAKE_SOURCE})
#: Stands in for a task id in the log and activity trail of a partner wake.
#: An external partner's task id is the PARTNER's, and would read here as one
#: of ours.
PARTNER_WAKE_LABEL = "a2a-partner"
#: The key a wake turn's reply (and its ``thinking`` / ``done`` frames) names
#: the work it reports on under: a reply nobody asked for in the chat,
#: told apart from a second answer to the last message (#11796).
WAKE_META_KEY = "wake"
#: What kind of work woke the turn, so the chat names it in its own words:
#: a sub-agent task finished, an A2A partner answered, a job (a Video Studio
#: job, an assignment run) ended. A note stored before the kind existed has
#: none and keeps the task wording.
WAKE_KIND_TASK = "task"
WAKE_KIND_PARTNER = "partner"
WAKE_KIND_JOB = "job"


def wake_note(*, task_id: str, agent_name: str | None, kind: str = WAKE_KIND_TASK) -> dict[str, str | None]:
    """What a wake turn's reply says woke it: the source, the task, its agent and the kind.

    ``task_id`` is the sub-agent task's id, :data:`PARTNER_WAKE_LABEL` for an
    A2A partner, or the label of other work (a Video Studio job). ``kind`` is
    :data:`WAKE_KIND_TASK`, :data:`WAKE_KIND_PARTNER` or :data:`WAKE_KIND_JOB`.
    """
    return {
        "source": PARENT_WAKE_SOURCE,
        "task_id": task_id,
        "agent_name": agent_name or None,
        "kind": kind,
    }


def wake_start_frame(note: dict[str, str | None]) -> dict[str, object]:
    """The frame a wake turn opens with on the conversation's streams."""
    return {"type": "thinking", WAKE_META_KEY: note}


_MAX_PARENT_WAKE_FOLLOWUPS = 3
# Hidden wake prompt carries the specialist output so the parent can
# summarize without a tool round. Longer results stay on the task and
# the prompt then requires ``get_full_task_result`` before answering.
_PARENT_WAKE_RESULT_CHARS = 32_000

# Strong refs so a wake task is not GC'd mid-run (asyncio only keeps weak
# refs to Tasks). Entries remove themselves on completion.
_PARENT_WAKE_TASKS: set[asyncio.Task] = set()


def format_parent_wake_prompt(
    *,
    task_id: str,
    agent_name: str,
    terminal_status: str,
    result_text: str | None = None,
    error_text: str | None = None,
    result_files: list | None = None,
) -> str:
    """Per-turn user text that resumes an idle parent. Not system-prompt text.

    Carries the specialist's full output (capped) so the parent summarizes
    from the actual result instead of a truncated extract dumped into the
    thread. Hidden from the user via ``PARENT_WAKE_SOURCE``.

    ``result_files`` are the ``FileRef``s of the files the specialist produced.
    They are named here - id, filename, type, size - because the
    result text only carries their ``[[FILE:id]]`` markers, and the wake turn
    puts the same refs into the parent's FileContext so ``read_file_content``
    resolves them.
    """
    outcome = "failed" if terminal_status == "failed" else "completed"
    raw = (error_text if outcome == "failed" else result_text) or ""
    body = raw.strip()
    truncated = len(body) > _PARENT_WAKE_RESULT_CHARS
    if truncated:
        body = body[:_PARENT_WAKE_RESULT_CHARS].rstrip()

    parts = [
        f"[Platform] Background task {task_id} ({agent_name}) {outcome}.",
        "Read the specialist result below. Then answer the user yourself: "
        "a complete summary of what was done, the findings that matter for "
        "their original request, and any remaining open items. Do not paste "
        "this platform notice. Do not mention extracts, background-task "
        "banners, or that a result is available on the task. Do not dump "
        "the raw specialist output verbatim. Do not wait passively.",
    ]
    if truncated:
        parts.append(
            f"The result below was truncated at {_PARENT_WAKE_RESULT_CHARS} "
            f"of {len(raw.strip()):,} characters. You MUST call "
            f"get_full_task_result({task_id}) and read the complete output "
            "before answering; a long answer comes by window, each read naming "
            "the next offset to pass until it says the end."
        )
        parts.append(body)
    elif body:
        parts.append("Full specialist result:")
        parts.append(body)
    else:
        parts.append("The specialist returned no output.")
    files_section = format_result_files_section(
        result_files or [], heading="Files produced by the specialist"
    )
    if files_section:
        parts.append(files_section)
    return "\n\n".join(parts)


def should_wake_parent(task: SubAgentTask | None) -> bool:
    """True when a terminal child should resume its originating conversation.

    Skip cancelled / delivery-suppressed / incomplete tasks, tasks with no
    conversation, tasks already woken, and anything that is not
    ``completed`` or ``failed``.
    """
    if task is None:
        return False
    if not getattr(task, "conversation_id", None):
        return False
    if getattr(task, "status", None) not in ("completed", "failed"):
        return False
    meta = getattr(task, "metadata_json", None) or {}
    if not isinstance(meta, dict):
        meta = {}
    return not bool(meta.get("delivery_suppressed") or meta.get("parent_woken"))


def schedule_parent_wake(
    *,
    task_id: str,
    conversation_id: str,
    user_id: str,
    agent_name: str,
    terminal_status: str,
) -> None:
    """Fire-and-forget parent resume. Never holds the sub-agent lease."""

    async def _run() -> None:
        try:
            await _wake_parent_conversation(
                task_id=task_id,
                conversation_id=conversation_id,
                user_id=user_id,
                agent_name=agent_name,
                terminal_status=terminal_status,
            )
        except Exception:
            logger.warning(
                "Parent wake failed for task %s conversation %s",
                task_id,
                conversation_id,
                exc_info=True,
            )

    _spawn_wake(_run(), label=task_id)


def _spawn_wake(coro, *, label: str) -> None:
    """Run a wake detached, holding a strong reference until it finishes.

    asyncio keeps only weak references to tasks, so a bare ``create_task``
    can be collected mid-run. The shutdown drain ends it like a request it
    cuts, and a recycle waits for it.
    """
    try:
        task = asyncio.create_task(coro)
    except RuntimeError:
        coro.close()
        logger.debug("No running event loop to schedule a wake for %s", label)
        return
    _PARENT_WAKE_TASKS.add(task)
    task.add_done_callback(_PARENT_WAKE_TASKS.discard)
    track_background_turn(task)


async def _run_turn_for(
    db,
    conv: Conversation,
    *,
    user_id: str,
    wake_text: str,
    task_id: str,
    result_files: list | None = None,
    partner_wake: bool = False,
    delegated_at: datetime | None = None,
    agent_name: str | None = None,
    kind: str = WAKE_KIND_TASK,
) -> bool:
    """Run one wake turn against a conversation already loaded on ``db``.

    Whether the turn was refused: agentic AI is unavailable here.

    The row is snapshotted into a detached object first, so follow-up turns
    survive ``expire_on_commit``. ``user_id`` is the fallback identity: the
    conversation's own owner runs the turn where it has one.

    ``result_files`` are the finished task's produced files, put into
    the wake turn's FileContext so the parent can read them. ``partner_wake``
    says *wake_text* is an A2A partner's answer. The reply and the turn's
    opening frame name the work that woke it (:func:`wake_note`, with
    ``agent_name`` and ``kind``), so the chat never reads it as a second answer.
    """
    run_as_user_id = str(conv.user_id) if conv.user_id else user_id
    if not run_as_user_id:
        logger.debug(
            "Wake skipped for %s — conversation %s has no user to run as",
            task_id,
            conv.id,
        )
        return False

    note = wake_note(task_id=task_id, agent_name=agent_name, kind=kind)
    return await _run_parent_wake_turn(
        db,
        conversation=_conversation_snapshot(conv),
        user_id=run_as_user_id,
        wake_text=wake_text,
        persist_user=True,
        followup_depth=0,
        task_id=task_id,
        activity_user_id=user_id,
        result_files=result_files,
        partner_wake=partner_wake,
        delegated_at=delegated_at,
        start_frame=wake_start_frame(note),
        reply_meta_extra={WAKE_META_KEY: note},
    )


def _conversation_snapshot(conv: Conversation) -> SimpleNamespace:
    """The conversation's scalars a wake turn reads, detached from its session."""
    return SimpleNamespace(
        id=conv.id,
        user_id=conv.user_id,
        agent_id=conv.agent_id,
        channel=conv.channel,
        channel_thread_id=conv.channel_thread_id,
        tool_overrides_json=getattr(conv, "tool_overrides_json", None),
    )


async def _wake_refused(db, *, label: str) -> bool:
    """Whether the wake turn must not start: agentic AI is unavailable.

    Asked by :func:`_run_parent_wake_turn` once the wake's text is persisted,
    and before the turn is acquired, so a refused wake starts no model. Its
    text waits in the history; a task's files, which only the turn would have
    carried, are delivered by :func:`_deliver_after_refused_wake`.
    """
    from app.services.agentic_gate import agentic_refusal

    refusal = await agentic_refusal(db)
    if refusal is None:
        return False
    logger.info(
        "Wake for %s not started: agentic AI unavailable (%s)", label, refusal
    )
    return True


async def _deliver_after_refused_wake(
    *, task_id: str, conversation_id: str, agent_name: str
) -> None:
    """Deliver a task whose wake was refused as a task whose parent does not
    wake is delivered.

    The refused wake started no turn, so nothing carried the task's files: the
    wake text in the history names them as attached to a context that never
    existed. The parent does not wake now, so the task gets that case's
    delivery - the completion notice (``sub_agent._deliver_task_result``),
    whose attachments a later turn rehydrates - on that case's conditions
    (``_should_deliver_completion_notice``: a completed task with a result,
    not suppressed, from an agent chat is enabled for). Never raises: a failed
    delivery costs the notice, not the wake.
    """
    from app.core.database import async_session
    from app.services.sub_agent import (
        _deliver_task_result,
        _should_deliver_completion_notice,
    )

    try:
        async with async_session() as db:
            task = (
                await db.execute(
                    select(SubAgentTask).where(SubAgentTask.id == uuid.UUID(task_id))
                )
            ).scalar_one_or_none()
            if task is None or task.status != "completed":
                return
            target = None
            target_agent_id = getattr(task, "target_agent_id", None)
            if target_agent_id:
                target = (
                    await db.execute(select(Agent).where(Agent.id == target_agent_id))
                ).scalar_one_or_none()
            if not _should_deliver_completion_notice(
                task,
                disable_chat=bool(getattr(target, "disable_chat", False)),
                has_response=bool(task.result),
            ):
                return
            result_text = task.result or ""
        await _deliver_task_result(
            task_id=task_id,
            conversation_id=conversation_id,
            agent_name=agent_name,
            result_text=result_text,
        )
    except Exception:
        logger.warning(
            "Could not deliver task %s after its refused wake", task_id, exc_info=True
        )


async def _wake_parent_conversation(
    *,
    task_id: str,
    conversation_id: str,
    user_id: str,
    agent_name: str,
    terminal_status: str,
) -> None:
    """Persist the wake message (if needed) and run or fold the parent turn."""
    from app.core.database import async_session
    from app.services.sub_agent import publish_task_event

    async with async_session() as db:
        result = await db.execute(
            select(SubAgentTask).where(SubAgentTask.id == uuid.UUID(task_id))
        )
        task_row = result.scalar_one_or_none()
        if not should_wake_parent(task_row):
            return

        # The files the child produced: named in the wake prompt and
        # handed to the wake turn's FileContext. ``load_result_file_refs``
        # never raises and reads on its OWN session - a lookup failure costs
        # the files, not the wake, and cannot leave this session in a failed
        # transaction the conversation read below would then trip over.
        result_files = await load_result_file_refs(task_result_file_ids(task_row))

        wake_text = format_parent_wake_prompt(
            task_id=task_id,
            agent_name=agent_name,
            terminal_status=terminal_status,
            result_text=getattr(task_row, "result", None),
            error_text=getattr(task_row, "error", None),
            result_files=result_files,
        )

        conv_result = await db.execute(
            select(Conversation).where(Conversation.id == uuid.UUID(conversation_id))
        )
        conv = conv_result.scalar_one_or_none()
        if not conv:
            logger.debug(
                "Parent wake skipped for task %s — conversation %s missing",
                task_id,
                conversation_id,
            )
            return

        meta = dict(task_row.metadata_json or {})
        meta["parent_woken"] = True
        task_row.metadata_json = meta
        await db.commit()

        refused = await _run_turn_for(
            db,
            conv,
            user_id=user_id,
            wake_text=wake_text,
            task_id=task_id,
            result_files=result_files,
            agent_name=agent_name,
        )

    if refused:
        await _deliver_after_refused_wake(
            task_id=task_id, conversation_id=conversation_id, agent_name=agent_name
        )

    await publish_task_event(
        user_id,
        "parent_wake",
        task_id,
        conversation_id=conversation_id,
        status="completed",
    )


def schedule_partner_wake(
    *,
    conversation_id: str,
    user_id: str,
    partner_name: str,
    result_text: str | None = None,
    error_text: str | None = None,
    delegated_at: datetime | None = None,
) -> None:
    """Resume a conversation whose external partner task has finished.

    The sibling of :func:`schedule_parent_wake`, for work with no
    ``SubAgentTask`` row to re-read: an A2A partner's task lives on the
    partner's infrastructure, so the conversation, the identity and the result
    are passed rather than looked up. Everything downstream is shared.

    ``delegated_at`` is when the work was sent: a Stop recorded after it
    (``Conversation.stopped_at``) means the user stopped this work, and the
    answer is not delivered.
    """

    async def _run() -> None:
        try:
            await _wake_conversation_with(
                conversation_id=conversation_id,
                user_id=user_id,
                wake_text=format_parent_wake_prompt(
                    task_id=PARTNER_WAKE_LABEL,
                    agent_name=partner_name,
                    terminal_status="completed" if error_text is None else "failed",
                    result_text=result_text,
                    error_text=error_text,
                ),
                delegated_at=delegated_at,
                agent_name=partner_name,
            )
        except Exception:
            logger.warning(
                "Partner wake failed for conversation %s", conversation_id,
                exc_info=True,
            )

    _spawn_wake(_run(), label=conversation_id)


async def wake_conversation(
    *,
    conversation_id: str,
    user_id: str,
    wake_text: str,
    label: str,
    result_files: list | None = None,
    delegated_at: datetime | None = None,
) -> None:
    """Resume a conversation with the outcome of work it started outside a sub-agent task.

    A Video Studio job that ended after the turn that started it: *wake_text*
    is composed by the caller, *result_files* are the files it carries, and
    *label* names the work in the log and the activity trail. Everything
    downstream is the task wake's - a hidden platform message, one turn, a
    fold into a turn already running - and a Stop recorded at or after
    *delegated_at*, when the work was started, keeps the conversation idle.
    """
    await _wake_conversation_with(
        conversation_id=conversation_id,
        user_id=user_id,
        wake_text=wake_text,
        delegated_at=delegated_at,
        label=label,
        partner_wake=False,
        result_files=result_files,
    )


#: Names the follow-up of a chat turn's unanswered folds in the log and the
#: activity trail, where a wake names its task.
FOLD_FOLLOW_UP_LABEL = "steering-follow-up"


def schedule_fold_follow_up(
    *, conversation_id: str, user_id: str | None, folds: list
) -> bool:
    """Answer the messages a chat turn took and ended without answering.

    A message folded into a running turn told its sender the turn took it.
    One the turn never read - folded after its last look at the inbox while
    it still held the lock, drained by its release
    (``chat_turn_guard.release_turn_and_drain``), or bridged into its queue
    after its loop returned (``PartnerAnswerQueue.take_unanswered``) - is
    answered here, by a follow-up turn started once the lock is released:
    the follow-up of a wake turn (:func:`_follow_up_folds`), whichever turn
    took them. The streamed web turn and the REST turn call it; a wake turn
    follows its own up.

    The caller hands back the agents the folds mention first
    (``requeue_owed_mentions``). The turn runs as the conversation's owner,
    else *user_id*. Returns whether a follow-up was scheduled: not for folds
    handed back mentions-only, whose text was answered already.
    """
    folds = [fold for fold in folds if not getattr(fold, "mentions_only", False)]
    if not folds:
        return False
    _spawn_wake(
        _answer_unanswered_folds(conversation_id=conversation_id, user_id=user_id, folds=folds),
        label=conversation_id,
    )
    return True


async def _answer_unanswered_folds(
    *, conversation_id: str, user_id: str | None, folds: list
) -> None:
    from app.core.database import async_session

    try:
        # A partner's answer whose work the user stopped is not followed up
        #: it is in the history, like one the bridge holds back.
        folds = await without_stopped_partner_answers(conversation_id, folds)
        if not folds:
            return
        async with async_session() as db:
            conv = (
                await db.execute(
                    select(Conversation).where(Conversation.id == uuid.UUID(conversation_id))
                )
            ).scalar_one_or_none()
            run_as = str(conv.user_id) if conv is not None and conv.user_id else user_id
            if conv is None or not run_as:
                logger.info(
                    "%d folded message(s) of conversation %s not followed up: "
                    "no conversation or no user to run as",
                    len(folds),
                    conversation_id,
                )
                return
            logger.info(
                "%d message(s) folded into a turn of conversation %s that "
                "ended first: answered by a follow-up turn",
                len(folds),
                conversation_id,
            )
            await _follow_up_folds(
                db,
                conversation=_conversation_snapshot(conv),
                user_id=run_as,
                folds=folds,
                followup_depth=0,
                task_id=FOLD_FOLLOW_UP_LABEL,
                activity_user_id=run_as,
            )
    except Exception:
        logger.warning(
            "Follow-up of the folded messages of conversation %s failed",
            conversation_id,
            exc_info=True,
        )


#: Labels the automatic resume of a cut turn in the log and the activity trail.
CUT_TURN_RESUME_LABEL = "auto-resume"


async def schedule_cut_turn_resume(cut) -> None:
    """Start the automatic resume of *cut* detached: the scheduler tick does not wait for the turn."""
    _spawn_wake(_resume_cut_turn_settled(cut), label=cut.conversation_id)


async def _resume_cut_turn_settled(cut) -> bool:
    """:func:`resume_cut_turn`, then drop the "being resumed" marker the sweep's take left.

    The marker keeps the notice on "resuming by itself" from the take until
    the resumed turn holds the conversation; once the resume ran or stood
    down, the conversation tells the truth again (Resume when it stood down).
    """
    from app.services.cut_turn_resume import put_back_cut_turn, resume_settled

    started = False

    def _started() -> None:
        nonlocal started
        started = True

    try:
        return await resume_cut_turn(cut, on_start=_started)
    except asyncio.CancelledError as exc:
        # Cut by the drain before its turn started (still reading the
        # conversation): nothing queued it again, the take had removed it.
        # Put the record back as taken; the next process resumes it.
        if not started and is_graceful_shutdown_cancellation(exc):
            await put_back_cut_turn(cut)
        raise
    finally:
        await resume_settled(cut.conversation_id)


async def resume_cut_turn(cut, *, on_start: Callable[[], None] | None = None) -> bool:
    """Resume a turn a platform stop cut, continuing the same user message.

    *cut* is the ``cut_turn_resume.CutTurn`` the sweep took. The turn runs
    like a wake (:func:`_run_parent_wake_turn`) with no new message: its
    history ends at the stored user message, the checkpoint of the cut turn
    is replayed after it, and this turn checkpoints its own work. Whether a
    turn ran.

    Not resumed, the person keeps the one-click Resume of the interrupted
    reply: the conversation or the message is gone, the person sent another
    message or stopped the conversation since the cut. A turn cut again by a
    platform stop is queued again: while each resume completes a tool round of
    its own, for ``MAX_AUTO_RESUME_SPAN_SECONDS`` after the first cut
    and up to ``MAX_TOTAL_AUTO_RESUMES``; ``MAX_AUTO_RESUMES`` in a row
    with no new round at most. *on_start* is called once the turn starts:
    a cancellation before it found the conversation unread and nothing
    queued again.
    """
    from app.core.database import async_session
    from app.models.chat import Message
    from app.services import turn_checkpoint
    from app.services.cut_turn_resume import (
        RESUMED_PARENT_RUN,
        ParentRunHandoff,
        cut_turn_trace_id,
        mark_trace_resumed_as,
        resumed_from_meta,
        resumed_from_trace_meta,
        resuming_frame,
    )
    from app.services.langfuse_tracing import RootTraceLink, next_root_trace

    conv_uuid = safe_uuid(cut.conversation_id)
    msg_uuid = safe_uuid(cut.user_message_id)
    if conv_uuid is None or msg_uuid is None:
        return False
    async with async_session() as db:
        conv = (
            await db.execute(select(Conversation).where(Conversation.id == conv_uuid))
        ).scalar_one_or_none()
        msg = (
            await db.execute(
                select(Message).where(Message.id == msg_uuid, Message.conversation_id == conv_uuid)
            )
        ).scalar_one_or_none()
        if conv is None or msg is None or msg.role != "user":
            logger.info("Cut turn of conversation %s not resumed: its message is gone", conv_uuid)
            return False
        cut_at = _parse_cut_at(cut.cut_at)
        # A message the PERSON wrote after the one the cut turn answered
        # A hidden wake is the platform's, not the
        # person's: a background task the turn delegated, cut and resumed with
        # it, wakes the conversation often before the sweep takes the cut
        # turn, and that never meant the person moved on. Any visible user
        # message still counts, the one a steer stored included: the cut
        # record's time is set after the cut, so it cannot tell a steer taken
        # in before the cut from one written after it.
        newer = (
            await db.execute(
                select(Message.id)
                .where(
                    Message.conversation_id == conv_uuid,
                    Message.role == "user",
                    Message.sequence > msg.sequence,
                    or_(Message.metadata_json.is_(None), ~Message.metadata_json.contains(_HIDDEN_MESSAGE_MARK)),
                )
                .limit(1)
            )
        ).scalar_one_or_none()
        # A message the person steered INTO the cut turn is not one written
        # after it: stored before the cut turn's interrupted reply, it
        # was the turn's to answer, so the resume answers it, on the latest.
        answered = msg
        if newer is not None:
            steered = await _steered_before_cut(db, conv_uuid, msg.sequence)
            if steered is None:
                # Written between the cut and this resume, the message was
                # folded into the cut turn (its lock was still held) and no
                # turn of its own ever ran: left there, nobody answers it.
                steered = await _left_unanswered_since_cut(db, conv_uuid, msg.sequence)
                if steered is not None:
                    logger.warning(
                        "Cut turn of conversation %s resumed on message %s the person wrote after the cut "
                        "and no turn answered",
                        conv_uuid,
                        steered.id,
                    )
            if steered is None:
                logger.warning(
                    "Cut turn of conversation %s not resumed: the person wrote message %s after it (turn cut at %s)",
                    conv_uuid,
                    newer,
                    cut.cut_at or "unknown",
                )
                return False
            logger.warning(
                "Cut turn of conversation %s resumed on message %s the person steered into it before the cut",
                conv_uuid,
                steered.id,
            )
            answered = steered
        if cut_at is not None and await _stopped_since(db, conv_uuid, cut_at):
            logger.warning(
                "Cut turn of conversation %s not resumed: the person stopped it since the cut at %s",
                conv_uuid,
                cut.cut_at,
            )
            return False
        run_as = str(conv.user_id) if conv.user_id else cut.user_id
        resumed, replay = await turn_checkpoint.load_replay(db, conv_uuid, user_id=run_as)
        checkpoint = turn_checkpoint.TurnCheckpoint(
            conv_uuid, turn_id=cut.turn_id, resumed=resumed, user_id=run_as
        )
        conv_snap = SimpleNamespace(
            id=conv.id,
            user_id=conv.user_id,
            agent_id=conv.agent_id,
            channel=conv.channel,
            channel_thread_id=conv.channel_thread_id,
            tool_overrides_json=getattr(conv, "tool_overrides_json", None),
        )
        user_text = answered.content or ""
        sequence = answered.sequence
        # The record names the trace of the hop it resumes when the cutting
        # process knew it (a resume cut in turn); the query is the fallback,
        # and on a chain it could return an earlier hop not flushed after.
        cut_trace = cut.trace_id or await cut_turn_trace_id(
            db, cut, not_before=getattr(msg, "created_at", None)
        )
        # The restart latency alone: from this hop's cut to its resume's
        # start, apart from the resumed turn's own remaining work.
        start_latency = _seconds_since(cut_at)
        _observe_resume_start(start_latency)
        logger.warning(
            "Resuming the chat turn of conversation %s cut by a platform %s (automatic resume %d of %d, "
            "%d completed tool call(s) replayed, cut trace %s, started %s s after the cut)",
            conv_uuid,
            cut.cause,
            cut.resumes + 1,
            _max_auto_resumes(),
            sum(len(m.tool_calls or ()) for m in resumed.messages),
            cut_trace or "unknown",
            f"{start_latency:.0f}" if start_latency is not None else "unknown",
        )
        trace_link: RootTraceLink | None = None
        requeued = False
        teardown_queued = False
        # The cut turn's chat-handover parent run, kept by the shutdown sweep:
        # the resumed turn adopts it.
        handoff = ParentRunHandoff(cut.parent_run_id) if cut.parent_run_id else None

        async def _queue_again(lock_token: str) -> None:
            # Called by the turn's own teardown while it still holds the
            # conversation, before its interrupted close and release. After
            # the drain's hand-over it writes the record again, now with the
            # lock token, so a process that dies before its release frees it.
            nonlocal requeued, teardown_queued
            if not teardown_queued:
                requeued = teardown_queued = True
                await _requeue_cut_again(
                    cut, checkpoint, lock_token=lock_token, trace_id=_own_trace_id(trace_link)
                )

        async def _hand_over_before_cancel() -> None:
            # Called by the shutdown drain BEFORE it cancels this turn: the
            # next resume is queued while the turn still runs, so neither a
            # slow unwinding nor the pod's kill loses it, and the cut trace
            # names this hop at once.
            nonlocal requeued
            requeued = True
            await _enqueue_next_resume(cut, checkpoint, trace_id=_own_trace_id(trace_link))
            _link_cut_trace_on_drain(cut_trace, trace_link)

        handoff_token = RESUMED_PARENT_RUN.set(handoff)
        try:
            if on_start is not None:
                on_start()
            with (
                before_cancel(_hand_over_before_cancel),
                next_root_trace(resumed_from_trace_meta(cut, cut_trace_id=cut_trace)) as trace_link,
            ):
                refused = await _run_parent_wake_turn(
                    db,
                    conversation=conv_snap,
                    user_id=run_as,
                    wake_text=user_text,
                    persist_user=False,
                    followup_depth=0,
                    task_id=CUT_TURN_RESUME_LABEL,
                    activity_user_id=run_as,
                    # A wake's text is the platform's; a person's message is not.
                    wake_from_platform=cut.entry == WAKE_ENTRY,
                    history_before_sequence=sequence + 1,
                    replay=replay,
                    on_checkpoint=checkpoint.offer,
                    start_frame=resuming_frame(cut),
                    reply_meta_extra=resumed_from_meta(cut, resumed_as=None),
                    on_cut=_queue_again,
                )
        except asyncio.CancelledError as exc:
            if is_graceful_shutdown_cancellation(exc):
                # Already queued by the turn's teardown, unless it was cut
                # before it took the conversation.
                if not requeued:
                    requeued = True
                    await _requeue_cut_again(cut, checkpoint, trace_id=_own_trace_id(trace_link))
                # The cut trace names its resume even when the resume is cut
                # in turn: before, only a resume that finished linked
                # it, so a chain of rollouts left no ``resumed_as`` at all.
                _link_cut_trace_on_drain(cut_trace, trace_link)
            raise
        except Exception:
            await checkpoint.aclose(flush=True)
            raise
        finally:
            RESUMED_PARENT_RUN.reset(handoff_token)
        if requeued:
            # The drain queued the next resume and the turn still finished
            # before its cancellation: the answer is stored, nothing to resume.
            from app.services.cut_turn_resume import forget_cut_turn

            with contextlib.suppress(Exception):
                await forget_cut_turn(cut.conversation_id, user_message_id=cut.user_message_id)
        # The cut turn's trace names the trace that resumed it (not awaited
        # on a cancellation: the drain does not wait for a trace link).
        if trace_link is not None and trace_link.trace_id is not None:
            await mark_trace_resumed_as(db, cut_trace, trace_link.trace_id)
        if refused:
            # Agentic AI is off here: the checkpoint stays for the one click.
            await checkpoint.aclose(flush=True)
            return False
        await checkpoint.aclose(flush=False)
        if checkpoint.may_hold_row:
            with contextlib.suppress(Exception):
                await turn_checkpoint.discard(db, conv_uuid)
                await db.commit()
        _note_resumed_chat_cut(cut, conv_snap, start_latency=start_latency)
        return True


def _seconds_since(moment: datetime | None) -> float | None:
    """Seconds from *moment* to now, ``None`` when unknown."""
    if moment is None:
        return None
    return max(0.0, (datetime.now(UTC) - moment).total_seconds())


def _observe_resume_start(seconds: float | None) -> None:
    """Observe a cut chat turn's restart latency on the rollout histogram; never raises."""
    from app.services.rollout_counters import observe_chat_turn_resume_start

    with contextlib.suppress(Exception):
        observe_chat_turn_resume_start(seconds)


def _note_resumed_chat_cut(cut: Any, conversation: Any, *, start_latency: float | None = None) -> None:
    """Record the answered resume of *cut* in the friction ledger and the rollout counter.

    How long the person waited runs from the first cut of the message to now,
    when the resumed answer is stored: past the person-visible bound it is an
    undisposed ``turn_resumed`` friction of the agent (#11924). *start_latency*
    is how long this hop waited for its resume to start after its cut, so the
    record splits the restart from the resumed turn's own work. Never raises.
    """
    from app.services.frictions import note_chat_turn_resumed

    try:
        first = _parse_cut_at(cut.first_cut)
        waited = (datetime.now(UTC) - first).total_seconds() if first is not None else None
        note_chat_turn_resumed(
            agent_id=getattr(conversation, "agent_id", None),
            user_id=getattr(conversation, "user_id", None) or cut.user_id,
            run_id=cut.parent_run_id,
            conversation_id=cut.conversation_id,
            entry=cut.entry,
            channel=getattr(conversation, "channel", None),
            waited_seconds=waited,
            resumes=cut.resumes,
            start_latency_seconds=start_latency,
        )
    except Exception:
        logger.debug("Resumed chat cut not recorded", exc_info=True)


async def _steered_before_cut(db, conversation_id: uuid.UUID, after_sequence: int):
    """The latest message the person steered into the cut turn, or ``None``.

    A steer is stored as a user message while the turn runs, so it sits after
    the message the cut turn answered and before the turn's interrupted reply
    (the first assistant message after it, marked ``interrupted``). The
    latest visible user message is returned only when it sits before that
    reply: one after it was written once the turn was cut, and the person's
    own turn answers it. No interrupted reply stored (its write failed): ``None``,
    the resume stands down as before.
    """
    from app.models.chat import Message
    from app.services.interrupted_turn import INTERRUPTED_META_KEY

    reply = (
        await db.execute(
            select(Message)
            .where(
                Message.conversation_id == conversation_id,
                Message.role == "assistant",
                Message.sequence > after_sequence,
            )
            .order_by(Message.sequence.asc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if reply is None:
        return None
    try:
        meta = json.loads(reply.metadata_json) if reply.metadata_json else {}
    except (TypeError, ValueError):
        meta = {}
    if not isinstance(meta, dict) or meta.get(INTERRUPTED_META_KEY) is not True:
        return None
    latest = (
        await db.execute(
            select(Message)
            .where(
                Message.conversation_id == conversation_id,
                Message.role == "user",
                Message.sequence > after_sequence,
                or_(Message.metadata_json.is_(None), ~Message.metadata_json.contains(_HIDDEN_MESSAGE_MARK)),
            )
            .order_by(Message.sequence.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if latest is None or latest.sequence > reply.sequence:
        return None
    return latest


async def _left_unanswered_since_cut(db, conversation_id: uuid.UUID, after_sequence: int):
    """The latest message the person wrote after the cut turn's that no turn answered, or ``None``.

    A message sent between a platform stop and the resume sweep finds the
    conversation lock still held by the cut turn (its process drains, or died
    and the lock waits out its grace), so it is folded into that turn and no
    turn of its own starts; the resume used to stand down on it as "the person
    moved on", and the message stayed unanswered. It is the resume's to answer
    when no assistant message follows it and no turn holds the conversation
    now (one that does answers it from its history). ``None`` otherwise: the
    person's own turn answered it, or runs.
    """
    from app.models.chat import Message
    from app.services import chat_turn_guard

    latest = (
        await db.execute(
            select(Message)
            .where(
                Message.conversation_id == conversation_id,
                Message.role == "user",
                Message.sequence > after_sequence,
                or_(Message.metadata_json.is_(None), ~Message.metadata_json.contains(_HIDDEN_MESSAGE_MARK)),
            )
            .order_by(Message.sequence.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if latest is None:
        return None
    answered = (
        await db.execute(
            select(Message.id)
            .where(
                Message.conversation_id == conversation_id,
                Message.role == "assistant",
                Message.sequence > latest.sequence,
            )
            .limit(1)
        )
    ).scalar_one_or_none()
    if answered is not None:
        return None
    try:
        if await chat_turn_guard.is_turn_active(str(conversation_id)):
            return None
    except Exception:
        return None
    return latest


def _max_auto_resumes() -> int:
    from app.services.cut_turn_resume import MAX_TOTAL_AUTO_RESUMES

    return MAX_TOTAL_AUTO_RESUMES


def _parse_cut_at(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _link_cut_trace_on_drain(cut_trace: object, trace_link) -> None:
    """Record ``resumed_as`` on the cut trace from a shutdown finalizer, on a session of its own."""
    if not cut_trace or trace_link is None or trace_link.trace_id is None:
        return
    from app.core.shutdown_finalizers import start_shutdown_finalizer

    async def _link() -> None:
        from app.core.database import async_session
        from app.services.cut_turn_resume import mark_trace_resumed_as

        async with async_session() as session:
            await mark_trace_resumed_as(session, cut_trace, trace_link.trace_id)

    with contextlib.suppress(Exception):
        start_shutdown_finalizer(_link(), name=f"cut-trace-link:{cut_trace}")


def _own_trace_id(trace_link) -> str | None:
    """The durable trace a resumed turn opened, ``None`` before it opened one."""
    trace_id = getattr(trace_link, "trace_id", None)
    return str(trace_id) if trace_id else None


async def _requeue_cut_again(
    cut, checkpoint, *, lock_token: str | None = None, trace_id: str | None = None
) -> None:
    """A resumed turn cut by the next platform stop: its work kept, queued once more.

    A resume that completed a tool round of its own (``checkpoint.advanced``)
    made progress: the stalled count starts over, so a long turn crossing
    many rollouts keeps resuming by itself. One that completed none
    adds one to it; :data:`cut_turn_resume.MAX_AUTO_RESUMES` in a row stop it.
    *lock_token* is the conversation lock the cut resume still holds when it
    is queued from its own teardown: a lock left holding it past the
    drain belongs to a process that died, and the sweep frees it.
    *trace_id* is the trace this resume opened: the next resume links to this
    hop, not to the one before (``None``: it opened none, the next resume
    links to the hop this one resumed).
    """
    from app.core.shutdown_finalizers import start_shutdown_finalizer

    start_shutdown_finalizer(
        checkpoint.aclose(flush=True), name=f"turn-checkpoint:{cut.conversation_id}"
    )
    await _enqueue_next_resume(cut, checkpoint, lock_token=lock_token, trace_id=trace_id)


async def _enqueue_next_resume(
    cut, checkpoint, *, lock_token: str | None = None, trace_id: str | None = None
) -> None:
    """Queue the next automatic resume of a resumed turn a platform stop cuts. Never raises.

    Idempotent: the record is written whole, so the drain's hand-over and the
    turn's own teardown may both write it (:func:`_requeue_cut_again`).
    """
    from app.core.worker_shutdown import drain_cut_cause
    from app.services.cut_turn_resume import enqueue_cut_turn
    from app.services.in_process_runs import chat_parent_run_for

    # The parent run the resume adopted or created, else the one the cut turn
    # had (still kept while it waits): the next resume adopts it.
    parent_run_id = chat_parent_run_for(cut.conversation_id) or cut.parent_run_id
    with contextlib.suppress(Exception):
        await enqueue_cut_turn(
            cut.conversation_id,
            user_id=cut.user_id,
            user_message_id=cut.user_message_id,
            turn_id=cut.turn_id,
            # Called on the drain's cancellation only: a platform stop.
            cause=drain_cut_cause(),
            resumes_made=cut.resumes + 1,
            stalled=0 if checkpoint.advanced else cut.stalled_resumes + 1,
            entry=cut.entry,
            lock_token=lock_token,
            parent_run_id=parent_run_id,
            # The span of a progressing turn runs from its first cut.
            first_cut_at=cut.first_cut,
            trace_id=trace_id or cut.trace_id,
        )


#: ``CutTurn.entry`` of a parent wake a platform stop cut: its text is
#: the platform's own, so the resume restores its secret markers.
WAKE_ENTRY = "wake"

#: How a hidden platform message (a wake) reads in ``Message.metadata_json``.
_HIDDEN_MESSAGE_MARK = '"hidden": true'


async def _queue_cut_wake(conv_id: object, *, user_id: str, message_id: str) -> None:
    """Queue the parent wake a platform stop cut for its automatic resume. Never raises."""
    from app.core.worker_shutdown import drain_cut_cause
    from app.services.cut_turn_resume import enqueue_cut_turn
    from app.services.in_process_runs import chat_parent_run_for

    with contextlib.suppress(Exception):
        await enqueue_cut_turn(
            conv_id,
            user_id=user_id,
            user_message_id=message_id,
            turn_id=None,
            cause=drain_cut_cause(),
            entry=WAKE_ENTRY,
            # The wake turn's chat-handover parent run, kept for its resume.
            parent_run_id=chat_parent_run_for(conv_id),
        )


async def _hand_on_cut_wake(
    exc: BaseException, conv_id: object, *, user_id: str, message_id: str | None
) -> bool:
    """Queue a stored wake a platform stop cut before its reply was stored; whether it was.

    Only the graceful drain's cancellation of a wake whose message this turn
    stored counts: a resumed turn (no message of its own) is queued again by
    :func:`resume_cut_turn`, any other cancellation is not a platform stop.
    """
    if (
        message_id is None
        or not isinstance(exc, asyncio.CancelledError)
        or not is_graceful_shutdown_cancellation(exc)
    ):
        return False
    await _queue_cut_wake(conv_id, user_id=user_id, message_id=message_id)
    return True


async def _wake_conversation_with(
    *,
    conversation_id: str,
    user_id: str,
    wake_text: str,
    delegated_at: datetime | None = None,
    label: str = PARTNER_WAKE_LABEL,
    partner_wake: bool = True,
    result_files: list | None = None,
    agent_name: str | None = None,
) -> None:
    """Run one wake turn for a conversation, from text already composed.

    ``partner_wake`` says *wake_text* is an A2A partner's answer; a
    Video Studio job's outcome is not (:func:`wake_conversation`).
    """
    from app.core.database import async_session

    async with async_session() as db:
        conv = (
            await db.execute(
                select(Conversation).where(Conversation.id == uuid.UUID(conversation_id))
            )
        ).scalar_one_or_none()
        if conv is None:
            logger.debug(
                "Wake %s skipped — conversation %s missing", label, conversation_id
            )
            return
        # Before anything is persisted: the answer was recorded, but the user
        # stopped the work since it was sent. Inclusive, like the
        # cascade's ``submitted_before``: work sent at the Stop's instant is
        # stopped work.
        # The combined record (the column and the turn guard's), so a Stop
        # the cascade has not committed yet also keeps the answer from being
        # folded into a turn started meanwhile.
        if delegated_at is not None and await _stopped_since(
            db, conv.id, delegated_at, inclusive=True
        ):
            logger.info(
                "Wake %s for conversation %s dropped: the user stopped the "
                "conversation after the work was sent",
                label,
                conversation_id,
            )
            return
        await _run_turn_for(
            db, conv, user_id=user_id, wake_text=wake_text,
            task_id=label, partner_wake=partner_wake,
            result_files=result_files, delegated_at=delegated_at,
            agent_name=agent_name,
            kind=WAKE_KIND_PARTNER if partner_wake else WAKE_KIND_JOB,
        )


@known_secrets_scoped("conversation.id")
async def _run_parent_wake_turn(
    db: AsyncSession,
    *,
    conversation: Conversation | SimpleNamespace,
    user_id: str,
    wake_text: str,
    persist_user: bool,
    followup_depth: int,
    task_id: str,
    activity_user_id: str,
    result_files: list | None = None,
    chain_started_at: datetime | None = None,
    partner_wake: bool = False,
    delegated_at: datetime | None = None,
    wake_from_platform: bool = True,
    history_before_sequence: int | None = None,
    replay: list | None = None,
    on_checkpoint=None,
    start_frame: dict | None = None,
    reply_meta_extra: dict | None = None,
    on_cut: Callable[[str], Awaitable[None]] | None = None,
) -> bool:
    """One parent continuation turn, REST-shaped (lock + bridge + persist).

    The automatic resume of a cut turn (:func:`resume_cut_turn`) runs
    here too, continuing a stored user message instead of a wake's text:
    ``history_before_sequence`` ends the history at that message,
    ``replay`` is the cut turn's checkpoint (``turn_checkpoint.load_replay``)
    appended after it, ``on_checkpoint`` keeps this turn's own work,
    ``start_frame`` tells the viewers the resume started and
    ``reply_meta_extra`` is stored on the reply (``resumed_from``) and
    ``on_cut`` is awaited with the turn's lock token when a platform stop cuts
    it before its reply is stored, while it still holds the conversation:
    the resume is queued again before the conversation reads as
    interrupted, so no Resume click races it.

    Whether the turn was refused: agentic AI is unavailable here, so
    no turn started and the wake's text waits in the history.

    The turn's FileContext is built the way the REST / web chat paths build
    theirs: the files already on the conversation's messages are
    rehydrated, and the finished task's ``result_files`` are added on top.
    Files new to the conversation when the loop returns - the task's output
    and anything the parent wrote while assessing it - are attached to the
    persisted assistant message and their rows bound to it, so the chips
    render, the download URLs resolve, and the next turn rehydrates them.

    ``partner_wake`` says *wake_text* is an A2A partner's answer, and rides on
    its fold when a turn is already running, with ``delegated_at``,
    when the work was sent: the fold is refused once a Stop at or after it is
    recorded, and every drain re-checks it.

    A message folded into the turn names agents of its own: they
    answer it once the conversation agent has, each reply its own message.
    What the turn ends without asking - its folds' agents, and those of a
    fold stranded at its release - goes back to the inbox for the turn that
    comes next, this chain's follow-up first.

    ``wake_from_platform`` says *wake_text* is the platform's own (a task's
    or a partner's wake), whose secret variable markers the turn restores.
    A follow-up answering a fold a person sent is ``False``: its
    text is defused like any message they write.
    """
    from app.channels.router import store_message
    from app.core.database import release_db_connection_before_loop
    from app.services import chat_turn_guard
    from app.services.channel_continuation import (
        WAKE_CHANNEL_TURN_WAIT_SECONDS,
        hold_off_channel_turns,
        release_channel_hold,
    )
    from app.tools.agent_loop import SteeringMessage
    from app.tools.base import FileContext, files_for_reply

    # Snapshot ORM scalars before commits expire the instance.
    conv_id = conversation.id
    channel = conversation.channel or "web"
    conv_agent_id = conversation.agent_id
    channel_thread_id = conversation.channel_thread_id

    # A channel message's turn drains nothing folded into it: the
    # wake waits it out, then takes its own turn or folds into one that
    # answers it. Past the wait it folds as before, its text in the history.
    channel_hold, _channel_busy = await hold_off_channel_turns(
        conv_id,
        wait_seconds=WAKE_CHANNEL_TURN_WAIT_SECONDS,
        before_wait=lambda: release_db_connection_before_loop(db),
    )
    if _channel_busy:
        logger.info(
            "Parent wake for task %s goes to conversation %s while a channel "
            "message's turn still runs: the next turn reads it",
            task_id,
            conv_id,
        )

    # The stored wake message: a platform stop cutting this turn resumes it
    # from there, like a person's message.
    wake_message_id: str | None = None
    if persist_user:
        _wake_msg = await store_message(
            db,
            conv_id,
            role="user",
            content=wake_text,
            channel=channel,
            metadata_json=PARENT_WAKE_METADATA,
        )
        wake_message_id = str(_wake_msg.id) if getattr(_wake_msg, "id", None) else None
        await db.commit()

    # A wake is a new agent turn nobody asked for: it does not start while
    # agentic AI is unavailable here. Asked only now, after the wake's
    # text is in the history - where a wake requeued behind a busy
    # conversation lives too - so the result is not lost: the next turn, once
    # AI is back, reads it.
    # From here until the reply is stored, a platform stop hands the stored
    # wake to the next process: a wake is consumed once delivered.
    try:
        _refused = await _wake_refused(db, label=task_id)
    except asyncio.CancelledError as exc:
        await _hand_on_cut_wake(exc, conv_id, user_id=user_id, message_id=wake_message_id)
        raise
    if _refused:
        await release_channel_hold(conv_id, channel_hold)
        return True

    # The task's files travel WITH the wake text: a wake folded into
    # the turn already running (the parent parked in ``wait(until=...)`` -
    # the fan-out case) is delivered by that turn's bridge, which registers
    # ``context_file_ids`` in ITS FileContext without inlining them - the
    # text alone would tell the model the files are attached while its
    # ``read_file_content`` finds nothing.
    result_file_ids = [
        str(ref.file_id)
        for ref in result_files or []
        if getattr(ref, "file_id", None)
    ]
    # The files of an EARLIER wake that lost its hand-off race and was
    # requeued: its text is in the history this turn loads, its files only
    # on the inbox entry this acquisition purges - so they are registered in
    # this turn's context below, like this wake's own.
    purged_context_ids: list[str] = []
    # Likewise a fold naming agents: its text is in the history, the
    # agents it mentions answer it after this turn's run.
    purged_mentions: list = []
    try:
        token, folded = await chat_turn_guard.acquire_or_fold(
            str(conv_id),
            wake_text,
            context_file_ids=result_file_ids,
            purged_context_file_ids=purged_context_ids,
            partner_wake=partner_wake,
            delegated_at=delegated_at,
            purged_mentions=purged_mentions,
            platform_text=wake_from_platform,
        )
    except asyncio.CancelledError as exc:
        await _hand_on_cut_wake(exc, conv_id, user_id=user_id, message_id=wake_message_id)
        raise
    except chat_turn_guard.FoldRefusedError:
        await release_channel_hold(conv_id, channel_hold)
        # The Stop was recorded between the check before delivery and this
        # hand-off to the turn already running. The answer is in the
        # history for the next turn the user starts.
        logger.info(
            "Partner wake for conversation %s not handed to the running turn: "
            "the user stopped the conversation after the work was sent",
            conv_id,
        )
        return False
    await release_channel_hold(conv_id, channel_hold)
    if token is None:
        if folded:
            logger.info(
                "Parent wake for task %s folded into the in-flight turn "
                "for conversation %s",
                task_id,
                conv_id,
            )
        else:
            logger.info(
                "Parent wake for task %s requeued for conversation %s",
                task_id,
                conv_id,
            )
        return False
    # When the chain of wake turns began: a Stop recorded after it stopped a
    # turn of this chain, and none follows up on the user's behalf.
    if chain_started_at is None:
        chain_started_at = datetime.now(UTC)

    from app.services.sub_agent import publish_task_event

    assistant_text = ""
    stranded: list = []
    # What its bridge queued after its loop's last look.
    unanswered: list = []
    publisher: chat_turn_stream.TurnPublisher | None = None
    released = False
    # Set once the reply is committed: a cut after it (the delivery, the
    # release) is not resumed, or the wake would be answered twice.
    answer_stored = False

    async def _release_turn() -> list:
        """Release the marker ONCE (the normal end of the turn or the
        ``finally``); returns the stranded folds the drain found. A failed
        attempt leaves ``released`` false so the ``finally`` retries it -
        a transient Redis error must not strand the marker until its TTL."""
        nonlocal released
        if released:
            return []
        try:
            stranded_folds = await chat_turn_guard.release_turn_and_drain(
                str(conv_id), token
            )
        except Exception:
            logger.warning(
                "Failed to release the parent-wake turn lock for conversation %s",
                conv_id,
                exc_info=True,
            )
            return []
        released = True
        return stranded_folds

    # A partner wake re-reads the Stop once it owns the turn: a Stop
    # that committed after the first check but before this acquisition found
    # no turn to cancel, so this is the last point it can be honoured. The
    # answer is already in the history for the next turn the user starts;
    # what folded into the marker meanwhile is requeued, as below.
    # The initial partner wake compares with the work's submission,
    # inclusively like the cascade; a follow-up made only of partner answers
    # compares with the start of its chain, strictly, like the guard below.
    fence: datetime | None = None
    fence_inclusive = False
    if delegated_at is not None:
        fence, fence_inclusive = delegated_at, True
    elif partner_wake and followup_depth > 0 and chain_started_at is not None:
        fence = chain_started_at
    try:
        _fenced = fence is not None and await _stopped_since(
            db, conv_id, fence, inclusive=fence_inclusive
        )
    except asyncio.CancelledError as exc:
        # Cut while holding the turn, before its outer ``try``: the lock is
        # released (shielded) so the resume does not wait for its TTL.
        await _hand_on_cut_wake(exc, conv_id, user_id=user_id, message_id=wake_message_id)
        with contextlib.suppress(Exception):
            stranded_cut = await _release_turn()
            if stranded_cut:
                await chat_turn_guard.requeue_stranded(str(conv_id), stranded_cut)
        raise
    if _fenced:
        stranded_now = await _release_turn()
        if stranded_now:
            with contextlib.suppress(Exception):
                await chat_turn_guard.requeue_stranded(str(conv_id), stranded_now)
        # The agents a fold this acquisition took mentions are still owed
        # their answer: the Stop turns away the partner's, not theirs.
        if purged_mentions:
            with contextlib.suppress(Exception):
                await chat_turn_guard.requeue_owed_mentions(str(conv_id), purged_mentions)
        logger.info(
            "Partner wake for conversation %s stopped after taking the turn: "
            "the user stopped the conversation after the work was sent",
            conv_id,
        )
        return False
    # Files on the conversation BEFORE this turn; anything else in the
    # context when the loop returns is new and gets attached to the reply.
    file_context = FileContext()
    initial_file_ids: set[str] = set()
    # The messages folded into this turn whose @mentioned agents it owes an
    # answer, and what answering them left: the replies, the
    # conversation agent's attribution, the files its own run wrote.
    owed: list = []
    folded_replies: list = []
    # The agent this turn's reply is from: the conversation's, or the one
    # standing in for the General Assistant, set once it is resolved.
    answerer_id = conv_agent_id
    primary_target = None
    primary_failed = False
    primary_file_ids: list[str] | None = None
    metering = TurnMetering()
    history: list = []
    # One outer try, opened the moment the lock is acquired, so it is
    # ALWAYS released — including when this fire-and-forget wake task is
    # CANCELLED (worker shutdown) while awaiting anything below: a
    # ``CancelledError`` is not an ``Exception``, so it would skip any
    # release placed only around the store at the bottom and strand the
    # marker for a full TTL — exactly the stuck-Thinking window this
    # module is meant to close. ``release_turn_and_drain`` is
    # cancellation-shielded, so the release completes even on that path.
    try:
        for purged in purged_mentions:
            purged_owed = folded_mentions(
                purged,
                await load_result_file_refs(purged.file_ids, user_id=user_id)
                if purged.file_ids
                else [],
            )
            if purged_owed is not None:
                owed.append(purged_owed)
        await publish_task_event(
            activity_user_id,
            "parent_wake",
            task_id,
            conversation_id=str(conv_id),
            status="running",
        )

        async def _lock_refresher() -> None:
            while True:
                await asyncio.sleep(chat_turn_guard.REST_REFRESH_INTERVAL_SECONDS)
                try:
                    await chat_turn_guard.refresh_turn(str(conv_id), token)
                except Exception:
                    logger.debug(
                        "Failed to refresh parent-wake turn lock for %s",
                        conv_id,
                        exc_info=True,
                    )

        refresher = asyncio.create_task(_lock_refresher())
        # Fan this turn's progress out to the conversation's open sockets and
        # keep its resumable journal: a parent wake is exactly the
        # turn the user comes back to mid-flight, and before this every
        # viewer saw a bare "Thinking..." until the reply landed.
        publisher = chat_turn_stream.TurnPublisher(str(conv_id), token)
        # A wake's opening frame is the turn's own ``thinking`` ack, the work
        # that woke it named on it (#11796); a resume's is a frame of its own.
        if start_frame is not None and start_frame.get("type") == "thinking":
            publisher.start(ack=start_frame)
        else:
            publisher.start()
            if start_frame:
                publisher.offer(start_frame)

        async def _relay_token(chunk: str) -> None:
            # The answer as it is written, as a web chat send relays it: a
            # resumed turn (a cut turn, a wake) showed its status steps live
            # but its text only once stored, minutes later (#10910).
            publisher.offer({"type": "stream", "content": chunk})

        # A web conversation only: a web send streams its answer the same way,
        # while a channel's answer is delivered whole, after the output guard
        # (a streamed answer is shown before a ``block`` rule can withhold it).
        _on_token = _relay_token if channel == "web" else None

        steering_queue = PartnerAnswerQueue(str(conv_id))
        wait_skip_event = asyncio.Event()
        # A parent-wake turn has no socket, so a cancel cannot arrive on one.
        # Set by the bridge below when a cross-process cancel request
        # addressed to this turn's token arrives (Stop clicked in the web UI).
        cancel_event = asyncio.Event()
        bridge_stop = asyncio.Event()

        async def _steering_bridge() -> None:
            while True:
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(
                        bridge_stop.wait(), timeout=chat_turn_guard.BRIDGE_POLL_SECONDS
                    )
                if bridge_stop.is_set():
                    return
                # Cross-process cancel — token-bound, so a stale
                # request for a previous turn is discarded, never honoured.
                try:
                    if await chat_turn_guard.consume_turn_cancel(
                        str(conv_id), token
                    ):
                        logger.info(
                            "Cross-process cancel received for the "
                            "parent-wake turn of conversation %s",
                            conv_id,
                        )
                        cancel_event.set()
                        wait_skip_event.set()
                except Exception:
                    logger.debug(
                        "Cross-process cancel check failed for %s",
                        conv_id,
                        exc_info=True,
                    )
                # "Continue now" pressed on any socket of the conversation:
                # this turn has none, so the skip only reaches it here.
                try:
                    if await chat_turn_guard.consume_wait_skip(str(conv_id), token):
                        wait_skip_event.set()
                except Exception:
                    logger.debug(
                        "Cross-process wait skip check failed for %s",
                        conv_id,
                        exc_info=True,
                    )
                try:
                    payloads = await chat_turn_guard.drain_steering_inbox(
                        str(conv_id)
                    )
                except Exception:
                    logger.debug(
                        "Parent-wake steering drain failed for %s",
                        conv_id,
                        exc_info=True,
                    )
                    continue
                payloads = await without_stopped_partner_answers(
                    str(conv_id), payloads
                )
                for payload in payloads:
                    # Same two channels as the REST / web chat bridges: a user's
                    # mid-run attachment rides ON the steering message (a
                    # multimodal user turn), while another task's files
                    # folded by a sibling wake are only REGISTERED in this
                    # turn's context. Both reads are user-scoped and
                    # never raise; a failed lookup costs the files, not the
                    # steering text.
                    steer_refs = await load_result_file_refs(
                        payload.file_ids, user_id=user_id
                    )
                    for ref in steer_refs:
                        file_context.add(ref)
                    await register_context_files(
                        file_context, payload.context_file_ids, user_id=user_id
                    )
                    # The agents it @mentions answer it once this turn's run
                    # has; a fold handed back is in the thread already.
                    payload_owed = folded_mentions(payload, steer_refs)
                    if payload_owed is not None:
                        owed.append(payload_owed)
                    if payload.mentions_only:
                        continue
                    steering_queue.put_fold(
                        SteeringMessage(text=payload.text, files=steer_refs)
                        if steer_refs
                        else payload.text,
                        payload,
                    )
                    wait_skip_event.set()

        async def _answer_folds() -> list:
            """Answer the agents the folds mention, after this turn's run.

            The web chat and REST turns' rules: each agent that runs is its
            own agentic action, a Stop or a refused charge asks none of them,
            one budget for all. Never raises: what it did not answer stays in
            ``owed``, handed back in the ``finally``.
            """
            nonlocal primary_target, primary_file_ids
            if cancel_event.is_set() or metering.refused:
                owed.clear()
            if not owed:
                return []
            from app.services.agent_chat import run_agent_chat as _run_mentioned_agent
            from app.services.langfuse_tracing import load_latest_langfuse_ids

            stopped = asyncio.Event()
            traces: dict = {}
            files_by_agent: dict = {}
            unavailable_by_agent: dict = {}

            async def _mention_event(event: dict) -> None:
                # A run the dispatch cut no longer publishes into the turn.
                if not stopped.is_set():
                    await publisher.on_event(event)

            async def _mention_start(target, index: int, total: int) -> None:
                publisher.offer(
                    {
                        "type": "status",
                        "status": "mention_answering",
                        "agent_id": target.agent_id,
                        "agent_name": target.name,
                        "agent_icon": target.icon,
                        "position": index + 1,
                        "total": total,
                    }
                )

            async def _mention_reply(reply) -> None:
                # What the run that wrote it left behind; a note no run wrote
                # carries neither a trace nor files.
                reply.trace = traces.pop(reply.target.agent_id, {})
                reply.file_ids = files_for_reply(
                    file_context, files_by_agent.pop(reply.target.agent_id, []), reply.text
                )
                reply.unavailable_integrations = unavailable_by_agent.pop(reply.target.agent_id, [])

            def _run_for(message):
                async def _run(mentioned_agent, mention_history, mention_cancel, agents_after) -> str:
                    async def _run_on(own_db, own_agent) -> str:
                        agent_key = str(own_agent.id)
                        files_before = {
                            str(ref.file_id)
                            for ref in file_context.list_files()
                            if getattr(ref, "file_id", None)
                        }
                        # Its own agentic action, charged before it runs.
                        _mention_quota: dict = {}
                        _mention_unavailable: list = []
                        try:
                            text, _ = await _run_mentioned_agent(
                                own_db,
                                user_id,
                                own_agent,
                                mention_history,
                                message.text,
                                conversation_id=str(conv_id),
                                files=message.files or None,
                                file_context=file_context,
                                steering_queue=steering_queue,
                                wait_skip_event=wait_skip_event,
                                cancel_event=mention_cancel,
                                on_event=_mention_event,
                                quota_out=_mention_quota,
                                unavailable_integrations_out=_mention_unavailable,
                                agents_after=agents_after,
                                allowed_handover_types=MENTIONED_AGENT_HANDOVER_TYPES,
                            )
                        finally:
                            # The files this run wrote are shown by its own
                            # reply, and so are the integrations it could not use.
                            files_by_agent[agent_key] = [
                                str(ref.file_id)
                                for ref in file_context.list_files()
                                if getattr(ref, "file_id", None)
                                and str(ref.file_id) not in files_before
                            ]
                            unavailable_by_agent[agent_key] = clean_unavailable_integrations(
                                _mention_unavailable
                            )
                        if quota_refused(_mention_quota):
                            raise TurnRefusedError(text)
                        traces[agent_key] = await load_latest_langfuse_ids(str(conv_id))
                        return text

                    return await run_on_own_session(mentioned_agent, _run_on)

                return _run

            try:
                primary_target = await conversation_agent_target(db, answerer_id)
                # The conversation agent's reply shows the files of its own run.
                primary_file_ids = [
                    str(ref.file_id)
                    for ref in file_context.list_files()
                    if getattr(ref, "file_id", None)
                    and str(ref.file_id) not in initial_file_ids
                ]
                return await answer_folded_mentions(
                    owed,
                    db=db,
                    user_id=uuid.UUID(str(user_id)),
                    answering_agent_id=str(answerer_id) if answerer_id else None,
                    history=folded_mention_history(
                        history,
                        wake_text,
                        []
                        if primary_failed or not (assistant_text or "").strip()
                        else [MentionReply(primary_target, assistant_text)],
                    ),
                    run_agent_for=_run_for,
                    cancel_event=cancel_event,
                    deadline=time.monotonic() + DISPATCH_BUDGET_SECONDS,
                    on_start=_mention_start,
                    on_reply=_mention_reply,
                    stopped=stopped,
                )
            except Exception:
                logger.warning(
                    "The agents mentioned in a message folded into the "
                    "parent-wake turn of conversation %s could not answer it",
                    conv_id,
                    exc_info=True,
                )
                return []

        bridge = asyncio.create_task(_steering_bridge())
        # The integrations this wake did not use for its user, shown on
        # the reply.
        unavailable_integrations: list[dict] = []
        try:
            from app.channels.router import get_conversation_history

            history = await get_conversation_history(
                db, conv_id, before_sequence=history_before_sequence
            )
            if (
                history
                and history[-1].role == "user"
                and history[-1].content == wake_text
            ):
                # The message this turn answers: its files stay readable
                # (a resumed cut turn's message carries the person's).
                for _href in getattr(history[-1], "files", None) or []:
                    file_context.add(_href)
                    if getattr(_href, "file_id", None):
                        initial_file_ids.add(str(_href.file_id))
                history = history[:-1]
            # The completed tool rounds of the cut turn this one resumes,
            # after the history like the web turns replay them.
            if replay:
                history = [*history, *replay]

            # Same hydration as the REST / web chat paths: the files already
            # attached to the conversation's messages are readable in this
            # turn too. Without it the wake turn would start with an EMPTY
            # context, so the parent could not even open a file the user had
            # uploaded earlier in the same conversation.
            for _hist_msg in history:
                for _href in getattr(_hist_msg, "files", None) or []:
                    file_context.add(_href)
                    if getattr(_href, "file_id", None):
                        initial_file_ids.add(str(_href.file_id))
            # The finished task's files. Not in ``initial_file_ids`` on
            # purpose: they are new to the conversation, so they get attached
            # to the reply below (and bound to it) like a file this turn wrote.
            for _ref in result_files or []:
                if getattr(_ref, "file_id", None):
                    file_context.add(_ref)
            # Same for the files of a purged earlier wake.
            await register_context_files(
                file_context, purged_context_ids, user_id=user_id
            )
            # Every stored file of the conversation, like the REST and web
            # turns: a fold follow-up answers the first folded message while
            # the files uploaded with the others (and a resumed turn's, past
            # compaction) are only in the file store, so without this the turn
            # saw the uploads on its messages but had no file_id for them.
            register_conversation_files(file_context, await conversation_file_refs(conv_id), initial_file_ids)

            # The session tools its web turns saved for the conversation's
            # agent (the one standing in answers with the General Assistant's
            # selection), less an id unbound since then.
            from app.services.chat_saved_tool_selection import session_tool_kwargs
            from app.services.chat_selection import usable_saved_selection

            saved_selection = await usable_saved_selection(db, user_id=user_id, conversation=conversation)

            await release_db_connection_before_loop(db)

            # A channel conversation the General Assistant owns is answered by
            # the agent linked to it, turn by turn, on its channel and
            # on the web: so is its wake. A web one is bound to that agent when
            # it is created, and one created before the link keeps the General
            # Assistant.
            from app.services.general_assistant_link import turn_stand_in

            stand_in = await turn_stand_in(
                db, agent_id=conv_agent_id, conversation_channel=channel, user_id=user_id
            )
            if stand_in is not None:
                answerer_id = stand_in.id

            if conv_agent_id or stand_in is not None:
                agent = stand_in or (
                    await db.execute(select(Agent).where(Agent.id == conv_agent_id))
                ).scalar_one_or_none()
                if not agent:
                    assistant_text = (
                        "The agent bound to this conversation no longer exists."
                    )
                    primary_failed = True
                elif not agent.enabled:
                    assistant_text = "This agent is currently disabled."
                    primary_failed = True
                else:
                    from app.services.agent_chat import run_agent_chat

                    assistant_text, _transfer = await run_agent_chat(
                        db,
                        user_id,
                        agent,
                        history,
                        wake_text,
                        conversation_id=str(conv_id),
                        file_context=file_context,
                        steering_queue=steering_queue,
                        wait_skip_event=wait_skip_event,
                        cancel_event=cancel_event,
                        meter_quota=True,
                        on_event=publisher.on_event,
                        on_token=_on_token,
                        restore_user_message=wake_from_platform,
                        on_checkpoint=on_checkpoint,
                        # A refusal asks no folded message's agent.
                        quota_out=metering.quota if owed else None,
                        unavailable_integrations_out=unavailable_integrations,
                        # The saved tools of the agent that answers, the one
                        # standing in included: its web turns saved them, and
                        # its channel turns run with them. A default the agent
                        # gained since the save is on, as a channel turn has it.
                        **session_tool_kwargs(saved_selection, agent),
                    )
            else:
                from app.services.general_chat import run_general_chat

                assistant_text, _new_agent_id = await run_general_chat(
                    db,
                    user_id,
                    history,
                    wake_text,
                    conversation_id=str(conv_id),
                    file_context=file_context,
                    steering_queue=steering_queue,
                    wait_skip_event=wait_skip_event,
                    cancel_event=cancel_event,
                    on_event=publisher.on_event,
                    on_token=_on_token,
                    restore_user_message=wake_from_platform,
                    on_checkpoint=on_checkpoint,
                    quota_out=metering.quota if owed else None,
                    unavailable_integrations_out=unavailable_integrations,
                    **session_tool_kwargs(saved_selection),
                )
            folded_replies = await _answer_folds()
        except Exception as exc:
            logger.error(
                "Parent wake chat error for conversation %s: %s",
                conv_id,
                format_exception_message(exc),
                exc_info=True,
            )
            assistant_text = (
                "I encountered an error while assessing the background task "
                f"result: {format_exception_message(exc)}"
            )
            # The failure is this agent's note; a folded message's agents
            # still answer.
            primary_failed = True
            folded_replies = await _answer_folds()
        finally:
            refresher.cancel()
            await chat_turn_guard.stop_bridge(bridge, bridge_stop)
            with contextlib.suppress(asyncio.CancelledError):
                await refresher
            # Bridged after the loop's last look: answered by the follow-up.
            unanswered = steering_queue.take_unanswered()

        if not assistant_text or not assistant_text.strip():
            assistant_text = (
                "I assessed the background task but produced no reply. "
                "Please continue from the result in this thread."
            )

        # Files new to the conversation (the task's output, plus anything the
        # parent produced while assessing it) ride on the reply as
        # ``attachments`` - the chat renders a ``[[FILE:id]]`` marker only for
        # a file the message carries, and shows the rest as fallback chips -
        # and their rows are bound to the conversation + this message so the
        # message-scoped download URL resolves. The admission read
        # runs on its own session (a failed read must not poison the one the
        # reply is persisted on) and admits only rows no message shows yet;
        # the bind repeats that predicate atomically and the ``attachments``
        # block is written from the rows that were actually bound. With a
        # folded message's agents answering, the reply shows only the
        # files of its own run: theirs are shown by their own replies. A file
        # the run only fetched is shown when the reply names it.
        new_file_ids = files_for_reply(
            file_context,
            list(primary_file_ids)
            if primary_file_ids is not None
            else [
                str(ref.file_id)
                for ref in file_context.list_files()
                if getattr(ref, "file_id", None)
                and str(ref.file_id) not in initial_file_ids
            ],
            assistant_text,
        )
        attachment_rows: list = []
        if new_file_ids:
            try:
                attachment_rows = await load_attachment_rows(
                    new_file_ids, conversation_id=conv_id
                )
            except Exception:
                logger.warning(
                    "Could not load the files of the parent-wake reply for "
                    "conversation %s; persisting the text alone",
                    conv_id,
                    exc_info=True,
                )

        # Answered by other agents after it: attributed like theirs.
        attributed = bool(folded_replies) and primary_target is not None
        _unavailable = clean_unavailable_integrations(unavailable_integrations)
        _reply_meta: dict = (
            dict(reply_metadata(primary_target, failed=primary_failed))
            if attributed
            else {}
        )
        if _unavailable:
            _reply_meta[UNAVAILABLE_INTEGRATIONS_META_KEY] = _unavailable
        if reply_meta_extra:
            _reply_meta.update(reply_meta_extra)
        assistant_msg = await store_message(
            db,
            conv_id,
            role="assistant",
            content=assistant_text,
            channel=channel,
            **({"metadata_json": json.dumps(_reply_meta)} if _reply_meta else {}),
        )
        if attachment_rows:
            await attach_rows_to_message(
                db, assistant_msg, attachment_rows, conversation_id=conv_id
            )
        await db.commit()
        answer_stored = True

        # The folded messages' agents, each reply its own message carrying
        # the trace and the files of the run that wrote it.
        folded_messages: list = []
        for reply in folded_replies:
            reply_rows: list = []
            if reply.file_ids:
                try:
                    reply_rows = await load_attachment_rows(
                        reply.file_ids, conversation_id=conv_id
                    )
                except Exception:
                    logger.warning(
                        "Could not load the files of a mentioned agent's reply "
                        "for conversation %s; persisting the text alone",
                        conv_id,
                        exc_info=True,
                    )
            reply_meta = {**reply.trace, **reply.metadata()}
            reply_msg = await store_message(
                db,
                conv_id,
                role="assistant",
                content=reply.text,
                channel=channel,
                metadata_json=json.dumps(reply_meta) if reply_meta else None,
                llm_trace_id=safe_uuid(reply.trace.get("llm_trace_id")),
            )
            if reply_rows:
                await attach_rows_to_message(
                    db, reply_msg, reply_rows, conversation_id=conv_id
                )
            folded_messages.append(reply_msg)
        if folded_messages:
            await db.commit()

        # The reply is stored: close the turn on the conversation's open
        # streams with the same ``done`` frame a web chat turn ends with,
        # BEFORE the marker is released in the ``finally``. The
        # frame is the message as stored - the files ``attach_rows_to_message``
        # bound included, through the serializer the web chat and REST terminals
        # use - because a viewer commits it into its cache under the row's
        # id and only a later refetch would repair a chip-less copy.
        _done_frame: dict = {
            "type": "done",
            "content": assistant_text,
            "conversation_id": str(conv_id),
        }
        _stored_id = getattr(assistant_msg, "id", None)
        if _stored_id:
            _done_frame["message_id"] = str(_stored_id)
            from app.services.chat_messages import parse_attachments

            _done_attachments = parse_attachments(
                getattr(assistant_msg, "metadata_json", None),
                conversation_id=str(conv_id),
                message_id=str(_stored_id),
            )
            if _done_attachments:
                _done_frame["attachments"] = _done_attachments
        if _unavailable:
            _done_frame[UNAVAILABLE_INTEGRATIONS_META_KEY] = _unavailable
        if reply_meta_extra:
            # The reply's own marker (``resumed_from``) lands with it,
            # not at the next refetch.
            _done_frame.update(reply_meta_extra)
        if folded_messages:
            # Every reply but the last is an intermediate segment.
            _done_frame["intermediate"] = True
            if attributed:
                _done_frame.update(reply_metadata(primary_target, failed=primary_failed))
        publisher.offer(_done_frame)
        if folded_messages:
            from app.services.chat_messages import parse_attachments

            for index, (reply, reply_msg) in enumerate(
                zip(folded_replies, folded_messages, strict=True)
            ):
                reply_frame: dict = {
                    "type": "done",
                    "content": reply.text,
                    "message_id": str(reply_msg.id),
                    "conversation_id": str(conv_id),
                    **reply.metadata(),
                }
                reply_attachments = parse_attachments(
                    getattr(reply_msg, "metadata_json", None),
                    conversation_id=str(conv_id),
                    message_id=str(reply_msg.id),
                )
                if reply_attachments:
                    reply_frame["attachments"] = reply_attachments
                if index < len(folded_messages) - 1:
                    reply_frame["intermediate"] = True
                publisher.offer(reply_frame)
        with contextlib.suppress(Exception):
            await publisher.aclose()
        # The turn is over for its viewers the moment ``done`` is published:
        # release the marker NOW, before the outbound channel delivery below
        # (an HTTP call that can take seconds). A relayed viewer handles
        # ``done``, stops its marker-only poll and refetches; a marker still
        # held at that refetch reads as ``is_processing`` and re-adopts a
        # phantom "Thinking" for as long as the delivery runs.
        stranded = await _release_turn()

        if channel not in ("web", "web_embed"):
            from app.services.sub_agent import _send_proactive_channel_message

            await _send_proactive_channel_message(
                channel=channel,
                channel_thread_id=channel_thread_id,
                # One outbound message, a section per agent.
                text=(
                    compose_channel_reply(
                        channel, [MentionReply(primary_target, assistant_text), *folded_replies]
                    )
                    if attributed
                    else assistant_text
                ),
                agent_name="Agent",
            )
    finally:
        # Cut by a platform stop before its reply was stored: queued
        # for the next process while this turn still holds the conversation,
        # so the sweep resumes the stored wake message once the lock is
        # released. Cut after it, nothing is queued: the answer is there.
        # Queued FIRST, before the viewers are told it was interrupted and
        # before the release: queued after them, the conversation read
        # as interrupted with nothing queued and offered the Resume click,
        # whose message then made the queued resume stand down. An automatic
        # resume cut in turn has no message of its own: ``on_cut`` queues it.
        _cut = sys.exc_info()[1]
        if _cut is not None and not answer_stored:
            await _hand_on_cut_wake(_cut, conv_id, user_id=user_id, message_id=wake_message_id)
            if (
                on_cut is not None
                and isinstance(_cut, asyncio.CancelledError)
                and is_graceful_shutdown_cancellation(_cut)
            ):
                with contextlib.suppress(Exception):
                    await on_cut(token)
        # A turn torn down before its ``done`` (cancellation, an error in
        # the persistence phase) is closed for its viewers with the
        # interrupted terminal and still drops its journal, so no viewer
        # holds "Thinking" until its next safety poll or is resumed into a
        # turn that is gone.
        if publisher is not None and not publisher.closed:
            with contextlib.suppress(Exception):
                await publisher.aclose(
                    terminal=chat_turn_stream.interrupted_frame()
                )
        # Persist first (same order as the web chat) so a client that
        # watches is_processing going false already sees the assistant
        # row. Releasing the marker before store left Thinking stuck:
        # the UI adopted the REST wake, then polling saw a gone marker
        # whose last visible message was still the previous answer. The
        # normal path released above; this is every other exit.
        if not released:
            stranded = await _release_turn()
        # What this turn ends without asking goes back to the inbox,
        # and so do the folds stranded at a torn-down turn's release: no
        # follow-up answers them.
        with contextlib.suppress(Exception):
            await chat_turn_guard.requeue_owed_mentions(
                str(conv_id),
                [m.payload for m in owed if m.payload is not None]
                + (stranded if sys.exc_info()[0] is not None else []),
            )

    # Queued after the loop's last look, like a fold stranded at the release:
    # answered by the follow-up. The agents they mention went back with
    # ``owed`` in the ``finally``, so they are not handed back twice.
    stranded = [
        *(
            replace(p, mentioned_agent_ids=[]) if any(p is m.payload for m in owed) else p
            for p in unanswered
        ),
        *stranded,
    ]
    # A fold an earlier turn handed back is in the thread already:
    # its agents are still owed an answer, its text is not followed up.
    handed_back = [p for p in stranded if getattr(p, "mentions_only", False)]
    stranded = [p for p in stranded if not getattr(p, "mentions_only", False)]
    with contextlib.suppress(Exception):
        await chat_turn_guard.requeue_owed_mentions(str(conv_id), handed_back)
    deferred: list = []
    if any(getattr(p, "partner_wake", False) for p in stranded):
        # A partner's answer folded into this turn is requeued like an
        # exhausted chain rather than followed up, since it is already in the
        # history the next turn reads, when the user stopped a turn of this
        # chain or the work the answer delivers. A message the
        # user sent, or a task's wake, is still answered.
        stopped_at = await _latest_stop(db, conv_id)
        chain_stopped = stopped_at is not None and stopped_at > chain_started_at
        deferred = [
            p
            for p in stranded
            if getattr(p, "partner_wake", False)
            and (chain_stopped or _stop_holds_back(p, stopped_at))
        ]
    if deferred:
        stranded = [p for p in stranded if all(p is not d for d in deferred)]
        with contextlib.suppress(Exception):
            await chat_turn_guard.requeue_stranded(str(conv_id), deferred)
        logger.info(
            "Parent wake follow-up for conversation %s: the user stopped the "
            "turn or the work; %d partner answer(s) requeued for the next turn",
            conv_id,
            len(deferred),
        )
    if stranded and followup_depth >= _MAX_PARENT_WAKE_FOLLOWUPS:
        # The follow-up chain is bounded, but a stranded wake is not dropped:
        # requeued, it is consumed live by whichever turn holds the
        # lock (its text was persisted before it was pushed, so that turn's
        # history snapshot does not have it) or, when no turn is running,
        # purged by the next acquisition - which hands its
        # ``context_file_ids`` to the turn it starts.
        with contextlib.suppress(Exception):
            await chat_turn_guard.requeue_stranded(str(conv_id), stranded)
        logger.info(
            "Parent wake follow-up depth exhausted for conversation %s; "
            "%d stranded fold(s) requeued for the next turn",
            conv_id,
            len(stranded),
        )
    elif stranded:
        logger.info(
            "Parent wake follow-up %d for conversation %s (%d stranded fold(s))",
            followup_depth + 1,
            conv_id,
            len(stranded),
        )
        # The agents a stranded fold mentions: handed back, so the
        # follow-up's acquisition answers them - or the next turn's, which
        # reads their text too, when the follow-up is refused.
        with contextlib.suppress(Exception):
            await chat_turn_guard.requeue_owed_mentions(str(conv_id), stranded)
        await _follow_up_folds(
            db,
            conversation=conversation,
            user_id=user_id,
            folds=stranded,
            followup_depth=followup_depth + 1,
            task_id=task_id,
            activity_user_id=activity_user_id,
            chain_started_at=chain_started_at,
        )
    return False


async def _follow_up_folds(
    db: AsyncSession,
    *,
    conversation: Conversation | SimpleNamespace,
    user_id: str,
    folds: list,
    followup_depth: int,
    task_id: str,
    activity_user_id: str,
    chain_started_at: datetime | None = None,
) -> None:
    """Run the follow-up turn that answers *folds*, messages folded into a turn that ended first.

    Their texts are stored already: the follow-up persists nothing, answers
    the first one and reads the others in its history (each fold was
    persisted before it was pushed). Run once the turn that took them
    released the lock, it takes the conversation or folds that text into the
    turn that took it meanwhile. The agents the folds mention are the
    caller's to hand back first (``requeue_owed_mentions``), so its
    acquisition answers them.
    """
    # A sibling wake that landed as the turn finished carries ITS task's
    # files as ``context_file_ids``: they become the follow-up
    # turn's ``result_files`` - registered in its context and attached to
    # its reply - or the follow-up would read a text saying they are
    # attached while holding none. EVERY fold's files, not the first one's:
    # the drain took them all off the inbox, and their texts all reach the
    # follow-up through the history it loads, so the files must arrive with
    # them or a burst of completions delivers the first sibling's files and
    # loses the rest.
    file_ids = [
        file_id
        for payload in folds
        for file_id in getattr(payload, "context_file_ids", None) or []
    ]
    await _run_parent_wake_turn(
        db,
        conversation=conversation,
        user_id=user_id,
        wake_text=folds[0].text,
        persist_user=False,
        followup_depth=followup_depth,
        task_id=task_id,
        activity_user_id=activity_user_id,
        result_files=await load_result_file_refs(file_ids, user_id=user_id),
        chain_started_at=chain_started_at,
        # A partner wake only when every fold it answers is a partner's
        # answer: the follow-up answers them all through the history, and
        # a Stop must never silence a message the user sent.
        partner_wake=all(getattr(p, "partner_wake", False) for p in folds),
        # Restored only when the platform wrote it: a person's folded
        # message is defused, never handed back with the values.
        wake_from_platform=getattr(folds[0], "platform_text", False) is True,
    )


async def _stopped_since(
    db, conversation_id, since: datetime, *, inclusive: bool = False
) -> bool:
    """Whether the user stopped this conversation after *since*.

    ``inclusive`` counts a Stop at *since* itself, for a submission time: the
    cascade stops work sent at or before the Stop.
    """
    latest = await _latest_stop(db, conversation_id)
    if latest is None:
        return False
    return latest >= since if inclusive else latest > since


async def _latest_stop(db, conversation_id) -> datetime | None:
    """When the user last stopped this conversation, or ``None``.

    Reads the durable record (``Conversation.stopped_at``) and the moment the
    turn guard recorded on the acknowledgement path, which covers the gap
    until the cascade commits the former; the later of the two counts.

    Fail-open: a lookup that fails answers "not stopped", which is how every
    wake behaved before the Stop was recorded.
    """
    from app.services import chat_turn_guard

    stopped_at = None
    try:
        stopped_at = (
            await db.execute(
                select(Conversation.stopped_at).where(Conversation.id == conversation_id)
            )
        ).scalar_one_or_none()
    except Exception:
        logger.debug(
            "Could not read the stop of conversation %s", conversation_id, exc_info=True
        )
        with contextlib.suppress(Exception):
            await db.rollback()
    moments = [stopped_at] if isinstance(stopped_at, datetime) else []
    try:
        recorded = await chat_turn_guard.recorded_stop(str(conversation_id))
    except Exception:
        recorded = None
    if isinstance(recorded, datetime):
        moments.append(recorded)
    return max(moments) if moments else None


def _stop_holds_back(payload, stopped_at: datetime | None) -> bool:
    """Whether a Stop at *stopped_at* stopped the work a fold delivers.

    Only a partner's answer carrying its work's submission time, compared
    inclusively like the cascade's ``submitted_before``.
    """
    delegated_at = getattr(payload, "delegated_at", None)
    return (
        bool(getattr(payload, "partner_wake", False))
        and isinstance(delegated_at, datetime)
        and stopped_at is not None
        and stopped_at >= delegated_at
    )


async def without_stopped_partner_answers(conversation_id, payloads: list) -> list:
    """*payloads* less the partner answers whose work the user stopped.

    Every drain that delivers a fold goes through this: the bridge of a
    running turn and the drain that follows one up. A partner's answer pushed
    before the Stop was recorded, and drained after it, is left out - its text
    is in the history the next turn the user starts loads. A message the user
    sent, a sub-agent task's wake, and an entry without a submission time are
    delivered as before.

    Reads the Stop only when a payload could be held back, on a session of
    its own: the running turn's is busy in its loop. Fail-open, like
    :func:`_latest_stop`.
    """
    if not any(
        getattr(p, "partner_wake", False) and getattr(p, "delegated_at", None)
        for p in payloads
    ):
        return list(payloads)
    from app.core.database import async_session

    stopped_at = None
    conversation_uuid = safe_uuid(str(conversation_id))
    try:
        if conversation_uuid is not None:
            async with async_session() as db:
                stopped_at = await _latest_stop(db, conversation_uuid)
    except Exception:
        logger.debug(
            "Could not read the stop of conversation %s", conversation_id, exc_info=True
        )
    kept = [p for p in payloads if not _stop_holds_back(p, stopped_at)]
    if len(kept) < len(payloads):
        logger.info(
            "%d partner answer(s) for conversation %s held back: the user "
            "stopped the conversation after the work was sent",
            len(payloads) - len(kept),
            conversation_id,
        )
    return kept


class PartnerAnswerQueue(SteeringQueue):
    """The steering queue of a turn whose inbox bridge folds partners' answers in.

    The bridge checks the Stop before it queues an answer, but the loop takes
    it only at its next iteration boundary, and a Stop sent from another
    socket reaches the turn at the bridge's next poll: the answer is checked
    once more as it is taken. What is held back is kept for a caller
    that replays what the loop injected (:meth:`take_held_back`).
    """

    def __init__(self, conversation_id: str) -> None:
        super().__init__()
        self._conversation_id = conversation_id
        self._folds: list[tuple[object, object]] = []
        self._bridged: list[tuple[object, object]] = []
        self._held_back: list = []

    def put_fold(self, item, payload) -> None:
        """Queue *item*, bridged from the inbox entry *payload*."""
        self.put_nowait(item)
        self._bridged.append((item, payload))
        if getattr(payload, "partner_wake", False) and getattr(
            payload, "delegated_at", None
        ):
            self._folds.append((item, payload))

    def message_ids_of(self, items: list) -> list[str]:
        """The stored message ids of the web steers among *items*, in their order.

        Read before :meth:`admit`, which forgets the inbox entries of what it
        is handed: a turn's leftover check moves those messages after the
        answer they arrived too late to shape.
        """
        ids: list[str] = []
        for item in items:
            payload = next((p for queued, p in self._bridged if queued is item), None)
            message_id = getattr(payload, "message_id", None)
            if message_id:
                ids.append(message_id)
        return ids

    def take_held_back(self) -> list:
        """The items held back since the last call, in the order they were."""
        held, self._held_back = self._held_back, []
        return held

    def take_unanswered(self) -> list:
        """Empty the queue, as the inbox entries its items were bridged from.

        What a turn's loop never took: queued after the loop's last look, the
        fold told its sender the turn took it, so the turn's end answers it in
        a follow-up (:func:`schedule_fold_follow_up`).
        """
        from app.services.chat_turn_guard import SteeringPayload
        from app.tools.agent_loop import drain_steering_queue, steering_text

        bridged, self._bridged = self._bridged, []
        return [
            next((payload for queued, payload in bridged if queued is item), None)
            or SteeringPayload(text=steering_text(item))
            for item in drain_steering_queue(self)
        ]

    async def admit(self, items: list) -> list:
        self._bridged = [
            (item, payload)
            for item, payload in self._bridged
            if all(item is not left for left in items)
        ]
        # Matched by identity: the queue hands back the objects put on it.
        taken = [
            (item, fold)
            for item, fold in self._folds
            if any(item is left for left in items)
        ]
        if not taken:
            return items
        self._folds = [
            (item, fold)
            for item, fold in self._folds
            if all(item is not left for left in items)
        ]
        kept = await without_stopped_partner_answers(
            self._conversation_id, [fold for _, fold in taken]
        )
        dropped = [item for item, fold in taken if all(fold is not k for k in kept)]
        self._held_back.extend(dropped)
        return [left for left in items if all(left is not d for d in dropped)]
