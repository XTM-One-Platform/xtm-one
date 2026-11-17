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

"""Hand the clicker: a co-presenter drives a deck someone else presents.

xtm-one #10552, slice B. The presenter's windows (the stage, the speaker view
and the audience window of "Present to a meeting") already move together over
the deck's ``BroadcastChannel``, which is same-browser only: a co-presenter on
another machine cannot reach it. The clicker is the server-side half.

* One clicker per deck, held in Redis (``presentation:clicker:<id>``): who
  presents (the person who handed it), who holds it, since when. Handing it to
  someone else replaces the holder: one clicker at a time.
* The holder's ``next`` / ``prev`` / ``goto`` reach the presenter through the
  presenter's personal activity channel (``activity:<user id>``), the Redis
  pub/sub the web app's SSE stream already relays: no new realtime path, no
  socket, and a command reaches every window of the presenter's session.
* The presenter always keeps control (their own keys never stop working) and
  takes the clicker back in one call; the holder can give it back too.
* A lost connection falls back to the presenter: the entry expires
  ``CLICKER_TTL_SECONDS`` after the holder's last command or state read, so a
  holder whose tab died stops counting as the driver on its own.

Ephemeral and fail-open like presence (``presentation_presence``): with Redis
down the clicker is simply not handed (the route answers 503), and the
presenter's own keys keep working. Who may hold it is the route's business:
someone who can already read the deck, never a public-link viewer.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from typing import Any

from app.core.redis import redis_client
from app.core.utils import format_exception_message

logger = logging.getLogger(__name__)

#: Silence after which a holder is considered gone and the clicker returns to
#: the presenter. The holder's view reads the state every few seconds.
CLICKER_TTL_SECONDS = 45
#: Commands one holder may send per deck and minute: a clicker press every
#: half second with room to spare, far past any real talk.
CLICKER_COMMANDS_PER_MINUTE = 120
#: Cap on the stored holder name (shown in the presenter's speaker view).
_NAME_MAX = 120

#: The ``type`` of the activity frames the clicker publishes.
CLICKER_EVENT_COMMAND = "presentation_clicker_command"
CLICKER_EVENT_STATE = "presentation_clicker_state"


class ClickerUnavailableError(RuntimeError):
    """Redis could not be reached: the clicker cannot be handed or used."""


def clicker_key(presentation_id: uuid.UUID | str) -> str:
    return f"presentation:clicker:{presentation_id}"


def _now() -> float:
    return time.time()


def _decode(raw: Any) -> dict[str, Any] | None:
    if raw is None:
        return None
    try:
        data = json.loads(raw if isinstance(raw, str) else raw.decode("utf-8"))
    except (TypeError, ValueError, AttributeError):
        return None
    if not isinstance(data, dict):
        return None
    if not isinstance(data.get("presenter_id"), str) or not isinstance(data.get("holder_id"), str):
        return None
    return data


async def read_clicker(presentation_id: uuid.UUID | str) -> dict[str, Any] | None:
    """The deck's clicker, or None when nobody holds it (or Redis is down)."""
    try:
        redis = await redis_client()
        raw = await redis.get(clicker_key(presentation_id))
    except Exception as exc:  # fail-open: no clicker
        logger.debug("clicker read skipped for %s: %s", presentation_id, format_exception_message(exc))
        return None
    return _decode(raw)


async def hand_clicker(
    presentation_id: uuid.UUID | str,
    *,
    presenter_id: uuid.UUID | str,
    holder_id: uuid.UUID | str,
    holder_name: str | None,
) -> dict[str, Any]:
    """Give the clicker to *holder_id*, replacing any previous holder."""
    entry = {
        "presenter_id": str(presenter_id),
        "holder_id": str(holder_id),
        "holder_name": (holder_name or "").strip()[:_NAME_MAX] or None,
        "handed_at": _now(),
    }
    try:
        redis = await redis_client()
        await redis.set(clicker_key(presentation_id), json.dumps(entry), ex=CLICKER_TTL_SECONDS)
    except Exception as exc:
        raise ClickerUnavailableError(format_exception_message(exc)) from exc
    return entry


async def keep_clicker(presentation_id: uuid.UUID | str) -> None:
    """The holder is still there: push the expiry back (fail-open)."""
    try:
        redis = await redis_client()
        await redis.expire(clicker_key(presentation_id), CLICKER_TTL_SECONDS)
    except Exception as exc:
        logger.debug("clicker keep skipped for %s: %s", presentation_id, format_exception_message(exc))


async def release_clicker(presentation_id: uuid.UUID | str) -> None:
    """Take the clicker back (presenter) or give it back (holder)."""
    try:
        redis = await redis_client()
        await redis.delete(clicker_key(presentation_id))
    except Exception as exc:
        raise ClickerUnavailableError(format_exception_message(exc)) from exc


def clicker_frame(
    presentation_id: uuid.UUID | str,
    *,
    command: str,
    index: int | None,
    sender_id: uuid.UUID | str,
) -> dict[str, Any]:
    """The activity frame a holder's command travels in."""
    return {
        "type": CLICKER_EVENT_COMMAND,
        "presentation_id": str(presentation_id),
        "command": command,
        "index": index if command == "goto" else None,
        "from_user_id": str(sender_id),
        "ts": _now(),
    }


async def publish_to_user(user_id: uuid.UUID | str, frame: dict[str, Any]) -> None:
    """Send *frame* to every window of *user_id* over the activity stream."""
    try:
        redis = await redis_client()
        await redis.publish(f"activity:{user_id}", json.dumps(frame))
    except Exception as exc:
        raise ClickerUnavailableError(format_exception_message(exc)) from exc


__all__ = [
    "CLICKER_COMMANDS_PER_MINUTE",
    "CLICKER_EVENT_COMMAND",
    "CLICKER_EVENT_STATE",
    "CLICKER_TTL_SECONDS",
    "ClickerUnavailableError",
    "clicker_frame",
    "hand_clicker",
    "keep_clicker",
    "publish_to_user",
    "read_clicker",
    "release_clicker",
]
