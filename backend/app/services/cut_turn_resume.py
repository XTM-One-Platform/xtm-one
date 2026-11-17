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

"""Automatic resume of a chat turn cut by a platform stop.

A rolling deploy, a scale-in, a node drain or the worker recycle stops the
process a web turn runs in. Sections 7 to 9 of
``dev-docs/chat-turn-resilience.md`` already store what the cut turn did (its
interrupted reply) and keep its completed tool rounds (the turn checkpoint,
flushed by the shutdown drain). Before this module the person then had to
click Resume (or type "Continue") on every conversation the rollout cut.

Now the cut turn is put in a queue before its lock goes
(:func:`enqueue_cut_turn`), and the scheduler tick of the next process drains
it (:func:`sweep_cut_turns`): the conversation lock is taken again, the
checkpoint is replayed (``turn_checkpoint.load_replay``) and the SAME user
message is continued - no new user message, no prompt text beyond the
checkpoint's resume note (invariant 16).

Only a platform stop is resumed (:func:`resumable_cause`: the worker recorded
``stop`` or ``recycle``). A turn whose process died for another reason (an
out-of-memory kill, a crash the turn caused) keeps today's interrupted reply
and its one-click Resume: resuming it could loop. At most
:data:`MAX_AUTO_RESUMES` automatic resumes IN A ROW that completed no new tool
round are made per user message. A long turn that keeps making progress
across successive rollouts keeps resuming by itself for
:data:`MAX_AUTO_RESUME_SPAN_SECONDS` after its first cut, however many rollouts
that afternoon brings, with :data:`MAX_TOTAL_AUTO_RESUMES` in all as the
runaway ceiling; the next cut past any of them keeps the one click.

Best-effort by contract, like the checkpoint: a queue entry that cannot be
written costs the automatic resume (the one click stays), never the turn.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable, Iterable
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from app.core.utils import format_exception_message

logger = logging.getLogger(__name__)

#: How long a chat-handover parent run kept for a resume waits, once its
#: cut record has left the queue, for the resumed turn to adopt it: the sweep
#: took the record and the resumed turn is starting. Past it, unadopted, the
#: stale sweep fails it with a reason naming the missed resume.
PARENT_ADOPTION_GRACE_SECONDS = 120.0

#: ``AssignmentRun.trigger_context`` keys of a parent run kept for a resume.
AWAITING_AUTO_RESUME_KEY = "awaiting_auto_resume"
AWAITING_SINCE_KEY = "awaiting_since"


class ParentRunHandoff:
    """The parent run a resumed turn may adopt, and whether it did."""

    __slots__ = ("adopted", "run_id")

    def __init__(self, run_id: str) -> None:
        self.run_id = run_id
        self.adopted = False


#: Set by the resume runner (``parent_wake.resume_cut_turn``) around the resumed
#: turn: ``agent_chat``'s lazy chat-handover parent adopts it instead of creating
#: a new one, so the runs the turn delegates stay under the cut turn's parent.
RESUMED_PARENT_RUN: ContextVar[ParentRunHandoff | None] = ContextVar("resumed_parent_run", default=None)


def adoptable_parent_run(run: Any, *, agent_id: object, user_id: object, conversation_id: object) -> bool:
    """Whether a resumed turn may adopt *run* as its chat-handover parent.

    Only the same agent's, the same user's, still running chat-handover run
    (no assignment) of the same conversation: a record pointing anywhere else
    is never adopted.
    """
    context = run.trigger_context if isinstance(run.trigger_context, dict) else {}
    payload = run.trigger_payload if isinstance(run.trigger_payload, dict) else {}
    return (
        run.assignment_id is None
        and run.status == "running"
        and str(run.agent_id) == str(agent_id)
        and str(run.user_id) == str(user_id)
        and context.get("type") == "chat_handover"
        and payload.get("conversation_id") is not None
        and str(payload.get("conversation_id")) == str(conversation_id)
    )


#: The automatic resumes in a row that completed no new tool round one user
#: message may get (the loop guard); the next such cut keeps the one click.
MAX_AUTO_RESUMES = 3
#: The automatic resumes one user message may get in all, progress or not:
#: the runaway ceiling only. A progressing turn is bounded by time
#: (:data:`MAX_AUTO_RESUME_SPAN_SECONDS`): 20 in all stopped a turn that made
#: progress through an afternoon of about 20 rollouts.
MAX_TOTAL_AUTO_RESUMES = 100
#: How long after the first cut of a user message its turn keeps resuming by
#: itself while it makes progress: a rollout storm is counted in hours,
#: not in rollouts. Within the record's TTL (:data:`RECORD_TTL_SECONDS`).
MAX_AUTO_RESUME_SPAN_SECONDS = 12 * 3600
#: Redis set of the conversations with a cut turn waiting for its resume.
PENDING_KEY = "chat:turn:auto-resume:pending"
#: Redis key of a conversation's cut turn (JSON, :class:`CutTurn`).
_RECORD_KEY = "chat:turn:auto-resume:{conversation_id}"
#: How long a cut turn waits for its resume: the checkpoint's resume window.
RECORD_TTL_SECONDS = 24 * 3600
#: The worker stop causes that are the platform's own (``core/worker_shutdown``).
_PLATFORM_CAUSES = frozenset({"stop", "recycle"})
#: ``metadata_json`` key, on the reply of a resumed turn, of where it resumed from.
RESUMED_FROM_META_KEY = "resumed_from"
#: Frame type told to the conversation's viewers when a resume starts.
RESUMING_FRAME_TYPE = "turn_resuming"
#: ``metadata_json`` key, on the interrupted reply of a cut turn, saying it was
#: queued for its automatic resume (the reply needs no click).
AUTO_RESUME_META_KEY = "auto_resume"
#: How often each process looks at the queue: a cut turn is resumed
#: within this of its lock being free, not at the next scheduler window.
SWEEP_INTERVAL_SECONDS = 10.0
#: How long after its cut a conversation lock still held by the cut turn's own
#: token is taken for its process's drain; past it, that process is gone (killed
#: before its release) and the lock is freed for the resume.
DEAD_LOCK_GRACE_SECONDS = 30.0
#: How long a turn a stop cut waits for a process of another build: a
#: rollout replaces pods one by one, so a process of the cut turn's own build
#: is likely the next one stopped, and a resume started there is cut again
#: within seconds and spends one of the message's resumes. Past it (a scale-in,
#: a node drain: no new build comes), any process takes it.
SAME_BUILD_DEFER_SECONDS = 90.0


#: Redis key set while a taken cut turn is being resumed, before the resumed
#: turn holds the conversation: the record is gone, the lock not yet
#: taken, and the notice must still say the turn resumes by itself.
_RESUMING_KEY = "chat:turn:auto-resume:resuming:{conversation_id}"
#: How long that marker lasts at most: the resume runner clears it when it
#: returns; a process that died between the take and the lock leaves it this long.
RESUMING_TTL_SECONDS = 120


def record_key(conversation_id: object) -> str:
    return _RECORD_KEY.format(conversation_id=conversation_id)


def resuming_key(conversation_id: object) -> str:
    return _RESUMING_KEY.format(conversation_id=conversation_id)


async def _redis() -> Any:
    from app.core.redis import redis_client

    return await redis_client()


def resumable_cause(cause: str | None) -> bool:
    """Whether a turn cut with *cause* (``core.worker_shutdown.cut_cause()``) resumes by itself."""
    return cause in _PLATFORM_CAUSES


@dataclass(frozen=True)
class CutTurn:
    """A cut turn waiting for its automatic resume."""

    conversation_id: str
    user_id: str
    #: The stored user message the cut turn answered: the resume continues it.
    user_message_id: str | None
    #: The public id of the cut turn (``chat_turn_stream.public_turn_id``).
    turn_id: str | None
    cause: str
    #: The automatic resumes this user message already had, this one excluded.
    resumes: int
    cut_at: str
    #: Where the turn came from: ``web`` (WebSocket) or ``rest``.
    entry: str = "web"
    #: The conversation lock token the cut turn held: a lock still holding
    #: it after :data:`DEAD_LOCK_GRACE_SECONDS` belongs to a process that is gone.
    lock_token: str | None = None
    #: The build of the process the turn was cut on (``settings.app_version``).
    build: str | None = None
    #: The automatic resumes in a row, this one excluded, that were cut before
    #: they completed a tool round of their own: what
    #: :data:`MAX_AUTO_RESUMES` caps. A record of an earlier release has none:
    #: its ``resumes`` are taken for stalled ones, as that release counted them.
    stalled: int | None = None
    #: The chat-handover parent run of the cut turn: kept running by the
    #: shutdown sweep while this record is queued, and adopted by the resumed
    #: turn, so the runs it delegates stay under one parent. A record of an
    #: earlier release has none: the resumed turn creates its own, as before.
    parent_run_id: str | None = None
    #: When the user message's turn was first cut: what
    #: :data:`MAX_AUTO_RESUME_SPAN_SECONDS` bounds. A record of an earlier
    #: release has none: its ``cut_at`` is taken (:attr:`first_cut`).
    first_cut_at: str | None = None
    #: The durable trace (``llm_traces``) of the turn this record resumes, when
    #: the cutting process knew it: a resume cut in turn names its own trace,
    #: so the next resume links to that hop and not to an earlier one the
    #: trace query would find while the cut hop's trace is not flushed yet.
    #: ``None`` (a first cut, a record of an earlier release): the resume
    #: queries it (:func:`cut_turn_trace_id`).
    trace_id: str | None = None

    @property
    def stalled_resumes(self) -> int:
        return self.resumes if self.stalled is None else self.stalled

    @property
    def first_cut(self) -> str:
        return self.first_cut_at or self.cut_at

    def to_json(self) -> str:
        return json.dumps(self.__dict__)

    @classmethod
    def from_raw(cls, raw: object) -> CutTurn | None:
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", "replace")
        if not isinstance(raw, str):
            return None
        try:
            data = json.loads(raw)
            return cls(
                conversation_id=str(data["conversation_id"]),
                user_id=str(data["user_id"]),
                user_message_id=data.get("user_message_id"),
                turn_id=data.get("turn_id"),
                cause=str(data.get("cause") or ""),
                resumes=int(data.get("resumes") or 0),
                cut_at=str(data.get("cut_at") or ""),
                entry=str(data.get("entry") or "web"),
                lock_token=data.get("lock_token") or None,
                build=data.get("build") or None,
                stalled=None if data.get("stalled") is None else int(data["stalled"]),
                parent_run_id=str(data["parent_run_id"]) if data.get("parent_run_id") else None,
                first_cut_at=str(data["first_cut_at"]) if data.get("first_cut_at") else None,
                trace_id=str(data["trace_id"]) if data.get("trace_id") else None,
            )
        except (ValueError, KeyError, TypeError):
            return None


#: Write the cut record and its pending-set entry in one step. The
#: SADD runs first: a Lua error does not roll back a write already made, so a
#: failing SADD stops the script before anything is written and an earlier
#: queued record is never overwritten or deleted. A SET failing after it (out of
#: memory) leaves a member alone, which is harmless: is_queued needs the record
#: and the sweep drops a member with no record (take_cut_turn).
_QUEUE_CUT_TURN = """
redis.call('SADD', KEYS[2], ARGV[3])
redis.call('SET', KEYS[1], ARGV[1], 'EX', ARGV[2])
return 1
"""


async def enqueue_cut_turn(
    conversation_id: object,
    *,
    user_id: object,
    user_message_id: object,
    turn_id: str | None,
    cause: str | None,
    resumes_made: int = 0,
    stalled: int = 0,
    entry: str = "web",
    lock_token: str | None = None,
    parent_run_id: object = None,
    first_cut_at: str | None = None,
    trace_id: object = None,
    now: datetime | None = None,
) -> bool:
    """Queue the cut turn of *conversation_id* for its automatic resume; whether it was queued.

    Called while the cut turn still holds the conversation, after its
    interrupted reply and its checkpoint were handed to the shutdown drain.
    *resumes_made* is how many automatic resumes this user message already had
    (the turn being cut may itself be one); *stalled* how many of the last of
    them, in a row, were cut before they completed a tool round of their own.
    *parent_run_id* is the cut turn's chat-handover parent run, kept
    for the resumed turn. *first_cut_at* is when the user message's
    turn was first cut (ISO), ``None`` for this very cut. *trace_id* is the
    cut turn's durable trace when the caller knows it. Refused, and
    the person keeps today's one-click Resume: a cause
    that is not the platform's (:func:`resumable_cause`),
    :data:`MAX_AUTO_RESUMES` stalled resumes in a row (a turn that does not
    advance could loop), more than :data:`MAX_AUTO_RESUME_SPAN_SECONDS` since
    the first cut, or :data:`MAX_TOTAL_AUTO_RESUMES` in all. Logs one
    WARNING either way. Never raises.
    """
    cid = str(conversation_id)
    moment = _aware(now) if now is not None else datetime.now(UTC)
    first_cut = first_cut_at or moment.isoformat()
    if not resumable_cause(cause):
        logger.warning(
            "Chat turn of conversation %s cut (worker stop cause: %s): not a platform stop, "
            "so it is not resumed by itself; the person resumes it in one click",
            cid,
            cause or "unknown",
        )
        return False
    if stalled >= MAX_AUTO_RESUMES:
        logger.warning(
            "Chat turn of conversation %s cut by a platform %s again after %d automatic resume(s) in a row "
            "that completed no tool round (%d at most): the person resumes it in one click",
            cid,
            cause,
            stalled,
            MAX_AUTO_RESUMES,
        )
        return False
    if resumes_made >= MAX_TOTAL_AUTO_RESUMES:
        logger.warning(
            "Chat turn of conversation %s cut by a platform %s again after %d automatic resume(s), "
            "the most a message gets (%d): the person resumes it in one click",
            cid,
            cause,
            resumes_made,
            MAX_TOTAL_AUTO_RESUMES,
        )
        return False
    first = _when(first_cut)
    if first is not None and (moment - first).total_seconds() > MAX_AUTO_RESUME_SPAN_SECONDS:
        logger.warning(
            "Chat turn of conversation %s cut by a platform %s again %.1f h after its first cut, after %d "
            "automatic resume(s) (%.0f h at most): the person resumes it in one click",
            cid,
            cause,
            (moment - first).total_seconds() / 3600,
            resumes_made,
            MAX_AUTO_RESUME_SPAN_SECONDS / 3600,
        )
        return False
    record = CutTurn(
        conversation_id=cid,
        user_id=str(user_id),
        user_message_id=str(user_message_id) if user_message_id else None,
        turn_id=turn_id,
        cause=str(cause),
        resumes=resumes_made,
        cut_at=moment.isoformat(),
        entry=entry,
        lock_token=lock_token,
        build=_current_build(),
        stalled=stalled,
        parent_run_id=str(parent_run_id) if parent_run_id else None,
        first_cut_at=first_cut,
        trace_id=str(trace_id) if trace_id else None,
    )
    try:
        client = await _redis()
        # Record and pending entry are written by one script, the entry first:
        # a failure leaves no new record, so is_queued never sees a record the
        # sweep would not take, nor
        # a stale member paired with a record the caller reported as not queued.
        # A stale member with no record leaves the queue at the next
        # sweep (take_cut_turn).
        await client.eval(
            _QUEUE_CUT_TURN, 2, record_key(cid), PENDING_KEY, record.to_json(), RECORD_TTL_SECONDS, cid
        )
    except Exception as exc:
        logger.warning(
            "Could not queue the cut turn of conversation %s for its automatic resume; "
            "the person resumes it in one click: %s",
            cid,
            format_exception_message(exc),
        )
        return False
    logger.warning(
        "Chat turn of conversation %s cut by a platform %s: queued for its automatic resume "
        "%d of %d by the next process (%d in a row without progress)",
        cid,
        cause,
        resumes_made + 1,
        MAX_TOTAL_AUTO_RESUMES,
        stalled,
    )
    return True


async def take_cut_turn(conversation_id: object) -> CutTurn | None:
    """Remove and return the cut turn of *conversation_id*: one resume, whoever asks first.

    ``GETDEL`` is atomic, so two sweeps (two workers) never both resume it. A
    person who wrote or stopped the conversation since the cut needs no
    separate forget: the resume runner reads it and runs nothing
    (``parent_wake.resume_cut_turn``).
    """
    cid = str(conversation_id)
    try:
        client = await _redis()
        raw = await client.getdel(record_key(cid))
        await client.srem(PENDING_KEY, cid)
    except Exception:
        logger.debug("Could not take the cut turn of conversation %s", cid, exc_info=True)
        return None
    taken = CutTurn.from_raw(raw)
    if taken is not None:
        # Until the resumed turn holds the conversation, the notice still reads
        # "resuming by itself": before, a refetch in that gap offered
        # Resume, and the click made the resume stand down.
        try:
            await client.set(resuming_key(cid), "1", ex=RESUMING_TTL_SECONDS)
        except Exception:
            logger.debug("Could not mark the cut turn of conversation %s as resuming", cid, exc_info=True)
    return taken


async def put_back_cut_turn(cut: CutTurn) -> bool:
    """Queue *cut* again exactly as it was taken; whether it was. Never raises.

    For a take whose resume never started its turn: the sweep's process was
    asked to stop right after the take, or the drain cancelled the resume
    while it was still reading the conversation. The record is the one taken
    (same resume count, same first cut), so the next process resumes it and
    no automatic resume is spent; before, the take had removed it and
    nothing queued it again, so the person had to click Resume.
    """
    cid = str(cut.conversation_id)
    try:
        client = await _redis()
        await client.eval(_QUEUE_CUT_TURN, 2, record_key(cid), PENDING_KEY, cut.to_json(), RECORD_TTL_SECONDS, cid)
    except Exception as exc:
        logger.warning(
            "Could not put back the cut turn of conversation %s for the next process; "
            "the person resumes it in one click: %s",
            cid,
            format_exception_message(exc),
        )
        return False
    logger.warning(
        "Cut turn of conversation %s put back in the queue: its resume %d of %d did not start "
        "before this process stopped; the next process resumes it",
        cid,
        cut.resumes + 1,
        MAX_TOTAL_AUTO_RESUMES,
    )
    return True


async def resume_settled(conversation_id: object) -> None:
    """The resume of a taken cut turn ran or stood down: drop its marker. Never raises."""
    try:
        client = await _redis()
        await client.delete(resuming_key(str(conversation_id)))
    except Exception:
        logger.debug("Could not clear the resuming marker of conversation %s", conversation_id, exc_info=True)


#: Delete the record only while it still holds the value read (compare-and-delete).
_FORGET_IF_SAME = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
  redis.call('DEL', KEYS[1])
  redis.call('SREM', KEYS[2], ARGV[2])
  return 1
end
return 0
"""


async def forget_cut_turn(conversation_id: object, *, user_message_id: object) -> bool:
    """Drop the queued resume of *user_message_id* only; whether it was dropped.

    Called by a cut turn that answered before its process ended. A record
    queued since for another message of the conversation (a concurrent
    stream cut meanwhile) is left: compared and deleted atomically. Never
    raises.
    """
    cid = str(conversation_id)
    try:
        client = await _redis()
        raw = await client.get(record_key(cid))
        record = CutTurn.from_raw(raw)
        if record is None or record.user_message_id != str(user_message_id):
            return False
        return bool(await client.eval(_FORGET_IF_SAME, 2, record_key(cid), PENDING_KEY, raw, cid))
    except Exception:
        logger.debug("Could not forget the cut turn of conversation %s", cid, exc_info=True)
        return False


async def peek_cut_turn(conversation_id: object) -> CutTurn | None:
    """The cut turn of *conversation_id* waiting for its resume, left in the queue; ``None`` when none."""
    try:
        client = await _redis()
        return CutTurn.from_raw(await client.get(record_key(str(conversation_id))))
    except Exception:
        logger.debug("Could not read the cut turn of conversation %s", conversation_id, exc_info=True)
        return None


async def is_queued(conversation_id: object) -> bool:
    """Whether a cut turn of *conversation_id* waits for its automatic resume.

    The conversation's interrupted notice reads it: while it is queued the turn
    resumes by itself and the notice offers no Resume click to race it. Queued
    means the record AND its entry in the pending set the sweep reads: a record
    left by a partial write is not swept, so it keeps the one click.
    """
    cid = str(conversation_id)
    try:
        client = await _redis()
        # Taken by a sweep and being resumed: still "resuming by itself".
        if await client.exists(resuming_key(cid)):
            return True
    except Exception:
        logger.debug("Could not read the resuming marker of conversation %s", cid, exc_info=True)
    if await peek_cut_turn(cid) is None:
        return False
    try:
        client = await _redis()
        return bool(await client.sismember(PENDING_KEY, cid))
    except Exception:
        logger.debug("Could not read the pending set for conversation %s", cid, exc_info=True)
        return False


async def resumes_by_itself(conversation_id: object) -> bool:
    """Whether the interrupted notice may say the turn resumes by itself.

    True only while the turn is queued (:func:`is_queued`) AND agentic AI may
    run: with AI off, :func:`sweep_once_if_pending` leaves the queue waiting
    (up to its 24 h TTL), so the notice keeps its Resume button (disabled by
    the same gate) instead of promising a resume that cannot start. The gate is
    read only for a queued turn, so an ordinary conversation opens no session.
    A gate that cannot be read answers ``False``: the one click stays.
    """
    if not await is_queued(conversation_id):
        return False
    try:
        return await _agentic_ai_on()
    except Exception:
        logger.debug("Could not read the agentic gate for conversation %s", conversation_id, exc_info=True)
        return False


async def queued_conversations(conversation_ids: Iterable[object]) -> set[str] | None:
    """Which of *conversation_ids* have a cut turn queued; ``None`` when Redis cannot say.

    Queued as :func:`is_queued` reads it: the record and its pending-set entry,
    or the resuming marker of a record another process's sweep already took.
    A rollout's next process takes a cut turn within seconds, long before the
    stopping process's shutdown sweep runs (after its drain): without the
    marker that sweep failed the parent run the resumed turn was adopting.
    The stale sweep reads ``None`` as "keep them": a parent run is never failed
    for a resume Redis could not confirm.
    """
    cids = sorted({str(c) for c in conversation_ids if c})
    if not cids:
        return set()
    try:
        client = await _redis()
        records = await client.mget(*(record_key(c) for c in cids))
        resuming = await client.mget(*(resuming_key(c) for c in cids))
        queued: set[str] = set()
        for cid, raw, marker in zip(cids, records, resuming, strict=True):
            if marker is not None:
                queued.add(cid)
                continue
            if CutTurn.from_raw(raw) is not None and await client.sismember(PENDING_KEY, cid):
                queued.add(cid)
        return queued
    except Exception:
        logger.debug("Could not read the queued cut turns of %d conversation(s)", len(cids), exc_info=True)
        return None


#: The resume runner the sweep hands a taken cut turn to; set by the chat layer
#: (``parent_wake.resume_cut_turn``) so this module imports no turn code.
Resumer = Callable[[CutTurn], Awaitable[Any]]


def _current_build() -> str | None:
    try:
        from app.core.config import settings

        return str(settings.app_version) or None
    except Exception:
        return None


def _waits_for_another_build(cut: CutTurn, now: datetime, build: str | None) -> bool:
    """Whether *cut*, cut by a stop, still waits for a process of a build other than its own."""
    if cut.cause != "stop" or not cut.build or cut.build != build:
        return False
    age = _cut_age_seconds(cut, now)
    return age is not None and age < SAME_BUILD_DEFER_SECONDS


def _process_stopping() -> bool:
    from app.core.worker_shutdown import shutdown_request

    return shutdown_request() is not None


async def _release_dead_lock(conversation_id: str, token: str) -> bool:
    from app.services.chat_turn_guard import release_turn

    return await release_turn(conversation_id, token)


def _cut_age_seconds(cut: CutTurn, now: datetime) -> float | None:
    cut_at = _when(cut.cut_at)
    return None if cut_at is None else (now - cut_at).total_seconds()


async def sweep_cut_turns(
    resume: Resumer,
    *,
    is_turn_active: Callable[[str], Awaitable[bool]] | None = None,
    is_stopping: Callable[[], bool] | None = None,
    release_dead_lock: Callable[[str, str], Awaitable[bool]] | None = None,
    now: datetime | None = None,
    build: str | None = None,
) -> int:
    """Resume the cut turns waiting in the queue; returns how many were handed to *resume*.

    Run every :data:`SWEEP_INTERVAL_SECONDS` by every process
    (:func:`run_sweep_loop`). A process that was itself asked to stop takes
    nothing: a resume started there is cut again within its drain and
    spends one of the message's automatic resumes. A conversation
    whose turn lock is held waits, unless the lock still holds the cut turn's
    own token :data:`DEAD_LOCK_GRACE_SECONDS` after the cut: that process
    died before its release, so the lock is freed (compare-and-delete). One
    whose record expired leaves the queue. A turn a stop cut on this very
    build waits :data:`SAME_BUILD_DEFER_SECONDS` for a process of the new
    one. The cut turn is taken atomically (:func:`take_cut_turn`) before it
    is handed on.
    """
    if (is_stopping or _process_stopping)():
        return 0
    build = build if build is not None else _current_build()
    if is_turn_active is None:
        from app.services.chat_turn_guard import is_turn_active as _active

        is_turn_active = _active
    release_dead_lock = release_dead_lock or _release_dead_lock
    moment = now or datetime.now(UTC)
    try:
        client = await _redis()
        members = await client.smembers(PENDING_KEY)
    except Exception as exc:
        logger.debug("Cut turn sweep could not read its queue: %s", format_exception_message(exc))
        return 0
    handed = 0
    for raw in members or ():
        cid = raw.decode() if isinstance(raw, bytes) else str(raw)
        try:
            waiting = await peek_cut_turn(cid)
            if waiting is not None and _waits_for_another_build(waiting, moment, build):
                continue
            if await is_turn_active(cid) and not await _freed_dead_lock(cid, moment, release_dead_lock):
                continue
            taken = await take_cut_turn(cid)
        except Exception:
            logger.debug("Cut turn sweep skipped conversation %s", cid, exc_info=True)
            continue
        if taken is None:
            continue
        if (is_stopping or _process_stopping)():
            # Asked to stop while it swept: a resume started here would be
            # cut within the drain, or never start. The next process takes it.
            await put_back_cut_turn(taken)
            await resume_settled(cid)
            return handed
        try:
            await resume(taken)
            handed += 1
        except Exception as exc:
            logger.warning(
                "Could not resume the cut turn of conversation %s; the person resumes it in one click: %s",
                cid,
                format_exception_message(exc),
            )
    return handed


async def _freed_dead_lock(
    cid: str, now: datetime, release_dead_lock: Callable[[str, str], Awaitable[bool]]
) -> bool:
    """Free the lock of *cid* when it still holds the cut turn's token past the drain; whether it was."""
    cut = await peek_cut_turn(cid)
    if cut is None or not cut.lock_token:
        return False
    age = _cut_age_seconds(cut, now)
    if age is None or age < DEAD_LOCK_GRACE_SECONDS:
        return False
    if not await release_dead_lock(cid, cut.lock_token):
        return False
    logger.warning(
        "Freed the turn lock of conversation %s: the process of its cut turn ended %.0f s ago "
        "without releasing it; resuming the cut turn",
        cid,
        age,
    )
    return True


async def _agentic_ai_on() -> bool:
    from app.core.database import async_session
    from app.services.agentic_gate import agentic_refusal

    async with async_session() as db:
        return await agentic_refusal(db) is None


async def sweep_once_if_pending() -> int:
    """One sweep when the queue holds something and agentic AI runs; how many were resumed.

    An empty queue costs one ``SCARD`` and no database session.
    """
    if _process_stopping():
        return 0
    try:
        client = await _redis()
        if not await client.scard(PENDING_KEY):
            return 0
    except Exception:
        return 0
    if not await _agentic_ai_on():
        # The queue waits (24 h). The notice reads the same gate
        # (:func:`resumes_by_itself`): it shows Resume, not "resuming
        # by itself", until AI is back on and this sweep takes the record.
        return 0
    from app.services.parent_wake import schedule_cut_turn_resume

    resumed = await sweep_cut_turns(schedule_cut_turn_resume)
    if resumed:
        logger.info("Resuming %d chat turn(s) cut by a platform stop", resumed)
    return resumed


async def run_sweep_loop(interval: float = SWEEP_INTERVAL_SECONDS) -> None:
    """Sweep the queue every *interval* seconds, from the start of the process.

    Every worker runs it; :func:`take_cut_turn` makes each resume happen once.
    The first sweep runs at once, so a new pod resumes the turns the rollout
    cut as soon as their locks are free, not at the next scheduler window
    (which a draining pod may hold). The Slack and Teams turns a rollout put
    back on their conversation's queue are drained by the same loop
    (:func:`sweep_channel_turns_once`): the scheduler's minute tick alone left
    the sender with no answer and no typing for a minute or more (#11924).
    Never raises; cancelled at shutdown.
    """
    import asyncio

    while True:
        try:
            await sweep_once_if_pending()
        except Exception as exc:
            logger.error("Cut turn resume sweep error: %s", format_exception_message(exc), exc_info=True)
        await sweep_channel_turns_once()
        await asyncio.sleep(interval)


async def sweep_channel_turns_once() -> int:
    """Drain the channel queues a cut turn or a dead holder left; how many drains started. Never raises.

    :func:`channel_continuation.sweep_channel_queues`: an empty index costs
    one ``SMEMBERS``, a stopping process drains nothing, and the drain takes
    the conversation's lock before it claims, so two workers never answer the
    same message.
    """
    try:
        from app.services.channel_continuation import sweep_channel_queues

        restarted = await sweep_channel_queues()
    except Exception as exc:
        logger.debug("Channel queue sweep skipped: %s", format_exception_message(exc))
        return 0
    if restarted:
        logger.info("Answering the channel queue of %d conversation(s) a cut or a dead turn left", restarted)
    return restarted


#: Trace metadata key, on the trace of a cut turn, of the trace that resumed it.
RESUMED_AS_TRACE_KEY = "resumed_as"
#: Trace metadata key, on the trace of a cut turn no resume follows, of why.
RESUME_SKIPPED_TRACE_KEY = "resume_skipped"


def resumed_from_trace_meta(cut: CutTurn, *, cut_trace_id: object) -> dict:
    """The metadata of a resumed turn's trace: the cut turn's trace, cause and attempt."""
    return {
        RESUMED_FROM_META_KEY: {
            "trace_id": str(cut_trace_id) if cut_trace_id else None,
            "turn_id": cut.turn_id,
            "cause": cut.cause,
            "cut_at": cut.cut_at,
            "resume": cut.resumes + 1,
            "of": MAX_TOTAL_AUTO_RESUMES,
            "entry": cut.entry,
        }
    }


async def cut_turn_trace_id(db: Any, cut: CutTurn, *, not_before: datetime | None = None) -> Any:
    """The durable trace of the cut turn, or ``None``.

    The conversation's latest root trace (``llm_traces``, no parent) started
    between *not_before* (the cut user message was stored) and the cut, so an
    earlier turn's trace is never taken for it. ``None`` when the cut process
    could not flush it: the link is then missing, never wrong. Never raises.
    """
    from sqlalchemy import select

    from app.models.llm_trace import LLMTrace

    try:
        stmt = select(LLMTrace.id).where(
            LLMTrace.conversation_id == _uuid(cut.conversation_id),
            LLMTrace.parent_trace_id.is_(None),
            # A consult flushed before its parent keeps the parent in its
            # metadata only: it is not the cut turn's root.
            LLMTrace.metadata_json["parent_trace_id"].astext.is_(None),
        )
        cut_at = _when(cut.cut_at)
        if cut_at is not None:
            stmt = stmt.where(LLMTrace.started_at <= cut_at)
        if not_before is not None:
            stmt = stmt.where(LLMTrace.started_at >= _aware(not_before))
        stmt = stmt.order_by(LLMTrace.started_at.desc()).limit(1)
        return (await db.execute(stmt)).scalar_one_or_none()
    except Exception:
        logger.debug("Could not read the trace of the cut turn of %s", cut.conversation_id, exc_info=True)
        try:
            await db.rollback()
        except Exception:
            logger.debug("Could not roll back after reading the cut trace", exc_info=True)
        return None


async def mark_trace_resumed_as(db: Any, cut_trace_id: object, resumed_trace_id: object) -> bool:
    """Record on the cut turn's trace the trace that resumed it (``resumed_as``); whether it was.

    A JSONB merge on the stored PostgreSQL row, so the trace's other metadata
    stays. The ClickHouse read store is append-only and keeps the row as it
    was flushed: there the link is read from the resumed trace's
    ``resumed_from``. Never raises; a failure rolls the session back.
    """
    if not cut_trace_id or not resumed_trace_id:
        return False
    return await _merge_trace_metadata(db, cut_trace_id, {RESUMED_AS_TRACE_KEY: str(resumed_trace_id)})


async def mark_trace_resume_skipped(db: Any, cut_trace_id: object, reason: str) -> bool:
    """Record on the cut turn's trace why no resume follows it (``resume_skipped``); whether it was.

    So a reader of a cut trace with no ``resumed_as`` tells a resume the
    platform chose not to make (the turn already acted, the cap reached, a
    cancellation that is not a platform stop) from one that was lost. The
    same JSONB merge as :func:`mark_trace_resumed_as`. Never raises.
    """
    if not cut_trace_id or not reason:
        return False
    return await _merge_trace_metadata(db, cut_trace_id, {RESUME_SKIPPED_TRACE_KEY: reason})


async def _merge_trace_metadata(db: Any, trace_id: object, patch_value: dict) -> bool:
    import sqlalchemy as sa
    from sqlalchemy.dialects.postgresql import JSONB

    from app.models.llm_trace import LLMTrace

    try:
        patch = sa.literal(patch_value, type_=JSONB)
        result = await db.execute(
            sa.update(LLMTrace)
            .where(LLMTrace.id == _uuid(trace_id))
            .values(metadata_json=sa.func.coalesce(LLMTrace.metadata_json, sa.literal({}, type_=JSONB)).op("||")(patch))
        )
        await db.commit()
        return bool(getattr(result, "rowcount", 0))
    except Exception:
        logger.debug("Could not update the metadata of the cut trace %s", trace_id, exc_info=True)
        # The caller's session goes on to discard the checkpoint: never leave
        # it in a failed transaction.
        try:
            await db.rollback()
        except Exception:
            logger.debug("Could not roll back after updating the cut trace", exc_info=True)
        return False


def _uuid(value: object) -> Any:
    import uuid

    return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))


def _when(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return _aware(parsed)


def _aware(moment: datetime) -> datetime:
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


def resumed_from_meta(cut: CutTurn, *, resumed_as: str | None) -> dict:
    """The trace link stored on the reply of a resumed turn: from the cut turn to this one."""
    return {
        RESUMED_FROM_META_KEY: {
            "turn_id": cut.turn_id,
            "cause": cut.cause,
            "cut_at": cut.cut_at,
            "resume": cut.resumes + 1,
            "of": MAX_TOTAL_AUTO_RESUMES,
            "resumed_as": resumed_as,
        }
    }


def resuming_frame(cut: CutTurn) -> dict:
    """The frame the conversation's viewers read when the resume starts."""
    return {
        "type": RESUMING_FRAME_TYPE,
        "cause": cut.cause,
        "resume": cut.resumes + 1,
        "of": MAX_TOTAL_AUTO_RESUMES,
    }
