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

"""One-time tokens behind the Approve / Reject buttons of a phone notification.

A notification for an unattended run waiting on tool approvals carries two
buttons. Pressing one sends its token from the phone's service worker to
``POST /mobile/push-actions`` (``api/v1/mobile_devices.py``), which has no
session: the token IS the credential, so it is built to be worth as little as
possible to anyone who gets hold of one.

* 32 random bytes, kept in Redis only as their SHA-256, never the token;
* bound to one run, one decision, one person, one phone, and the exact set of
  tool calls pending when it was minted: if the pause changed, it is refused;
* single use (``GETDEL``), expiring after :data:`TOKEN_TTL_SECONDS`;
* every token of a run is voided as soon as a decision lands, from anywhere
  (``void_run_tokens``), and every token of a phone when it is unlinked;
* Redis unavailable means refused: a token that cannot be checked is not
  honoured (the notification still opens the app, where the person decides).

The decision itself goes through the same route function as the web app's
(``respond_to_human_input``): locking, access checks and auditing are its.
"""

from __future__ import annotations

import json
import logging
import secrets
import uuid
from dataclasses import dataclass
from hashlib import sha256

from app.core.utils import format_exception_message

logger = logging.getLogger(__name__)

#: How long a notification's buttons work.
TOKEN_TTL_SECONDS = 15 * 60

_TOKEN_KEY = "push-action:{}"  # noqa: S105 - a Redis key pattern, not a secret
_RUN_SET_KEY = "push-action-run:{}"
_DEVICE_SET_KEY = "push-action-device:{}"

DECISIONS: tuple[str, ...] = ("approve", "reject")

#: The labels of the buttons (the push carries them as text).
ACTION_TITLES: dict[str, str] = {"approve": "Approve", "reject": "Reject"}


@dataclass(frozen=True, slots=True)
class PushAction:
    run_id: uuid.UUID
    user_id: uuid.UUID
    device_id: uuid.UUID
    decision: str
    tool_call_ids: tuple[str, ...]


def _digest(token: str) -> str:
    return sha256(token.encode()).hexdigest()


def pending_call_ids(pending: list) -> tuple[str, ...]:
    """The tool calls a pause waits on, in a stable order."""
    return tuple(sorted(str(p.get("tool_call_id")) for p in pending if isinstance(p, dict) and p.get("tool_call_id")))


async def mint_token(
    *, run_id: uuid.UUID, user_id: uuid.UUID, device_id: uuid.UUID, decision: str, tool_call_ids: tuple[str, ...]
) -> str | None:
    """A new token for one button, or ``None`` when it cannot be stored."""
    from app.core.redis import redis_client

    if decision not in DECISIONS or not tool_call_ids:
        return None
    token = secrets.token_urlsafe(32)
    digest = _digest(token)
    record = json.dumps(
        {
            "run_id": str(run_id),
            "user_id": str(user_id),
            "device_id": str(device_id),
            "decision": decision,
            "tool_call_ids": list(tool_call_ids),
        }
    )
    try:
        redis = await redis_client()
        pipe = redis.pipeline()
        pipe.set(_TOKEN_KEY.format(digest), record, ex=TOKEN_TTL_SECONDS)
        for set_key in (_RUN_SET_KEY.format(run_id), _DEVICE_SET_KEY.format(device_id)):
            pipe.sadd(set_key, digest)
            pipe.expire(set_key, TOKEN_TTL_SECONDS)
        await pipe.execute()
    except Exception as exc:
        logger.warning("Could not store a notification action token: %s", format_exception_message(exc))
        return None
    return token


async def approval_actions(
    *, run_id: uuid.UUID, user_id: uuid.UUID, device_id: uuid.UUID, pending: list
) -> list[dict] | None:
    """The Approve / Reject buttons of one phone's notification, or ``None``."""
    call_ids = pending_call_ids(pending)
    if not call_ids:
        return None
    actions = []
    for decision in DECISIONS:
        token = await mint_token(
            run_id=run_id, user_id=user_id, device_id=device_id, decision=decision, tool_call_ids=call_ids
        )
        if token is None:
            return None
        actions.append({"action": decision, "title": ACTION_TITLES[decision], "token": token})
    return actions


async def consume_token(token: object) -> PushAction | None:
    """The action a token stands for, spent; ``None`` for anything else."""
    from app.core.redis import redis_client

    if not isinstance(token, str) or not 20 <= len(token) <= 100:
        return None
    try:
        redis = await redis_client()
        raw = await redis.getdel(_TOKEN_KEY.format(_digest(token)))
    except Exception as exc:
        logger.warning("Could not read a notification action token: %s", format_exception_message(exc))
        return None
    if not raw:
        return None
    try:
        data = json.loads(raw)
        return PushAction(
            run_id=uuid.UUID(data["run_id"]),
            user_id=uuid.UUID(data["user_id"]),
            device_id=uuid.UUID(data["device_id"]),
            decision=str(data["decision"]),
            tool_call_ids=tuple(str(i) for i in data["tool_call_ids"]),
        )
    except (ValueError, KeyError, TypeError):
        return None


async def _void(set_key: str) -> None:
    from app.core.redis import redis_client

    try:
        redis = await redis_client()
        digests = await redis.smembers(set_key)
        keys = [_TOKEN_KEY.format(d.decode() if isinstance(d, bytes) else d) for d in digests]
        if keys:
            await redis.delete(*keys)
        await redis.delete(set_key)
    except Exception as exc:
        logger.warning("Could not void notification action tokens: %s", format_exception_message(exc))


async def void_run_tokens(run_id: object) -> None:
    """Spend every button of a run's notifications: it was decided."""
    await _void(_RUN_SET_KEY.format(run_id))


async def void_device_tokens(device_id: object) -> None:
    """Spend every button sent to a phone: it was unlinked."""
    await _void(_DEVICE_SET_KEY.format(device_id))
