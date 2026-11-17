"""A take's pacing pass is checkpointed and resumed by the next worker, never run again from zero (#10030).

A recording finished from its keyframes after a rollout (#9812) ran its pacing pass again from its first scroll:
under deploys a few minutes apart a long 60 fps take never got past "Pacing its scrolls". Proven here on the
synthetic take of ``test_video_studio_scroll_pacing_6408``: the steps a pass hands over, packed into a chunk and
unpacked on another worker, are reused, the frames byte for byte the same as an uninterrupted pass's, the step it
resumed at said; a chunk of other keyframes or another rate is never reused; and a take queued again to be finished
from its keyframes keeps its progress.
"""

from __future__ import annotations

import asyncio
import copy
import uuid

from app.services.video_studio import jobs, pacing_checkpoint, runners, scroll_pacing
from tests.test_video_studio_scroll_pacing_6408 import RATIO, _page, _take


def _paced(tmp_path, *, done=None):
    page = _page()
    frames, events = _take(tmp_path, page)
    steps: dict[str, dict] = {}
    paced = scroll_pacing.pace(
        frames, events, workdir=str(tmp_path), fps=30, pixel_ratio=RATIO, done=done, on_step=steps.__setitem__
    )
    return frames, events, paced, steps


def test_a_pass_resumed_from_its_checkpoint_reuses_its_steps_and_draws_the_same_frames(tmp_path):
    first, second = tmp_path / "first", tmp_path / "second"
    first.mkdir()
    second.mkdir()
    frames, events, paced, steps = _paced(first)
    assert set(steps) == {pacing_checkpoint.scroll_key(0), pacing_checkpoint.TRANSITIONS}
    assert paced.resumed_at is None and paced.reused == []
    # The first worker stored its first scroll, then was stopped: the next worker unpacks the chunk and resumes.
    chunk = tmp_path / "chunk.zip"
    packed = pacing_checkpoint.pack_chunk(
        chunk, {"s0": steps["s0"]}, workdir=first, keyframes=[name for _t, name in frames]
    )
    assert packed == steps["s0"]["kept"]["frames"]
    done = pacing_checkpoint.unpack_chunk(chunk, second)
    _frames, events_again, resumed, steps_again = _paced(second, done=done)
    assert resumed.reused == ["s0"] and resumed.resumed_at == 2
    assert "s0" not in steps_again and pacing_checkpoint.TRANSITIONS in steps_again
    # The same frames, byte for byte, and the same scroll kept by the take.
    assert resumed.frames == paced.frames
    for _t, name in paced.frames:
        assert (first / name).read_bytes() == (second / name).read_bytes()
    assert events_again[0]["scrolls"] == events[0]["scrolls"]
    assert events_again[0]["fps"] == events[0]["fps"] == 30.0
    note = pacing_checkpoint.resumed_note(resumed.resumed_at, len(resumed.reused))
    assert note.startswith("Pacing resumed at step 2")


def test_a_step_of_another_scroll_or_with_frames_missing_is_paced_again(tmp_path):
    first, second = tmp_path / "first", tmp_path / "second"
    first.mkdir()
    second.mkdir()
    _frames, _events, paced, steps = _paced(first)
    moved = copy.deepcopy(steps["s0"])
    moved["span"] = [moved["span"][0] + 0.5, moved["span"][1]]
    _f, _e, again, _s = _paced(second, done={"s0": moved})
    assert again.reused == [] and again.resumed_at is None and again.frames == paced.frames
    # Its frames not restored on this worker: never reused.
    third = tmp_path / "third"
    third.mkdir()
    _f, _e, again, _s = _paced(third, done={"s0": steps["s0"]})
    assert again.reused == [] and again.frames == paced.frames


def test_a_checkpoint_is_bound_to_its_keyframes_its_rate_and_its_ratio():
    keyframes = uuid.uuid4()
    point = pacing_checkpoint.point(keyframes=keyframes, fps=60, pixel_ratio=2.0, frames=120, chunks=[uuid.uuid4()])
    capture = {pacing_checkpoint.CAPTURE_KEY: point}
    assert pacing_checkpoint.pacing_point(capture, keyframes=keyframes) == point
    assert pacing_checkpoint.pacing_point(capture, keyframes=uuid.uuid4()) is None
    assert pacing_checkpoint.pacing_point({}, keyframes=keyframes) is None
    assert pacing_checkpoint.matches(point, fps=60, pixel_ratio=2.0, frames=120)
    assert not pacing_checkpoint.matches(point, fps=30, pixel_ratio=2.0, frames=120)
    assert not pacing_checkpoint.matches(point, fps=60, pixel_ratio=1.0, frames=120)
    assert not pacing_checkpoint.matches(point, fps=60, pixel_ratio=2.0, frames=119)


def test_a_chunk_unreadable_or_of_another_version_holds_no_step(tmp_path):
    bad = tmp_path / "bad.zip"
    bad.write_bytes(b"not a zip")
    assert pacing_checkpoint.unpack_chunk(bad, tmp_path) == {}
    other = tmp_path / "other.zip"
    import json
    import zipfile

    with zipfile.ZipFile(other, "w") as archive:
        archive.writestr(pacing_checkpoint.MANIFEST, json.dumps({"version": 99, "steps": {"s0": {}}}))
    assert pacing_checkpoint.unpack_chunk(other, tmp_path) == {}


def test_resumed_at_names_the_first_scroll_paced_again():
    assert pacing_checkpoint.resumed_at(None, []) is None
    assert pacing_checkpoint.resumed_at({"s0": {}}, []) is None
    assert pacing_checkpoint.resumed_at({"s0": {}, "s1": {}}, ["s0", "s1"]) == 3
    assert pacing_checkpoint.resumed_at({"s0": {}, "s1": {}}, ["s0", "s1", pacing_checkpoint.TRANSITIONS]) == 3


def test_a_take_queued_to_be_finished_from_its_keyframes_keeps_its_progress(monkeypatch):
    written: list[dict] = []

    async def mark_if_open(_kind, _job_id, **changes):
        written.append(changes)
        return True

    monkeypatch.setattr(jobs, "mark_if_open", mark_if_open)
    asyncio.run(runners._queued_after_stop(str(uuid.uuid4()), runners.TAKE_RESUME_STAGE))
    asyncio.run(runners._queued_after_stop(str(uuid.uuid4()), runners.TAKE_STOPPED_STAGE))
    resumed, recorded = written
    assert "progress" not in resumed and resumed["stage"] == runners.TAKE_RESUME_STAGE
    assert recorded["progress"] == 0
