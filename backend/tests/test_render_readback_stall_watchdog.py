"""The presence read-back's watchdog and its stalled stage line (dev:render-readback-hang-live-lease).

A high render resumed "Reading the screen inserts back off the render" near its end, reached frame 2,947 of 2,973
"about 1 min left", then made no progress for over 15 minutes while its lease heartbeat kept renewing: nothing timed
out and the render slot stayed held. Now:

* a hand-over in the last frames of the presence pass keeps its place and the resumed attempt finishes the pass with
  the verdict an uninterrupted read gives;
* no new presence frame past the watchdog (``ReadbackWhere.stall_seconds``: 5 min at least, 10x the attempt's own
  pace when longer) while the heartbeat beats ends the read-back with ``ReadbackStalledError`` naming the frame, and
  the heartbeat stops beating at that moment;
* the stage line says "stalled" once the time on one frame exceeds the time it said was left.

Synthetic fixtures only: the fake decoder of ``test_render_presence_resume_8958``, no ffmpeg, no browser.
"""

from __future__ import annotations

import asyncio

import pytest

from app.services.video_studio import insert_checks, render
from app.services.video_studio.render import (
    READBACK_PACE_MIN_FRAMES,
    READBACK_STALL_FLOOR_SECONDS,
    ReadbackStalledError,
    ReadbackWhere,
    reading_back,
)
from tests.test_render_presence_resume_8958 import _run, _setup, _verdict, decoder  # noqa: F401  (the fixture)
from tests.test_render_readback_handover_9143 import _gate


async def test_a_hand_over_in_the_last_frames_resumes_and_finishes_the_pass(decoder, tmp_path):  # noqa: F811
    document, audits, placed, _frames, pictures = _setup()
    decoder["pictures"] = pictures
    whole = _verdict(await _run(document, audits, placed, workdir=tmp_path / "whole"))
    total = len(decoder["judged"])
    decoder["judged"].clear()

    place = insert_checks.PresencePlace(tmp_path / "relaunch" / "presence-place.npz", "drawing-digest")
    near_end = max(1, total - max(1, total // 20))  # the last 5 % of the judged frames

    def handing_over() -> bool:
        return len(decoder["judged"]) >= near_end

    with pytest.raises(insert_checks.PresenceHandedOverError) as stopped:
        await _gate(document, audits, placed, workdir=tmp_path / "relaunch", place=place, handing_over=handing_over)
    decoder["judged"].clear()

    resumed = _verdict(await _gate(document, audits, placed, workdir=tmp_path / "relaunch", place=place))
    assert decoder["judged"], "the resumed attempt judged the frames left"
    assert decoder["judged"][0] >= stopped.value.frame
    assert resumed == whole


def test_the_watchdog_scales_with_the_attempt_pace_and_never_drops_under_its_floor():
    where = ReadbackWhere(step="presence frames", last=2_973)
    where.began_frame, where.began_at = 2_450, 0.0
    where.frame = 2_450 + READBACK_PACE_MIN_FRAMES - 1
    assert where.stall_seconds(10.0) == READBACK_STALL_FLOOR_SECONDS
    where.frame = 2_450 + 100  # 100 frames in 1,000 s: 10 s a frame, a 100 s watchdog, under the floor
    assert where.stall_seconds(1_000.0) == READBACK_STALL_FLOOR_SECONDS
    where.frame = 2_450 + 100  # 100 frames in 10,000 s: 100 s a frame, 10x is 1,000 s
    assert where.stall_seconds(10_000.0) == pytest.approx(1_000.0)


def test_the_stage_line_says_stalled_once_the_wait_exceeds_the_time_left():
    where = ReadbackWhere(step="presence frames", last=2_973)
    where.began_frame, where.began_at = 2_450, 0.0
    where.frame = 2_947  # 497 frames in 497 s: 26 frames left, about 1 min
    where.moved(497.0)
    assert where.stage_words(500.0).endswith("frame 2,947 of 2,973, about 1 min left")
    said = where.stage_words(497.0 + 1_057.0)
    assert "frame 2,947 of 2,973, stalled: no new frame for 17 min" in said
    assert "min left" not in said


def test_a_step_outside_the_presence_frames_is_never_watched():
    where = ReadbackWhere(step="presence frames", last=10, frame=5)
    where.moved(0.0)
    where.at("insert reports")
    assert where.still_seconds(10_000.0) is None


async def test_a_stalled_presence_frame_ends_the_read_back_with_its_reason(monkeypatch):
    monkeypatch.setattr(render, "READBACK_BEAT_SECONDS", 0.01)
    monkeypatch.setattr(render, "READBACK_STALL_FLOOR_SECONDS", 0.05)
    beats = {"n": 0}

    def beat() -> None:
        beats["n"] += 1

    with pytest.raises(ReadbackStalledError) as stalled:
        async with reading_back(beat, budget=None, fps=30) as where:
            where.at("presence frames")
            where.last, where.frame = 2_973, 2_947
            where.moved(render.time.monotonic())
            await asyncio.sleep(5)  # a frame that never comes
    assert "stalled at presence frames, frame 2,947 of 2,973" in str(stalled.value)
    ended_at = beats["n"]
    await asyncio.sleep(0.05)
    assert beats["n"] == ended_at, "no heartbeat (no lease renewal) past the watchdog"


async def test_a_moving_read_back_is_never_stopped(monkeypatch):
    monkeypatch.setattr(render, "READBACK_BEAT_SECONDS", 0.01)
    monkeypatch.setattr(render, "READBACK_STALL_FLOOR_SECONDS", 0.05)
    async with reading_back(lambda: None, budget=None, fps=30) as where:
        where.at("presence frames")
        where.last = 100
        for frame in range(20):
            where.frame = frame
            where.moved(render.time.monotonic())
            await asyncio.sleep(0.01)
