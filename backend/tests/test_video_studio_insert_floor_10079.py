"""A recording made for a screen insert held to the insert capture floor when recorded and when placed (#10079).

Synthetic values only - no browser, no database: the decision a recorded take gets (``insert_floor.judge``), the size
a refusal names (``insert_floor.sustained_size``), the mark a take carries (``insert_floor.mark``) and the save note
naming a screen insert that plays a take under the floor (``insert_floor.save_notes``).
"""

from __future__ import annotations

import asyncio
import uuid
from types import SimpleNamespace

import pytest

from app.services.video_studio import insert_floor, render_checks
from app.services.video_studio.perceptual import MIN_INSERT_PAINTED_FPS

TAKE = "00000000-0000-0000-0000-00000000f079"
VIDEO = "00000000-0000-0000-0000-00000000a079"


def _marked(**extra):
    return insert_floor.mark_of({**insert_floor.mark(video_id=VIDEO, scene="s1", insert=0, quality="ultra"), **extra})


def test_a_take_not_made_for_an_insert_is_never_judged():
    assert insert_floor.mark_of(None) is None
    assert insert_floor.mark_of({"keyframes": "x"}) is None
    assert insert_floor.judge(None, captured_frames=50, duration=10.0, viewport=(1600, 900), pixel_ratio=2) is None


def test_a_take_over_the_floor_on_its_first_take_is_ready_as_before():
    assert (
        insert_floor.judge(_marked(), captured_frames=300, duration=10.0, viewport=(1600, 900), pixel_ratio=2) is None
    )
    # Its rate unknown: nothing decided either.
    assert insert_floor.judge(_marked(), captured_frames=0, duration=10.0, viewport=(1600, 900), pixel_ratio=2) is None


def test_a_5_fps_take_is_recorded_again_keeping_its_first_rate_and_worker():
    judged = insert_floor.judge(
        _marked(), captured_frames=54, duration=10.0, viewport=(1600, 900), pixel_ratio=2, worker="w1"
    )
    assert judged.kind == insert_floor.AGAIN and judged.fps == pytest.approx(5.4)
    assert judged.stage().startswith("Recording again: its first take painted 5.4 fps, under the 15")
    assert len(judged.stage()) <= 200
    capture = insert_floor.retry_capture(_marked(), judged)
    # Its first take's keyframes and resume point are not carried: the second take records from its script.
    assert set(capture) == {insert_floor.MARK_KEY, insert_floor.RETRY_KEY}
    assert capture[insert_floor.RETRY_KEY] == {"fps": 5.4, "worker": "w1"}
    assert capture[insert_floor.MARK_KEY]["video_id"] == VIDEO


def test_a_second_take_still_under_the_floor_fails_naming_both_rates_and_the_size_to_record_at():
    marked = _marked(**{insert_floor.RETRY_KEY: {"fps": 5.4, "worker": "w1"}})
    judged = insert_floor.judge(
        marked, captured_frames=70, duration=10.0, viewport=(1600, 900), pixel_ratio=2, worker="w2"
    )
    assert judged.kind == insert_floor.FAILED
    said = judged.words()
    assert said.startswith(
        "captured 7 fps, under the 15 an insert source needs (recorded twice; its first take painted 5.4 fps"
    )
    assert "the first on worker w1, this one on worker w2" in said
    assert "would sustain it on this worker" in said and "or a GPU worker at its size" in said


def test_a_second_take_over_the_floor_is_ready_and_says_it_was_recorded_twice():
    marked = _marked(**{insert_floor.RETRY_KEY: {"fps": 5.4, "worker": "w1"}})
    judged = insert_floor.judge(
        marked, captured_frames=290, duration=10.0, viewport=(1600, 900), pixel_ratio=2, worker="w1"
    )
    assert judged.kind == insert_floor.PASSED_AGAIN
    assert judged.words() == (
        "Recorded twice for its screen insert: its first take painted 5.4 fps, under the 15 an insert source needs; "
        "this one painted 29 fps, both on worker w1."
    )


def test_the_size_named_keeps_the_window_at_a_lower_pixel_ratio_first_else_shrinks_it():
    # 10 fps at 1600x900 x2: x1.5 paints 56% of the pixels, under the 10/15 x 0.85 = 57% allowed.
    assert insert_floor.sustained_size((1600, 900), 2.0, 10.0) == ((1600, 900), 1.5)
    # 5.4 fps: x1.5 (56%) is over the 31% allowed, x1 (25%) is under it.
    assert insert_floor.sustained_size((1600, 900), 2.0, 5.4) == ((1600, 900), 1.0)
    # 2 fps at x1: a smaller window at pixel ratio 1, its aspect kept.
    (width, height), ratio = insert_floor.sustained_size((1600, 900), 1.0, 2.0)
    assert ratio == 1.0 and width < 1600 and width % 8 == 0 and abs(width / height - 16 / 9) < 0.02
    assert insert_floor.sustained_size((0, 0), 2.0, 5.0) is None


def test_the_floor_is_the_one_the_preview_lint_holds_to():
    assert MIN_INSERT_PAINTED_FPS == 15.0


class _Db:
    def __init__(self, rows):
        self.rows = rows

    async def get(self, _model, uid):
        return self.rows.get(str(uid))


def _spec(take_id: str):
    part = SimpleNamespace(kind="take", ref=take_id, index=0, pane=None)
    return {"insert/s1.0": part}


def test_a_save_names_an_insert_playing_a_take_under_the_floor_with_the_call_to_record_it_again(monkeypatch):
    from app.services.video_studio import timeline

    monkeypatch.setattr(timeline, "insert_sources", lambda spec: _spec(TAKE))

    async def quality(*_args, **_kwargs):
        return "ultra"

    monkeypatch.setattr(render_checks, "target_quality", quality)
    slow = SimpleNamespace(captured_frames=54, duration_ms=10_000)
    project = SimpleNamespace(id=uuid.UUID(VIDEO))
    [note] = asyncio.run(insert_floor.save_notes(_Db({TAKE: slow}), object(), project))
    assert note.startswith(f"Scene s1: its screen insert 0 plays take `{TAKE}`, painted at 5.4 fps, under the 15")
    assert "(insert_capture_floor)" in note
    assert (
        f'record_browser_demo(from_recording_id: "{TAKE}", target_render: "ultra", video_id: "{VIDEO}", scene: "s1", '
        "insert: 0)"
    ) in note
    # Over the floor, or unread: nothing said.
    fast = SimpleNamespace(captured_frames=290, duration_ms=10_000)
    assert asyncio.run(insert_floor.save_notes(_Db({TAKE: fast}), object(), project)) == []
    assert asyncio.run(insert_floor.save_notes(_Db({}), object(), project)) == []


def test_a_save_note_that_cannot_be_read_is_not_given(monkeypatch):
    from app.services.video_studio import timeline

    def broken(_spec):
        raise RuntimeError("unreadable")

    monkeypatch.setattr(timeline, "insert_sources", broken)
    assert asyncio.run(insert_floor.save_notes(_Db({}), object(), None)) == []
