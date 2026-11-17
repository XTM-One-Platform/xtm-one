"""Issue #8721: a cut join never encodes a frame of both scenes, and the picture gate reads every cut join.

1. ``timeline.plan`` places every cut on a whole output frame, whatever the fractional length of the scene before.
2. Under motion blur no sub-frame of a frame is drawn across a cut (``frame_owners.shutter_at``).
3. ``cut_blend`` reads the frames around each cut join and raises a ``double_image`` finding, with its frame, its
   second and its scene pair, on a frame that is a mix of its neighbours; clean joins and transitions that last
   raise nothing.
"""

import asyncio
from itertools import pairwise

import numpy as np
import pytest

from app.services.video_studio import cut_blend, encoder, motion_design, render_checks, spec, timeline
from app.services.video_studio.frame_owners import FrameOwners, cut_times, first_at, shutter_at

FPS = 60


def _clips(lengths: list[float], transition: dict | None = None) -> spec.VideoSpec:
    """Statement scenes of the given lengths, joined by *transition* (a cut by default)."""
    raw = {
        "title": "Cuts",
        "voiceover": {"enabled": False},
        "default_transition": transition or {"type": "cut"},
        "scenes": [{"type": "title", "title": f"Scene {i}", "duration": length} for i, length in enumerate(lengths)],
    }
    return spec.parse_video_spec(spec.validate_video_spec(raw))


def _on_grid(seconds: float, fps: int = FPS) -> bool:
    """On a whole frame's second as the plan stores it: rounded to 4 decimals (445/60 is stored 7.4167)."""
    return abs(seconds - round(seconds * fps) / fps) <= 5e-5 + 1e-9


def test_a_cut_lands_on_a_whole_frame_whatever_the_fractional_length_before():
    plan = timeline.plan(_clips([7.42, 8.0, 8.0, 3.3]), timeline.PlanMedia(), fps=FPS, theme={})
    scenes = plan.document["scenes"]
    cuts = [scene for scene in scenes[1:] if scene["transition_in"]["type"] == "cut"]
    assert len(cuts) == len(scenes) - 1
    for before, after in pairwise(scenes):
        # One join: the scene before ends where the next starts, on the frame grid.
        assert before["end"] == after["start"]
        assert _on_grid(after["start"]), after["start"]
    # The remainder is carried, never lost: the video is as long as it was planned, to a frame.
    assert abs(plan.duration - sum(scene["end"] - scene["start"] for scene in scenes)) < 1e-6


def test_the_cut_never_moves_later_than_the_scene_before_planned():
    """Floored to the frame: the scene before never plays past its planned end (its narration, its inserts)."""
    assert timeline.cut_on_frame(7.42 + 0.0, FPS) <= 7.42
    assert timeline.cut_on_frame(7.42, FPS) == pytest.approx(445 / FPS)
    assert timeline.cut_on_frame(8.0, FPS) == pytest.approx(8.0)
    # A second already on a frame stays there, through float noise.
    assert timeline.cut_on_frame(481 / FPS, FPS) == pytest.approx(481 / FPS)


def test_a_join_already_on_a_frame_is_unchanged():
    plan = timeline.plan(_clips([3.0, 4.0, 2.5]), timeline.PlanMedia(), fps=FPS, theme={})
    assert [scene["start"] for scene in plan.document["scenes"]] == [0.0, 3.0, 7.0]


def test_a_transition_that_lasts_is_placed_as_before():
    plan = timeline.plan(_clips([7.42, 8.0], {"type": "crossfade", "duration": 0.6}), timeline.PlanMedia(), fps=FPS, theme={})
    first, second = plan.document["scenes"]
    assert first["end"] == pytest.approx(7.42)
    assert second["start"] == pytest.approx(7.42 - 0.6)


def test_no_sub_frame_of_motion_blur_is_drawn_across_a_cut():
    plan = timeline.plan(_clips([7.42, 8.0, 8.0]), timeline.PlanMedia(), fps=FPS, theme={})
    document = plan.document
    cuts = cut_times(document)
    assert len(cuts) == 2
    offsets = motion_design.blur_offsets(motion_design.BLUR_SAMPLES)
    for cut in cuts:
        join = first_at(cut, FPS)
        for frame in range(join - 2, join + 2):
            for offset in offsets:
                at = shutter_at(frame, frame + offset, FPS, cuts)
                # The page paints by ``T >= start``: the sub-frame is on its frame's side of every cut.
                assert (at / FPS >= cut) == (frame / FPS >= cut), (cut, frame, offset)


def test_a_cut_off_the_frame_grid_used_to_put_a_shutter_across_it():
    """Before the fix: a cut at 7.42 s at 60 fps fell 0.2 frame past frame 445, inside frame 445's shutter."""
    cut = 7.42
    assert shutter_at(445, 445 + 0.25, FPS, (cut,)) == 445.0
    assert shutter_at(445, 445 - 0.25, FPS, (cut,)) == pytest.approx(445 - 0.25)


def _picture(seed: int, size=(64, 36)) -> np.ndarray:
    """A grey picture with edges of its own: bars and blocks placed by *seed*."""
    rng = np.random.default_rng(seed)
    picture = np.full((size[1], size[0]), 40.0, dtype=np.float32)
    for _ in range(8):
        x, y = int(rng.integers(0, size[0] - 12)), int(rng.integers(0, size[1] - 8))
        picture[y : y + 8, x : x + 12] = float(rng.integers(150, 250))
    return picture


def _document(cut_frame: int, *, blended: bool = False) -> dict:
    start = cut_frame / FPS
    duration = start + 2.0
    return {
        "fps": FPS,
        "frames": int(duration * FPS),
        "duration": duration,
        "scenes": [
            {"id": "a", "type": "title", "start": 0.0, "end": start, "transition_in": {"type": "cut", "duration": 0.0}},
            {
                "id": "b",
                "type": "title",
                "start": start - (0.5 if blended else 0.0),
                "end": duration,
                "transition_in": {"type": "crossfade", "duration": 0.5} if blended else {"type": "cut", "duration": 0.0},
            },
        ],
    }


def test_the_gate_flags_a_blended_frame_at_a_cut_with_its_time_and_scene_pair():
    a, b = _picture(1), _picture(2)
    join = cut_blend.cut_joins(FrameOwners.of(_document(445)))[0]
    assert (join.frame, join.before, join.after) == (445, "a", "b")
    frames = [a, a, (a + b) / 2, b]
    finding = cut_blend.judge(join, frames, FPS)
    assert finding is not None
    assert finding.kind == render_checks.DOUBLE_IMAGE
    assert finding.data["frame"] == 445 and finding.data["scenes"] == ["a", "b"] and finding.data["join"] == "cut"
    assert finding.at == pytest.approx(445 / FPS, abs=1e-3)
    assert finding.data["blend"] == pytest.approx(0.5, abs=0.05)
    assert "7.42" in finding.message or render_checks.clock(445 / FPS) in finding.message


def test_the_gate_flags_a_blended_last_frame_before_the_cut():
    a, b = _picture(3), _picture(4)
    join = cut_blend.cut_joins(FrameOwners.of(_document(300)))[0]
    finding = cut_blend.judge(join, [a, 0.7 * a + 0.3 * b, b, b], FPS)
    assert finding is not None and finding.data["frame"] == 299 and finding.scene_id == "a"


def test_a_clean_cut_raises_nothing():
    a, b = _picture(5), _picture(6)
    join = cut_blend.cut_joins(FrameOwners.of(_document(445)))[0]
    assert cut_blend.judge(join, [a, a, b, b], FPS) is None
    # Encoder noise on the frame after the cut is no mix.
    noisy = b + np.random.default_rng(7).normal(0, 2, b.shape).astype(np.float32)
    assert cut_blend.judge(join, [a, a, noisy, b], FPS) is None


def test_a_transition_that_lasts_is_never_read_by_the_cut_gate():
    assert cut_blend.cut_joins(FrameOwners.of(_document(445, blended=True))) == []


def test_a_preview_reads_only_the_joins_its_mp4_plays():
    owners = FrameOwners.of(_document(445))
    assert cut_blend.cut_joins(owners, first=0, end=400) == []
    assert [j.frame for j in cut_blend.cut_joins(owners, first=400, end=500)] == [445]


def test_check_cuts_reads_the_frames_around_each_join_from_the_mp4(monkeypatch, tmp_path):
    a, b = _picture(8), _picture(9)
    asked = []

    async def gray_frames(video_path, *, start, seconds, fps, size, out_path):
        asked.append((round(start * fps), round(seconds * fps), fps))
        return [frame.astype(np.uint8).tobytes() for frame in (a, a, (a + b) / 2, b)]

    monkeypatch.setattr(encoder, "gray_frames", gray_frames)
    found = asyncio.run(cut_blend.check_cuts(str(tmp_path / "v.mp4"), _document(445), size=(64, 36)))
    assert asked == [(443, 4, FPS)]
    assert len(found) == 1 and found[0].data["frame"] == 445
    # A scene preview starting at 7 s: the finding timed from its own start.
    asked.clear()
    found = asyncio.run(cut_blend.check_cuts(str(tmp_path / "v.mp4"), _document(445), size=(64, 36), offset=7.0, seconds=2.0))
    assert asked == [(443 - 420, 4, FPS)]
    assert found[0].at == pytest.approx(445 / FPS - 7.0, abs=1e-3)


# Issue #8935: what the gate read is stored and said, a clean pass told from a gate that never ran.


def _three_scenes(lengths=(7.42, 8.0, 8.0), transition: dict | None = None) -> dict:
    return timeline.plan(_clips(list(lengths), transition), timeline.PlanMedia(), fps=FPS, theme={}).document


def test_a_clean_pass_reports_the_joins_it_read(monkeypatch, tmp_path):
    a, b = _picture(10), _picture(11)

    async def gray_frames(video_path, *, start, seconds, fps, size, out_path):
        return [frame.astype(np.uint8).tobytes() for frame in (a, a, b, b)]

    monkeypatch.setattr(encoder, "gray_frames", gray_frames)
    document = _three_scenes()
    check = asyncio.run(cut_blend.read_cuts(str(tmp_path / "v.mp4"), document, size=(64, 36)))
    assert check.findings == []
    summary = check.as_summary()
    assert summary["status"] == render_checks.PASSED and summary["joins_read"] == 2 and summary["fps"] == FPS
    expected = [join.frame for join in cut_blend.cut_joins(FrameOwners.of(document))]
    assert len(expected) == 2 and [join["frame"] for join in summary["joins"]] == expected
    assert summary["joins"][0]["scenes"][1] == summary["joins"][1]["scenes"][0]
    assert summary["joins"][0]["seconds"] == pytest.approx(expected[0] / FPS, abs=1e-3)
    line = cut_blend.summary_line(summary)
    assert line == "Cuts: 2 joins read at 60 fps on the final MP4, no frame of two scenes."
    # Stored on the checks, read back, and said among the render's facts.
    checks = render_checks.Checks(cuts=summary)
    stored = checks.as_dict()
    assert stored["cuts"]["joins_read"] == 2
    assert render_checks.Checks.from_dict(stored).cuts == summary
    assert line in render_checks.check_lines(stored)
    assert line in render_checks.format_checks(stored)


def test_a_video_with_only_crossfades_reports_no_cut_join(monkeypatch, tmp_path):
    async def gray_frames(*_args, **_kwargs):  # pragma: no cover - never asked
        raise AssertionError("no frame is read without a cut join")

    monkeypatch.setattr(encoder, "gray_frames", gray_frames)
    document = _three_scenes(transition={"type": "crossfade", "duration": 0.6})
    summary = asyncio.run(cut_blend.read_cuts(str(tmp_path / "v.mp4"), document, size=(64, 36))).as_summary()
    assert summary["status"] == render_checks.PASSED and summary["joins_read"] == 0
    assert cut_blend.summary_line(summary) == "Cuts: no cut join on the final MP4."


def test_a_gate_that_could_not_run_says_not_read():
    cuts = cut_blend.not_read("the check could not run (ffmpeg exited 1)")
    assert cuts["status"] == render_checks.SKIPPED
    assert cut_blend.summary_line(cuts) == "Cuts: not read (the check could not run (ffmpeg exited 1))."
    assert "Cuts: not read" in "\n".join(render_checks.check_lines({"status": "passed", "cuts": cuts}))


def test_a_double_image_is_counted_in_the_cuts_line(monkeypatch, tmp_path):
    a, b = _picture(12), _picture(13)

    async def gray_frames(video_path, *, start, seconds, fps, size, out_path):
        return [frame.astype(np.uint8).tobytes() for frame in (a, a, (a + b) / 2, b)]

    monkeypatch.setattr(encoder, "gray_frames", gray_frames)
    summary = asyncio.run(cut_blend.read_cuts(str(tmp_path / "v.mp4"), _document(445), size=(64, 36))).as_summary()
    assert summary["status"] == render_checks.FINDINGS and summary["findings"] == 1
    assert cut_blend.summary_line(summary) == (
        "Cuts: 1 join read at 60 fps on the final MP4, 1 frame of two scenes (listed below)."
    )


def test_a_join_too_close_to_the_end_of_a_preview_is_said_not_read(monkeypatch, tmp_path):
    async def gray_frames(*_args, **_kwargs):  # pragma: no cover - never asked
        raise AssertionError("a join the MP4 cuts off is never read")

    monkeypatch.setattr(encoder, "gray_frames", gray_frames)
    # A preview from 6 s ending one frame after the join: the frame after the first of the scene coming in is missing.
    check = asyncio.run(
        cut_blend.read_cuts(str(tmp_path / "v.mp4"), _document(445), size=(64, 36), offset=6.0, seconds=(446 - 360) / FPS)
    )
    assert check.joins == [] and [s["frame"] for s in check.skipped] == [445]
    assert "1 not read (too close to the start or end of the MP4)" in cut_blend.summary_line(check.as_summary())


def test_the_quality_report_moved_with_the_cuts_fact():
    from app.services.video_studio import quality

    assert quality.QUALITY_REPORT_VERSION >= 50
