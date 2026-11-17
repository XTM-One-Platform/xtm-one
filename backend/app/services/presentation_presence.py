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

"""Who has a presentation open right now.

Presence is ephemeral by nature, so it lives in Redis, never in PostgreSQL:
one hash per deck (``presentation:presence:<id>``), one field per user holding
a small JSON heartbeat (name, what they are doing to the deck, which slide
they are on, when). The viewer heartbeats every ``PRESENCE_HEARTBEAT_SECONDS``
and the whole hash expires ``PRESENCE_TTL_SECONDS`` after the last one, so a
deck nobody looks at costs nothing. A closed tab that never said goodbye
disappears once its entry is older than the TTL  -  the read side filters
those, so a stale entry is never shown even before the key expires.

An entry's ``activity`` is what the other readers are told:

* ``viewing``   - the deck is open. An open studio rail or editor panel with
  nothing happening in it is viewing, not editing: what a collaborator wants
  to know is whether the deck is about to change, and a panel left open for
  an afternoon says nothing about that.
* ``editing``   - the person is changing the deck in the manual editor: a
  save is coming or just landed (the editor autosaves).
  Client-asserted, because only the client knows about a draft.
* ``assistant`` - an agent turn is in flight in one of the person's
  conversations bound to the deck (the studio). SERVER-DERIVED from the chat
  turn marker by the heartbeat route, never taken from the client: a claim
  cannot be made falsely, and a client whose "sending" state got stuck cannot
  keep the badge on, because the marker goes when the turn does.

``editing`` (the boolean) stays on every entry as ``activity != "viewing"``,
which is what the sort order and the badge key on.

While someone's editor is open, their entry also says where they work:
``slide_id`` (the slide's stable id) and ``block`` (the spec path of
the selected block), validated by the route's schema; the others' stages
outline that block. Such a viewer beats faster than the advertised cadence
while they edit, well inside the TTL.

Fail-open on purpose: if Redis is unreachable the heartbeat returns an empty
room and logs at debug. Presence is a courtesy in the toolbar, not a lock  -
the deck's optimistic ``expected_revision`` check is what actually protects
against two people saving over each other.
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

#: How often a viewer should heartbeat; the frontend polls at this cadence.
PRESENCE_HEARTBEAT_SECONDS = 15
#: How long an entry stays visible without a heartbeat (three missed beats).
PRESENCE_TTL_SECONDS = 45
#: Beats one person may send for one deck per minute: an editor beats
#: every 2.5 s and once more per settled selection change, two tabs double it;
#: past this the room is simply not refreshed until the window turns.
PRESENCE_BEATS_PER_MINUTE = 150
#: Cap on the name stored per entry  -  the toolbar shows an avatar and a tooltip.
_NAME_MAX = 120

#: What a viewer is doing to the deck, as told to the other readers.
ACTIVITY_VIEWING = "viewing"
ACTIVITY_EDITING = "editing"
ACTIVITY_ASSISTANT = "assistant"
PRESENCE_ACTIVITIES: tuple[str, ...] = (ACTIVITY_VIEWING, ACTIVITY_EDITING, ACTIVITY_ASSISTANT)


def presence_key(presentation_id: uuid.UUID | str) -> str:
    return f"presentation:presence:{presentation_id}"


def _now() -> float:
    return time.time()


def resolve_activity(*, editing: bool = False, assistant: bool = False) -> str:
    """The one activity an entry carries.

    An assistant turn outranks a pending manual draft: it is the change that
    lands on its own, under the other readers, and the half the client did not
    get to claim.
    """
    if assistant:
        return ACTIVITY_ASSISTANT
    if editing:
        return ACTIVITY_EDITING
    return ACTIVITY_VIEWING


def _decode_activity(data: dict[str, Any]) -> str:
    """Read an entry's activity, deriving it for older entries."""
    activity = data.get("activity")
    if isinstance(activity, str) and activity in PRESENCE_ACTIVITIES:
        return activity
    return ACTIVITY_EDITING if data.get("editing") else ACTIVITY_VIEWING


async def heartbeat_presence(
    presentation_id: uuid.UUID | str,
    *,
    user_id: uuid.UUID | str,
    name: str | None,
    editing: bool = False,
    assistant: bool = False,
    slide_index: int | None = None,
    slide_id: str | None = None,
    block: str | None = None,
) -> list[dict[str, Any]]:
    """Record that *user_id* has the deck open and return the live room.

    ``editing`` is the client's "I hold unsaved edits"; ``assistant`` is the
    route's own reading of the chat turn marker (see the module docstring) -
    the two are folded into one ``activity`` here so every reader of the room
    sees the same word.

    The room is what the caller renders  -  one round-trip does both, so the
    viewer's poll is a single request.
    """
    key = presence_key(presentation_id)
    activity = resolve_activity(editing=editing, assistant=assistant)
    entry = {
        "user_id": str(user_id),
        "name": (name or "").strip()[:_NAME_MAX] or None,
        "activity": activity,
        "editing": activity != ACTIVITY_VIEWING,
        "slide_index": (int(slide_index) if isinstance(slide_index, int) and slide_index >= 0 else None),
        "slide_id": slide_id if isinstance(slide_id, str) and slide_id else None,
        "block": block if isinstance(block, str) and block else None,
        "ts": _now(),
    }
    try:
        redis = await redis_client()
        await redis.hset(key, str(user_id), json.dumps(entry))
        await redis.expire(key, PRESENCE_TTL_SECONDS)
        raw = await redis.hgetall(key)
    except Exception as exc:  # presence is fail-open
        logger.debug("presence heartbeat skipped for %s: %s", key, format_exception_message(exc))
        return []
    return _decode_room(raw, viewer_id=str(user_id))


async def leave_presence(presentation_id: uuid.UUID | str, *, user_id: uuid.UUID | str) -> None:
    """Drop *user_id* from the room immediately (viewer closed / navigated away)."""
    key = presence_key(presentation_id)
    try:
        redis = await redis_client()
        await redis.hdel(key, str(user_id))
    except Exception as exc:
        logger.debug("presence leave skipped for %s: %s", key, format_exception_message(exc))


async def list_presence(
    presentation_id: uuid.UUID | str, *, viewer_id: uuid.UUID | str | None = None
) -> list[dict[str, Any]]:
    """The live room without heartbeating (read-only surfaces, tests)."""
    key = presence_key(presentation_id)
    try:
        redis = await redis_client()
        raw = await redis.hgetall(key)
    except Exception as exc:
        logger.debug("presence read skipped for %s: %s", key, format_exception_message(exc))
        return []
    return _decode_room(raw, viewer_id=str(viewer_id) if viewer_id else None)


def _decode_room(raw: Any, *, viewer_id: str | None) -> list[dict[str, Any]]:
    """Decode a hash into the sorted room, dropping stale and malformed entries.

    Sorted so the toolbar is stable: the viewer first, then editors, then by
    name  -  a poll must not reshuffle the avatars every 15 seconds.
    """
    cutoff = _now() - PRESENCE_TTL_SECONDS
    room: list[dict[str, Any]] = []
    for field, value in (raw or {}).items():
        try:
            data = json.loads(value if isinstance(value, str) else value.decode("utf-8"))
        except (TypeError, ValueError, AttributeError):
            continue
        if not isinstance(data, dict):
            continue
        ts = data.get("ts")
        if not isinstance(ts, int | float) or ts < cutoff:
            continue
        uid = str(data.get("user_id") or (field if isinstance(field, str) else field.decode()))
        activity = _decode_activity(data)
        room.append(
            {
                "user_id": uid,
                "name": data.get("name") or None,
                "activity": activity,
                "editing": activity != ACTIVITY_VIEWING,
                "slide_index": (data["slide_index"] if isinstance(data.get("slide_index"), int) else None),
                "slide_id": data["slide_id"] if isinstance(data.get("slide_id"), str) else None,
                "block": data["block"] if isinstance(data.get("block"), str) else None,
                "last_seen_at": float(ts),
                "is_self": viewer_id is not None and uid == viewer_id,
            }
        )
    room.sort(
        key=lambda e: (
            0 if e["is_self"] else 1,
            0 if e["editing"] else 1,
            (e["name"] or "").lower(),
            e["user_id"],
        )
    )
    return room


__all__ = [
    "ACTIVITY_ASSISTANT",
    "ACTIVITY_EDITING",
    "ACTIVITY_VIEWING",
    "PRESENCE_ACTIVITIES",
    "PRESENCE_BEATS_PER_MINUTE",
    "PRESENCE_HEARTBEAT_SECONDS",
    "PRESENCE_TTL_SECONDS",
    "heartbeat_presence",
    "leave_presence",
    "list_presence",
    "presence_key",
    "resolve_activity",
]
