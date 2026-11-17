"""An AI track is composed for the video that will render, and never stops before it.

A track composed for a video is asked for the video's planned length plus a
margin (``music.fitted_seconds``). The plan used to place every clip scene at
the planner's 4 s when its video was not at hand, while AI shots last 5 to 8 s,
so the track came out shorter than the rendered timeline. The estimate now
plans each AI shot at its file's length once made, at the length it was asked
for meanwhile (the preview's rule). A track asked to play once that still stops
before the end is never looped: it fades out on its own end, silence
held after it.
"""

from __future__ import annotations

import asyncio
import uuid
from types import SimpleNamespace

import pytest

from app.models.video_studio import JOB_PENDING, JOB_READY
from app.services.video_studio import encoder, music, projects, timeline
from app.services.video_studio.spec import Music, parse_video_spec, validate_video_spec

SHOTS = [f"00000000-0000-0000-0000-0000000001{i:02d}" for i in range(8)]


def _effective() -> dict:
    scenes: list[dict] = [{"id": "open", "type": "title", "title": "Intro card", "duration": 4}]
    for index, clip_id in enumerate(SHOTS):
        scenes.append({"id": f"shot{index}", "type": "clip", "clip_id": clip_id})
        if index % 3 == 2:
            scenes.append({"id": f"words{index}", "type": "statement", "lines": ["A statement"], "duration": 5})
    scenes.append({"id": "close", "type": "outro", "title": "Outro card", "duration": 5})
    return validate_video_spec(
        {
            "title": "Synthetic",
            "freestyle": True,
            "default_transition": {"type": "crossfade", "duration": 0.8},
            "scenes": scenes,
        }
    )


def _shots(seconds: float) -> dict[str, timeline.ClipMedia]:
    frames = int(seconds * 24)
    return {f"shot{index}": timeline.ClipMedia(seconds, 1280, 720, 24.0, frames) for index in range(len(SHOTS))}


def test_the_estimate_plans_each_shot_at_its_length_and_the_track_outlasts_the_render():
    spec = parse_video_spec(_effective())
    rendered = timeline.plan(spec, timeline.PlanMedia(clips=_shots(6.5)), fps=60, theme={}).duration
    blind = timeline.estimate_duration(spec, {})
    informed = timeline.estimate_duration(spec, {}, _shots(6.5))
    # Eight shots of 6.5 s planned at 4 s: the track came out 20 s short.
    assert blind < rendered - 15
    assert informed == pytest.approx(rendered, abs=0.1)
    assert music.fitted_seconds(informed) >= rendered + 2


class _Db:
    def __init__(self, rows: dict) -> None:
        self.rows = rows

    async def get(self, _model, key):
        return self.rows.get(str(key))


def _shot_row(*, status: str, duration_ms: int | None = None, asked: float | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        status=status,
        file_id=uuid.uuid4() if status == JOB_READY else None,
        duration_ms=duration_ms,
        width=1280,
        height=720,
        has_audio=False,
        expired_at=None,
        params={"duration_seconds": asked} if asked else {},
        progress=100 if status == JOB_READY else 30,
        error=None,
    )


def test_a_project_is_planned_with_its_shots_made_and_the_length_asked_of_those_still_being_made():
    rows = {
        clip_id: _shot_row(status=JOB_READY, duration_ms=6500) if index % 2 else _shot_row(status=JOB_PENDING, asked=8)
        for index, clip_id in enumerate(SHOTS)
    }
    seconds = asyncio.run(projects.estimate_seconds(_Db(rows), _effective()))
    media = {
        f"shot{index}": timeline.ClipMedia(6.5 if index % 2 else 8.0, 1280, 720, 24.0, 1) for index in range(len(SHOTS))
    }
    rendered = timeline.plan(parse_video_spec(_effective()), timeline.PlanMedia(clips=media), fps=30, theme={})
    assert seconds == pytest.approx(rendered.duration, abs=0.1)
    assert music.fitted_seconds(seconds) >= rendered.duration + 2


def _bed(**fields) -> music.Bed:
    return music.Bed.of(Music(file_id="00000000-0000-0000-0000-0000000000c3", **fields), 120.0)


def test_a_track_asked_to_play_once_that_stops_before_the_end_fades_on_its_own_end_and_never_loops():
    once = _bed(loop=False, start=10)
    segments = encoder.bed_segments(100.0, once.duration, start=once.start, loop=once.loop, ending=once.ending)
    # Played once from its start to its end: never repeated.
    assert segments == [(10.0, 100.0)]
    graph = encoder.bed_filter(
        segments, duration=once.duration, crossfade=2.0, gain_db=0.0, fade_in=once.fade_in, fade_out=once.fade_out
    )
    # Its fade out ends where the track does (90 s in), the bed held silent to the video's 120 s.
    assert "acrossfade" not in graph
    assert "afade=t=out:st=87.500:d=2.500" in graph
    assert graph.endswith("apad=whole_dur=120.000,atrim=duration=120.000[bed]")
    assert music.bed_length_note(once, 100.0) == (
        "Music: the music file lasts 100 s (90 s from its 10 s start) under the 120 s video: asked to play once, it "
        "fades out at 90 s, leaving the video's last 30 s without music."
    )


def test_a_looping_or_long_enough_track_fades_on_the_videos_last_frame():
    looping = _bed(loop=True)
    segments = encoder.bed_segments(30.0, looping.duration, loop=True, crossfade=2.0)
    assert len(segments) > 1 and encoder.joined_seconds(segments, 2.0) >= looping.duration
    graph = encoder.bed_filter(segments, duration=120.0, crossfade=2.0, gain_db=0.0, fade_in=1.0, fade_out=2.5)
    assert "afade=t=out:st=117.500:d=2.500" in graph
    once = _bed(loop=False)
    long_enough = encoder.bed_segments(125.0, 120.0, loop=False)
    assert long_enough == [(0.0, 120.0)]
    graph = encoder.bed_filter(long_enough, duration=120.0, crossfade=2.0, gain_db=0.0, fade_in=1.0, fade_out=2.5)
    assert "afade=t=out:st=117.500:d=2.500" in graph
    assert music.bed_length_note(once, 125.0).endswith("it fades out at the video's end.")
