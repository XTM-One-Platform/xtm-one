"""A render handed over closes its open segments at the frames reached: the resume starts there (#8958).

Live retest of 6 Nov 2026: a high render lost about 331 frames across one relaunch ("95%: 526 left", then "93%:
857 left"). Each page encodes a segment of up to ``compositor.SEGMENT_SECONDS`` and a segment is kept only once
closed whole, so a worker that stopped killed every page's half-drawn segment and the next attempt drew it again.

* a worker handing a render over sends it SIGUSR1 and waits up to ``HANDOVER_CLOSE_SECONDS`` for it to exit,
  before the kill: the process closes and stores what it drew;
* the frames a page reached in a segment closed short are kept, so the resume starts at the frame reached before
  the stop. The bound after a hand-over is 0 frames per page; after a loss without a stop request (SIGKILL, OOM,
  node loss) nothing can be closed and the bound stays one segment per page (``compositor.segment_frames``).
"""

from __future__ import annotations

import asyncio
import json
import signal
import sys
import textwrap
import time
from pathlib import Path

import pytest

from app.services.video_studio import compositor, render_process

pytestmark = pytest.mark.skipif(not hasattr(signal, "SIGUSR1"), reason="POSIX signals only")


def _child(body: str) -> list[str]:
    prelude = "import json, signal, sys, time\ndef report(event):\n    sys.stdout.write(json.dumps(event) + '\\n')\n    sys.stdout.flush()\n"
    return [sys.executable, "-c", prelude + textwrap.dedent(body)]


async def _stop_once_ready(ready: Path, stopping: asyncio.Event) -> None:
    """Hand over only once the child says it is ready: its SIGUSR1 handler installed and its progress reported.

    A fixed timer raced the child's start-up under xdist load: SIGUSR1 reached it before its handler, the default
    action killed it (returncode -10) and it had reported nothing (progress 0) (#9996).
    """
    while not ready.exists():
        await asyncio.sleep(0.01)
    stopping.set()


async def test_a_render_handed_over_closes_its_segment_at_the_frame_reached(tmp_path: Path):
    """The supervisor asks the process to close (SIGUSR1) and lets it exit before any kill."""
    segments = tmp_path / compositor.SEGMENTS_DIR
    segments.mkdir()
    (segments / "plan.json").write_text(json.dumps({"digest": "d"}), encoding="utf-8")
    ready = tmp_path / "ready"
    body = f"""
    reached = {{"frame": 0}}
    closing = {{"asked": False}}
    signal.signal(signal.SIGUSR1, lambda *_: closing.update(asked=True))
    report({{"type": "progress", "progress": 60, "stage": "Drawing frames (55%)"}})
    while not closing["asked"]:
        reached["frame"] += 1
        if reached["frame"] == 5:
            open({str(ready)!r}, "w").close()
        time.sleep(0.01)
    time.sleep(0.3)  # the encoder's flush and the store
    open({str(segments)!r} + "/seg-0000000-%07d.mp4" % reached["frame"], "wb").write(b"x")
    """
    stopping = asyncio.Event()
    ready_watch = asyncio.create_task(_stop_once_ready(ready, stopping))
    began = time.monotonic()
    ended = await asyncio.wait_for(
        render_process.supervise(
            _child(body), memory_limit=None, stall_seconds=60, stopping=stopping, poll_seconds=0.1
        ),
        30,
    )
    ready_watch.cancel()
    assert ended.kind == "handed_over" and ended.progress == 60
    assert time.monotonic() - began < render_process.HANDOVER_CLOSE_SECONDS
    kept = compositor.frames_kept(tmp_path)
    assert kept > 0, "the half-drawn segment was closed at the frame reached, not thrown away"
    # The resume plans its pieces from there: the first piece left to draw starts at the frame reached.
    finished = compositor.kept_segments(segments, "d")
    plan = compositor.plan_segments(0, 10_000, 600, finished)
    todo = [piece for piece in plan if piece not in finished]
    assert todo[0][0] == kept


async def test_a_render_that_does_not_close_in_time_is_killed(tmp_path: Path):
    """A process deaf to the hand-over is killed past the close bound: the worker's grace is never overrun."""
    ready = tmp_path / "ready"
    body = f"""
    signal.signal(signal.SIGUSR1, lambda *_: None)
    report({{"type": "progress", "progress": 40, "stage": "Drawing frames (35%)"}})
    open({str(ready)!r}, "w").close()
    time.sleep(120)
    """
    stopping = asyncio.Event()
    ready_watch = asyncio.create_task(_stop_once_ready(ready, stopping))
    began = time.monotonic()
    ended = await asyncio.wait_for(
        render_process.supervise(
            _child(body),
            memory_limit=None,
            stall_seconds=60,
            stopping=stopping,
            poll_seconds=0.1,
            handover_close_seconds=1.0,
        ),
        30,
    )
    ready_watch.cancel()
    # Killed past the close bound, not by SIGUSR1's default action: its handler was installed first.
    assert ended.kind == "handed_over" and ended.progress == 40 and time.monotonic() - began < 10


def test_the_close_bound_fits_in_the_drain_margin():
    assert render_process.HANDOVER_CLOSE_SECONDS < render_process.DRAIN_MARGIN_SECONDS


def test_the_hand_over_flag_is_what_the_pages_read(monkeypatch):
    monkeypatch.setitem(compositor._HANDING_OVER, "set", False)
    assert not compositor.handing_over()
    compositor.hand_over_now()
    assert compositor.handing_over()
