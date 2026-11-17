"""The frozen-picture watch never holds a render past its resumes.

* a 60 s video whose last 4 s are a near-static closing card, crossfaded in
  and under the video's fade out, is watched in well under 10 s: the card is
  never compared, its hold reported ``by_design``, never a frozen picture;
* a resume injected after the encode keeps every frame (the encoded video,
  never "0 frames kept") and its watch goes on from the window it reached,
  finding what one uninterrupted watch finds;
* a window whose frames stop coming is left unjudged with its time range
  and scenes, and the watch goes on from the next window on a new decoder;
* each window is named with its scenes as it starts, so a stall says where;
* what a resume keeps counts the scene ranges' segments too.
"""

from __future__ import annotations

import asyncio
import subprocess
import time
from pathlib import Path

import numpy as np
import pytest

from app.services.video_studio import compositor, encoder, frame_freeze, render_answer, render_checks, render_process

FPS = 30
W, H = 320, 180
#: The fixture: a moving clip that freezes from 18 s to 23 s, crossfaded at 55.5 s into a closing card held to 60 s,
#: the video fading out over its last 2 s.
DOCUMENT = {
    "duration": 60.0,
    "fade_out": 2.0,
    "scenes": [
        {"id": "s01", "type": "clip", "start": 0.0, "end": 56.0},
        {"id": "s02", "type": "outro", "start": 55.5, "end": 60.0, "transition_in": {"type": "crossfade", "duration": 0.5}},
    ],
}
needs_ffmpeg = pytest.mark.skipif(not encoder.encoder_available(), reason="ffmpeg is not installed")


@pytest.fixture(scope="module")
def outro_video(tmp_path_factory) -> Path:
    path = tmp_path_factory.mktemp("freeze-watch") / "video.mp4"
    graph = (
        f"testsrc2=size={W}x{H}:rate={FPS}:duration=56,split[source][still];"
        f"[source][still]freezeframes=first={18 * FPS}:last={23 * FPS - 1}:replace={18 * FPS}[clip];"
        f"color=c=0x0b1a2e:size={W}x{H}:rate={FPS}:duration=4.5,"
        f"drawbox=x=60:y=60:w=200:h=60:color=0x22d3ee:t=fill,noise=alls=1:allf=t[card];"
        "[clip][card]xfade=transition=fade:duration=0.5:offset=55.5,fade=t=out:st=58:d=2,format=yuv420p"
    )
    subprocess.run(  # noqa: S603
        [encoder.ffmpeg_path(), "-hide_banner", "-v", "error", "-y", "-filter_complex", graph, "-c:v", "libx264", "-preset", "ultrafast", str(path)],
        check=True,
        timeout=180,
    )
    return path


def _starts(monkeypatch) -> list[float]:
    """Where each decoder the watch opens starts, the real decoder behind it."""
    starts: list[float] = []
    real = encoder.iter_gray_frames

    def recorded(path, *, width, height, fps, start=0.0):
        starts.append(start)
        return real(path, width=width, height=height, fps=fps, start=start)

    monkeypatch.setattr(encoder, "iter_gray_frames", recorded)
    return starts


def _watch(path: Path, **options) -> frame_freeze.FrameCheck:
    return asyncio.run(frame_freeze.check_frames(str(path), document=DOCUMENT, width=W, height=H, fps=FPS, **options))


@needs_ffmpeg
def test_a_60_s_video_ending_on_a_near_static_outro_is_watched_in_time_and_its_card_is_by_design(outro_video):
    began = time.monotonic()
    check = _watch(outro_video)
    assert time.monotonic() - began < 10.0
    assert check.summary["frames"] == 60 * FPS and "unjudged" not in check.summary
    # The clip's own freeze is found; nothing in the closing card, its crossfade in or the video's fade out.
    assert [(f.scene_id, round(f.at), round(f.end or 0)) for f in check.findings] == [("s01", 18, 23)]
    held = check.summary["by_design"]
    assert [(item["scene"], item["type"], item["because"]) for item in held] == [("s02", "outro", "a closing card holds still by design")]
    assert held[0]["seconds"] > 1.0
    answer = render_answer.of_checks({"frames": check.as_summary()})
    assert [(item["scene"], item["because"]) for item in answer.by_design] == [("s02", "a closing card holds still by design")]


@needs_ffmpeg
def test_a_resume_injected_after_the_encode_keeps_every_frame_and_watches_on_from_where_it_was(outro_video, tmp_path, monkeypatch):
    workdir = tmp_path / "xtm-render-r"
    workdir.mkdir()
    video = workdir / "video.mp4"
    video.write_bytes(outro_video.read_bytes())
    # The frames encoded: a resume keeps all of them, whatever the segments a stall finds.
    compositor.keep_encoded(workdir, "the drawing", str(video), 60 * FPS, {"k": {"frames": {"12": {"ok": True}}}})
    assert compositor.frames_kept(workdir) == 60 * FPS
    assert render_process._kept_work(workdir) == f"{60 * FPS} frames"
    kept = compositor.encoded_video(workdir, "the drawing")
    assert kept is not None and kept[0] == str(video) and kept[1]["k"]["frames"] == {12: {"ok": True}}

    whole = _watch(outro_video)
    starts = _starts(monkeypatch)
    checkpoint = frame_freeze.Checkpoint(workdir / "frame-watch.json", "the drawing|30")

    class StoppedError(Exception):
        pass

    async def stalls_at_20_s(seconds: float, _total: float, _window) -> None:
        if seconds >= 20.0:
            raise StoppedError

    with pytest.raises(StoppedError):
        _watch(video, checkpoint=checkpoint, watched=stalls_at_20_s)
    resumed = _watch(video, checkpoint=checkpoint)
    # Resumed from the start of the run under way (the clip froze at 18 s), never from its first frame.
    assert starts[0] == 0.0 and starts[1] == pytest.approx(18.0, abs=0.05)
    assert resumed.summary["resumed_at"] == pytest.approx(18.0) and resumed.summary["frames"] == 60 * FPS
    assert [(f.scene_id, round(f.at, 1), round(f.end or 0, 1)) for f in resumed.findings] == [
        (f.scene_id, round(f.at, 1), round(f.end or 0, 1)) for f in whole.findings
    ]
    assert [item["scene"] for item in resumed.summary["by_design"]] == ["s02"]
    # Another drawing's video is never kept: forgotten, and nothing counted as kept.
    assert compositor.encoded_video(workdir, "another drawing") is None and compositor.frames_kept(workdir) == 0


def _moving(count: int, start: int = 0) -> list[bytes]:
    rng = np.random.default_rng(1)
    picture = np.kron(rng.uniform(0, 255, (18, 32)), np.ones((6, 6))).astype(np.uint8)
    return [np.roll(picture, start + i, axis=1).tobytes() for i in range(count)]


def test_a_window_whose_frames_stop_coming_is_left_unjudged_and_the_watch_goes_on(monkeypatch):
    frames = _moving(90)
    starts: list[float] = []

    async def stream(_path, *, width, height, fps, start=0.0):
        starts.append(start)
        first = round(start * fps)
        for index in range(first, len(frames)):
            if not first and index == 40:
                await asyncio.sleep(3600)  # the decoder stops delivering mid-window
            yield frames[index]

    told: list[str | None] = []

    async def watched(_seconds, _total, window) -> None:
        told.append(window.where() if window is not None else None)

    monkeypatch.setattr(encoder, "iter_gray_frames", stream)
    monkeypatch.setattr(frame_freeze, "WINDOW_SECONDS", 1.0)
    # The window's judging deadline on a frozen clock: a loaded runner never leaves a window unjudged here,
    # while the scripted stall is still cut after 0.2 s by the per-frame wait on the event loop's own clock.
    monkeypatch.setattr(frame_freeze, "_now", lambda: 0.0)
    document = {"duration": 3.0, "scenes": [{"id": "a", "type": "clip", "start": 0.0, "end": 1.5}, {"id": "b", "type": "clip", "start": 1.5, "end": 3.0}]}
    began = time.monotonic()
    check = asyncio.run(
        frame_freeze.check_frames("final.mp4", document=document, width=1920, height=1080, fps=30, budget=0.2, watched=watched)
    )
    assert time.monotonic() - began < 5.0
    # Frames 30-39 judged, 40-59 left unjudged; the next decoder starts at the next window.
    assert check.summary["unjudged"] == [{"at": 1.333, "end": 2.0, "scenes": ["a", "b"], "why": "no frame decoded in 0.2 s"}]
    assert starts[0] == 0.0 and starts[1] == pytest.approx(2.0, abs=0.02) and check.summary["frames"] == 90
    assert told == [
        "0:00.00 to 0:01.00, scene `a` (clip)",
        "0:01.00 to 0:02.00, scenes `a` (clip) and `b` (clip)",
        "0:02.00 to 0:03.00, scene `b` (clip)",
        None,
    ]
    line = render_checks.check_lines({"frames": check.as_summary()})[0]
    assert line == "Picture: passed; 1 window not judged: 0:01.33 to 0:02.00 in `a`, `b` (no frame decoded in 0.2 s)."


def test_a_card_is_never_compared_and_only_its_hold_is_measured(monkeypatch):
    still = _moving(1, 50)[0]
    frames = _moving(45) + [still] * 45

    async def stream(_path, *, width, height, fps, start=0.0):
        for frame in frames:
            yield frame

    fed: list[int] = []
    real = frame_freeze.FreezeWatch.feed

    def feed(self, frame, take=None, parts=None):
        fed.append(self.count)
        return real(self, frame, take, parts)

    monkeypatch.setattr(encoder, "iter_gray_frames", stream)
    monkeypatch.setattr(frame_freeze.FreezeWatch, "feed", feed)
    document = {"duration": 3.0, "scenes": [{"id": "c", "type": "clip", "start": 0.0, "end": 1.5}, {"id": "end", "type": "outro", "start": 1.5, "end": 3.0}]}
    check = asyncio.run(frame_freeze.check_frames("final.mp4", document=document, width=1920, height=1080, fps=30))
    assert fed == list(range(45)) and check.findings == []
    assert check.summary["by_design"] == [
        {"scene": "end", "type": "outro", "at": 1.5, "end": 3.0, "seconds": 1.5, "because": "a closing card holds still by design"}
    ]


def test_what_a_resume_keeps_counts_the_scene_ranges_segments(tmp_path):
    for directory, pieces in ((tmp_path / compositor.SEGMENTS_DIR, [(0, 300)]), (tmp_path / "range-1" / compositor.SEGMENTS_DIR, [(300, 600), (600, 900)])):
        compositor.kept_segments(directory, "d")
        for first, end in pieces:
            (directory / compositor.segment_name(first, end)).write_bytes(b"x" * 64)
    # The ranges' own joined videos beside them are no segments.
    (tmp_path / "range-001.mp4").write_bytes(b"x" * 64)
    assert compositor.frames_kept(tmp_path) == 900


def test_the_window_budget_stays_inside_the_stall_window():
    assert frame_freeze.window_budget(120) == 60.0
    assert frame_freeze.window_budget(40) == 20.0
    assert frame_freeze.window_budget(0) == frame_freeze.WINDOW_BUDGET_SECONDS
