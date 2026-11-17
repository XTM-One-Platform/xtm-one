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

"""One reply per inbound chat message, at the send.

``chat_turn_coalescing`` keeps the messages of one conversation in one turn
and runs the turns of a conversation one at a time. This is the last line
behind it, on the send itself: an assignment run started by a chat message
(a conversation event, its payload carrying ``conversation_key``) is bound to
the inbound message ids it answers (``bind_turn``); when the run's agent
replies in that same conversation, the channel's send tool claims those ids
(``claim``) before posting. An id another run of the same assignment already
answered stays with that run; when every inbound id of the turn is already
answered by another run, the reply is refused (``Claim.refusal``), logged with
the inbound ids and counted per channel
(``xtmone_chat_duplicate_replies_refused_total``). Whatever let a second run
start on an answered message (a notification and the poll behind it, a
replayed delivery), the sender reads one answer.

What is never refused:

- a second post of the same run (a "looking into it" then the answer, a
  follow-up the agent chose): the claim names the run that holds it;
- a reply that answers at least one inbound id no run answered yet (a turn
  that merged an answered message with a new one);
- a send outside a conversation event's run, or into another conversation
  than the one the run answers: nothing is bound, nothing is claimed;
- a send while the store cannot be reached: fail-open, logged.

A claim is taken for ``PENDING_TTL_SECONDS`` before the post and made
durable (``CLAIM_TTL_SECONDS``) once the post succeeded (``Claim.confirm``);
a failed send gives back the ids it claimed (``Claim.release``), and a worker
that dies mid-post leaves a claim that expires within minutes, so the next
attempt (this run's resume or another run) can still answer them. Pure rules plus one Redis
key per (assignment, inbound id); the channel's send tool names the channel
and the conversation key its payload builder stamps.
"""

from __future__ import annotations

import json
import logging
import time
from contextvars import ContextVar
from dataclasses import dataclass, field

from app.core.metrics import get_or_create_counter
from app.core.redis import redis_client
from app.core.utils import format_exception_message
from app.services.assignment_dispatch import CONVERSATION_KEY_FIELD

logger = logging.getLogger(__name__)

#: How long an answered inbound id stays claimed: far past the coalescing
#: window, the delivery queue and a resumed run of the same turn.
CLAIM_TTL_SECONDS = 86400
#: How long a claim taken before the post holds when the post never reports back.
PENDING_TTL_SECONDS = 120
#: Inbound ids one turn claims at most (``chat_turn_coalescing.MAX_MERGED_MESSAGES``).
MAX_INBOUND_IDS = 20

_KEY_PREFIX = "chat_reply_guard"

duplicate_replies_refused = get_or_create_counter(
    "xtmone_chat_duplicate_replies_refused_total",
    "Chat replies refused at the send because every inbound message they answer was already answered by another run",
    ["channel"],
)


@dataclass(frozen=True)
class Turn:
    """The conversation an assignment run answers and the inbound ids of its turn."""

    scope: str
    run_id: str
    conversation_key: str
    inbound_ids: tuple[str, ...]


_turn: ContextVar[Turn | None] = ContextVar("chat_reply_guard_turn", default=None)


def turn_of(payload: object, *, scope: object, run_id: object) -> Turn | None:
    """The turn a run's trigger payload carries, or ``None`` when it is not a conversation event."""
    if not isinstance(payload, dict) or not scope or not run_id:
        return None
    conversation = str(payload.get(CONVERSATION_KEY_FIELD) or "").strip()
    if not conversation:
        return None
    raw = payload.get("message_ids")
    ids: list[str] = []
    if isinstance(raw, list):
        ids = [str(i) for i in raw if i]
    if not ids and payload.get("message_id"):
        ids = [str(payload["message_id"])]
    ids = list(dict.fromkeys(ids))[:MAX_INBOUND_IDS]
    if not ids:
        return None
    return Turn(scope=str(scope), run_id=str(run_id), conversation_key=conversation, inbound_ids=tuple(ids))


def bind_turn(payload: object, *, scope: object = None, run_id: object = None):
    """Bind the turn the executing run answers (``None`` unbinds); returns the token to reset."""
    return _turn.set(turn_of(payload, scope=scope, run_id=run_id))


def reset_turn(token) -> None:
    _turn.reset(token)


def current_turn() -> Turn | None:
    """The turn the run executing in this context answers, if any."""
    return _turn.get()


def extend_turn(inbound_ids: list[str]) -> Turn | None:
    """Add the inbound ids a mid-run steer brought in to the executing turn (slice 3).

    Bound in the current context, so the tool calls the loop starts after the
    steer claim them with the turn's own; ``None`` when no turn is bound.
    """
    turn = _turn.get()
    if turn is None:
        return None
    ids = list(dict.fromkeys([*turn.inbound_ids, *(str(i) for i in inbound_ids if i)]))[:MAX_INBOUND_IDS * 2]
    extended = Turn(scope=turn.scope, run_id=turn.run_id, conversation_key=turn.conversation_key, inbound_ids=tuple(ids))
    _turn.set(extended)
    return extended


def claim_key(scope: str, inbound_id: str) -> str:
    return f"{_KEY_PREFIX}:answered:{scope}:{inbound_id}"


@dataclass
class Claim:
    """The outcome of one claim: what this send took, and who answered the rest."""

    channel: str
    turn: Turn | None = None
    claimed: list[str] = field(default_factory=list)
    #: Inbound id -> the record of the run that already answered it.
    answered_elsewhere: dict[str, dict] = field(default_factory=dict)

    @property
    def refused(self) -> bool:
        """Every inbound id of the turn is answered by another run: this reply would be a second answer."""
        return (
            self.turn is not None
            and not self.claimed
            and bool(self.answered_elsewhere)
            and len(self.answered_elsewhere) == len(self.turn.inbound_ids)
        )

    def refusal(self, *, edit_tool: str | None = None) -> str:
        """The tool result of a refused reply: who answered, and what to do instead."""
        earlier = next(iter(self.answered_elsewhere.values()), {})
        ids = ", ".join(self.answered_elsewhere)
        when = earlier.get("at")
        when_text = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(when)) if isinstance(when, (int, float)) else "earlier"
        correction = (
            f"if the earlier answer needs a correction, edit it with {edit_tool}."
            if edit_tool
            else "the earlier answer stands."
        )
        return (
            f"Reply refused: the message(s) this run answers ({ids}) were already answered in this "
            f"conversation by another run ({earlier.get('run') or 'unknown'}, {when_text}). "
            f"A second reply would make the sender read the same answer twice. Do not send it again; {correction}"
        )

    async def confirm(self) -> None:
        """Keep the ids this send claimed for ``CLAIM_TTL_SECONDS`` (the post succeeded). Never raises."""
        if not self.claimed or self.turn is None:
            return
        try:
            redis = await redis_client()
            for inbound_id in self.claimed:
                await redis.expire(claim_key(self.turn.scope, inbound_id), CLAIM_TTL_SECONDS)
        except Exception as exc:
            logger.warning("Chat reply guard: could not keep a claim (a duplicate may follow): %s", format_exception_message(exc))

    async def release(self) -> None:
        """Give back the ids this send claimed (the send failed). Never raises."""
        if not self.claimed or self.turn is None:
            return
        keys = [claim_key(self.turn.scope, i) for i in self.claimed]
        self.claimed = []
        try:
            redis = await redis_client()
            await redis.delete(*keys)
        except Exception as exc:
            logger.debug("Chat reply guard: could not release a claim (it expires): %s", format_exception_message(exc))


def _record(turn: Turn, now: float) -> str:
    return json.dumps({"run": turn.run_id, "conversation": turn.conversation_key, "at": now})


def _holder(raw: object) -> dict:
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", "replace")
    try:
        data = json.loads(raw) if isinstance(raw, str) else None
    except ValueError:
        data = None
    return data if isinstance(data, dict) else {}


async def claim(channel: str, conversation_key: str, *, now: float | None = None) -> Claim:
    """Claim the inbound ids of the executing run's turn before replying in *conversation_key*.

    A send into another conversation than the turn's, or outside a
    conversation event's run, claims nothing and is never refused. A refused
    claim is logged and counted; the caller returns ``Claim.refusal``.
    """
    turn = current_turn()
    result = Claim(channel=channel)
    if turn is None or not conversation_key or turn.conversation_key != conversation_key:
        return result
    result.turn = turn
    stamp = time.time() if now is None else now
    try:
        redis = await redis_client()
        for inbound_id in turn.inbound_ids:
            key = claim_key(turn.scope, inbound_id)
            if await redis.set(key, _record(turn, stamp), nx=True, ex=PENDING_TTL_SECONDS):
                result.claimed.append(inbound_id)
                continue
            holder = _holder(await redis.get(key))
            if holder.get("run") and holder.get("run") != turn.run_id:
                result.answered_elsewhere[inbound_id] = holder
    except Exception as exc:
        logger.warning("Chat reply guard: could not claim the turn's inbound messages (replying unguarded): %s", format_exception_message(exc))
        await result.release()
        return Claim(channel=channel)
    if result.refused:
        duplicate_replies_refused.labels(channel=channel).inc()
        logger.warning(
            "Chat reply guard: refused a second reply on %s in %s: inbound %s already answered by run %s (this run %s)",
            channel,
            conversation_key,
            ",".join(result.answered_elsewhere),
            ",".join(sorted({str(h.get("run")) for h in result.answered_elsewhere.values()})),
            turn.run_id,
        )
    return result


def answered_note(result: Claim) -> str:
    """The line a successful reply's tool result adds: which inbound messages it answered."""
    if not result.claimed:
        return ""
    return f" Answers inbound message(s): {', '.join(result.claimed)}."
