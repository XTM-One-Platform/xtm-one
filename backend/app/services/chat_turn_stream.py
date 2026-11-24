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

"""Live fan-out and resume of an in-flight chat turn.

The agent loop reports a turn's progress (``thinking`` ack, ``status``
frames, ``stream`` tokens, intermediate segments, the terminal ``done``)
through the ``on_event`` / ``on_token`` callbacks of whatever STARTED the
turn. This module makes that progress a property of the CONVERSATION, so a
page reloaded mid-turn, a second device or an impersonating admin follows the
turn as it runs:

* :class:`TurnPublisher` — held by the turn owner (the web chat's send, REST
  ``send_message``, parent wake). Every outbound turn frame is offered to it
  (non-blocking) and a single drainer task writes, once per drain cycle and
  in ONE Redis pipeline: the live batch on the conversation's pub/sub
  channel, plus a compact **journal** of the in-flight turn keyed by the turn
  token — the ``status`` frames in order, the reasoning text and the answer
  text of the current segment as two appendable strings, the turn's start
  time and a sequence number. Terminal frames end the journal.
* :func:`relay_turn_frames` — run by EVERY stream of the conversation. It
  listens on the channel through the process-wide :class:`_RelayHub` (one
  Redis subscription per process, fanned out to in-process queues, so a
  viewer costs no pooled Redis connection) and, whenever it (re)attaches,
  **resumes** the stream into the turn in flight: the journal is replayed
  through the SAME frame vocabulary the client already handles — a
  ``thinking`` ack flagged ``resumed`` carrying ``elapsed_s``, the status
  frames, one ``thinking_text`` frame holding the reasoning so far, one
  ``stream`` frame holding the answer so far, the ``approval_required``
  prompt a paused turn waits on — and the live frames follow,
  deduplicated by ``(turn, seq)`` against the snapshot. Frames the stream
  itself originated are never echoed back to it.

Publishing is best-effort by contract: a dead Redis must never slow a turn
down, so the drainer is decoupled from the loop by a queue and, after a
failure, goes quiet for :data:`FAILURE_COOLDOWN_SECONDS` (the same contract
as ``flow_run_stream.py``). The client's polling of ``is_processing`` stays
the floor; this is the accelerator that makes it the same experience.

Best-effort never means a relayed turn is left open: a client
following a relayed turn has no poll behind it. A terminal frame a Redis failure kept off the channel is delivered once
Redis answers (the publisher's terminal recovery), the tombstone keeps the
terminal so a resync after the turn ended delivers it, and a turn the relay
shows whose end never arrives is probed (:data:`LIVENESS_CHECK_SECONDS`)
and closed - with its own terminal when readable, with
:func:`lost_turn_frame` once its lock is gone and nothing else can deliver
it. Every frame of a turn names it (``turn_id``, :func:`public_turn_id`)
and relayed frames say so (``relayed``), so a client can always tell a
turn it started from one it did not.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import time
import uuid
import weakref
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from app.core.redaction import redact_frame
from app.core.utils import chat_processing_key, format_exception_message
from app.services.chat_turn_guard import (
    REST_REFRESH_INTERVAL_SECONDS,
    get_pending_approvals,
    interrupted_turn,
    mark_turn_interrupted,
)
from app.services.cut_turn_resume import RESUMING_FRAME_TYPE
from app.services.runner_activity import (
    CONSULT_PROGRESS_STATUS,
    MAX_CONSULT_DEPTH,
    unwrap_consult_frame,
)

logger = logging.getLogger(__name__)

# ── Wire contract ────────────────────────────────────────────────────────

#: ``on_event`` status of an agentic-action charge: the turn was
#: charged ``actions`` agentic actions - one event per metered request, so a
#: turn that runs several passes (a steering message folded in) sends one per
#: pass - counted in the quota ``period`` it names.
AGENTIC_ACTION_STATUS = "agentic_action"

#: Frame type the charge travels as (:func:`event_frame`); the web chat counter
#: shows it as the in-progress ``(+N)``. A type of its own rather than a
#: ``status``: every client released before it (the web chat, the Office
#: add-in, the voice dialog) ignores a frame type it does not know, where a
#: status lands on its working label. Journaled as running totals per period
#: in the state hash, never in the trimmed status list: a charge is the first
#: frame of its pass, and the list keeps the LAST statuses. Mirrored by
#: ``AGENTIC_ACTION_FRAME`` in ``frontend/src/lib/agentic-usage.ts``.
AGENTIC_ACTION_FRAME = "agentic_action"

#: Outbound frame types that belong to the TURN and are fanned out to every
#: stream of the conversation. Everything else a send stream writes
#: (``wait_skipped``, ``folded_into_active_turn``, ``turn_started``, an
#: out-of-turn ``error``) stays on that stream.
RELAYED_FRAME_TYPES = frozenset(
    {
        "thinking",
        "status",
        "stream",
        "done",
        "cancelled",
        "error",
        "user_message",
        AGENTIC_ACTION_FRAME,
        # A paused turn's prompt and its end: any client of the
        # conversation may answer it, so every stream must see it appear and go.
        "approval_required",
        "approval_resolved",
        # The opening of a turn the platform resumed by itself after a cut
        # (``cut_turn_resume.resuming_frame``): every viewer says so.
        RESUMING_FRAME_TYPE,
    }
)

#: The frames that belong to ONE turn (every relayed type but
#: ``user_message``). Each names its turn with ``turn_id`` - on the stream
#: that runs the turn, and on every stream the turn is relayed to.
TURN_FRAME_TYPES = RELAYED_FRAME_TYPES - {"user_message"}

#: Frame types :func:`publish_live` also fans out that belong to no turn: a new
#: conversation's AI title, generated once its first turn is done, and the
#: user's likely next message (``services/chat_reply_suggestions.py``),
#: proposed once a turn is done.
#: Kept out of :data:`RELAYED_FRAME_TYPES`, so a turn publisher never journals
#: or republishes one.
LIVE_ONLY_FRAME_TYPES = frozenset({"conversation_title", "reply_suggestion"})

#: The fanned-out frames that belong to no turn and name none: the only ones
#: :func:`publish_live` sends.
NON_TURN_FRAME_TYPES = frozenset({"user_message"}) | LIVE_ONLY_FRAME_TYPES


def event_frame(event: dict) -> dict:
    """The frame one ``on_event`` event travels as: a ``status`` frame, except
    the agentic-action charge, which has a frame type of its own."""
    if event.get("status") == AGENTIC_ACTION_STATUS:
        return {
            "type": AGENTIC_ACTION_FRAME,
            **{k: v for k, v in event.items() if k != "status"},
        }
    return {"type": "status", **event}


#: Frame types that end a turn (a ``done`` flagged ``intermediate`` is a
#: mid-turn segment, not a terminal). The publisher remembers whether
#: one was offered so :meth:`TurnPublisher.aclose` can synthesize one on the
#: exit paths that never reached it.
TERMINAL_FRAME_TYPES = frozenset({"done", "cancelled", "error"})

#: ``reason`` of a terminal the turn's PRODUCER synthesized: the turn was torn
#: down before its own ``done`` (cancellation, a persistence failure, a
#: server shutdown).
TERMINAL_REASON_INTERRUPTED = "interrupted"
#: ``reason`` of a terminal the RELAY synthesized for a turn it was showing:
#: the turn is over - its lock released, lapsed (the process running it
#: stopped) or taken by another turn - and its own terminal could not be
#: recovered. The conversation history holds whatever the turn stored.
TERMINAL_REASON_LOST = "lost"

#: The terminal frame an INTERRUPTED turn closes with — a turn torn down
#: before its own ``done`` (cancellation, a persistence failure, a server
#: shutdown). Every viewer of the turn resets its working state on it at
#: once instead of at its next safety poll; the owner's stream is either
#: gone or told the real cause on its own frame.
TURN_INTERRUPTED_MESSAGE = "The response was interrupted before it completed."
#: What a viewer is told when the relay closes a turn whose end it could not
#: recover: the turn may well have finished, so the history is where to look.
TURN_LOST_MESSAGE = (
    "The end of this response could not be delivered. Reload the conversation to see it."
)


def interrupted_frame() -> dict:
    """The terminal frame offered on behalf of a turn that never reached its own."""
    return {
        "type": "error",
        "content": TURN_INTERRUPTED_MESSAGE,
        "reason": TERMINAL_REASON_INTERRUPTED,
    }


def lost_turn_frame() -> dict:
    """The terminal the relay closes a shown turn with when its own is gone."""
    return {"type": "error", "content": TURN_LOST_MESSAGE, "reason": TERMINAL_REASON_LOST}


def public_turn_id(token: str) -> str:
    """The id clients see for the turn holding *token* (``turn_id``).

    A one-way digest: the token is the value of the conversation's turn LOCK
    (``chat_turn_guard``) and never leaves the server. Stable for the whole
    turn and identical on every stream and process, so the frames a stream
    receives for its own turn and the relayed copies other streams receive
    carry the same id.
    """
    return hashlib.sha256(f"chat-turn:{token}".encode()).hexdigest()[:32]


def is_terminal_frame(frame: dict) -> bool:
    """Whether *frame* ends a turn for the client (an intermediate ``done`` does not)."""
    return frame.get("type") in TERMINAL_FRAME_TYPES and frame.get("intermediate") is not True


def is_provisional_end(frame: dict) -> bool:
    """Whether *frame* ends a turn its producer then ends again with the STORED end.

    The acknowledgement of a Stop that reached the connection running the turn
    (``cancelled``) and the ``error`` a web chat turn sends when its
    agent loop raised: that turn still stores its reply and closes with it,
    a ``cancelled`` / ``done`` carrying ``message_id`` - the frame a client
    reads the stored thread back on - or, when storing it failed, the
    ``interrupted`` end. A provisional end says so (``provisional: true``);
    any other end is final.
    """
    return frame.get("type") in ("cancelled", "error") and frame.get("provisional") is True


#: Transport-only keys stamped on every published frame; stripped before a
#: frame is handed to a client.
TRANSPORT_KEYS = frozenset({"origin", "turn", "seq"})

#: ``status`` values relayed live but kept OUT of the journal's status list:
#: ``thinking_text`` goes to the reasoning string instead, a ``tool_heartbeat``
#: carries an elapsed anchor that is stale the moment it is replayed (the next
#: live beat re-anchors within ~10 s), and ``stream_retract`` only resets the
#: answer string.
_UNJOURNALED_STATUSES = frozenset({"thinking_text", "tool_heartbeat", "stream_retract"})

#: The status that opens a pause, and the key its journaled copy carries the
#: time it was journaled under (stripped from the replayed copy, whose
#: ``seconds`` become the seconds left: :func:`_replayed_status`).
WAIT_START_STATUS = "wait_start"
JOURNALED_AT_KEY = "journaled_at"

#: ``consult_progress`` inner statuses that are relayed live but not journaled
#: (the consulted agent's own reasoning chatter). ``streaming`` / ``composing``
#: ARE journaled: the client removes the consult from its active map on them.
_UNJOURNALED_INNER_STATUSES = frozenset({"thinking_text", "tool_heartbeat"})

#: ``tool_done`` payload fields no client-side state reads (the reasoning
#: dialog reads the PERSISTED ``tool_call_trace`` instead). Dropped from the
#: published copy so a 500 KB tool output is not fanned out to every viewer
#: and journaled per call, and from the owner's own stream frame.
_HEAVY_TOOL_DONE_KEYS = ("tool_output", "tool_output_full", "tool_input_full")

#: The prompt and raw-response copies an ``llm_step`` / ``tool_start`` event
#: carries for the evaluation wrapper, which reads them inside
#: ``run_agent_chat``, upstream of every stream: up to ~190K characters per
#: model call that no client reads.
_EVAL_ONLY_KEYS = ("llm_system_prompt", "llm_user_prompt", "llm_raw_response")

#: What each bucket of a ``thinking`` event's context reading is made of, read
#: server-side by the chat's ``/context`` command (#6590): only those who may
#: send in the conversation see it, and a frame reaches every viewer.
_SERVER_ONLY_KEYS = ("context_detail",)

#: ``done`` payload fields that feed the reasoning-details panel only: the
#: first dropped from a terminal the tombstone keeps when it is too large
#: (the client's end-of-turn refetch reads them off the stored message).
_TERMINAL_PANEL_KEYS = ("tool_call_trace", "reasoning", "runner_activity")

# ── Bounds ───────────────────────────────────────────────────────────────

#: TTL of the journal keys, refreshed on every flush. Longer than the turn
#: marker TTL (300 s) so a journal never outlives its marker by less than a
#: keepalive cycle; the publisher deletes the keys explicitly at turn end.
JOURNAL_TTL_SECONDS = 900
#: TTL of the tombstone the publisher leaves in the journal's state hash
#: when it closes (``ended``): the turn's owner releases the marker right
#: AFTER closing the publisher, and a stream attaching in between sees the
#: marker but has missed the terminal frame — without the tombstone it
#: would be resumed into a finished turn and sit on "Thinking" until its
#: safety poll. Outlives the marker TTL so a marker whose release failed
#: cannot resume anyone either until it lapses.
TOMBSTONE_TTL_SECONDS = 360
#: Most status frames kept per turn (oldest trimmed first). A replay past
#: this bound loses the earliest steps, never the current state.
MAX_JOURNALED_STATUS_FRAMES = 400
#: Per-string cap on the journaled reasoning / answer text of one segment.
MAX_JOURNALED_TEXT_CHARS = 200_000
#: Batch window of the drainer: a token stream produces many frames per
#: second and one pipeline per window is plenty (the web chat's coalescer already
#: batches tokens at 50 ms).
DRAIN_INTERVAL_SECONDS = 0.05
#: Upper bound on one Redis round-trip of this module.
PUBLISH_TIMEOUT_SECONDS = 3.0
#: After a publish failure, skip publishing for this long — polling is the
#: floor, so losing the accelerator briefly costs nothing.
FAILURE_COOLDOWN_SECONDS = 30.0
#: Frames the publisher holds while Redis is unreachable before dropping the
#: oldest — bounds memory on a stalled Redis, never blocks the loop.
MAX_PENDING_FRAMES = 5_000
#: Per-listener queue bound; a listener that falls this far behind is
#: resynced from the journal instead of holding the hub back.
LISTENER_QUEUE_SIZE = 2_000
#: A relayed turn that sent this stream nothing for this long is probed (its
#: lock, its tombstone): a turn whose end never arrived is closed rather than
#: left open on a client that has no poll behind it.
LIVENESS_CHECK_SECONDS = 15.0
#: How long a shown turn may be seen over with no recoverable terminal before
#: the relay closes it with :func:`lost_turn_frame`: long enough for the
#: producer's own terminal recovery after a quiet period, and for a lock lost
#: with Redis to be re-created by the turn's refresher (the REST and
#: parent-wake refreshers tick every ``REST_REFRESH_INTERVAL_SECONDS``).
TERMINAL_GRACE_SECONDS = max(FAILURE_COOLDOWN_SECONDS, REST_REFRESH_INTERVAL_SECONDS) + 30.0
#: How long a closed publisher keeps trying to deliver a terminal frame (and
#: its tombstone) that a Redis failure or a quiet period kept off the channel.
TERMINAL_RECOVERY_WINDOW_SECONDS = float(TOMBSTONE_TTL_SECONDS)
#: Pause between the end of a quiet period and a recovery attempt.
TERMINAL_RECOVERY_RETRY_SECONDS = 1.0
#: Largest terminal frame the tombstone keeps, in JSON characters. Past it
#: the reasoning-panel fields are dropped (the refetch brings them); a frame
#: still larger is not kept, and a resync then closes the turn as ``lost``.
MAX_STORED_TERMINAL_CHARS = 512_000
#: Turns a relay remembers it closed, so a late frame of one (a second copy
#: of its end, a producer that outlived its lock) never reopens it.
ENDED_TURNS_KEPT = 16

#: Sentinel a listener receives when it must re-read the journal: on
#: (re)subscription and after its queue overflowed.
RESYNC = object()


def turn_channel(conversation_id: str) -> str:
    """Pub/sub channel carrying the live frames of one conversation's turns."""
    return f"chat:turn:{conversation_id}"


def _state_key(conversation_id: str, token: str) -> str:
    return f"chat:turn:state:{conversation_id}:{token}"


def _status_key(conversation_id: str, token: str) -> str:
    return f"chat:turn:status:{conversation_id}:{token}"


def _reasoning_key(conversation_id: str, token: str) -> str:
    return f"chat:turn:reasoning:{conversation_id}:{token}"


def _answer_key(conversation_id: str, token: str) -> str:
    return f"chat:turn:answer:{conversation_id}:{token}"


def journal_keys(conversation_id: str, token: str) -> tuple[str, str, str, str]:
    """The four journal keys of one turn: state hash, status list, reasoning, answer."""
    return (
        _state_key(conversation_id, token),
        _status_key(conversation_id, token),
        _reasoning_key(conversation_id, token),
        _answer_key(conversation_id, token),
    )


async def _redis():
    """Resolve the shared Redis client lazily (test fixtures patch the module)."""
    from app.core.redis import redis_client

    return await redis_client()


# Process-wide quiet period after a publish failure. Shared by every
# publisher and by the live-frame path on purpose: a dead Redis is a
# property of the deployment, not of one turn, and a per-publisher state
# would make every NEW turn pay the connect timeout again.
_down_until = 0.0


def _redis_is_quiet() -> bool:
    return time.monotonic() < _down_until


def _trip_quiet_period(what: str, conversation_id: str, exc: Exception) -> None:
    global _down_until
    _down_until = time.monotonic() + FAILURE_COOLDOWN_SECONDS
    logger.warning(
        "chat turn %s failed for %s (quiet for %.0fs): %s",
        what,
        conversation_id,
        FAILURE_COOLDOWN_SECONDS,
        format_exception_message(exc),
    )


async def _pubsub_client():
    """Resolve a dedicated pub/sub client lazily (same patch seam as above)."""
    from app.core.redis import redis_pubsub_client

    return await redis_pubsub_client()


def _is_stream(frame: dict) -> bool:
    return frame.get("type") == "stream"


def _is_thinking_text(frame: dict) -> bool:
    return frame.get("type") == "status" and frame.get("status") == "thinking_text"


def coalesce_frames(batch: list[dict]) -> list[dict]:
    """Merge consecutive token / reasoning chunks of one drain cycle.

    A token stream lands as many tiny ``stream`` frames and a reasoning
    stream as many ``thinking_text`` frames; merging neighbours preserves
    ordering exactly (only adjacent frames of the same kind merge) while
    bounding the number of published frames per cycle. Inputs are never
    mutated.
    """
    out: list[dict] = []
    for frame in batch:
        if out:
            prev = out[-1]
            if (_is_stream(frame) and _is_stream(prev)) or (
                _is_thinking_text(frame) and _is_thinking_text(prev)
            ):
                out[-1] = {
                    **prev,
                    "content": str(prev.get("content") or "")
                    + str(frame.get("content") or ""),
                }
                continue
        out.append(frame)
    return out


def _slim_event(event: dict) -> dict:
    """*event* without the fields no client reads; *event* itself when it has none."""
    heavy = _HEAVY_TOOL_DONE_KEYS if event.get("status") == "tool_done" else ()
    dropped = {key for key in (*heavy, *_EVAL_ONLY_KEYS, *_SERVER_ONLY_KEYS) if key in event}
    if not dropped:
        return event
    return {k: v for k, v in event.items() if k not in dropped}


def slim_frame(frame: dict) -> dict:
    """Drop the payload fields no client state reads.

    The heavy ``tool_done`` fields, the evaluation-only prompt copies of
    ``llm_step`` / ``tool_start`` and the server-only detail of a context
    reading. A consulted agent's events arrive as ``consult_progress`` with
    the event under ``inner``, once per consult level; each one's payload is
    stripped the same way, or a consult's 500 KB tool output would still be
    fanned out to every viewer and journaled per call.
    """
    if frame.get("type") != "status":
        return frame
    return _slim_consult_levels(frame, MAX_CONSULT_DEPTH)


def _slim_consult_levels(event: dict, depth: int) -> dict:
    """``_slim_event`` of *event* and of the event under each of its consult wrappers."""
    slimmed = _slim_event(event)
    inner = event.get("inner")
    if depth and event.get("status") == CONSULT_PROGRESS_STATUS and isinstance(inner, dict):
        slim_inner = _slim_consult_levels(inner, depth - 1)
        if slim_inner is not inner:
            slimmed = {**slimmed, "inner": slim_inner}
    return slimmed


def _is_count(value: Any) -> bool:
    """A positive integer action count (``bool`` is an ``int`` subclass, not a count)."""
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _charged_actions(frame: dict) -> int:
    """The agentic actions an ``AGENTIC_ACTION_FRAME`` frame announces."""
    actions = frame.get("actions")
    return actions if _is_count(actions) else 0


def _charged_period(frame: dict) -> str:
    """The quota period an ``AGENTIC_ACTION_FRAME`` frame names (``""`` when none)."""
    period = frame.get("period")
    return period if isinstance(period, str) else ""


def _read_resuming(raw: str | None) -> dict | None:
    """The journaled ``turn_resuming`` frame (malformed or of another type -> none)."""
    try:
        frame = json.loads(raw or "null")
    except (TypeError, ValueError):
        return None
    if isinstance(frame, dict) and frame.get("type") == RESUMING_FRAME_TYPE:
        return frame
    return None


def _read_agentic_actions(raw: str | None) -> dict[str, int]:
    """The journal's per-period charge totals (malformed -> none)."""
    try:
        totals = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return {}
    if not isinstance(totals, dict):
        return {}
    return {
        str(period): count
        for period, count in totals.items()
        if _is_count(count)
    }


def _journaled_status(frame: dict) -> bool:
    """Whether a ``status`` frame belongs in the journal's status list."""
    status = frame.get("status")
    if status in _UNJOURNALED_STATUSES:
        return False
    if status == CONSULT_PROGRESS_STATUS:
        inner = unwrap_consult_frame(frame)
        if inner is not frame and inner.get("status") in _UNJOURNALED_INNER_STATUSES:
            return False
    return True


# ── Publisher (turn owner side) ──────────────────────────────────────────


class TurnPublisher:
    """Fan out one in-flight turn's frames and keep its resumable journal.

    Owned by whoever holds the conversation's turn lock. :meth:`offer` is
    synchronous and never blocks the agent loop; the drainer task batches,
    coalesces and writes. :meth:`on_event` is the ``run_agent_chat`` /
    ``run_general_chat`` ``on_event`` adapter for turns that have no stream
    (REST, parent wake), so a viewer sees those run step by step too.
    """

    def __init__(
        self, conversation_id: str, turn_token: str, *, origin: str | None = None
    ) -> None:
        self.conversation_id = conversation_id
        self.turn_token = turn_token
        #: Identity of the producer — the client's id for a web chat send, a
        #: random id otherwise. The relay never echoes a frame back to its origin.
        self.origin = origin or uuid.uuid4().hex
        self.started_at = time.time()
        self._pending: list[dict] = []
        self._wake = asyncio.Event()
        #: Cuts the drain window short: set by a terminal frame and by
        #: :meth:`aclose`, whose callers (the owner's ``done``, the REST
        #: response) wait for the flush.
        self._flush_now = asyncio.Event()
        self._task: asyncio.Task | None = None
        self._closed = False
        self._seq = 0
        self._reasoning_len = 0
        self._answer_len = 0
        #: Agentic actions charged to the turn so far, per quota period
        #: (``AGENTIC_ACTION_FRAME``).
        self._agentic_actions: dict[str, int] = {}
        #: The turn's ``turn_resuming`` frame, when the platform resumed it.
        self._resuming: dict | None = None
        self._journal_dropped = False
        self._terminal_offered = False
        # Terminal delivery: the LAST terminal offered, its stamped
        # copy (and the frame it was stamped from), whether that copy reached
        # the channel, and whether the tombstone keeping it was written. What
        # a Redis failure kept off is delivered by the recovery task.
        self._terminal: dict | None = None
        self._stamped_terminal: dict | None = None
        self._stamped_from: dict | None = None
        self._terminal_published = False
        self._tombstoned = False
        self._recovery: asyncio.Task | None = None

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def turn_id(self) -> str:
        """The turn's public id (``turn_id`` on every frame it publishes)."""
        return public_turn_id(self.turn_token)

    @property
    def terminal_offered(self) -> bool:
        """Whether a final end (``done`` / ``cancelled`` / ``error``, not provisional) was offered."""
        return self._terminal_offered

    @property
    def seq(self) -> int:
        """Sequence number of the last frame flushed (0 before the first)."""
        return self._seq

    def start(self, ack: dict[str, Any] | None = None) -> None:
        """Start the drainer and announce the turn (the ``thinking`` ack).

        ``ack`` adds fields to that one ack (a wake turn names the work that
        woke it, ``parent_wake.wake_start_frame``): the turn opens once.
        """
        if self._task is not None or self._closed:
            return
        self._task = asyncio.create_task(
            self._run(), name=f"chat-turn-publisher:{self.conversation_id}"
        )
        self.offer({**(ack or {}), "type": "thinking"})

    def offer(self, frame: Any) -> None:
        """Queue one outbound frame for fan-out. Never blocks, never raises."""
        if self._closed or not isinstance(frame, dict):
            return
        if frame.get("type") not in RELAYED_FRAME_TYPES:
            return
        # Offered in the turn's known-secrets scope: an echoed secret variable
        # value is fanned out and journaled as the display text.
        frame = redact_frame(frame, display=True)
        # A frame this stream RECEIVED from the relay must never be
        # re-published under a new origin — that is a fan-out loop.
        if frame.get("relayed"):
            return
        if is_terminal_frame(frame):
            if not is_provisional_end(frame):
                self._terminal_offered = True
            self._terminal = frame
            self._terminal_published = False
            self._flush_now.set()
        if len(self._pending) >= MAX_PENDING_FRAMES:
            del self._pending[0]
        self._pending.append(frame)
        self._wake.set()

    async def on_event(self, event: dict) -> None:
        """``on_event`` adapter for stream-less turns (REST, parent wake)."""
        if isinstance(event, dict):
            self.offer(event_frame(event))

    async def aclose(self, *, terminal: dict | None = None) -> None:
        """Flush what is pending, stop the drainer and drop the journal.

        *terminal* is the frame the turn should end with when it never
        reached its own — the abnormal exits (cancellation, a persistence
        failure, a server shutdown) hand :func:`interrupted_frame` over so
        every viewer leaves the working state NOW rather than at its next
        safety poll. Offered only when no final end was offered before, so a
        turn that ended properly is never told twice; a provisional end does
        not count, since the stored end it announced never came.

        Bounded: a stalled Redis costs at most one publish timeout. Safe to
        call more than once and from a cancellation path. A terminal frame or
        a tombstone that Redis refused is handed to a recovery task that
        delivers it once Redis answers again (:meth:`_recover`), so a viewer
        is never left inside a turn that ended.

        A turn closed with *terminal* never stored its answer: its record is
        kept for the conversation to say so after the turn's release
        (``chat_turn_guard.mark_turn_interrupted``).
        """
        if self._closed:
            return
        if terminal is not None and not self._terminal_offered:
            self.offer(terminal)
            await mark_turn_interrupted(self.conversation_id, self.turn_token)
        self._closed = True
        self._flush_now.set()
        self._wake.set()
        task = self._task
        if task is not None:
            try:
                await asyncio.wait_for(
                    asyncio.shield(task), timeout=PUBLISH_TIMEOUT_SECONDS + 0.5
                )
            except (TimeoutError, asyncio.CancelledError):
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
            except Exception:
                # ``_run`` only raises past its own guard on a programming
                # error; the journal is still dropped below.
                logger.debug(
                    "chat turn fan-out drainer for %s ended with an error",
                    self.conversation_id,
                    exc_info=True,
                )
        await self._drop_journal()
        if not self._tombstoned or (
            self._terminal is not None and not self._terminal_published
        ):
            self._start_recovery()

    async def _run(self) -> None:
        try:
            while True:
                await self._wake.wait()
                self._wake.clear()
                if not self._closed and not self._flush_now.is_set():
                    with contextlib.suppress(TimeoutError):
                        async with asyncio.timeout(DRAIN_INTERVAL_SECONDS):
                            await self._flush_now.wait()
                self._flush_now.clear()
                batch, self._pending = self._pending, []
                final = self._closed
                if batch or final:
                    await self._flush(batch, final=final)
                if self._closed and not self._pending:
                    return
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # pragma: no cover - _flush swallows its own
            logger.warning(
                "chat turn fan-out drainer stopped for %s: %s",
                self.conversation_id,
                format_exception_message(exc),
            )

    def _stamp(self, frame: dict) -> dict:
        """The published copy of *frame*: slimmed, named, sequenced."""
        self._seq += 1
        return {
            **slim_frame(frame),
            "turn_id": self.turn_id,
            "origin": self.origin,
            "turn": self.turn_token,
            "seq": self._seq,
        }

    def _current_stamped_terminal(self) -> dict | None:
        """The stamped copy of the LAST terminal offered (stamped now if the
        drainer never got to it), or ``None`` when no terminal was offered."""
        if self._terminal is None:
            return None
        if self._stamped_from is not self._terminal:
            self._stamped_terminal = self._stamp(self._terminal)
            self._stamped_from = self._terminal
        return self._stamped_terminal

    async def _flush(self, batch: list[dict], *, final: bool = False) -> None:
        """Publish *batch* and journal it - or, for the *final* batch of a
        closed publisher, publish it and drop the journal in the same
        transaction: the frames would be journaled only to be deleted, and a
        second round-trip sat between the terminal frame and the release."""
        frames = []
        flushed_terminal: dict | None = None
        for frame in coalesce_frames(batch):
            stamped = self._stamp(frame)
            if frame is self._terminal:
                self._stamped_terminal, self._stamped_from = stamped, frame
                flushed_terminal = frame
            frames.append(stamped)
            if frame.get("type") == AGENTIC_ACTION_FRAME:
                self._count_charge(frame)
        if _redis_is_quiet():
            # Quiet period after a failure: the frames are dropped, the
            # journal bookkeeping below is skipped too, and the next
            # resync reads whatever the journal holds. The charges are
            # counted above: the journal's next write still carries them.
            # A terminal among them is delivered by the recovery once Redis
            # answers.
            return
        try:
            r = await _redis()
            pipe = r.pipeline(transaction=True)
            if not final:
                self._journal(pipe, frames)
            if frames:
                pipe.publish(
                    turn_channel(self.conversation_id), json.dumps(frames, default=str)
                )
            if final:
                self._queue_journal_drop(pipe)
            await asyncio.wait_for(pipe.execute(), timeout=PUBLISH_TIMEOUT_SECONDS)
            if final:
                self._journal_dropped = True
                self._tombstoned = True
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            _trip_quiet_period("fan-out publish", self.conversation_id, exc)
            return
        # A newer terminal offered while this one was on the wire is still
        # owed: only the frame that IS the last terminal counts as delivered.
        if flushed_terminal is not None and flushed_terminal is self._terminal:
            self._terminal_published = True

    def _journal(self, pipe: Any, frames: list[dict]) -> None:
        """Queue the journal writes for *frames* on *pipe* (no I/O here).

        Mirrors the client's own state transitions so a replay of the
        journal lands on the state a live stream holds: a per-iteration
        ``thinking`` status and a ``stream_retract`` reset the answer text,
        a ``transfer_start`` resets the reasoning, an intermediate ``done``
        (a committed mid-turn segment) resets both. Terminal frames
        and the ``thinking`` acks are not journaled — the resume synthesizes
        its own ack, and a journal only exists while the turn does.
        """
        state, status_list, reasoning, answer = journal_keys(
            self.conversation_id, self.turn_token
        )
        for frame in frames:
            ftype = frame.get("type")
            if ftype == "stream":
                self._append_text(pipe, answer, frame.get("content"), "_answer_len")
            elif ftype == "status":
                st = frame.get("status")
                if st == "thinking_text":
                    self._append_text(
                        pipe, reasoning, frame.get("content"), "_reasoning_len"
                    )
                    continue
                if st in ("thinking", "stream_retract"):
                    pipe.delete(answer)
                    self._answer_len = 0
                elif st == "transfer_start":
                    pipe.delete(reasoning)
                    self._reasoning_len = 0
                if not _journaled_status(frame):
                    continue
                entry = {k: v for k, v in frame.items() if k not in TRANSPORT_KEYS}
                if st == WAIT_START_STATUS:
                    # When the pause began, so a replay counts down what is left.
                    entry[JOURNALED_AT_KEY] = round(time.time(), 3)
                pipe.rpush(status_list, json.dumps(entry, default=str))
                pipe.ltrim(status_list, -MAX_JOURNALED_STATUS_FRAMES, -1)
            elif ftype == "done" and frame.get("intermediate") is True:
                pipe.delete(answer)
                pipe.delete(reasoning)
                self._answer_len = 0
                self._reasoning_len = 0
            elif ftype == RESUMING_FRAME_TYPE:
                # Kept for the whole turn: a socket attaching mid-resume (a
                # reload, another device) is told it too.
                self._resuming = {k: v for k, v in frame.items() if k not in TRANSPORT_KEYS}
        mapping: dict[str, Any] = {
            "seq": self._seq,
            "started_at": f"{self.started_at:.3f}",
            "origin": self.origin,
        }
        if self._agentic_actions:
            mapping["agentic_actions"] = json.dumps(self._agentic_actions, sort_keys=True)
        if self._resuming is not None:
            mapping["resuming"] = json.dumps(self._resuming, default=str)
        pipe.hset(state, mapping=mapping)
        for key in (state, status_list, reasoning, answer):
            pipe.expire(key, JOURNAL_TTL_SECONDS)

    def _count_charge(self, frame: dict) -> None:
        """Add an ``AGENTIC_ACTION_FRAME`` frame to the turn's per-period totals.

        Counted as its batch is flushed, never when it is offered: the totals
        are journaled with the ``seq`` of the batch that carried the frame, so
        a snapshot never counts a charge a viewer still receives live.
        """
        actions = _charged_actions(frame)
        if actions:
            period = _charged_period(frame)
            self._agentic_actions[period] = self._agentic_actions.get(period, 0) + actions

    def _append_text(self, pipe: Any, key: str, text: Any, counter: str) -> None:
        if not isinstance(text, str) or not text:
            return
        used = getattr(self, counter)
        room = MAX_JOURNALED_TEXT_CHARS - used
        if room <= 0:
            return
        chunk = text[:room]
        pipe.append(key, chunk)
        setattr(self, counter, used + len(chunk))

    def _queue_journal_drop(self, pipe: Any) -> None:
        """Queue the journal drop and the ``ended`` tombstone on *pipe*.

        The tombstone keeps the turn's terminal frame (``terminal``) for as
        long as it lives, so a stream that resyncs after the turn ended - a
        hub reconnect, a listener overflow - is handed the end it missed
        instead of being left inside the turn.
        """
        state, status_list, reasoning, answer = journal_keys(
            self.conversation_id, self.turn_token
        )
        pipe.delete(status_list, reasoning, answer)
        mapping: dict[str, Any] = {"ended": 1}
        terminal = self._current_stamped_terminal()
        stored = _stored_terminal(terminal) if terminal is not None else None
        if stored is not None:
            mapping["terminal"] = stored
        pipe.hset(state, mapping=mapping)
        pipe.expire(state, TOMBSTONE_TTL_SECONDS)

    async def _drop_journal(self) -> None:
        """Drop the journal and leave the ``ended`` tombstone in its place.

        The state hash is kept (``TOMBSTONE_TTL_SECONDS``) with ``ended``
        set, so :func:`read_snapshot` answers "no turn in flight" for the
        moments between this close and the owner's release of the marker.
        A tombstone Redis refused is written by the recovery instead.
        """
        if self._journal_dropped:
            return
        self._journal_dropped = True
        if _redis_is_quiet():
            return  # written by the recovery once Redis answers
        try:
            r = await _redis()
            pipe = r.pipeline(transaction=True)
            self._queue_journal_drop(pipe)
            await asyncio.wait_for(pipe.execute(), timeout=PUBLISH_TIMEOUT_SECONDS)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            _trip_quiet_period("journal drop", self.conversation_id, exc)
            return
        self._tombstoned = True

    def _start_recovery(self) -> None:
        if self._recovery is not None and not self._recovery.done():
            return
        task = asyncio.create_task(
            self._recover(), name=f"chat-turn-terminal-recovery:{self.conversation_id}"
        )
        self._recovery = task
        _background_tasks.add(task)
        task.add_done_callback(_background_tasks.discard)

    async def _recover(self) -> None:
        """Deliver what a Redis failure kept off the channel at the turn's end.

        The terminal frame (unless it went out) and the tombstone keeping
        it, in ONE pipeline, one attempt after each quiet period until it
        lands or :data:`TERMINAL_RECOVERY_WINDOW_SECONDS` have passed.
        Publishing is best-effort, a turn's END is not: a client following
        a relayed turn waits for its terminal with nothing else to release it.
        The frame keeps its turn and sequence stamp, so a relay that already
        closed the turn from the tombstone recognises the copy and drops it.
        Past the window, the relay's liveness probe closes the turn on its
        viewers.
        """
        deadline = time.monotonic() + TERMINAL_RECOVERY_WINDOW_SECONDS
        while True:
            delay = max(0.0, _down_until - time.monotonic()) + TERMINAL_RECOVERY_RETRY_SECONDS
            if time.monotonic() + delay > deadline:
                logger.warning(
                    "chat turn end of %s could not be delivered within %.0fs; "
                    "its viewers are closed by their liveness probe",
                    self.conversation_id,
                    TERMINAL_RECOVERY_WINDOW_SECONDS,
                )
                return
            await asyncio.sleep(delay)
            if _redis_is_quiet():
                continue  # another failure meanwhile: wait that period out too
            terminal = self._current_stamped_terminal()
            publish = terminal is not None and not self._terminal_published
            try:
                r = await _redis()
                pipe = r.pipeline(transaction=True)
                if publish:
                    pipe.publish(
                        turn_channel(self.conversation_id),
                        json.dumps([terminal], default=str),
                    )
                self._queue_journal_drop(pipe)
                await asyncio.wait_for(pipe.execute(), timeout=PUBLISH_TIMEOUT_SECONDS)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                _trip_quiet_period("terminal recovery", self.conversation_id, exc)
                continue
            if publish:
                self._terminal_published = True
            self._tombstoned = True
            logger.info(
                "chat turn end of %s delivered after a Redis failure", self.conversation_id
            )
            return


def _stored_terminal(frame: dict) -> str | None:
    """The terminal frame as the tombstone keeps it (JSON), ``None`` if too large.

    Transport stamps are dropped (a relay stamps what it delivers); the
    reasoning-panel fields go first when the frame is over
    :data:`MAX_STORED_TERMINAL_CHARS` - the client's end-of-turn refetch
    brings them back from the stored message.
    """
    kept = {k: v for k, v in frame.items() if k not in TRANSPORT_KEYS}
    raw = json.dumps(kept, default=str)
    if len(raw) <= MAX_STORED_TERMINAL_CHARS:
        return raw
    kept = {k: v for k, v in kept.items() if k not in _TERMINAL_PANEL_KEYS}
    raw = json.dumps(kept, default=str)
    return raw if len(raw) <= MAX_STORED_TERMINAL_CHARS else None


def _decode_terminal(raw: Any) -> dict | None:
    """A tombstone's ``terminal`` field back as a frame (malformed → ``None``)."""
    if not raw:
        return None
    try:
        frame = json.loads(_as_str(raw))
    except (TypeError, ValueError):
        return None
    return frame if isinstance(frame, dict) and is_terminal_frame(frame) else None


# ── Live-only frames (no journal) ────────────────────────────────────────

_background_tasks: set[asyncio.Task] = set()


async def _publish_payload(conversation_id: str, payload: str) -> None:
    try:
        r = await _redis()
        await asyncio.wait_for(
            r.publish(turn_channel(conversation_id), payload),
            timeout=PUBLISH_TIMEOUT_SECONDS,
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        _trip_quiet_period("live frame publish", conversation_id, exc)


def publish_live(conversation_id: str, frame: dict, *, origin: str) -> None:
    """Fan out one frame that is not part of a turn's journal.

    Used for ``user_message`` — a user message persisted on one stream (a new
    turn, a mid-turn steering message, a REST send) appears on every other
    stream of the conversation immediately — and for the
    :data:`LIVE_ONLY_FRAME_TYPES`. Fire-and-forget, best-effort.
    """
    if _redis_is_quiet():
        return
    # A turn's frames go through its publisher, which names the turn.
    if not isinstance(frame, dict) or frame.get("type") not in NON_TURN_FRAME_TYPES:
        return
    payload = json.dumps([{**frame, "origin": origin}], default=str)
    task = asyncio.create_task(_publish_payload(conversation_id, payload))
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)


#: How a stored message reads in a ``user_message`` frame: the chat API's own
#: message serializer, so the relayed row is what a refetch returns. The API
#: registers it (:func:`set_user_message_serializer`); a service that
#: publishes a user message never imports the API layer.
_user_message_serializer: Callable[[Any], dict] | None = None


def set_user_message_serializer(serializer: Callable[[Any], dict]) -> None:
    """Register how a stored message is serialized for :func:`publish_user_message`."""
    global _user_message_serializer
    _user_message_serializer = serializer


def publish_user_message(conversation_id: str, message: Any, *, origin: str) -> None:
    """Fan a just-persisted user message out to the conversation's other streams.

    Used by the web chat's sends and steering messages and by a channel
    turn. Best-effort: a row that cannot be serialized (a stub in
    tests, a malformed metadata blob), or no serializer registered, is
    skipped rather than raised into the turn - the next refetch shows it.
    """
    serializer = _user_message_serializer
    if serializer is None:
        logger.debug("No user message serializer registered; not relaying the message")
        return
    try:
        frame = {"type": "user_message", "message": serializer(message)}
    except Exception:
        logger.debug("Could not build the relayed user_message frame", exc_info=True)
        return
    publish_live(conversation_id, frame, origin=origin)


# ── Snapshot / resume (viewer side) ──────────────────────────────────────


@dataclass
class TurnSnapshot:
    """What the journal holds about the turn in flight, at one instant."""

    token: str
    seq: int = 0
    started_at: float | None = None
    origin: str | None = None
    statuses: list[dict] = field(default_factory=list)
    reasoning: str = ""
    answer: str = ""
    #: The turn's agentic charges, per quota period (``""`` = unnamed).
    agentic_actions: dict[str, int] = field(default_factory=dict)
    #: Proposals the turn is paused on, from ``chat_turn_guard``'s record.
    pending_approvals: list[dict] = field(default_factory=list)
    #: The ``turn_resuming`` frame of a turn the platform resumed by itself.
    resuming: dict | None = None


def _as_str(value: Any) -> str:
    if isinstance(value, bytes):
        return value.decode()
    return "" if value is None else str(value)


async def read_snapshot(conversation_id: str) -> TurnSnapshot | None:
    """Read the in-flight turn's journal, or ``None`` when no turn is running.

    The turn marker (the single-flight lock of ``chat_turn_guard``) decides
    whether a turn is in flight; the journal keyed by its token describes it.
    A marker without a readable journal (a turn started before its publisher
    flushed, a producer without one, a Redis blip) yields a bare snapshot —
    the resume then announces the turn and nothing more, exactly what the
    ``is_processing`` probe used to give. A marker whose journal carries the
    ``ended`` tombstone is a turn whose publisher already closed: its
    terminal frame is out and its owner is releasing the marker, so it is
    reported as no turn at all rather than resumed into.
    """
    r = await _redis()
    token = _as_str(await r.get(chat_processing_key(conversation_id)))
    if not token:
        return None
    snapshot = TurnSnapshot(token=token)
    # Apart from the journal: an unreadable journal must not also hide the
    # prompt the turn is waiting on, since the client's own restore stands down.
    try:
        snapshot.pending_approvals = await get_pending_approvals(conversation_id) or []
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.debug("Pending approvals unreadable for %s", conversation_id, exc_info=True)
    try:
        state_key, status_key, reasoning_key, answer_key = journal_keys(
            conversation_id, token
        )
        pipe = r.pipeline(transaction=True)
        pipe.hgetall(state_key)
        pipe.lrange(status_key, 0, -1)
        pipe.get(reasoning_key)
        pipe.get(answer_key)
        state, raw_statuses, reasoning, answer = await asyncio.wait_for(
            pipe.execute(), timeout=PUBLISH_TIMEOUT_SECONDS
        )
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.debug(
            "Chat turn journal unreadable for %s — resuming from the marker alone",
            conversation_id,
            exc_info=True,
        )
        return snapshot
    state = {_as_str(k): _as_str(v) for k, v in (state or {}).items()}
    if state.get("ended"):
        return None
    with contextlib.suppress(ValueError, TypeError):
        snapshot.seq = int(state.get("seq") or 0)
    with contextlib.suppress(ValueError, TypeError):
        if state.get("started_at"):
            snapshot.started_at = float(state["started_at"])
    snapshot.agentic_actions = _read_agentic_actions(state.get("agentic_actions"))
    snapshot.resuming = _read_resuming(state.get("resuming"))
    snapshot.origin = state.get("origin") or None
    for raw in raw_statuses or []:
        try:
            entry = json.loads(_as_str(raw))
        except (TypeError, ValueError):
            continue
        if isinstance(entry, dict):
            snapshot.statuses.append(entry)
    snapshot.reasoning = _as_str(reasoning)
    snapshot.answer = _as_str(answer)
    return snapshot


@dataclass
class TurnEnd:
    """What Redis says about ONE turn a relay is showing (:func:`probe_turn_end`)."""

    #: The turn is no longer in flight: its tombstone says it ended, or the
    #: lock no longer holds its token (released, lapsed, taken by another).
    over: bool
    #: Its tombstone is written: its producer closed it, so nothing more of
    #: it will ever be published.
    ended: bool = False
    #: Its own terminal frame, when the tombstone kept one.
    terminal: dict | None = None


async def probe_turn_end(conversation_id: str, token: str) -> TurnEnd | None:
    """Whether the turn holding *token* is still in flight, and how it ended.

    ``None`` when Redis cannot be read: a relay then keeps the turn open and
    asks again, since an unreadable Redis says nothing about the turn. The
    lock is the liveness signal - every producer refreshes it while it runs
    (``chat_turn_guard.refresh_turn``) and it lapses
    (``TURN_MARKER_TTL_SECONDS``) once the process running the turn is gone
    - the same signal ``is_processing`` reads, so a relay never closes a
    turn the conversation still reports as running.
    """
    try:
        r = await _redis()
        pipe = r.pipeline(transaction=True)
        pipe.get(chat_processing_key(conversation_id))
        pipe.hgetall(_state_key(conversation_id, token))
        marker, state = await asyncio.wait_for(
            pipe.execute(), timeout=PUBLISH_TIMEOUT_SECONDS
        )
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.debug(
            "chat turn liveness unreadable for %s", conversation_id, exc_info=True
        )
        return None
    state = {_as_str(k): _as_str(v) for k, v in (state or {}).items()}
    ended = bool(state.get("ended"))
    if ended or _as_str(marker) != token:
        return TurnEnd(
            over=True, ended=ended, terminal=_decode_terminal(state.get("terminal"))
        )
    return TurnEnd(over=False)


async def _lock_token(conversation_id: str) -> str | None:
    """The token holding the conversation's turn lock (``None``: none, or unreadable)."""
    try:
        r = await _redis()
        token = _as_str(
            await asyncio.wait_for(
                r.get(chat_processing_key(conversation_id)),
                timeout=PUBLISH_TIMEOUT_SECONDS,
            )
        )
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.debug("chat turn lock unreadable for %s", conversation_id, exc_info=True)
        return None
    return token or None


async def current_turn_id(conversation_id: str) -> str | None:
    """The ``turn_id`` of the turn holding the conversation's lock, if any.

    Best-effort: ``None`` when no turn holds the lock or Redis cannot say.
    """
    token = await _lock_token(conversation_id)
    return public_turn_id(token) if token else None


async def fold_turn_id(
    conversation_id: str, accepted_token: str | None, *, folded: bool
) -> str | None:
    """The ``turn_id`` a send that started no turn is told.

    Folded: the turn that accepted the message, named from the token the
    fold's own marker check read (``acquire_or_fold(accepted_by=...)``),
    never from a second read of the lock - by then that turn may have
    released it, or the next one taken it. ``None`` when the exiting turn's
    release-drain took the message: that turn is gone. Queued (the requeue
    fallback): the turn holding the lock now, whose bridge picks the
    persisted message up live.
    """
    if folded:
        return public_turn_id(accepted_token) if accepted_token else None
    return await current_turn_id(conversation_id)


def _replayed_status(frame: dict, now: float | None) -> dict:
    """A journaled status as a stream attaching now receives it.

    A ``wait_start`` carries the seconds still to wait, not the seconds
    granted when the pause began: replayed as journaled, a page reloaded
    in the middle of a pause counted the whole pause down again (#10910).
    """
    at = frame.get(JOURNALED_AT_KEY)
    if at is None:
        return frame
    replayed = {k: v for k, v in frame.items() if k != JOURNALED_AT_KEY}
    seconds = replayed.get("seconds")
    if isinstance(at, int | float) and isinstance(seconds, int | float) and not isinstance(seconds, bool):
        elapsed = max(0.0, (now or time.time()) - float(at))
        replayed["seconds"] = max(0.0, round(float(seconds) - elapsed, 1))
    return replayed


def resume_frames(snapshot: TurnSnapshot, *, now: float | None = None) -> list[dict]:
    """The frame sequence that lands a fresh stream on the turn's live state.

    Replayed through the client's ordinary handlers: the ``resumed`` ack
    (re)arms the working state and resets both text buffers, the status
    frames rebuild the label / tool-usage / consult state in order, and the
    two text frames refill the reasoning window and the answer bubble — the
    journal already applied every reset the status sequence implies, so
    the texts describe the CURRENT segment. The turn's agentic charges come
    right after the ack, ONE frame per quota period carrying its total. A
    paused turn ends the sequence with its ``approval_required`` prompt.
    """
    ack: dict = {"type": "thinking", "resumed": True}
    if snapshot.started_at is not None:
        ack["elapsed_s"] = max(0.0, round((now or time.time()) - snapshot.started_at, 1))
    frames: list[dict] = [ack]
    # Right after the ack: the line saying the turn resumed by itself shows
    # before any replayed step.
    if snapshot.resuming:
        frames.append(snapshot.resuming)
    for period, actions in sorted(snapshot.agentic_actions.items()):
        charge: dict = {"type": AGENTIC_ACTION_FRAME, "actions": actions}
        if period:
            charge["period"] = period
        frames.append(charge)
    frames.extend(_replayed_status(status, now) for status in snapshot.statuses)
    if snapshot.reasoning:
        frames.append(
            {"type": "status", "status": "thinking_text", "content": snapshot.reasoning}
        )
    if snapshot.answer:
        frames.append({"type": "stream", "content": snapshot.answer})
    # Last, so it lands on the rebuilt state: without it a stream attaching
    # mid-pause shows "Thinking" while the turn waits on it.
    if snapshot.pending_approvals:
        frames.append({"type": "approval_required", "proposals": snapshot.pending_approvals})
    return frames


def decode_batch(payload: Any) -> list[dict]:
    """Parse one pub/sub message back into its frames (malformed → empty)."""
    try:
        data = json.loads(_as_str(payload))
    except (TypeError, ValueError):
        return []
    if isinstance(data, dict):
        return [data]
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    return []


# ── Relay hub (one Redis subscription per process) ───────────────────────


def _offer_to_listener(queue: asyncio.Queue, item: Any) -> None:
    """Deliver *item* to a listener; a listener that fell behind is resynced."""
    try:
        queue.put_nowait(item)
    except asyncio.QueueFull:
        # Drop everything queued and ask the listener to re-read the journal:
        # a partial frame sequence would leave holes in its text buffers.
        while True:
            try:
                queue.get_nowait()
            except asyncio.QueueEmpty:
                break
        with contextlib.suppress(asyncio.QueueFull):
            queue.put_nowait(RESYNC)


class _SubscribeFailedError(RuntimeError):
    """Raised by the hub reader to tear down a live connection on which a
    channel SUBSCRIBE failed, so the reconnect covers that channel again."""


class _RelayHub:
    """One pub/sub subscription per process, fanned out to in-process queues.

    A viewer stream holds a bounded ``asyncio.Queue`` here, never a Redis
    connection of its own: with a default pool of 50 and the SSE activity
    stream already pinning one connection per tab, a per-stream subscription
    would exhaust the pool on the first busy afternoon. Channels are
    subscribed on the first listener and unsubscribed on the last; a
    subscription confirmation (or a reconnect) queues :data:`RESYNC` for
    every listener of the channel so nothing published in between is lost.
    """

    def __init__(self) -> None:
        self._listeners: dict[str, set[asyncio.Queue]] = {}
        self._confirmed: dict[str, asyncio.Event] = {}
        # Guards the bookkeeping only. No network I/O ever runs under it: a
        # stream tearing down must never wait on a Redis connect timeout.
        self._lock = asyncio.Lock()
        self._task: asyncio.Task | None = None
        # The reader the last ``detach`` cancelled and that has not finished
        # its teardown yet. ``attach`` waits for it before starting the next
        # generation: the reader's ``finally`` closes whatever connection the
        # hub holds at that moment, so a replacement that connected in
        # between would have its connection closed under it (and its
        # readiness reset) by a task that no longer serves anyone.
        self._retiring: asyncio.Task | None = None
        self._pubsub: Any = None
        self._client: Any = None
        # True once the CURRENT connection has subscribed every listener
        # channel; from then on a channel change applies itself. Read and
        # written under the lock so an attach can never fall between the
        # reader's last "anything pending?" check and its readiness.
        self._ready = False
        # Serializes the SUBSCRIBE / UNSUBSCRIBE commands of the current
        # connection, and nothing else. ``attach`` / ``detach`` only decide
        # the DESIRED state (under ``_lock``); ``_apply`` re-reads it under
        # this lock right before each command, so two changes of the same
        # channel racing each other - its last listener leaving, a
        # replacement attaching - converge on the desired state whatever
        # their order, instead of a stale unsubscribe landing after the
        # replacement's subscribe and silencing a healthy reader.
        self._wire_lock = asyncio.Lock()
        #: Channels subscribed on the CURRENT connection.
        self._wire: set[str] = set()
        # Detach never waits on Redis: its change is applied by a task.
        self._apply_tasks: set[asyncio.Task] = set()
        # Set by ``_apply`` when a SUBSCRIBE failed on a connection that is
        # otherwise alive: the reader tears that connection down and the
        # reconnect covers every listener channel again. Without it the
        # channel would stay unsubscribed for as long as the connection
        # lived, its listeners getting neither a confirmation nor a frame.
        self._resubscribe = False
        self._warned = False

    async def attach(self, conversation_id: str) -> asyncio.Queue:
        channel = turn_channel(conversation_id)
        queue: asyncio.Queue = asyncio.Queue(maxsize=LISTENER_QUEUE_SIZE)
        apply_now = False
        while True:
            async with self._lock:
                retiring = self._retiring
                if retiring is not None and retiring.done():
                    self._retiring = retiring = None
                if retiring is None:
                    listeners = self._listeners.setdefault(channel, set())
                    first = not listeners
                    listeners.add(queue)
                    if self._task is None or self._task.done():
                        self._task = asyncio.create_task(
                            self._run(), name="chat-turn-relay-hub"
                        )
                    elif first:
                        self._confirmed[channel] = asyncio.Event()
                        apply_now = self._ready
                    elif (
                        self._confirmed.get(channel) is not None
                        and self._confirmed[channel].is_set()
                    ):
                        # Already subscribed and confirmed: nothing will
                        # announce this listener, so ask it to read the
                        # journal now.
                        _offer_to_listener(queue, RESYNC)
                    break
            # One hub generation at a time: the previous reader is still
            # tearing down (bounded by its own close timeouts). ``wait``
            # rather than ``await``, so its cancellation is not mistaken for
            # ours — our own cancellation still propagates from here.
            await asyncio.wait({retiring})
        if apply_now:
            await self._apply(channel)
        return queue

    async def detach(self, conversation_id: str, queue: asyncio.Queue) -> None:
        channel = turn_channel(conversation_id)
        apply_now = False
        stop_task: asyncio.Task | None = None
        async with self._lock:
            listeners = self._listeners.get(channel)
            if listeners is not None:
                listeners.discard(queue)
                if not listeners:
                    self._listeners.pop(channel, None)
                    self._confirmed.pop(channel, None)
                    apply_now = self._ready
            if not self._listeners and self._task is not None:
                stop_task, self._task = self._task, None
                self._retiring = stop_task
        if stop_task is not None:
            # The last listener is gone: the reader's own ``finally`` closes
            # the subscription — nothing to unsubscribe, nothing to await
            # here (a stream tearing down never waits on Redis); the next
            # ``attach`` waits for it instead.
            stop_task.cancel()
        elif apply_now:
            # Same rule: the unsubscribe is applied by a task, and applies
            # the desired state as it stands when it runs.
            task = asyncio.create_task(self._apply(channel))
            self._apply_tasks.add(task)
            task.add_done_callback(self._apply_tasks.discard)

    async def _apply(self, channel: str) -> None:
        """Bring the wire subscription of *channel* in line with its listeners.

        Decided under ``_wire_lock`` from the CURRENT desired state, never
        from the change that scheduled it: an unsubscribe scheduled by the
        last listener's departure is a no-op once a replacement attached,
        and that replacement's own apply finds the channel still subscribed
        and announces it (no confirmation will).
        """
        async with self._wire_lock:
            pubsub = self._pubsub
            async with self._lock:
                if pubsub is None or not self._ready:
                    return  # the (re)connect path subscribes every listener channel
                wanted = channel in self._listeners
            if wanted and channel not in self._wire:
                try:
                    await asyncio.wait_for(
                        pubsub.subscribe(channel), timeout=PUBLISH_TIMEOUT_SECONDS
                    )
                except asyncio.CancelledError:
                    raise
                except Exception:
                    # A dead connection the reader notices by itself; a live
                    # one that merely failed this command would keep the
                    # channel unsubscribed for good - so ask the reader to
                    # reconnect, which re-subscribes every listener channel,
                    # this one included.
                    logger.debug(
                        "chat turn relay subscribe failed for %s; reconnecting",
                        channel,
                        exc_info=True,
                    )
                    if pubsub is self._pubsub:
                        self._resubscribe = True
                    return
                if pubsub is self._pubsub:
                    self._wire.add(channel)
            elif not wanted and channel in self._wire:
                try:
                    await asyncio.wait_for(pubsub.unsubscribe(channel), timeout=2.0)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.debug(
                        "chat turn relay unsubscribe failed for %s", channel, exc_info=True
                    )
                    return
                if pubsub is self._pubsub:
                    self._wire.discard(channel)
            elif wanted:
                # Still subscribed from before its last listener left: no
                # confirmation will announce the listeners that re-attached
                # meanwhile, so ask them to read the journal now.
                async with self._lock:
                    event = self._confirmed.get(channel)
                    if event is not None and not event.is_set():
                        event.set()
                        for queue in self._listeners.get(channel, ()):
                            _offer_to_listener(queue, RESYNC)

    async def _close_pubsub(self) -> None:
        pubsub, client = self._pubsub, self._client
        self._pubsub = None
        self._client = None
        self._wire.clear()
        from app.core.redis import close_pubsub

        await close_pubsub(pubsub, client)

    async def _connect(self) -> None:
        """Open the subscription and cover every listener channel.

        Channels attach while the connect is in flight, so the pending set
        is re-read under the lock until nothing is missing; readiness is
        declared in that same critical section, which is what guarantees a
        channel is subscribed exactly once per connection — by this loop
        when it landed before, by ``attach`` itself when it lands after.
        """
        from app.core.redis import open_pubsub

        client = await _pubsub_client()
        # No channel yet: ``_connect`` / ``_apply`` subscribe later, and a failed
        # subscribe there is closed by ``_run``'s own except / finally.
        pubsub = await open_pubsub(client)
        self._client, self._pubsub = client, pubsub
        self._wire = set()
        self._resubscribe = False
        while True:
            async with self._lock:
                pending = [ch for ch in self._listeners if ch not in self._wire]
                if not pending:
                    self._ready = True
                    # A channel whose listeners all left while the connect
                    # was in flight is subscribed for nobody: its detach
                    # could not apply anything yet, so apply it now.
                    stale = [ch for ch in self._wire if ch not in self._listeners]
                    break
                for channel in pending:
                    self._confirmed.setdefault(channel, asyncio.Event())
            await asyncio.wait_for(pubsub.subscribe(*pending), timeout=PUBLISH_TIMEOUT_SECONDS)
            self._wire.update(pending)
        for channel in stale:
            task = asyncio.create_task(self._apply(channel))
            self._apply_tasks.add(task)
            task.add_done_callback(self._apply_tasks.discard)

    def _dispatch(self, message: dict) -> None:
        mtype = _as_str(message.get("type"))
        channel = _as_str(message.get("channel"))
        if mtype == "subscribe":
            # A confirmation for a channel whose listeners already left
            # (unsubscribed by ``_apply``) must not leave a confirmation
            # event behind for a channel nobody holds.
            if channel not in self._listeners:
                return
            event = self._confirmed.setdefault(channel, asyncio.Event())
            event.set()
            for queue in self._listeners.get(channel, ()):
                _offer_to_listener(queue, RESYNC)
        elif mtype == "message":
            for queue in self._listeners.get(channel, ()):
                _offer_to_listener(queue, message.get("data"))

    async def _run(self) -> None:
        backoff = 1.0
        try:
            while self._listeners:
                try:
                    await self._connect()
                    backoff = 1.0
                    while self._listeners:
                        if self._resubscribe:
                            raise _SubscribeFailedError(
                                "a channel subscription failed on the live connection"
                            )
                        message = await self._pubsub.get_message(
                            ignore_subscribe_messages=False, timeout=1.0
                        )
                        if message:
                            self._dispatch(message)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    log = logger.debug if self._warned else logger.warning
                    self._warned = True
                    log(
                        "chat turn relay subscription lost (retry in %.0fs): %s",
                        backoff,
                        format_exception_message(exc),
                    )
                    self._ready = False
                    await self._close_pubsub()
                    for channel in self._listeners:
                        self._confirmed[channel] = asyncio.Event()
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, 30.0)
        finally:
            self._ready = False
            await self._close_pubsub()


_hubs: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, _RelayHub] = (
    weakref.WeakKeyDictionary()
)


def _hub() -> _RelayHub:
    """The relay hub of the running event loop (one per loop, one per process)."""
    loop = asyncio.get_running_loop()
    hub = _hubs.get(loop)
    if hub is None:
        hub = _RelayHub()
        _hubs[loop] = hub
    return hub


SendFrame = Callable[[dict], Awaitable[bool]]


class _TurnRelay:
    """One stream's relay state (see :func:`relay_turn_frames`)."""

    def __init__(
        self,
        conversation_id: str,
        *,
        origin: str,
        send: SendFrame,
        is_closed: Callable[[], bool],
        attached: asyncio.Event | None,
    ) -> None:
        self.conversation_id = conversation_id
        self.origin = origin
        self._send = send
        self._is_closed = is_closed
        self._attached = attached
        # Which turn's frames are forwarded (the one-turn guard).
        self.following: str | None = None
        self.replayed_turn: str | None = None
        self.replayed_seq = 0
        # The relayed turn this stream SHOWS: some of its frames were
        # forwarded and its terminal was not. The relay owes it an end.
        self.shown: str | None = None
        self._shown_checked_at = 0.0
        self._lost_since: float | None = None
        # The recent turns whose end this stream was handed (insertion order,
        # bounded): a later frame of one reopens nothing. ``False`` for a turn
        # a provisional end released: only its final end still lands.
        self._ended: dict[str, bool] = {}

    def _show(self, turn: str) -> None:
        if self.shown != turn:
            self.shown = turn
            self._lost_since = None
        self._shown_checked_at = time.monotonic()

    def _forget_shown(self) -> None:
        self.shown = None
        self._lost_since = None

    def _note_end(self, turn: str, *, final: bool = True) -> None:
        if self.shown == turn:
            self._forget_shown()
        if self.following == turn:
            self.following = None
            self.replayed_turn, self.replayed_seq = None, 0
        self._ended.pop(turn, None)
        self._ended[turn] = final
        while len(self._ended) > ENDED_TURNS_KEPT:
            self._ended.pop(next(iter(self._ended)))

    def _drops_late_frame(self, turn: str, frame: dict) -> bool:
        """A frame of a turn this stream already saw end: dropped, but for the
        final end (stored, or ``interrupted``) of a turn a provisional end
        released."""
        if turn not in self._ended:
            return False
        final_end = is_terminal_frame(frame) and not is_provisional_end(frame)
        return self._ended[turn] or not final_end

    async def _deliver_end(self, turn: str, frame: dict) -> bool:
        out = {k: v for k, v in frame.items() if k not in TRANSPORT_KEYS}
        out.setdefault("turn_id", public_turn_id(turn))
        out["relayed"] = True
        self._note_end(turn)
        return await self._send(out)

    async def _supersede(self) -> bool | None:
        """End the shown turn before another turn's frames replace it on screen.

        ``None`` - nothing ended, the caller drops the other turn's frame -
        while the shown turn may still be the conversation's turn: it holds
        the lock (the other turn is a producer that outlived its lock, an end
        recovered late), or Redis cannot say, since an unreadable probe proves
        nothing. ``False`` once the stream is gone.
        """
        turn = self.shown
        if turn is None:
            return True
        if self._is_closed():
            return False
        end = await probe_turn_end(self.conversation_id, turn)
        if self._is_closed():
            return False
        if end is None or not end.over:
            return None
        return await self._deliver_end(turn, end.terminal or lost_turn_frame())

    async def _probe_shown(self) -> bool:
        """Is the shown turn still in flight? End it once it is known over.

        Its own terminal when the tombstone kept one; otherwise
        :func:`lost_turn_frame` - at once when the tombstone says its producer
        closed it (nothing more will come), after
        :data:`TERMINAL_GRACE_SECONDS` of seeing it over when there is no
        tombstone at all: its producer may still deliver the real end (its
        recovery after a quiet period, a lock re-created by its refresher).
        """
        turn = self.shown
        self._shown_checked_at = time.monotonic()
        if turn is None:
            return True
        end = await probe_turn_end(self.conversation_id, turn)
        if end is None:
            return True
        if not end.over:
            self._lost_since = None
            return True
        if end.terminal is not None:
            return await self._deliver_end(turn, end.terminal)
        if end.ended:
            return await self._deliver_end(turn, lost_turn_frame())
        now = time.monotonic()
        if self._lost_since is None:
            self._lost_since = now
        if now - self._lost_since < TERMINAL_GRACE_SECONDS:
            return True
        return await self._deliver_end(turn, lost_turn_frame())

    async def _report_interrupted(self) -> bool:
        """Say how the conversation's last turn ended, when a restart cut it.

        A stream that attaches with no turn in flight - the one a client
        reopens once the backend is back - otherwise never hears the end of a
        turn cut by the shutdown: its interrupted terminal went out while the
        stream was down, and only a stream that showed the turn is told of its
        end. The tombstone keeps that terminal for ``TOMBSTONE_TTL_SECONDS``;
        delivered once per stream (``_deliver_end`` records the turn ended).
        """
        try:
            cut = await interrupted_turn(self.conversation_id)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.debug(
                "interrupted turn unreadable for %s", self.conversation_id, exc_info=True
            )
            return True
        if cut is None or cut.interrupted_at is None or cut.token in self._ended:
            return True
        end = await probe_turn_end(self.conversation_id, cut.token)
        if end is None or end.terminal is None or self._is_closed():
            return True
        return await self._deliver_end(cut.token, end.terminal)

    def _probe_in(self) -> float | None:
        if self.shown is None:
            return None
        return max(0.0, self._shown_checked_at + LIVENESS_CHECK_SECONDS - time.monotonic())

    async def run(self, queue: asyncio.Queue) -> None:
        while not self._is_closed():
            timeout = self._probe_in()
            try:
                if timeout is None:
                    item = await queue.get()
                else:
                    item = await asyncio.wait_for(queue.get(), timeout=timeout)
            except TimeoutError:
                if not await self._probe_shown():
                    return
                continue
            if item is RESYNC:
                resynced = await self._resync()
                if self._attached is not None:
                    self._attached.set()
                if not resynced:
                    return
                continue
            for frame in decode_batch(item):
                if not await self._relay(frame):
                    return

    async def _resync(self) -> bool:
        # Whatever was followed before the resync is over or unknown: the
        # snapshot (re)establishes it, or nothing does.
        self.following = None
        self.replayed_turn, self.replayed_seq = None, 0
        try:
            snapshot = await read_snapshot(self.conversation_id)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.debug(
                "chat turn resync failed for %s", self.conversation_id, exc_info=True
            )
            return True  # a shown turn stays owed: the liveness probe ends it
        if self.shown is not None and (snapshot is None or snapshot.token != self.shown):
            if snapshot is not None:
                # Another turn is in flight: the shown one is over, and ends
                # before the resume below replaces it on screen.
                superseded = await self._supersede()
                if superseded is None:
                    # The shown turn holds the lock again (its refresher
                    # re-created a lapsed marker), or Redis cannot say:
                    # nothing else to resume into.
                    return True
                if not superseded:
                    return False
            elif not await self._probe_shown():
                return False
        if snapshot is None:
            return await self._report_interrupted()
        if snapshot.token in self._ended:
            # A turn this stream already saw end (its journal is dropped just
            # after its terminal) is not reopened.
            return True
        self.following = snapshot.token
        self.replayed_turn, self.replayed_seq = snapshot.token, snapshot.seq
        turn_id = public_turn_id(snapshot.token)
        if snapshot.origin == self.origin:
            # The client's own turn, which its send stream carries: a client
            # re-attaching mid-turn (a network blip, the events stream's
            # lifetime) is not handed it a second time. Followed all the same,
            # so its terminal - skipped as this origin's - ends the following.
            # Its ``turn_resuming`` frame is the exception: another producer
            # publishes it, never the send stream, so a stream that attached
            # after it went out is told it here, or the line saying the turn
            # resumed by itself never shows on the client that runs it.
            if snapshot.resuming:
                return await self._send({**snapshot.resuming, "turn_id": turn_id, "relayed": True})
            return True
        for frame in resume_frames(snapshot):
            if not await self._send({**frame, "turn_id": turn_id, "relayed": True}):
                return False
        self._show(snapshot.token)
        return True

    async def _relay(self, frame: dict) -> bool:
        turn = frame.get("turn")
        seq = frame.get("seq")
        # The followed turn's own terminal ends it for this relay WHETHER OR
        # NOT the frame is forwarded: a resync that read the snapshot of a
        # turn this relay's origin runs binds ``following`` to it, and the
        # origin check below skips its frames -
        # kept bound past that turn's end, the guard would drop the terminal
        # of a later turn whose opening frames a quiet period swallowed.
        terminal = is_terminal_frame(frame)
        ends_followed = self.following is not None and turn == self.following and terminal
        try:
            if frame.get("origin") == self.origin:
                return True
            if isinstance(turn, str) and self._drops_late_frame(turn, frame):
                # A turn this stream already saw end: a late frame of it (a
                # second copy of its end, a producer that outlived its lock)
                # reopens nothing and closes nothing.
                return True
            followed = self.following
            if isinstance(turn, str) and turn != self.following:
                current = self.following or self.shown
                if (
                    terminal
                    and current is not None
                    and current != turn
                    and await _lock_token(self.conversation_id) != turn
                ):
                    # A lone terminal of another turn: the late end of a turn
                    # that is over (a producer that did not honour the order,
                    # an end recovered after a quiet period) - unless that
                    # turn holds the lock: it is then the newest turn, whose
                    # opening frames were lost.
                    return True
                self.following = turn
            if (
                self.replayed_turn is not None
                and turn == self.replayed_turn
                and isinstance(seq, int)
                and seq <= self.replayed_seq
                # A replay never carries a terminal, even one whose sequence
                # number the snapshot already counted.
                and not terminal
            ):
                return True
            has_turn = isinstance(turn, str)
            if has_turn and self.shown is not None and turn != self.shown:
                superseded = await self._supersede()
                if superseded is None:
                    # The shown turn still holds the lock, or Redis cannot
                    # say: this frame is dropped, and the shown turn stays on
                    # screen until it is known over.
                    self.following = followed
                    return True
                if not superseded:
                    return False
            out = {k: v for k, v in frame.items() if k not in TRANSPORT_KEYS}
            if isinstance(turn, str):
                out.setdefault("turn_id", public_turn_id(turn))
            out["relayed"] = True
            if not await self._send(out):
                return False
            if has_turn:
                if not terminal:
                    self._show(turn)
                else:
                    # A provisional end (a Stop acknowledgement, a failed
                    # loop's ``error``) releases the client, and what the turn
                    # still sends before its final end reopens nothing; the
                    # final end - the stored one, carrying the ``message_id``
                    # that reads the stored thread back, or ``interrupted`` -
                    # still lands.
                    self._note_end(turn, final=not is_provisional_end(frame))
            return True
        finally:
            if ends_followed:
                self.following = None
                self.replayed_turn, self.replayed_seq = None, 0


async def relay_turn_frames(
    conversation_id: str,
    *,
    origin: str,
    send: SendFrame,
    is_closed: Callable[[], bool] = lambda: False,
    attached: asyncio.Event | None = None,
) -> None:
    """Feed a stream every turn frame of the conversation it did not produce.

    Runs for the lifetime of the stream (cancel it at teardown). On every
    (re)attachment it resumes the stream into the turn in flight, then
    relays live frames, skipping frames this stream originated and frames
    the replayed snapshot already covered. ``attached`` is set each time a
    (re)attachment's resume is handled: a turn over by then never reaches
    the relay live.
    ``send`` returns ``False`` once the stream is dead, which ends the relay.
    Every relayed frame is flagged ``relayed`` and a frame of a turn names it
    (``turn_id``, derived from the transport stamp for a producer that
    predates the field).

    Every turn the relay SHOWS (it forwarded a frame of it, or resumed the
    stream into it) is owed an end: nothing else releases a client
    following it. The end is
    the turn's own terminal when it arrives live; when a resync finds the
    turn over, the one its tombstone kept; when the turn goes quiet for
    :data:`LIVENESS_CHECK_SECONDS`, the relay probes it
    (:func:`probe_turn_end`) and, once it is known over with no terminal to
    recover, closes it with :func:`lost_turn_frame` after
    :data:`TERMINAL_GRACE_SECONDS`; when another turn's frames arrive, it is
    ended first so a client never holds two turns open - unless it still
    holds the lock, which makes the other turn's frame the stale one (a
    producer that outlived its lock) and drops it, as does a probe Redis
    could not answer. A copy of an end already
    delivered is dropped. A provisional end (:func:`is_provisional_end`: a
    Stop acknowledgement, the ``error`` of a web chat turn whose loop
    raised) releases the client too, and what the turn sends before its
    final end reopens nothing; that end (the stored one, carrying
    ``message_id``, or ``interrupted``) still lands.

    The relay follows ONE turn at a time: the marker holder at the last
    resync, or the turn whose frames it saw since. A TERMINAL frame stamped
    with another turn while one is being followed (or shown) is the late
    terminal of a turn that is over and is dropped - forwarded, it would
    close the newer turn on this stream - unless its turn holds the lock:
    every producer publishes its terminal BEFORE it releases the lock (the
    REST path too, via ``_rest_end_turn``), so that turn is the newest one,
    whose opening frames a publish quiet period swallowed, and it replaces
    the followed turn. Any other frame of another turn moves the relay onto
    that turn (its opening ack, or its first frame after a quiet period that
    swallowed the ack).

    ``following`` is a turn IN FLIGHT as far as this relay knows, never a
    turn it saw end. A resync that finds NO turn in flight (no marker, or
    the ``ended`` tombstone of a turn that already published its terminal)
    follows nothing from then on: the turn the stream followed before is
    over, and a terminal queued right behind the resync can only belong to
    a turn that started AND ended between the subscription confirmation and
    the snapshot read — the newer answer this stream must still deliver,
    not a stale one to drop. A resync whose snapshot could not be read
    follows nothing either, and so does the relay once it forwarded the
    followed turn's own terminal: the next live frame names the turn it
    belongs to — which is how the ``done`` of a turn whose opening frames a
    publish quiet period swallowed still reaches the stream.
    """
    hub = _hub()
    queue = await hub.attach(conversation_id)
    relay = _TurnRelay(
        conversation_id,
        origin=origin,
        send=send,
        is_closed=is_closed,
        attached=attached,
    )
    try:
        await relay.run(queue)
    finally:
        await hub.detach(conversation_id, queue)
