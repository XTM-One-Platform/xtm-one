"""The screen inserts' read-back of an encoded render beats while it works and has its own bound.

A render whose frames were all drawn stalled at 94 % "Reading the screen inserts back off the render": its steps
(ffmpeg passes picking join frames, brand marks matched over footage frames) worked for minutes without telling the
supervisor, whose stall window stopped them each time.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable

import pytest

from app.services.video_studio import render

#: The wall time a test step may wait for its beats: never reached on a sane runner, a loaded xdist shard included.
BEATS_DEADLINE_SECONDS = 30.0


def _counting_beat(wanted: int) -> tuple[list[int], asyncio.Event, Callable[[], None]]:
    """A beat callback, the beats it recorded and an event set once *wanted* beats were recorded."""
    beats: list[int] = []
    enough = asyncio.Event()

    def beat() -> None:
        beats.append(1)
        if len(beats) >= wanted:
            enough.set()

    return beats, enough, beat


@pytest.mark.asyncio
async def test_reading_back_beats_while_a_step_works(monkeypatch):
    monkeypatch.setattr(render, "READBACK_BEAT_SECONDS", 0.01)
    beats, enough, beat = _counting_beat(3)
    async with render.reading_back(beat, budget=BEATS_DEADLINE_SECONDS * 2, fps=60) as where:
        where.at("duplicate logo frames")
        # A step that works without a frame of its own, until three beats were seen: an event, never a fixed
        # sleep, so a starved event loop on a loaded runner delays the beats without failing the test.
        await asyncio.wait_for(enough.wait(), BEATS_DEADLINE_SECONDS)
    assert len(beats) >= 3
    count = len(beats)
    await asyncio.sleep(0.05)
    assert len(beats) == count  # the beat ends with the read-back


@pytest.mark.asyncio
async def test_reading_back_past_its_bound_names_where_it_stood(monkeypatch):
    monkeypatch.setattr(render, "READBACK_BEAT_SECONDS", 0.01)
    with pytest.raises(render.ReadbackTimeoutError) as raised:
        async with render.reading_back(lambda: None, budget=0.05, fps=60) as where:
            where.at("presence frames")
            where.frame = 6000
            where.inserts = ("dashboard", "console")
            await asyncio.sleep(1)
    said = str(raised.value)
    assert "presence frames" in said
    assert "frame 6000 (100.0 s)" in said
    assert "dashboard, console" in said
    # A RuntimeError: the render fails with it said, never retried as a crash.
    assert isinstance(raised.value, RuntimeError)


@pytest.mark.asyncio
async def test_a_steps_own_timeout_is_not_the_bound():
    with pytest.raises(TimeoutError) as raised:
        async with render.reading_back(lambda: None, budget=5.0, fps=60):
            raise TimeoutError("an ffmpeg pass ran past its own timeout")
    assert not isinstance(raised.value, render.ReadbackTimeoutError)


@pytest.mark.asyncio
async def test_an_unbounded_read_back_beats_and_never_fails_on_time(monkeypatch):
    # The duplicate-logo check (a check that cannot run is a note, never a failed render) beats while it
    # works, outside the read-back's bound: its cost follows the footage scenes, not the presence frames.
    monkeypatch.setattr(render, "READBACK_BEAT_SECONDS", 0.01)
    beats, enough, beat = _counting_beat(3)
    async with render.reading_back(beat, budget=None, fps=60):
        await asyncio.wait_for(enough.wait(), BEATS_DEADLINE_SECONDS)
    assert len(beats) >= 3


def test_the_duplicate_logo_check_is_outside_the_bound():
    import inspect

    source = inspect.getsource(render)
    bounded = source.index("budget=readback_budget(len(presence))")
    unbounded = source.index("frame_done, budget=None, fps=fps, say=logo_say")
    check = source.index("twice = await _duplicate_logos(")
    assert bounded < unbounded < check


def test_the_bound_grows_with_the_frames_read():
    assert render.readback_budget(0) == render.READBACK_FLOOR_SECONDS
    # About 3,000 insert frames at 1080p60 get well past the stall window.
    assert render.readback_budget(3000) > render.READBACK_FLOOR_SECONDS + 1000
    assert render.readback_budget(-5) == render.READBACK_FLOOR_SECONDS


def test_where_words_without_a_frame():
    where = render.ReadbackWhere()
    where.at("bezel readings")
    assert where.words(60) == "bezel readings"
