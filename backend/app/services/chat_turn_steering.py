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

"""A chat message arriving mid-run steers the running turn.

``chat_turn_coalescing`` runs the turns of a conversation one at a time: a
message that arrives while the run of its conversation is already in its agent
loop used to wait in the delivery queue and start a second turn once the first
had answered, so the sender read an answer that ignored what they had just
added, then a second answer. This module folds such a message into the
running turn instead, at the loop's next iteration boundary, as a user steer
(the same ``steering_queue`` the interactive chat uses).

- **live**: a run started by a conversation event (``chat_reply_guard.Turn``)
  opens a steering session around its agent loop (``session``): a marker per
  (assignment, conversation) names the run and its sender, refreshed while the
  loop runs.
- **offer**: the assignment engine offers a later message of the same
  conversation and the same sender to the live run (``offer``) before queuing
  it; the message is pushed to the session's inbox and the delivery answers
  ``steered``. Another sender's message, a conversation with a waiting turn,
  or an assignment whose ``trigger_config.steer_mid_run`` is ``false`` queues
  as before.
- **inject**: a bridge moves the inbox into the loop's steering queue; what
  the loop takes extends the run's reply-guard turn (``extend_turn``), so the
  one reply answers the steered ids too.
- **leftover**: a message the loop never took (it arrived during the final
  answer, or the run failed) is handed back to the engine when the session
  closes (``on_leftover``) and queues as the conversation's next turn. Never
  lost, never answered twice.

A store that cannot be reached never steers: the message queues as before.
The push-then-check of ``offer`` and the delete-then-pop of the close make the
hand-off race-free without a script: an entry is either taken back by its
offer, or popped by the closing session and handed back.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from app.core.redis import redis_client
from app.core.utils import format_exception_message
from app.services import chat_reply_guard
from app.services.chat_turn_coalescing import messages_of, sender_of
from app.tools.agent_loop import SteeringMessage, SteeringQueue

logger = logging.getLogger(__name__)

#: ``trigger_config`` key that turns mid-run steering off for an assignment.
STEER_CONFIG = "steer_mid_run"
#: How long a live marker holds without a refresh (a worker that died mid-run).
LIVE_TTL_SECONDS = 90
#: How long an inbox entry waits for the bridge at most.
INBOX_TTL_SECONDS = 600
#: How often the bridge moves the inbox into the loop and refreshes the marker.
POLL_SECONDS = 1.0
#: Messages one turn takes in by steering at most; the next ones queue.
MAX_STEERED_MESSAGES = 20

_KEY_PREFIX = "chat_turn_steering"


def live_key(scope: str, conversation_key: str) -> str:
    return f"{_KEY_PREFIX}:live:{scope}:{conversation_key}"


def inbox_key(scope: str, conversation_key: str) -> str:
    return f"{_KEY_PREFIX}:inbox:{scope}:{conversation_key}"


def enabled_for(trigger_config: dict | None) -> bool:
    """Whether an assignment lets a mid-run message steer its running turn (default on)."""
    return not (isinstance(trigger_config, dict) and trigger_config.get(STEER_CONFIG) is False)


def steer_text(message: dict) -> str:
    """The user steer the loop injects for one inbound message."""
    name = str(message.get("from_name") or "the sender").strip() or "the sender"
    text = str(message.get("text") or "").strip()
    return (
        f"[New message from {name} in this conversation, received while you were working on this turn]\n"
        f"{text}\n\n"
        "Take it into account and answer the conversation once, in a single reply."
    )


def _decode(raw: object) -> dict:
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", "replace")
    try:
        data = json.loads(raw) if isinstance(raw, str) else None
    except ValueError:
        data = None
    return data if isinstance(data, dict) else {}


async def offer(
    scope: object,
    conversation_key: str,
    payload: dict | None,
    trigger_context: dict | None,
) -> str | None:
    """Push *payload* to the live run of its conversation; that run's id, or ``None``.

    ``None`` means nothing was steered (no live run, another sender, a full
    turn, the store unreachable) and the caller queues the message as before.
    """
    if not scope or not conversation_key or not isinstance(payload, dict):
        return None
    sender = sender_of(payload)
    if not sender:
        return None
    scope = str(scope)
    try:
        redis = await redis_client()
        live = _decode(await redis.get(live_key(scope, conversation_key)))
        if not live.get("run") or live.get("sender") != sender:
            return None
        entry = json.dumps(
            {
                "id": uuid.uuid4().hex,
                "run": live["run"],
                "payload": payload,
                "trigger_context": trigger_context or {},
            },
            default=str,
        )
        inbox = inbox_key(scope, conversation_key)
        await redis.rpush(inbox, entry)
        await redis.expire(inbox, INBOX_TTL_SECONDS)
        if await redis.get(live_key(scope, conversation_key)):
            return str(live["run"])
        # The run closed its session between the read and the push: take the
        # entry back, unless its close already popped it (then it hands it back).
        if await redis.lrem(inbox, 1, entry):
            return None
        return str(live["run"])
    except Exception as exc:
        logger.warning(
            "Chat turn steering: could not offer a message to the running turn of %s (queuing it): %s",
            conversation_key,
            format_exception_message(exc),
        )
        return None


class ChatTurnSteeringQueue(SteeringQueue):
    """The steering queue of a conversation event's run.

    Keeps the inbox entry behind each item it holds, so what the loop takes
    extends the reply-guard turn and what it never takes is handed back.
    """

    def __init__(self, known_ids: tuple[str, ...] = ()) -> None:
        super().__init__()
        self._pending: list[tuple[SteeringMessage, dict]] = []
        self._seen: set[str] = set(known_ids)
        self.steered_ids: list[str] = []

    def put_entry(self, entry: dict) -> bool:
        """Queue the messages of *entry* the turn does not carry yet (a known one is skipped).

        ``True`` when the entry is taken in, whether it had a new message or not.
        """
        payload = entry.get("payload") if isinstance(entry.get("payload"), dict) else {}
        # The same message delivered twice (a notification and the poll behind
        # it) is taken in once: the turn already carries it.
        messages = [m for m in messages_of(payload) if str(m.get("message_id") or "") not in self._seen]
        if not messages:
            return True
        self._seen.update(str(m["message_id"]) for m in messages if m.get("message_id"))
        item = SteeringMessage(text="\n\n".join(steer_text(m) for m in messages))
        self._pending.append((item, entry))
        self.put_nowait(item)
        return True

    async def admit(self, items: list) -> list:
        taken = [(item, entry) for item, entry in self._pending if any(item is left for left in items)]
        self._pending = [(item, entry) for item, entry in self._pending if all(item is not left for left in items)]
        ids: list[str] = []
        for _, entry in taken:
            for message in messages_of(entry.get("payload")):
                if message.get("message_id"):
                    ids.append(str(message["message_id"]))
        if ids:
            self.steered_ids.extend(ids)
            chat_reply_guard.extend_turn(ids)
        return items

    def leftover(self) -> list[dict]:
        """The entries the loop never took, in arrival order; empties the queue."""
        while True:
            try:
                self.get_nowait()
            except asyncio.QueueEmpty:
                break
        entries = [entry for _, entry in self._pending]
        self._pending = []
        return entries


@dataclass
class _Session:
    scope: str
    conversation_key: str
    run_id: str
    sender: str
    queue: ChatTurnSteeringQueue = field(default_factory=ChatTurnSteeringQueue)
    taken: int = 0
    overflow: list[dict] = field(default_factory=list)

    @property
    def marker(self) -> str:
        return json.dumps({"run": self.run_id, "sender": self.sender})

    def accept(self, raw: object) -> None:
        entry = _decode(raw)
        if not entry:
            return
        if self.taken >= MAX_STEERED_MESSAGES:
            self.overflow.append(entry)
            return
        if self.queue.put_entry(entry):
            self.taken += 1

    async def pump(self) -> None:
        redis = await redis_client()
        await redis.set(live_key(self.scope, self.conversation_key), self.marker, ex=LIVE_TTL_SECONDS)
        inbox = inbox_key(self.scope, self.conversation_key)
        while True:
            raw = await redis.lpop(inbox)
            if raw is None:
                return
            self.accept(raw)

    async def close(self) -> list[dict]:
        """Delete the marker, then pop what the inbox still holds; the entries to hand back."""
        try:
            redis = await redis_client()
            await redis.delete(live_key(self.scope, self.conversation_key))
            inbox = inbox_key(self.scope, self.conversation_key)
            while True:
                raw = await redis.lpop(inbox)
                if raw is None:
                    break
                self.accept(raw)
        except Exception as exc:
            logger.warning(
                "Chat turn steering: could not close the session of run %s (its marker expires): %s",
                self.run_id,
                format_exception_message(exc),
            )
        return [*self.queue.leftover(), *self.overflow]


def _session_for(turn: chat_reply_guard.Turn | None, payload: object) -> _Session | None:
    if turn is None or not isinstance(payload, dict):
        return None
    sender = sender_of(payload)
    if not sender:
        return None
    return _Session(
        scope=turn.scope,
        conversation_key=turn.conversation_key,
        run_id=turn.run_id,
        sender=sender,
        queue=ChatTurnSteeringQueue(turn.inbound_ids),
    )


@contextlib.asynccontextmanager
async def session(
    payload: object,
    trigger_config: dict | None,
    on_leftover: Callable[[list[dict]], Awaitable[Any]],
    *,
    turn: chat_reply_guard.Turn | None = None,
) -> AsyncIterator[ChatTurnSteeringQueue | None]:
    """Open the steering session of the executing conversation event's run.

    Yields the loop's steering queue, or ``None`` when the run is not a
    conversation event, steering is off for the assignment, or the store is
    unreachable. On close, the messages the loop never took go to
    *on_leftover* (never raises).
    """
    current = turn if turn is not None else chat_reply_guard.current_turn()
    steering = _session_for(current, payload) if enabled_for(trigger_config) else None
    if steering is not None:
        try:
            await steering.pump()
        except Exception as exc:
            logger.warning(
                "Chat turn steering: could not open the session of run %s (no mid-run steering): %s",
                steering.run_id,
                format_exception_message(exc),
            )
            steering = None
    if steering is None:
        yield None
        return

    async def _bridge() -> None:
        while True:
            await asyncio.sleep(POLL_SECONDS)
            try:
                await steering.pump()
            except Exception as exc:
                logger.debug("Chat turn steering: inbox poll failed (retrying): %s", format_exception_message(exc))

    bridge = asyncio.create_task(_bridge())
    try:
        yield steering.queue
    finally:
        bridge.cancel()
        with contextlib.suppress(BaseException):
            await bridge
        leftover = await steering.close()
        if steering.queue.steered_ids:
            logger.info(
                "Chat turn steering: run %s took %d message(s) in mid-run (%s)",
                steering.run_id,
                len(steering.queue.steered_ids),
                ",".join(steering.queue.steered_ids),
            )
        if leftover:
            handing = asyncio.ensure_future(on_leftover(leftover))
            try:
                await asyncio.shield(handing)
            except BaseException as exc:
                logger.warning(
                    "Chat turn steering: %d message(s) run %s never took in may be delayed: %s",
                    len(leftover),
                    steering.run_id,
                    format_exception_message(exc) if isinstance(exc, Exception) else type(exc).__name__,
                )
