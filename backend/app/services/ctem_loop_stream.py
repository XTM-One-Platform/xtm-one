# SPDX-License-Identifier: Apache-2.0

"""Live CTEM loop frames on the SSE activity stream (#838, ``dev-docs/ctem-loop-visibility.md`` section 5).

A signal recorded, decided or moved tells the browsers that may read it *when
to look*: one ``ctem_signal_update`` frame carrying ids and states only, never
a subject, evidence, score or name. The client re-reads the signal through
``GET /api/v1/ctem-signals/{id}``, which applies the reader's visibility, so a
frame never leaks what the channel's owner could not read.

Channels follow the read rule of ``services/ctem_signals.py``: a signal recorded
for a person goes to ``activity:<that person>`` and to every administrator (who
reads the organization's signals whole); a listener's signal (recorded by
``system``) goes to the administrators only. Never ``activity:company``.

Best effort, after the commit: a dead Redis or a slow publish never fails or
slows the write (2 s bound, then a quiet cooldown, as ``flow_run_stream``).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
import uuid
from collections.abc import Iterable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.redis import redis_client
from app.core.utils import format_exception_message
from app.models.ctem_signal import CtemSignal
from app.models.user import User

logger = logging.getLogger(__name__)

#: The frame type the web app routes to the signal queries.
SIGNAL_FRAME = "ctem_signal_update"
#: The frame type the web app routes to the case queries (CTEM-03-UI, #892).
CASE_FRAME = "ctem_case_update"
#: One publish round, at most (seconds).
PUBLISH_TIMEOUT_SECONDS = 2.0
#: After a failure, no publish for this long (seconds): the 30 s polling of the
#: command center stays the floor.
_FAILURE_COOLDOWN_SECONDS = 30.0
_down_until: float = 0.0


def signal_frame(signal: CtemSignal) -> dict[str, str]:
    """The frame of *signal*: ids and states only (section 5)."""
    at = signal.updated_at or signal.last_seen_at
    return {
        "type": SIGNAL_FRAME,
        "signal_id": str(signal.id),
        "state": signal.state,
        "trigger_path": signal.trigger_path,
        "lens": signal.lens,
        "at": at.isoformat() if at else "",
    }


def signal_channels(recorded_by: str | None, admin_ids: Iterable[str]) -> list[str]:
    """The personal channels a frame of a signal recorded by *recorded_by* goes to, each once.

    The recorder when it is a person (a user id), plus every administrator; a
    listener's signal (``system`` or anything that is not a user id) reaches
    the administrators alone.
    """
    readers: list[str] = []
    with contextlib.suppress(TypeError, ValueError):
        readers.append(str(uuid.UUID(str(recorded_by))))
    for admin in admin_ids:
        if admin not in readers:
            readers.append(admin)
    return [f"activity:{reader}" for reader in readers]


def case_frame(case_id: str, state: str, event_kind: str, at: str) -> dict[str, str]:
    """The frame of one case event: ids and states only (section 5), never a title, subject or evidence."""
    return {"type": CASE_FRAME, "case_id": case_id, "state": state, "event_kind": event_kind, "at": at}


def case_channels(user_ids: Iterable[str]) -> list[str]:
    """The personal channels a case frame goes to, each once: every active user, whose re-read decides what they see."""
    seen: list[str] = []
    for user_id in user_ids:
        if user_id not in seen:
            seen.append(user_id)
    return [f"activity:{user_id}" for user_id in seen]


async def _active_user_ids() -> list[str]:
    from app.core.database import async_session

    async with async_session() as db:
        rows = await db.execute(select(User.id).where(User.is_active.is_(True)))
        return [str(user_id) for user_id in rows.scalars().all()]


async def publish_case_updates(frames: Iterable[dict[str, str]]) -> int:
    """Publish the case frames collected by a write; call it AFTER the commit. Never raises; how many were sent."""
    global _down_until
    frames = list(frames)
    if not frames or time.monotonic() < _down_until:
        return 0
    sent = 0
    try:
        async with asyncio.timeout(PUBLISH_TIMEOUT_SECONDS):
            channels = case_channels(await _active_user_ids())
            redis = await redis_client()
            for frame in frames:
                payload = json.dumps(frame)
                for channel in channels:
                    await redis.publish(channel, payload)
                sent += 1
    except Exception as exc:
        _down_until = time.monotonic() + _FAILURE_COOLDOWN_SECONDS
        logger.warning(
            "CTEM case SSE publish failed (quiet for %.0fs): %s",
            _FAILURE_COOLDOWN_SECONDS,
            format_exception_message(exc),
        )
    return sent


async def _admin_ids(db: AsyncSession) -> list[str]:
    rows = await db.execute(select(User.id).where(User.role == "admin", User.is_active.is_(True)))
    return [str(user_id) for user_id in rows.scalars().all()]


async def publish_signal_update(db: AsyncSession, signal: CtemSignal) -> None:
    """Publish one frame for *signal*; call it AFTER the commit. Never raises."""
    global _down_until
    if time.monotonic() < _down_until:
        return
    try:
        async with asyncio.timeout(PUBLISH_TIMEOUT_SECONDS):
            payload = json.dumps(signal_frame(signal))
            channels = signal_channels(signal.recorded_by, await _admin_ids(db))
            redis = await redis_client()
            for channel in channels:
                await redis.publish(channel, payload)
    except Exception as exc:
        _down_until = time.monotonic() + _FAILURE_COOLDOWN_SECONDS
        logger.warning(
            "CTEM signal SSE publish failed (quiet for %.0fs): %s",
            _FAILURE_COOLDOWN_SECONDS,
            format_exception_message(exc),
        )
