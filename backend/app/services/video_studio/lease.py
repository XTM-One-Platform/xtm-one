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

"""A render's lease, renewed while it moves, from a thread of its own.

The worker's reconciler renews every execution's lease on a timer, from its
own event loop. A render process renews its lease too, so a render that keeps
moving is never taken for a dead one, whatever the worker's loop is doing
meanwhile.

:class:`FrameLease` renews it from a **thread of its own**, on its own
event loop and its own database connection, every third of the lease
(:attr:`FrameLease.interval`): never from the drawing loop, whose frames and
reports only say the render moves (:meth:`FrameLease.frame_drawn`,
:meth:`FrameLease.moved`). Two High renders lost their lease at about 60%
while they were drawing frames: both the worker's heartbeat and the renewals
the frames triggered fell silent for over a minute, and the engine
dead-lettered renders that were still making progress. A thread is neither a
frame nor the worker's event loop: a slow frame, an event loop busy with a
4K screenshot or a worker starved of CPU no longer silence it.

It renews only while the render moves: once nothing moved for the stall
window (``VIDEO_STUDIO_RENDER_STALL_SECONDS``), the supervisor's stall detector
decides, and the lease is left to expire. A renewal that hangs is given up
after an interval. Every renewal is logged with the time since the previous
one and the frames drawn meanwhile; a lease the engine took back or ended (the
execution was reclaimed, cancelled or dead-lettered) is logged and stops the
render at the next frame (``LeaseLostError``): another attempt, or nobody,
owns the render now.
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
import time
import uuid

from app.core.config import settings
from app.core.database import async_session
from app.core.utils import format_exception_message
from app.services import orchestration_engine as engine
from app.services.orchestration_engine import LeaseLostError

logger = logging.getLogger(__name__)

#: The environment a render process reads its execution's lease from.
EXECUTION_ENV = "XTM_RENDER_EXECUTION_ID"
OWNER_ENV = "XTM_RENDER_LEASE_OWNER"
#: The seconds the lease lasts at each renewal (the render adapter's own).
LEASE_ENV = "XTM_RENDER_LEASE_SECONDS"
#: The clock the movement is timed by (a test drives its own).
_monotonic = time.monotonic
#: How long a renewal that failed waits before it is tried again.
_RETRY_SECONDS = 5.0


def renewal_interval(lease_seconds: float) -> float:
    """How often a lease of *lease_seconds* is renewed: a third of it (at least a second)."""
    return max(1.0, float(lease_seconds) / 3)


def _lease_seconds() -> int:
    """The lease the worker handed this render process, else the engine's."""
    raw = os.environ.get(LEASE_ENV, "").strip()
    return int(raw) if raw.isdigit() and int(raw) > 0 else int(settings.orchestration_lease_seconds)


async def renew(execution_id: uuid.UUID, owner: str, *, lease_seconds: int | None = None) -> None:
    """Renew the execution's lease once, now, as *owner*; ``LeaseLostError`` once the engine took it back.

    What a stalled render resumed in place does before its process starts again: it keeps the
    execution it holds rather than leaving it to expire and start over.
    """
    async with async_session() as db:
        await engine.extend_lease(db, execution_id=execution_id, worker_id=owner, lease_seconds=lease_seconds)
        await db.commit()


class FrameLease:
    """The execution's lease, renewed every :attr:`interval` seconds from a thread while the render moves."""

    def __init__(
        self,
        execution_id: uuid.UUID,
        owner: str,
        *,
        interval: float | None = None,
        lease_seconds: int | None = None,
        window: float | None = None,
    ) -> None:
        self.execution_id = execution_id
        self.owner = owner
        self.lease_seconds = int(lease_seconds or _lease_seconds())
        self.interval = interval if interval is not None else renewal_interval(self.lease_seconds)
        #: How long the render may stand still and keep its lease: the stall window; ``None``, always.
        if window is None:
            stall = float(settings.video_studio_render_stall_seconds)
            window = stall if stall > 0 else None
        self.window = window
        self.frames = 0
        self.renewals = 0
        self.failures = 0
        #: Why the lease is gone, once it is.
        self.lost: str | None = None
        self._moved_at = _monotonic()
        self._renewed_frames = 0
        self._renewed_at = _monotonic()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._db_engine = None
        self._sessions = None

    @classmethod
    def from_environment(cls) -> FrameLease | None:
        """The lease the worker handed this render process, or ``None`` when it handed none."""
        execution_id = os.environ.get(EXECUTION_ENV, "").strip()
        owner = os.environ.get(OWNER_ENV, "").strip()
        if not execution_id or not owner:
            return None
        try:
            return cls(uuid.UUID(execution_id), owner)
        except ValueError:
            logger.warning("Render process: %s=%r is not an execution id", EXECUTION_ENV, execution_id)
            return None

    # ── The render's side: it moves ───────────────────────────────────────

    def moved(self) -> None:
        """The render moved (a report, a stage): its lease is renewed for another stall window."""
        self._moved_at = _monotonic()

    def frame_drawn(self) -> None:
        """Count a frame and say the render moved. Raises once the lease is gone."""
        if self.lost is not None:
            raise LeaseLostError(self.lost)
        self.frames += 1
        self._moved_at = _monotonic()

    def moving(self) -> bool:
        """Whether the render moved within its window, so its lease is renewed."""
        return self.window is None or _monotonic() - self._moved_at <= self.window

    def frames_per_second(self, now: float | None = None) -> float:
        """The frames drawn per second since the last renewal (or the start)."""
        elapsed = (_monotonic() if now is None else now) - self._renewed_at
        return (self.frames - self._renewed_frames) / elapsed if elapsed > 0 else 0.0

    # ── The thread's side: it renews ──────────────────────────────────────

    def start(self) -> None:
        """Start renewing the lease from a thread of its own, every :attr:`interval` seconds."""
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, name="render-lease", daemon=True)
            self._thread.start()

    def _run(self) -> None:
        with asyncio.Runner() as runner:
            try:
                delay = self.interval
                while not self._stop.wait(delay):
                    if not self.moving():
                        delay = self.interval
                        continue
                    delay = runner.run(self._renew())
                    if self.lost is not None:
                        return
            finally:
                runner.run(self._dispose())

    async def _extend(self) -> None:
        """Extend the lease once, on the thread's own connection (a pool belongs to one event loop)."""
        if self._sessions is None:
            from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
            from sqlalchemy.pool import NullPool

            from app.core.database import ENGINE_CONNECT_ARGS

            self._db_engine = create_async_engine(settings.database_url, poolclass=NullPool, connect_args=ENGINE_CONNECT_ARGS)
            self._sessions = async_sessionmaker(self._db_engine, expire_on_commit=False)
        async with self._sessions() as db:
            await engine.extend_lease(
                db, execution_id=self.execution_id, worker_id=self.owner, lease_seconds=self.lease_seconds
            )
            await db.commit()

    async def _dispose(self) -> None:
        if self._db_engine is not None:
            try:
                await self._db_engine.dispose()
            except Exception as exc:
                logger.debug("Render lease connection not closed: %s", format_exception_message(exc))

    async def _renew(self) -> float:
        """Renew the lease once; the seconds until the next renewal."""
        asked_at = _monotonic()
        frames = self.frames
        since = asked_at - self._renewed_at
        fps = self.frames_per_second(asked_at)
        try:
            # A renewal that hangs (a database that stopped answering) never holds the next one back.
            await asyncio.wait_for(self._extend(), timeout=self.interval)
        except LeaseLostError as exc:
            self.lost = str(exc)
            logger.warning(
                "Render of execution %s: its lease is gone at frame %d (%.1f frames/s over the %.1f s since its "
                "last renewal): %s; it stops",
                self.execution_id,
                frames,
                fps,
                since,
                exc,
            )
            return self.interval
        except Exception as exc:
            self.failures += 1
            logger.warning(
                "Render of execution %s: its lease could not be renewed at frame %d, %.1f s after the last renewal "
                "(%d failures, %.1f frames/s, the lease lasts %d s): %s",
                self.execution_id,
                frames,
                since,
                self.failures,
                fps,
                self.lease_seconds,
                format_exception_message(exc) or type(exc).__name__,
            )
            return min(_RETRY_SECONDS, self.interval)
        self.renewals += 1
        logger.info(
            "Render of execution %s: lease renewed at frame %d, %.1f s after the last renewal (%d frames, %.1f frames/s)",
            self.execution_id,
            frames,
            since,
            frames - self._renewed_frames,
            fps,
        )
        self._renewed_frames = frames
        self._renewed_at = _monotonic()
        return self.interval

    async def close(self) -> None:
        """Stop the thread, waiting for a renewal in flight: the process must not exit in the middle of one."""
        self._stop.set()
        if self._thread is not None and self._thread.is_alive():
            await asyncio.to_thread(self._thread.join, self.interval + 10)
