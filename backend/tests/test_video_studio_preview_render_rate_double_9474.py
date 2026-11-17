"""Issue #9474 slice 2: a scene preview reads its takes' synthesised frames at the render's frame rate."""

from __future__ import annotations

from types import SimpleNamespace

import numpy as np
import pytest

from app.services.video_studio import double_take


def _page(shift: int) -> np.ndarray:
    picture = np.zeros((60, 108), dtype=np.float32)
    for row in range(5, 55, 10):
        picture[row : row + 3, 10 + shift : 90 + shift] = 255.0
    return picture


def test_shown_frames_follow_the_take_frame_at_each_render_frame_centre() -> None:
    scene = {"start": 1.0, "end": 2.0, "src_fps": 30, "src_frames": 100, "time_map": [[0.0, 0.0], [1.0, 1.0]]}
    shown = double_take.shown_frames(scene, 60.0, [(0.5, 0.6)])
    # Render frames 60..119 show take seconds 0..1; the span 0.5-0.6 widened by one take frame on each side.
    assert shown and all(60 <= frame < 120 for frame, _take in shown)
    assert all(14 <= take <= 19 for _frame, take in shown)
    # 60 fps over a 30 fps take: each take frame shown on two render frames.
    takes = [take for _frame, take in shown]
    assert len(takes) > len(set(takes))


def test_stepping_spans_read_the_take_capture_notes() -> None:
    take = SimpleNamespace(stepping=[{"t": [1.0, 2.5]}, {"t": [3.0, 3.0]}, {"bad": True}])
    assert double_take.stepping_spans(take) == [(1.0, 2.5)]
    assert double_take.stepping_spans(SimpleNamespace()) == []


def test_paced_page_transitions_are_synthesised_spans_too() -> None:
    take = SimpleNamespace(
        stepping=[{"t": [4.0, 5.0]}],
        events=[
            {"motion": [{"t": [1.0, 1.3], "paced": True, "fps": 60.0}, {"t": [2.0, 2.2], "paced": False}]},
            {"motion": "bad"},
            {"scrolls": [{"t": [3.0, 3.5]}]},
            "bad",
        ],
    )
    assert double_take.paced_spans(take) == [(1.0, 1.3)]
    assert double_take.synthesised_spans(take) == [(1.0, 1.3), (4.0, 5.0)]
    assert double_take.paced_spans(SimpleNamespace()) == []


@pytest.mark.asyncio
async def test_a_page_transition_blend_the_preview_steps_over_is_found_at_the_render_rate(monkeypatch) -> None:
    """A 60 fps take whose dialog opening was paced from two keyframes (no stepping motion): the 30 fps preview
    shows take frames 0, 2, 4 only; the render at 60 fps shows frame 1, a 33% mix of the page before and after."""
    before, after = _page(0), _page(6)

    async def frames(path, wanted, *, src_fps, size):
        mixes = {0: before, 1: (2 * before + after) / 3, 2: after, 3: after, 4: after}
        return {n: mixes[n] for n in wanted if n in mixes}

    monkeypatch.setattr(double_take, "read_take_frames", frames)
    scene = {
        "id": "w08",
        "type": "recording",
        "start": 0.0,
        "end": 5 / 60,
        "src_fps": 60,
        "src_frames": 5,
        "time_map": [[0.0, 0.0], [5 / 60, 5 / 60]],
    }
    takes = {"w08": SimpleNamespace(stepping=[], events=[{"motion": [{"t": [0.0, 2 / 60], "paced": True}]}])}
    sources = {"take/w08": SimpleNamespace(path="w08.mp4", fps=60.0)}
    found, summary = await double_take.findings(
        {"scenes": [scene]}, sources=sources, takes=takes, render_fps=60.0, span=(0.0, 5 / 60)
    )
    assert summary["scenes"] == ["w08"]
    assert [f.data["take_frame"] for f in found] == [1]
    assert 0.25 < found[0].data["blend"] < 0.45


def test_runs_split_where_render_frames_stop_following() -> None:
    assert double_take.runs_of([(1, 0), (2, 0), (5, 1), (6, 2)]) == [[(1, 0), (2, 0)], [(5, 1), (6, 2)]]


def test_a_synthesised_blend_between_two_painted_frames_is_found() -> None:
    one, other = _page(0), _page(6)
    pictures = {0: one, 1: 0.5 * one + 0.5 * other, 2: other}
    run = [(10, 0), (11, 0), (12, 1), (13, 1), (14, 2), (15, 2)]
    hits = double_take.judge(run, pictures)
    assert [(frame, take) for frame, take, _b, _s in hits] == [(12, 1)]
    assert 0.4 < hits[0][2] < 0.6


def test_clean_steps_raise_nothing() -> None:
    pictures = {0: _page(0), 1: _page(6), 2: _page(12)}
    assert double_take.judge([(1, 0), (2, 1), (3, 2)], pictures) == []


@pytest.mark.asyncio
async def test_findings_are_timed_in_the_preview_and_skip_scenes_without_synthesised_spans(monkeypatch) -> None:
    one, other = _page(0), _page(6)

    async def frames(path, wanted, *, src_fps, size):
        return {0: one, 1: 0.5 * one + 0.5 * other, 2: other}

    monkeypatch.setattr(double_take, "read_take_frames", frames)
    document = {
        "scenes": [
            {
                "id": "a",
                "type": "recording",
                "start": 2.0,
                "end": 2.1,
                "src_fps": 30,
                "src_frames": 3,
                "time_map": [[0.0, 0.0], [0.1, 0.1]],
            },
            {"id": "b", "type": "recording", "start": 2.1, "end": 3.0, "src_fps": 30, "src_frames": 3},
        ]
    }
    sources = {"take/a": SimpleNamespace(path="a.mp4", fps=30.0), "take/b": SimpleNamespace(path="b.mp4", fps=30.0)}
    takes = {"a": SimpleNamespace(stepping=[{"t": [0.0, 0.1]}]), "b": SimpleNamespace(stepping=[])}
    found, summary = await double_take.findings(
        document, sources=sources, takes=takes, render_fps=60.0, offset=2.0, span=(2.0, 1.0)
    )
    assert summary["scenes"] == ["a"]
    assert len(found) == 1
    finding = found[0]
    assert finding.kind == "double_image" and finding.scene_id == "a"
    assert finding.data["take_frame"] == 1 and finding.data["render_fps"] == 60.0
    assert 0.0 <= finding.at < 0.1
