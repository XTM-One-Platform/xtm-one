"""A capture change never fails a render, the platform's failures run again once, and a wall is never told a push.

Three gaps seen on 22 Oct 2026. Begun-frame capture shipped as the default on Linux and the first production
ultra render died at 12% on ``Page.captureScreenshot``; a failed render's finding told the author to "fix what its
reason names" on a Chromium protocol error; and the save's legibility advisory kept proposing a push on two wall
inserts drawn under 0.3x, whose page a push crops. What is proven:

* the capture is probed once per worker - one begun frame within 2 s on the compositor's Chromium - and falls back
  to screenshots on that worker, the probe's result said by the pre-flight; a render process takes its worker's,
  and begun frames that fail later demote the worker;
* a render's Chromium failing it is named as such (``compositor.ChromiumError``); a failure of the platform - its
  Chromium on the last attempt, the memory ceiling, a stall, a crash on the last attempt, a lapsed lease - runs the
  render again once on its revision, no attempt spent, and one that fails again says so in its finding;
* no push is offered on a source drawn under 0.3x, every advice offers ``legibility: ambient``, and a wall's panes
  are said once together.
"""

from __future__ import annotations

import asyncio
import os
import time
from types import SimpleNamespace

import pytest

from app.core.config import settings
from app.models.video_studio import JOB_FAILED, JOB_PENDING, JOB_RUNNING
from app.services.orchestration_adapters import video_studio as adapters
from app.services.video_studio import (
    compositor,
    insert_legibility,
    jobs,
    quality,
    render_answer,
    render_preflight,
    render_process,
    render_retry,
    runners,
    timeline,
)
from app.services.video_studio.insert_legibility import Legibility, TextSize

TAKE = "00000000-0000-0000-0000-00000000a329"


# ── The capture, probed per worker ────────────────────────────────────────


@pytest.fixture
def fresh_capture(monkeypatch):
    monkeypatch.setattr(compositor, "_CAPTURE", [])
    monkeypatch.setattr(compositor, "_CAPTURE_PROBE", [])
    monkeypatch.delenv(compositor.FRAME_CAPTURE_ENV, raising=False)
    monkeypatch.setattr(settings, "video_studio_frame_capture", "begin_frame")


async def test_screenshots_asked_are_never_probed(fresh_capture, monkeypatch):
    monkeypatch.setattr(settings, "video_studio_frame_capture", "screenshot")

    async def never(*_args):
        raise AssertionError("no Chromium is started for screenshots")

    monkeypatch.setattr(compositor, "_probe_begun_frame", never)
    found = await compositor.worker_capture()
    assert (found.asked, found.mode, found.probed, found.words()) == ("screenshot", "screenshot", False, "")


async def test_a_worker_whose_chromium_gives_no_begun_frame_in_2_s_takes_screenshots(fresh_capture, monkeypatch):
    probes = []

    async def hangs(wait):
        probes.append(wait)
        raise TimeoutError

    monkeypatch.setattr(compositor, "_probe_begun_frame", hangs)
    found = await compositor.worker_capture()
    # Tried twice: 2 s, then 5 s for a Chromium starting cold.
    assert (found.asked, found.mode, found.reason) == ("begin_frame", "screenshot", "no begun frame within 2 s, then 5 s")
    # Probed once per worker: every render of it draws with what the probe settled.
    assert (await compositor.worker_capture()) is found and probes == [2.0, 5.0]
    assert await compositor.render_capture() == "screenshot"
    assert found.words() == (
        "Frames are taken by screenshot on the render worker: VIDEO_STUDIO_FRAME_CAPTURE asks for begun frames, and its "
        "probe at start had no begun frame within 2 s, then 5 s."
    )


async def test_a_worker_that_begins_a_frame_draws_begun_frames_until_they_fail_a_render(fresh_capture, monkeypatch):
    async def begins(_wait):
        return 0.21

    monkeypatch.setattr(compositor, "_probe_begun_frame", begins)
    compositor.start_capture_probe()
    found = await compositor.worker_capture()
    assert (found.mode, found.reason) == ("begin_frame", "a begun frame in 0.21 s")
    assert found.words() == "Frames are begun one by one on the render worker (its probe at start: a begun frame in 0.21 s)."
    # A render process takes what its worker handed it, never probes.
    monkeypatch.setenv(compositor.FRAME_CAPTURE_ENV, "screenshot")
    assert await compositor.render_capture() == "screenshot"
    monkeypatch.delenv(compositor.FRAME_CAPTURE_ENV)
    compositor.demote_capture("Page.captureScreenshot: Timeout 60000ms exceeded")
    demoted = await compositor.worker_capture()
    assert (demoted.mode, demoted.demoted) == ("screenshot", True)
    assert "then a render's begun frames failed (Page.captureScreenshot: Timeout 60000ms exceeded)" in demoted.words()
    assert await compositor.render_capture() == "screenshot"


async def test_a_probe_that_raises_never_fails_anything(fresh_capture, monkeypatch):
    async def broken(_wait):
        raise RuntimeError("Target page, context or browser has been closed")

    monkeypatch.setattr(compositor, "_probe_begun_frame", broken)
    found = await compositor.probe_capture()
    assert found.mode == "screenshot" and found.reason.startswith("no begun frame: Target page")


def test_the_pre_flight_line_says_what_the_worker_probed():
    published = compositor.FrameCapture("begin_frame", "screenshot", "no begun frame within 2 s").as_dict()
    words = render_preflight.capture_words({"capture": published})
    assert words.startswith("Frames are taken by screenshot on the render worker")
    assert render_preflight.capture_words({}) == ""
    assert render_preflight.capture_words({"capture": {"mode": "magic"}}) == ""
    said = render_preflight.estimate(frames=900, size=(1920, 1080), pages=2, cpus=4, quality="high")
    import dataclasses

    line = dataclasses.replace(said, capture=words).line()
    assert words in line and dataclasses.replace(said, capture=words).as_dict()["capture"] == words


def test_the_worker_publishes_its_capture_once_probed(monkeypatch):
    class _Redis:
        def __init__(self):
            self.values = {}

        async def set(self, key, value, ex=None):
            self.values[key] = value

        async def get(self, key):
            return self.values.get(key)

    redis = _Redis()

    async def client():
        return redis

    monkeypatch.setattr("app.core.redis.redis_client", client)
    monkeypatch.setattr(render_preflight, "_published_at", [])
    monkeypatch.setattr(compositor, "_CAPTURE", [])
    monkeypatch.setattr(settings, "video_studio_frame_capture", "begin_frame")
    asyncio.run(render_preflight.publish_worker())
    assert asyncio.run(render_preflight.worker_capacity())["capture"] is None
    # Probed meanwhile: published at once, never five minutes later.
    monkeypatch.setattr(compositor, "_CAPTURE", [compositor.FrameCapture("begin_frame", "begin_frame", "a begun frame in 0.30 s")])
    asyncio.run(render_preflight.publish_worker())
    assert asyncio.run(render_preflight.worker_capacity())["capture"]["mode"] == "begin_frame"


@pytest.mark.chromium
async def test_a_real_chromium_probe_settles_within_its_bound(fresh_capture):
    try:
        from playwright.async_api import async_playwright

        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True)
            await browser.close()
    except Exception as exc:
        if os.environ.get("REQUIRE_CHROMIUM") == "1":
            raise
        pytest.skip(f"Playwright's Chromium is missing: {str(exc)[:160]}")
    began = time.monotonic()
    found = await compositor.probe_capture()
    assert time.monotonic() - began < 35
    assert found.mode in ("begin_frame", "screenshot") and found.reason


# ── The platform's failures, run again once ───────────────────────────────


def test_a_chromium_failure_is_told_from_the_videos():
    from playwright.async_api import Error as PlaywrightError

    assert compositor.chromium_failed(TimeoutError()) and compositor.chromium_failed(PlaywrightError("Protocol error"))
    assert not compositor.chromium_failed(compositor.RenderCancelledError("cancelled"))
    assert not compositor.chromium_failed(RuntimeError("The recording is not ready"))
    assert render_retry.cause_of_exception(compositor.ChromiumError("x")) == render_retry.CHROMIUM
    assert render_retry.cause_of_exception(RuntimeError("x")) is None
    # Never a ``RuntimeError``: the render's media failures - the person's to fix - are those.
    assert not isinstance(compositor.ChromiumError("x"), RuntimeError)
    assert render_retry.cause_of_exit("memory") == render_retry.MEMORY
    assert render_retry.cause_of_exit("crashed", oom_killed=True) == render_retry.MEMORY
    assert render_retry.cause_of_exit("crashed") == render_retry.CRASH
    assert render_retry.cause_of_exit("stalled") == render_retry.STALL
    assert render_retry.cause_of_exit("cancelled") is None


@pytest.fixture
def row(monkeypatch):
    state = SimpleNamespace(status=JOB_RUNNING, marks=[], again=[], revision_kept=True)

    async def load(kind, job_id):
        return SimpleNamespace(status=state.status)

    async def mark_if_open(kind, job_id, **changes):
        if state.status not in (JOB_PENDING, JOB_RUNNING):
            return False
        state.marks.append(changes)
        state.status = changes.get("status", state.status)
        return True

    async def same_revision(render_id):
        return 4 if state.revision_kept else None

    monkeypatch.setattr(jobs, "load", load)
    monkeypatch.setattr(jobs, "mark_if_open", mark_if_open)
    monkeypatch.setattr(render_retry, "same_revision", same_revision)
    return state


def _exit(kind: str, **fields) -> render_process.RenderExit:
    return render_process.RenderExit(kind=kind, peak_memory=5 << 30, progress=12, stage="Drawing frames (0%)", **fields)


@pytest.mark.parametrize("kind", ["memory", "stalled"])
async def test_the_memory_ceiling_or_a_stall_runs_the_render_again_once_on_its_revision(row, kind):
    outcome = await render_process.settle("r", _exit(kind), last_attempt=False, ceiling=4 << 30, stall_seconds=120, again=True)
    assert outcome.kind == "again" and outcome.again["revision"] == 4
    assert outcome.again["cause"] == render_retry.cause_of_exit(kind)
    assert row.status == JOB_PENDING and row.marks[0]["stage"].startswith("Running again once on revision 4: ")
    # Run again once already: it fails, its row saying the platform's cause and the run again it had.
    row.status, row.marks = JOB_RUNNING, []
    outcome = await render_process.settle(
        "r", _exit(kind), last_attempt=False, ceiling=4 << 30, stall_seconds=120, again=False, retried=outcome.again
    )
    assert outcome.kind == "failed" and row.status == JOB_FAILED
    assert row.marks[0]["failure"]["retried"] is True and row.marks[0]["failure"]["revision"] == 4


async def test_a_video_changed_meanwhile_is_never_run_again(row):
    row.revision_kept = False
    outcome = await render_process.settle("r", _exit("memory"), last_attempt=False, ceiling=4 << 30, stall_seconds=120, again=True)
    assert outcome.kind == "failed" and row.marks[0]["failure"] == render_retry.failure_record("memory", retried=None)


async def test_chromium_failing_the_last_attempt_runs_it_again_and_an_earlier_one_is_the_engines(row):
    said = "The render failed: The render's headless Chromium failed (begin_frame capture): Page.captureScreenshot: Timeout"
    failed = _exit("retry", error=said, cause=render_retry.CHROMIUM)
    earlier = await render_process.settle("r", failed, last_attempt=False, ceiling=None, stall_seconds=120, again=True)
    assert (earlier.kind, earlier.cause, row.marks) == ("retry", render_retry.CHROMIUM, [])
    last = await render_process.settle("r", failed, last_attempt=True, ceiling=None, stall_seconds=120, again=True, attempt=(3, 3))
    assert last.kind == "again" and row.status == JOB_PENDING and "at 12% (Drawing frames (0%))" in last.error
    row.status, row.marks = JOB_RUNNING, []
    final = await render_process.settle(
        "r", failed, last_attempt=True, ceiling=None, stall_seconds=120, again=False, retried=last.again, attempt=(3, 3)
    )
    assert final.kind == "failed" and row.status == JOB_FAILED
    assert row.marks[0]["error"].endswith("This was its last attempt (3 of 3).")
    assert row.marks[0]["failure"]["cause"] == render_retry.CHROMIUM
    # A failure the video caused is no platform's: the engine's retry, as before.
    plain = await render_process.settle("r", _exit("retry", error="KeyError"), last_attempt=True, ceiling=None, stall_seconds=120, again=True)
    assert plain.kind == "retry"


async def test_a_render_run_again_is_deferred_first_in_the_queue_with_no_attempt_spent(monkeypatch):
    seen = {}

    async def run_isolated(_render_id, **options):
        seen.update(options)
        return runners.JobOutcome("again", "The render used 17 GiB.", cause="memory", again={"cause": "memory", "revision": 4})

    async def no_wait(_render_id):
        return None

    async def _noop():
        return None

    monkeypatch.setattr(render_process, "run_isolated", run_isolated)
    monkeypatch.setattr(jobs, "preview_waits", no_wait)
    execution = SimpleNamespace(
        id="e1", payload={"render_id": "r1"}, adapter_state=None, attempt_count=3, max_attempts=3, deadline_at=None
    )
    ctx = SimpleNamespace(db=SimpleNamespace(commit=_noop), worker_id="w:1", cancel_event=None, stopping=None)
    result = await adapters.VideoRenderAdapter().execute(ctx, execution)
    assert seen["retried"] is None
    assert (result.kind, result.adapter_state, result.place) == ("defer", render_retry.INFRA_RETRY_STATE, "head")
    assert result.payload_update == {render_retry.INFRA_RETRY_KEY: {"cause": "memory", "revision": 4}}
    # The next run is told it already ran again: never a second time.
    execution.payload = {"render_id": "r1", **result.payload_update}
    await adapters.VideoRenderAdapter().execute(ctx, execution)
    assert seen["retried"] == {"cause": "memory", "revision": 4}


async def test_a_lapsed_lease_runs_the_render_again_once_then_fails_saying_so(monkeypatch):
    calls = SimpleNamespace(requeued=[], dead=[])
    expiry = SimpleNamespace(describe=lambda: "last heartbeat 74 s ago")

    async def not_moving(_execution, _expiry):
        return False

    async def requeue(execution, cause, reason):
        calls.requeued.append((cause, reason))
        return render_retry.record_of(execution.payload) is None

    async def dead_letter(kind, job_id, error, **options):
        calls.dead.append(options)

    async def _noop():
        return None

    monkeypatch.setattr("app.services.orchestration_engine.lease_expiry", lambda _execution: expiry)
    monkeypatch.setattr(render_process, "resume_after_lost_lease", not_moving)
    monkeypatch.setattr(render_retry, "requeue", requeue)
    monkeypatch.setattr(runners, "dead_letter", dead_letter)
    monkeypatch.setattr("app.services.video_studio.wake.schedule", lambda *_args: None)
    ctx = SimpleNamespace(db=SimpleNamespace(commit=_noop))
    execution = SimpleNamespace(payload={"render_id": "r1"}, last_error="Lease expired")
    await adapters.VideoRenderAdapter().on_dead_letter(ctx, execution)
    assert calls.requeued == [("lease", "its lease lapsed (last heartbeat 74 s ago)")] and calls.dead == []
    execution.payload = {"render_id": "r1", render_retry.INFRA_RETRY_KEY: {"cause": "lease", "revision": 4}}
    await adapters.VideoRenderAdapter().on_dead_letter(ctx, execution)
    [dead] = calls.dead
    assert dead["lease"] is expiry and dead["failure"]["cause"] == "lease" and dead["failure"]["retried"] is True


def test_a_render_the_platform_failed_says_so_never_fix_what_its_reason_names():
    retried = {"cause": "chromium", "reason": "Page.captureScreenshot: Timeout", "revision": 4}
    render = SimpleNamespace(
        status=JOB_FAILED,
        error="The render failed: The render's headless Chromium failed: Target closed. This was its last attempt (3 of 3).",
        failure=render_retry.failure_record("chromium", retried=retried),
        scene_id=None,
        notes=None,
    )
    [finding] = render_answer.of_render(render, None).findings
    assert finding["kind"] == render_answer.RENDER_FAILED
    assert "An infrastructure failure, not the video's: the headless Chromium it draws in failed." in finding["what"]
    assert "it ran again once on revision 4 and failed again" in finding["what"]
    assert finding["fix"].startswith("nothing in the video: render it again as it is")
    assert "reason names" not in finding["fix"]
    # One its video failed names what to change, as before.
    render.failure, render.error = None, "Recording `x` is not ready."
    [finding] = render_answer.of_render(render, None).findings
    assert finding["fix"] == "what its reason names, then render again"


# ── The legibility advice: no push that crops a page, said once per insert ──


def _drawn(scale: float, **fields) -> Legibility:
    """A 3200 px recording at pixel ratio 2 on a quad 500 px wide, drawn at *scale*, its text reading 8 px."""
    return Legibility(
        timeline.insert_key("s1", 0, fields.pop("pane", None)),
        ("take", TAKE),
        (3200, 1800),
        500.0,
        scale,
        TextSize(8.0 / scale, 40),
        viewport=1600.0,
        pixel_ratio=2.0,
        **fields,
    )


def test_no_push_is_offered_on_a_source_drawn_under_0_3x():
    under = _drawn(0.29)
    assert not under.pushable and not under.readable
    fix = under.fix()
    assert "push in on it" not in fix and "no push: drawn at 0.29x, under 0.3x" in fix
    assert "of the 1080p frame's width (" in fix
    over = _drawn(0.31)
    assert over.pushable and "push in on it with zoom {start, end, scale: 1.5}" in over.fix()
    assert "no push" not in over.fix()
    # A browser zoom whose push left after it would crop the page is not offered either; one with no push left is.
    held = {"holds_down_to": 400, "changes_at": None, "why": None}
    assert "then push in" not in _drawn(0.29, layout=held).fix()
    for item in (under, over):
        assert item.fix().endswith(insert_legibility.AMBIENT_OFFER)


def test_a_walls_panes_are_said_once_together_never_one_more_on_each_save(monkeypatch):
    from app.services.video_studio import render_cache

    kept: dict = {}

    async def get_json(kind, digest):
        return kept.get((kind, digest))

    async def put_json(kind, digest, value):
        kept[(kind, digest)] = value

    monkeypatch.setattr(render_cache, "get_json", get_json)
    monkeypatch.setattr(render_cache, "put_json", put_json)

    def pane(index: int, wall: int = 0) -> quality.QualityFinding:
        insert = f"insert/s1.{wall}.{index}|take:{TAKE}-{index}"
        return quality.QualityFinding(0, "s1", "clip", "Wall", "insert_legibility", "advisory", "words", "content", insert=insert)

    # Two walls of four panes: the answer lists three of the first wall's.
    report = quality.QualityReport(scene_count=1, findings=tuple(pane(n) for n in range(4)) + tuple(pane(n, 1) for n in range(4)))
    first = asyncio.run(insert_legibility.said_once("video", report, shown=3))
    assert len(first.findings) == 8
    # The first wall is remembered whole; the second, which the answer listed nothing of, is said by the next save.
    again = asyncio.run(insert_legibility.said_once("video", report, shown=8))
    assert [f.insert.split("|")[0] for f in again.findings] == [f"insert/s1.1.{n}" for n in range(4)]
    assert asyncio.run(insert_legibility.said_once("video", report, shown=8)).findings == ()
    assert insert_legibility.insert_of(f"insert/s1.0.3|take:{TAKE}") == "insert/s1.0"
    assert insert_legibility.insert_of(f"insert/s1.2|take:{TAKE}") == "insert/s1.2"
    assert insert_legibility.insert_of("screen/s4|clip:c|ultra") == "screen/s4"
