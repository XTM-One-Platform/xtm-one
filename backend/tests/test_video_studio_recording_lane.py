"""The recording lane of the render queue: a take short of its fps beside other jobs waits, never refused.

60 fps takes measured 10 to 26 fps at 3200x1800 beside two scene previews and a whole render on 8 CPUs and were refused
four times in a row. What is proven here without a database (the lane on PostgreSQL, through the engine's claims:
``test_video_studio_recording_lane_postgres.py``):

* a warm-up short of its fps beside other jobs waits (``CapturePlan.waits``): no lower pixel ratio, no smaller window,
  no refusal measured under a load that is not the worker's, and nothing it measured enters its worker shape's table;
* on an idle worker measuring 20 fps, the refusal stays as it was - the take's painted size and the largest
  window sustaining 60 fps named -, and says nothing else ran there;
* past its longest wait, a busy warm-up is decided as on an idle worker, and its refusal says the jobs beside it and
  offers the lane again;
* a waiting take measures again only once its worker's load lightened, on another worker, or past its longest wait;
* the warm-up and the capture run at the worker's priority, a scene preview's process below it;
* the queue line says its place, the recordings ahead and the jobs it waits behind; the action log, the wait.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta

import pytest

from app.services.video_studio import capture_rate, cpu_share, jobs, recording_lane, render_process, spec, take_state
from app.services.video_studio.recording_lane import Beside, Load, Wait

#: The take of the report: a 1600x900 window at pixel ratio 2 (3200x1800 painted), at 60 fps.
OWN_PIXELS = 3200 * 1800


def _sixty(**fields) -> spec.TakeScript:
    return spec.parse_take_script(
        spec.validate_take_script(
            {"steps": [{"action": "wait", "seconds": 1}], "viewport": {"width": 1600, "height": 900}, "pixel_ratio": 2, "fps": 60, **fields}
        )
    )


def _worker(at_own_size: float, calls: list | None = None):
    """A fixture worker capturing the take's own size at *at_own_size* fps: a frame's cost goes with its pixels."""

    async def measure(window: tuple[int, int], ratio: float) -> float:
        if calls is not None:
            calls.append((window, ratio))
        pixels = round(window[0] * ratio) * round(window[1] * ratio)
        return min(120.0, at_own_size * OWN_PIXELS / pixels)

    return measure


def _load(*kinds: str) -> Load:
    return recording_lane._ordered([Beside(kind, f"e{index}") for index, kind in enumerate(kinds)])


def _reads(*loads: Load | None):
    """What the warm-up reads beside it, read after read (the last one again once they run out)."""
    left = list(loads)

    async def beside():
        return left.pop(0) if len(left) > 1 else left[0]

    return beside


BUSY = _load(cpu_share.PREVIEW, cpu_share.PREVIEW, cpu_share.RENDER)


@pytest.fixture(autouse=True)
def fresh_table(monkeypatch):
    monkeypatch.setattr(capture_rate, "_TABLE", {})


# ── The warm-up beside other jobs ─────────────────────────────────────────


async def test_a_take_short_of_its_fps_beside_previews_and_a_render_waits_and_nothing_else_is_measured():
    calls: list = []
    planned = await capture_rate.choose(
        _sixty(), (1600, 900), 2.0, _worker(21.0, calls), shape=(8, 0), beside=_reads(BUSY), may_wait=True
    )

    assert planned.waits and planned.refused is None and planned.window is None and planned.fps is None
    assert planned.pixel_ratio == 2.0, "no lower pixel ratio is taken for a load that is not the worker's"
    assert calls == [((1600, 900), 2.0)], "the take's own size alone: no lowered ratio, no search of smaller sizes"
    assert planned.beside == BUSY and planned.beside.busy_words() == "busy: 2 scene previews and 1 render on this worker"
    assert planned.measured["fps"] == 21.0 and planned.measured["size"] == [3200, 1800]
    assert planned.measured["beside"]["said"] == "2 scene previews and 1 render"
    assert capture_rate.measured_table((8, 0)) == {}, "a rate measured beside other jobs never stands for the worker shape"


async def test_a_job_that_starts_during_the_warm_up_counts_as_beside_it():
    preview = _load(cpu_share.PREVIEW)
    planned = await capture_rate.choose(
        _sixty(), (1600, 900), 2.0, _worker(21.0), shape=(8, 0), beside=_reads(Load(), preview), may_wait=True
    )
    assert planned.waits and planned.beside == preview


async def test_a_take_its_busy_worker_still_captures_records_as_asked_saying_what_ran_beside_it():
    planned = await capture_rate.choose(
        _sixty(), (1600, 900), 2.0, _worker(58.0), shape=(8, 0), beside=_reads(BUSY), may_wait=True
    )
    assert not planned.waits and planned.refused is None and planned.pixel_ratio == 2.0
    assert planned.record()["preflight"]["beside"]["said"] == "2 scene previews and 1 render"


async def test_an_idle_worker_measuring_20_fps_still_refuses_naming_the_window_sizes():
    calls: list = []
    planned = await capture_rate.choose(
        _sixty(), (1600, 900), 2.0, _worker(20.0, calls), shape=(8, 0), beside=_reads(Load()), may_wait=True
    )

    assert not planned.waits and planned.refused is not None and planned.window is None
    assert planned.refused.startswith(
        "Refused before recording: this worker captures 3200x1800 frames at 20 fps (36 fps at pixel ratio 1.5), "
        "under the take's 60 fps, so its animations would step; the largest size measured sustaining 60 fps on this "
        "worker (8 CPUs) is a "
    )
    named = re.search(r"is a (\d+)x(\d+) window at pixel ratio 1\.5 \((\d+)x(\d+) painted, (\d+) fps measured\)", planned.refused)
    assert named, planned.refused
    assert int(named[5]) >= 60 and int(named[3]) * int(named[4]) <= OWN_PIXELS * 20 / 60
    assert "fps 30 at the requested size would not hold either (20 fps measured there)" in planned.refused
    assert planned.refused.endswith(" No other Video Studio job ran on this worker while it measured.")
    assert "busy" not in planned.refused and len(calls) > 2
    # Measured on an idle worker: what it measured stands for the worker shape.
    assert capture_rate.measured_table((8, 0))[(3200, 1800)] == 20.0


async def test_a_refusal_with_no_load_read_keeps_its_words():
    planned = await capture_rate.choose(_sixty(), (1600, 900), 2.0, _worker(20.0), shape=(8, 0))
    assert planned.refused.endswith(" The worker may be busy: recording again later may sustain it.")


async def test_past_its_longest_wait_a_busy_take_is_decided_and_its_refusal_says_the_jobs_and_offers_the_lane():
    planned = await capture_rate.choose(
        _sixty(), (1600, 900), 2.0, _worker(21.0), shape=(8, 0), beside=_reads(BUSY), may_wait=False
    )
    assert not planned.waits and planned.refused is not None
    assert planned.refused.endswith(
        " It measured beside other jobs (busy: 2 scene previews and 1 render on this worker): record it again to queue "
        "it in the recording lane, where it starts by itself once their load allows 60 fps."
    )
    assert capture_rate.measured_table((8, 0)) == {}


# ── When a waiting take measures again ────────────────────────────────────


def _wait(beside: Load, *, worker: str = "w1", since: datetime | None = None) -> Wait:
    return Wait(since or datetime.now(UTC), worker, beside, 21.0, (3200, 1800), 60.0)


def test_a_waiting_take_measures_again_only_once_its_workers_load_lightened():
    now = datetime.now(UTC)
    waited = _wait(BUSY)
    # The same jobs, or as heavy ones: it waits again, nothing measured.
    assert recording_lane.held(waited, "w1", BUSY, now)
    assert recording_lane.held(waited, "w1", _load(cpu_share.PREVIEW, cpu_share.RENDER, cpu_share.RENDER), now)
    # A preview ended, the render ended, the worker is idle: it measures again.
    assert not recording_lane.held(waited, "w1", _load(cpu_share.PREVIEW, cpu_share.RENDER), now)
    assert not recording_lane.held(waited, "w1", _load(cpu_share.PREVIEW, cpu_share.PREVIEW), now)
    assert not recording_lane.held(waited, "w1", Load(), now)
    # Another worker's load was never measured: it measures there.
    assert not recording_lane.held(waited, "w2", BUSY, now)
    # Past its longest wait it measures once more, decided as on an idle worker.
    old = _wait(BUSY, since=now - recording_lane.MAX_WAIT - timedelta(seconds=1))
    assert old.expired(now) and not recording_lane.held(old, "w1", BUSY, now)
    assert not recording_lane.held(None, "w1", BUSY, now)


def test_a_render_weighs_more_than_a_scene_preview_against_a_recording():
    # A preview runs below the worker's priority: a render beside the take outweighs any preview slot count.
    assert _load(cpu_share.RENDER).weight > _load(*[cpu_share.PREVIEW] * 9).weight
    assert _load(cpu_share.RECORDING).weight == _load(cpu_share.RENDER).weight


def test_the_warm_up_and_the_capture_run_above_a_scene_previews_priority():
    # The take's capture and its warm-up run in the worker's process; a preview's process is niced below them.
    assert cpu_share.nice_of(cpu_share.RECORDING) == 0 < cpu_share.nice_of(cpu_share.PREVIEW)
    assert render_process.nice_for(preview=True) == cpu_share.PREVIEW_NICE > render_process.nice_for(preview=False) == 0


async def test_the_takes_of_one_worker_measure_one_at_a_time_and_never_count_each_other():
    async with recording_lane.measuring("first"):
        assert {"first"} <= recording_lane._MEASURING
        assert recording_lane._lock().locked()
    assert "first" not in recording_lane._MEASURING and not recording_lane._lock().locked()


def test_a_wait_survives_its_executions_payload():
    waited = recording_lane.again(_wait(BUSY), _load(cpu_share.RENDER))
    assert Wait.of(waited.as_dict()) == waited
    assert Wait.of({"since": "never"}) is None and Wait.of(None) is None


# ── What it says ──────────────────────────────────────────────────────────


def test_a_waiting_takes_queue_line_says_its_place_the_recordings_ahead_and_the_jobs_it_waits_behind():
    now = datetime.now(UTC)
    waited = _wait(BUSY, since=now - timedelta(minutes=4))
    place = jobs.QueuePlace(running=0, ahead=1, per_worker=2, lane="recording", recordings=("Login demo",), wait=waited)
    assert recording_lane.line(place, now) == (
        'waiting in the recording lane, place 2 (1 recording ahead ("Login demo")): busy: 2 scene previews and 1 render '
        "on its worker; it measured 21 fps at 3200x1800 beside them, under its 60 fps, and starts by itself once its "
        "worker's load allows 60 fps (waiting 4 min so far, at most 6 h; nothing was refused)"
    )
    assert place.as_dict()["recordings_ahead"] == ["Login demo"] and place.as_dict()["waits_for_load"]["fps"] == 21.0
    # Its worker's load changed since it measured: the line says both.
    lighter = recording_lane.again(waited, _load(cpu_share.RENDER))
    moved = jobs.QueuePlace(running=0, ahead=0, per_worker=2, lane="recording", recordings=(), wait=lighter)
    assert "nothing queued ahead of it): busy: 1 render on its worker; it measured 21 fps at 3200x1800 beside 2 scene previews and 1 render" in recording_lane.line(moved, now)
    # A take waiting for a slot of the lane only.
    queued = jobs.QueuePlace(running=2, ahead=0, per_worker=2, lane="recording", recordings=())
    state = jobs.JobState(kind="take", id="t", status=jobs.STATE_QUEUED, percent=0, stage="Queued", last_progress_at=None, queue=queued)
    assert jobs.describe(state) == (
        "waiting for a worker in the recording lane, place 1: 2 recordings being made now, nothing queued ahead of it "
        "(a worker makes 2 at a time)"
    )


def test_a_take_that_waited_says_the_wait_in_its_action_log_and_its_stage():
    now = datetime.now(UTC)
    waited = _wait(BUSY, since=now - timedelta(minutes=4))
    said = waited.waited_words(now, 59.0)
    assert said == (
        "waited 4 min in the recording lane: 21 fps at 3200x1800 beside 2 scene previews and 1 render on its worker, "
        "under its 60 fps; 59 fps measured once its load allowed"
    )
    opened = {"asked": "https://app.example.test/", "carried": False, "url": "https://app.example.test/"}
    assert take_state.opened_words(opened, None, said).endswith(f"; {said}")
    assert waited.stage() == (
        "Waiting in the recording lane: busy: 2 scene previews and 1 render on its worker (21 fps measured beside 2 "
        "scene previews and 1 render, under its 60 fps)"
    )
    assert waited.refused_words(now).startswith(" It waited 4 min in the recording lane first (21 fps at 3200x1800")
