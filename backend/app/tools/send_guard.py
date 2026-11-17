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

"""Duplicate-send guard: the same message to the same recipients, twice.

A model that loses track of a send it already made (a resumed turn, a retry
after a slow answer, a second run of the same agent minutes later) posts the
same Teams message or mails the same people again. The loop screens every
call of a tool that declares ``ToolDefinition.send_guard``
(:class:`~app.tools.base.SendGuard`) before dispatch: when the call's
recipient set and body hash match a send that SUCCEEDED in this run, or
one the same agent made in the last :data:`WINDOW_SECONDS`, the call is
refused with the earlier call's reference. A call carrying
``allow_duplicate: true`` goes through (the flag is taken off before the
tool runs); a refusal and an override each leave an audit entry.

What is never refused:

- a send to other recipients, or another body (any byte of difference);
- a send whose earlier twin failed (only a successful send is recorded);
- a send while the store cannot be reached: fail-open, logged.

The module names no tool: each integration declares on its own send
definition which arguments name the recipients and which carry the body.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from collections.abc import Callable
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any

from app.core.utils import format_exception_message
from app.tools.base import SendGuard, ToolCall

logger = logging.getLogger(__name__)

#: How long a successful send is remembered for the same agent across runs.
WINDOW_SECONDS = 600
#: The argument that lets a deliberate repeat through.
ALLOW_DUPLICATE = "allow_duplicate"

_KEY_PREFIX = "send_guard"

#: The agent whose loop is running in this context: what the cross-run
#: window is keyed on. Unbound (``None``), only the run's own sends count.
_sender: ContextVar[str | None] = ContextVar("send_guard_sender", default=None)

#: The declared send guard of a tool, by the name the executor runs.
SendGuardLookup = Callable[[str], SendGuard | None]


def bind_sender(agent_id: object | None):
    """Bind the agent whose run is executing; returns the token to reset."""
    return _sender.set(str(agent_id) if agent_id else None)


def reset_sender(token) -> None:
    _sender.reset(token)


def current_sender() -> str | None:
    return _sender.get()


@dataclass(frozen=True)
class SendFingerprint:
    """What makes two sends the same: the tool, the recipients, the body."""

    tool: str
    recipients: tuple[str, ...]
    body_hash: str

    @property
    def key(self) -> str:
        raw = json.dumps([self.tool, list(self.recipients), self.body_hash], separators=(",", ":"))
        return hashlib.sha256(raw.encode()).hexdigest()


@dataclass(frozen=True)
class EarlierSend:
    """The send a duplicate matches: its call id and when it was made."""

    call_id: str
    at: float
    same_run: bool


def _values(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        out: list[str] = []
        for item in value:
            out.extend(_values(item))
        return out
    if isinstance(value, dict):
        return _values(value.get("address") or value.get("email") or value.get("id"))
    text = str(value)
    return [part.strip().lower() for part in text.replace(";", ",").split(",") if part.strip()]


def fingerprint(call: ToolCall, spec: SendGuard, *, tool: str | None = None) -> SendFingerprint | None:
    """The call's fingerprint, or ``None`` when it names no recipient or no body."""
    args = call.arguments if isinstance(call.arguments, dict) else {}
    recipients = sorted({v for name in spec.recipients for v in _values(args.get(name))})
    body_parts = [str(args.get(name)) for name in spec.body if args.get(name) not in (None, "")]
    if not recipients or not body_parts:
        return None
    body_hash = hashlib.sha256("\x1f".join(body_parts).encode()).hexdigest()
    return SendFingerprint(tool=tool or call.name, recipients=tuple(recipients), body_hash=body_hash)


def wants_duplicate(call: ToolCall) -> bool:
    args = call.arguments if isinstance(call.arguments, dict) else {}
    value = args.get(ALLOW_DUPLICATE)
    return value is True or (isinstance(value, str) and value.strip().lower() == "true")


def strip_flag(call: ToolCall) -> ToolCall:
    """The call without ``allow_duplicate``: the tool never sees the flag."""
    args = call.arguments if isinstance(call.arguments, dict) else {}
    if ALLOW_DUPLICATE not in args:
        return call
    return ToolCall(id=call.id, name=call.name, arguments={k: v for k, v in args.items() if k != ALLOW_DUPLICATE})


class SendLedger:
    """The successful sends of one run, and the agent's recent ones in Redis."""

    def __init__(self) -> None:
        self._run: dict[str, EarlierSend] = {}

    async def earlier(self, fp: SendFingerprint) -> EarlierSend | None:
        hit = self._run.get(fp.key)
        if hit is not None:
            return hit
        sender = current_sender()
        if not sender:
            return None
        try:
            from app.core.redis import redis_client

            redis = await redis_client()
            raw = await redis.get(_redis_key(sender, fp))
        except Exception as exc:
            logger.warning("Send guard: store unreachable, not checked (%s)", format_exception_message(exc))
            return None
        if not raw:
            return None
        try:
            data = json.loads(raw)
            return EarlierSend(call_id=str(data["call_id"]), at=float(data["at"]), same_run=False)
        except (ValueError, KeyError, TypeError):
            return None

    async def record(self, fp: SendFingerprint, call_id: str) -> None:
        now = time.time()
        self._run[fp.key] = EarlierSend(call_id=call_id, at=now, same_run=True)
        sender = current_sender()
        if not sender:
            return
        try:
            from app.core.redis import redis_client

            redis = await redis_client()
            await redis.set(
                _redis_key(sender, fp),
                json.dumps({"call_id": call_id, "at": now}),
                ex=WINDOW_SECONDS,
            )
        except Exception as exc:
            logger.warning("Send guard: could not record send %s (%s)", call_id, format_exception_message(exc))


def _redis_key(sender: str, fp: SendFingerprint) -> str:
    return f"{_KEY_PREFIX}:{sender}:{fp.key}"


def format_refusal(earlier: EarlierSend, *, now: float | None = None) -> str:
    """The tool result of a refused duplicate: one paragraph, naming the earlier call."""
    when = "earlier in this run" if earlier.same_run else (
        f"{max(0, int((now or time.time()) - earlier.at))} s ago by this agent in another run"
    )
    return (
        f"[Duplicate send] Not sent: the same message to the same recipients was already sent {when} "
        f"(call {earlier.call_id}). Do not send it again. If a second identical send is really "
        f"intended, repeat the call with \"{ALLOW_DUPLICATE}\": true."
    )


async def audit(action: str, *, tool: str, fp: SendFingerprint, call_id: str, earlier: EarlierSend | None) -> None:
    """An audit row for a refused duplicate or a deliberate repeat; never raises."""
    from app.llm.provider_access import acting_user_id

    user_id = acting_user_id()
    summary = (
        f"Refused a duplicate {tool} (same recipients and body as call {earlier.call_id if earlier else '?'})"
        if action == "duplicate_send_refused"
        else f"Sent a deliberate duplicate {tool} (allow_duplicate)"
    )
    details = {
        "tool": tool,
        "call_id": call_id,
        "earlier_call_id": earlier.call_id if earlier else None,
        "recipients": len(fp.recipients),
        "body_hash": fp.body_hash[:16],
        "agent_id": current_sender(),
    }
    if not user_id:
        logger.info("Send guard %s (no user to attribute it to): %s", action, summary)
        return
    try:
        import uuid

        from app.core.database import async_session
        from app.services.audit import log_system_audit

        async with async_session() as db:
            await log_system_audit(
                db,
                actor="send_guard",
                anchor_user_id=uuid.UUID(str(user_id)),
                action=action,
                entity_type="agent",
                entity_id=current_sender(),
                summary=summary,
                details=details,
            )
            await db.commit()
    except Exception as exc:
        logger.warning("Send guard: could not audit %s (%s)", action, format_exception_message(exc))
