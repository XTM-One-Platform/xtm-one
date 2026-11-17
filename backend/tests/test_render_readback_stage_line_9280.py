"""The read-back's stage line never keeps a stale ETA (issue #9280).

A high render sat at 94 % "about 1 min left" for 18 minutes: the presence frames' line was written every 30 s, the
steps after it ended within 30 s and wrote nothing, and the duplicate logo pass ran minutes with no stage at all. Now:

* the line is written at the first beat after the read-back moves to another step;
* a step outside the presence frames says how long it has run (no invented ETA) and, when it reads samples, which;
* the duplicate logo pass has its own title.

Synthetic: no ffmpeg, no browser.
"""

from __future__ import annotations

import asyncio

from app.services.video_studio import render
from app.services.video_studio.render import DUPLICATE_LOGO_STAGE, READBACK_SAY_SECONDS, ReadbackWhere, reading_back


def test_a_step_after_the_presence_frames_drops_the_eta_and_says_how_long_it_runs():
    where = ReadbackWhere(step="presence frames", last=2_973)
    where.began_frame, where.began_at = 2_450, 0.0
    where.frame = 2_947
    where.moved(497.0)
    assert where.stage_words(500.0).endswith("about 1 min left")
    where.at("insert reports", now=500.0)
    assert where.stage_words(510.0) == "Reading the screen inserts back off the render: insert reports"
    assert where.stage_words(500.0 + READBACK_SAY_SECONDS + 5).endswith("insert reports, running for 35 s")
    said = where.stage_words(500.0 + 18 * 60)
    assert said.endswith("insert reports, running for 18 min")
    assert "min left" not in said


def test_the_duplicate_logo_pass_has_its_own_title_and_counts_its_samples():
    where = ReadbackWhere(title=DUPLICATE_LOGO_STAGE)
    where.at("footage frames", now=0.0)
    where.sample, where.samples = 3, 12
    said = where.stage_words(90.0)
    assert said == f"{DUPLICATE_LOGO_STAGE}: footage frames, sample 3 of 12, running for 1 min"
    assert "Reading the screen inserts back" not in said


async def test_a_step_change_is_said_at_the_next_beat_not_thirty_seconds_later(monkeypatch):
    monkeypatch.setattr(render, "READBACK_BEAT_SECONDS", 0.01)
    lines: list[str] = []

    async def say(where: ReadbackWhere) -> None:
        lines.append(where.step)

    async with reading_back(lambda: None, budget=None, fps=30, say=say) as where:
        where.at("bezel readings")
        await asyncio.sleep(0.05)
        where.at("insert reports")
        await asyncio.sleep(0.05)
    assert lines[:2] == ["bezel readings", "insert reports"]
    assert len(lines) == 2, "said once per step while under READBACK_SAY_SECONDS"


def test_the_render_tools_are_cut_safe_and_replay_safe():
    # A render_video in flight at two hand-backs runs again and answers the render it started (start_render reuses
    # the render of that revision and quality in flight), never refused by the poison guard.
    from app.services.builtin_tools.video_studio import _definitions

    tools = {tool.name: tool for tool in _definitions(["draft", "high"])}
    for name in ("render_video", "render_video_scene"):
        assert tools[name].cut_safe and tools[name].replay_safe, name


async def test_reading_back_takes_a_title():
    async with reading_back(lambda: None, budget=None, fps=30, title=DUPLICATE_LOGO_STAGE) as where:
        assert where.title == DUPLICATE_LOGO_STAGE
    async with reading_back(lambda: None, budget=None, fps=30) as where:
        assert where.title == "Reading the screen inserts back off the render"
