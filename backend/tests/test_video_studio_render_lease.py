"""A render's lease is kept while it moves, and its expiry is never called a crash.

Render 2c435ac6 was drawing frames when the engine took its lease back and
dead-lettered it as "(worker crash)" - the worker had not restarted. Two High
renders lost theirs at about 60% while drawing, "with no attempt left".
These tests pin the halves of the fix:

* the render process renews the execution's lease from a thread of its own
  (``lease.FrameLease``), every third of the lease, while the render moves:
  never from a frame and never from an event loop, so a slow frame or a busy
  loop does not silence it; a hung renewal given up, a lease that is gone
  stopping the render at the next frame;
* a render's execution holds a lease of its own (``RENDER_LEASE_SECONDS``),
  claimed and heartbeated by the reconciler at that length;
* the worker hands the process its lease, and a render that lost it ends
  without touching its row (``lease_lost``);
* the engine records what it saw of an expiry - the last heartbeat - and says
  so instead of "worker crash", and a Video Studio job's row says where it
  was ("Lease expired at 63% ..., last heartbeat 74 s ago") - unless the
  render still moved, which runs again from its segments instead;
* the supervisor's ``/proc`` sampling never blocks the worker's event loop.
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import logging
import sys
import threading
import time
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from app.models.orchestration import OrchestrationExecution
from app.services import orchestration_engine as engine
from app.services import orchestration_reconciler
from app.services.video_studio import compositor, jobs, render_process, runners
from app.services.video_studio import lease as frame_lease

# ── The lease renewed from a thread while the render moves ────────


class _Keeper(frame_lease.FrameLease):
    """The render's lease keeper, each renewal recorded and never sent to a database."""

    def __init__(self, *, interval=0.05, window=None, error=None, gate=None, lease_seconds=180):
        super().__init__(uuid.uuid4(), "worker-1", interval=interval, lease_seconds=lease_seconds, window=window)
        self.calls: list[float] = []
        self.error = error
        self.gate = gate

    async def _extend(self) -> None:
        self.calls.append(time.monotonic())
        if self.gate is not None:
            while not self.gate.is_set():
                await asyncio.sleep(0.01)
        if self.error is not None:
            raise self.error


async def _until(predicate, seconds: float = 30.0) -> None:
    """Wait for *predicate*; the bound only keeps a broken keeper from hanging the suite, never a timing."""
    deadline = time.monotonic() + seconds
    while not predicate() and time.monotonic() < deadline:
        await asyncio.sleep(0.01)


def test_a_third_of_the_lease_is_the_interval():
    assert frame_lease.renewal_interval(60) == 20
    assert frame_lease.renewal_interval(180) == 60
    assert frame_lease.renewal_interval(1.5) == 1.0


async def test_the_lease_is_renewed_while_the_event_loop_is_blocked_and_no_frame_is_drawn():
    """A heartbeat that a busy loop and a slow frame cannot silence."""
    keeper = _Keeper(interval=0.05)
    keeper.frame_drawn()
    keeper.start()
    try:
        # The render's event loop busy with one long frame: nothing it runs can renew. It stays blocked
        # (never awaiting) until the thread renewed five times, whatever the runner's scheduling; the
        # bound only keeps a broken keeper from hanging the suite.
        deadline = time.monotonic() + 30
        while keeper.renewals < 5 and time.monotonic() < deadline:
            time.sleep(0.01)
        assert len(keeper.calls) >= 5 and keeper.renewals >= 5
    finally:
        await keeper.close()


async def test_a_render_that_stopped_moving_keeps_no_lease_and_one_that_moves_again_does(monkeypatch):
    # The movement is timed by a clock the test drives, never by the runner's scheduling: on the real
    # clock a 0.1 s window after moved() could close before a loaded runner woke the thread (run 42228061919).
    now = [1000.0]
    monkeypatch.setattr(frame_lease, "_monotonic", lambda: now[0])
    keeper = _Keeper(interval=0.01, window=0.1)
    now[0] += 1.0  # the render stood still past its window
    keeper.start()
    try:
        await asyncio.sleep(0.2)  # the thread polls, and finds it still each time
        assert keeper.calls == [] and not keeper.moving()  # left to the stall detector, never held
        keeper.moved()  # a report: the render moves again, and stays moving until the clock moves on
        assert keeper.moving()
        # Wait on what is asserted: calls is appended when _extend starts, renewals counted once it returned
        # (FrameLease._renew), so waiting on calls raced the thread (run 42452702799).
        await _until(lambda: keeper.renewals >= 1)
        assert len(keeper.calls) >= 1 and keeper.renewals >= 1
    finally:
        await keeper.close()


async def test_a_renewal_that_hangs_is_given_up_so_the_next_can_start(caplog):
    keeper = _Keeper(interval=0.05, gate=threading.Event())  # never set: the database stopped answering
    with caplog.at_level(logging.WARNING, logger=frame_lease.__name__):
        keeper.start()
        await _until(lambda: keeper.failures >= 2)
        await keeper.close()
    assert keeper.failures >= 2 and len(keeper.calls) >= 2 and keeper.lost is None
    assert "could not be renewed" in caplog.text and "TimeoutError" in caplog.text


async def test_a_lease_that_is_gone_stops_the_render_at_the_next_frame(caplog):
    keeper = _Keeper(interval=0.05, error=engine.LeaseLostError("Execution x is pending"))
    keeper.frame_drawn()
    with caplog.at_level(logging.WARNING, logger=frame_lease.__name__):
        keeper.start()
        await _until(lambda: keeper.lost is not None)
    with pytest.raises(engine.LeaseLostError, match="is pending"):
        keeper.frame_drawn()
    await keeper.close()
    assert keeper._thread is not None and not keeper._thread.is_alive() and len(keeper.calls) == 1
    assert "its lease is gone at frame 1" in caplog.text and "frames/s over the" in caplog.text


async def test_a_renewal_that_fails_is_said_and_tried_again(caplog):
    keeper = _Keeper(interval=0.05, error=ConnectionError("database unreachable"))
    with caplog.at_level(logging.WARNING, logger=frame_lease.__name__):
        keeper.start()
        await _until(lambda: keeper.failures >= 2)
        await keeper.close()
    assert keeper.failures >= 2 and keeper.lost is None
    assert "could not be renewed" in caplog.text and "database unreachable" in caplog.text
    assert "the lease lasts 180 s" in caplog.text


async def test_a_renewal_extends_the_lease_by_the_renders_own_length(monkeypatch):
    seen: list = []

    @contextlib.asynccontextmanager
    async def session():
        yield SimpleNamespace(commit=_noop)

    async def extend_lease(db, *, execution_id, worker_id, lease_seconds=None):
        seen.append((execution_id, worker_id, lease_seconds))

    monkeypatch.setattr(frame_lease, "async_session", session)
    monkeypatch.setattr(frame_lease.engine, "extend_lease", extend_lease)
    execution_id = uuid.uuid4()
    await frame_lease.renew(execution_id, "w", lease_seconds=jobs.RENDER_LEASE_SECONDS)
    assert seen == [(execution_id, "w", 180)]


async def test_close_stops_the_thread():
    keeper = _Keeper(interval=0.05)
    keeper.start()
    await keeper.close()
    assert not keeper._thread.is_alive()
    calls = len(keeper.calls)
    await asyncio.sleep(0.2)
    assert len(keeper.calls) == calls


# ── A render's execution holds a lease of its own ─────────────────


def test_a_render_execution_holds_a_three_minute_lease_every_other_one_the_engines():
    from app.services.orchestration_adapters.video_studio import VideoClipAdapter, VideoRenderAdapter

    assert jobs.RENDER_LEASE_SECONDS == 180
    assert orchestration_reconciler._lease_seconds_of(VideoRenderAdapter()) == 180
    assert orchestration_reconciler._lease_seconds_of(VideoClipAdapter()) is None


async def test_the_reconciler_heartbeats_an_execution_at_its_adapters_lease(monkeypatch):
    renewed: list = []

    @contextlib.asynccontextmanager
    async def session():
        yield SimpleNamespace(commit=_noop)

    async def extend_lease(db, *, execution_id, worker_id, lease_seconds=None):
        renewed.append(lease_seconds)

    monkeypatch.setattr(orchestration_reconciler.settings, "orchestration_lease_seconds", 60)
    monkeypatch.setattr(orchestration_reconciler, "async_session", session)
    monkeypatch.setattr(orchestration_reconciler.engine, "extend_lease", extend_lease)
    beat = asyncio.create_task(orchestration_reconciler._heartbeat(uuid.uuid4(), "w", lease_seconds=3))
    await asyncio.sleep(1.3)
    beat.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await beat
    # Every third of its own 3 s lease, never of the engine's 60 s.
    assert renewed == [3]


async def test_a_reconciler_heartbeat_that_hangs_is_given_up_after_an_interval(monkeypatch, caplog):
    @contextlib.asynccontextmanager
    async def session():
        yield SimpleNamespace(commit=_noop)

    async def hangs(*_args, **_kwargs):
        await asyncio.Event().wait()

    monkeypatch.setattr(orchestration_reconciler.settings, "orchestration_lease_seconds", 3)
    monkeypatch.setattr(orchestration_reconciler, "async_session", session)
    monkeypatch.setattr(orchestration_reconciler.engine, "extend_lease", hangs)
    execution_id = uuid.uuid4()
    with caplog.at_level(logging.WARNING, logger=orchestration_reconciler.__name__):
        beat = asyncio.create_task(orchestration_reconciler._heartbeat(execution_id, "w"))
        await asyncio.sleep(2.3)
        beat.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await beat
    assert f"Heartbeat of execution {execution_id} failed (1 in a row" in caplog.text and "TimeoutError" in caplog.text


def test_a_lease_is_late_against_the_lease_its_holder_took():
    now = datetime.now(UTC)
    # Renewed 45 s ago for 180 s: on time. The same silence under a 60 s lease missed a renewal.
    long_lease = SimpleNamespace(
        state="running",
        lease_expires_at=now + timedelta(seconds=135),
        updated_at=now - timedelta(seconds=45),
        attempt_count=1,
        max_attempts=3,
    )
    short_lease = SimpleNamespace(**{**vars(long_lease), "lease_expires_at": now + timedelta(seconds=15)})
    held = engine.lease_heartbeat(long_lease, now)
    assert held.lease_seconds == 180 and held.silent_seconds == 45 and not held.late
    assert engine.lease_heartbeat(short_lease, now).late


# ── A checkpoint every 10% of the frames at least ─────────────────


def test_a_segment_holds_ten_seconds_of_frames_and_never_more_than_a_tenth_of_the_render():
    assert compositor.segment_frames(7800, 60) == 600  # the 130 s brand film: 10 s segments, under 8% each
    assert compositor.segment_frames(1800, 60) == 180  # 30 s: a tenth, 3 s
    assert compositor.segment_frames(6000, 30) == 300
    assert compositor.segment_frames(300, 60) == 60  # never under a second: a segment opens its own encoder
    assert compositor.segment_ranges(0, 1800, compositor.segment_frames(1800, 60))[:2] == [(0, 180), (180, 360)]


# ── Every frame loop of the render process keeps the lease ────────

#: The scene of render 14d6bcbd: 24.5 s of a figure at 24 frames a second, drifting to its end - 588 frames.
_FIGURE_SCENE = "s05-platform"
_FIGURE_SECONDS = 24.5
_FIGURE_FPS = 24
_ARCHITECTURE = {
    "type": "architecture",
    "nodes": [
        {"id": "hub", "label": "XTM Hub", "row": 1, "col": 1, "col_span": 3},
        {"id": "cti", "label": "OpenCTI", "row": 2, "col": 1},
        {"id": "aev", "label": "OpenAEV", "row": 2, "col": 3},
    ],
    "edges": [{"source": "hub", "target": "cti"}, {"source": "hub", "target": "aev"}],
}


class _Clock:
    """A clock the test drives, the lease keeper's movement timed by it."""

    def __init__(self, monkeypatch):
        self.now = 1000.0
        monkeypatch.setattr(frame_lease, "_monotonic", lambda: self.now)

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _figure_page(monkeypatch, *, frame_seconds: float, clock: _Clock | None, on_screenshot=None):
    """The figure page as the render drives it, each screenshot taking *frame_seconds* of *clock*; the screenshots."""
    from contextlib import asynccontextmanager
    from pathlib import Path

    from app.services import presentation_export
    from app.services.video_studio import encoder

    shots: list[str] = []

    class _Page:
        async def evaluate(self, script, arg=None):
            if "describe" in script:
                return {"steps": [{"labels": [node["label"]]} for node in _ARCHITECTURE["nodes"]]}
            if "plan" in script:
                # ``motion: drift``: something moves to the very end, so every frame is screenshot.
                return {"active": [[0.0, arg["length"]]]}
            return None

        async def screenshot(self, *, path, **_kw):
            if on_screenshot is not None:
                on_screenshot(len(shots))
            await asyncio.sleep(0)  # Chromium answers on the loop: a renewal in flight runs meanwhile
            if clock is not None:
                clock.advance(frame_seconds)
            Path(path).write_bytes(b"jpeg")
            shots.append(path)

    @asynccontextmanager
    async def open_page(url, **_kw):
        yield _Page()

    async def frames_to_mezzanine(frames, *, out_path, **_kw):
        Path(out_path).write_bytes(b"mp4")

    async def probe(_path):
        return SimpleNamespace(width=1920, height=1080)

    monkeypatch.setattr(presentation_export, "open_render_page", open_page)
    monkeypatch.setattr(encoder, "frames_to_mezzanine", frames_to_mezzanine)
    monkeypatch.setattr(encoder, "probe", probe)
    return shots


def _figure_render():
    """The render's figure stage for the scene of render 14d6bcbd: ``(spec, plan, media)``."""
    from app.services.video_studio import figures, render, timeline
    from app.services.video_studio.spec import parse_video_spec, validate_video_spec

    spec = parse_video_spec(
        validate_video_spec(
            {
                "title": "Platform",
                "format": {"aspect_ratio": "16:9"},
                "scenes": [{"id": _FIGURE_SCENE, "type": "figure", "block_spec": _ARCHITECTURE, "duration": _FIGURE_SECONDS}],
            }
        )
    )
    assert spec.scenes[0].motion == "drift"
    media = render._Media()
    source = figures.source_from_spec(spec.scenes[0])
    media.figures[_FIGURE_SCENE] = (source, figures.figure_document(source, scheme=spec.theme.mode, palette={}))
    return spec, timeline.plan(spec, media.plan, fps=_FIGURE_FPS, theme={}), media


async def _draw_figure(spec, plan, media, workdir, on_frame) -> None:
    from app.services.video_studio import render

    async def progress(*_args, **_kwargs) -> None:
        return None

    await render._draw_figures(
        spec, plan, media, user=None, fps=_FIGURE_FPS, scale=1, workdir=workdir, progress=progress, on_frame=on_frame
    )


async def test_a_figure_scene_drifting_for_25_s_keeps_its_render_moving_at_every_frame(monkeypatch, tmp_path):
    """Render 14d6bcbd's scene: 588 frames at 0.2 s each in a 2 CPU container, nearly two minutes, no report between.

    Each frame says the render moves, so its lease keeper keeps renewing the lease.
    """
    clock = _Clock(monkeypatch)
    shots = _figure_page(monkeypatch, frame_seconds=0.2, clock=clock)
    spec, plan, media = _figure_render()
    keeper = frame_lease.FrameLease(uuid.uuid4(), "worker-1", window=60)
    moves: list[float] = []

    def on_frame() -> None:
        keeper.frame_drawn()
        moves.append(clock.now)

    started = clock.now
    await _draw_figure(spec, plan, media, tmp_path, on_frame)
    assert len(shots) == round(_FIGURE_SECONDS * _FIGURE_FPS) == 588
    assert clock.now - started == pytest.approx(117.6)
    assert keeper.frames >= 588 and keeper.moving()
    held = [started, *moves, clock.now]
    assert max(b - a for a, b in itertools.pairwise(held)) < 1
    assert _FIGURE_SCENE in media.plan.clips and f"clip/{_FIGURE_SCENE}" in media.assets.sources


async def test_a_lease_lost_while_a_figure_is_drawn_stops_the_render_as_such(monkeypatch, tmp_path):
    """Never wrapped as a figure that could not be drawn: the render ends ``lease_lost`` and leaves its row alone."""
    _figure_page(monkeypatch, frame_seconds=0.0, clock=None)
    spec, plan, media = _figure_render()
    drawn = 0

    def on_frame() -> None:
        nonlocal drawn
        drawn += 1
        if drawn == 40:
            raise engine.LeaseLostError("Execution is pending")

    with pytest.raises(engine.LeaseLostError, match="is pending"):
        await _draw_figure(spec, plan, media, tmp_path, on_frame)


async def test_a_cancel_while_a_figure_is_drawn_cancels_the_render(monkeypatch, tmp_path):
    from app.services.video_studio import compositor, render

    _figure_page(monkeypatch, frame_seconds=0.0, clock=None)
    spec, plan, media = _figure_render()

    async def progress(*_args, **_kwargs) -> None:
        raise compositor.RenderCancelledError("The render was cancelled.")

    with pytest.raises(compositor.RenderCancelledError):
        await render._draw_figures(spec, plan, media, user=None, fps=_FIGURE_FPS, scale=1, workdir=tmp_path, progress=progress)


async def test_the_frozen_picture_check_renews_the_lease_from_the_frames_it_watches(monkeypatch):
    from app.services.video_studio import encoder, frame_freeze

    async def stream(_path, *, width, height, fps, start=0.0):
        for index in range(90):
            yield bytes([index % 250]) * (width * height)

    monkeypatch.setattr(encoder, "iter_gray_frames", stream)
    watched: list[int] = []
    document = {"frames": 90, "fps": 30, "scenes": []}
    check = await frame_freeze.check_frames(
        "final.mp4", document=document, width=1280, height=720, fps=30, on_frame=lambda: watched.append(1)
    )
    assert check.summary["frames"] == len(watched) == 90


async def test_a_lease_lost_during_the_frame_check_is_never_a_check_that_could_not_run(monkeypatch, tmp_path):
    from app.services.video_studio import audio_qc, frame_freeze, render

    async def heard(**_kwargs):
        return SimpleNamespace(as_summary=dict, pronunciation={}, findings=[], notes=[])

    async def watched(*_args, on_frame=None, **_kwargs):
        on_frame()

    def lost() -> None:
        raise engine.LeaseLostError("Execution is dead_letter")

    monkeypatch.setattr(audio_qc, "check_audio", heard)
    monkeypatch.setattr(frame_freeze, "check_frames", watched)
    plan = SimpleNamespace(document={"frames": 30, "fps": 30, "scenes": []}, narration_spans={}, voice_windows=lambda **_: [])
    with pytest.raises(engine.LeaseLostError, match="dead_letter"):
        await render.check_render(
            final_path="final.mp4", stem=None, plan=plan, media=render._Media(), size=(1280, 720), fps=30, workdir=tmp_path, on_frame=lost
        )


async def test_the_check_stage_says_each_narration_heard_and_each_stretch_watched(monkeypatch, tmp_path):
    """"Hearing and watching the render" sat at 93% for 111 s with no sub-stage; only the lease said it moved."""
    from app.services.video_studio import audio_qc, compositor, frame_freeze, render

    async def heard(*, heard, **_kwargs):
        await heard(0, 2)
        await heard(2, 2)
        return SimpleNamespace(as_summary=dict, pronunciation={}, findings=[], notes=[])

    async def watched(*_args, watched, document, duration, **_kwargs):
        # A whole render's MP4 is its plan: the watch reads its length there.
        assert duration is None
        await watched(0.0, document["duration"], None)
        # Each window named as it starts, with its scenes: a stall there says where it stood.
        await watched(180.0, document["duration"], frame_freeze.Window(180.0, 185.0, (("s12", "outro"),)))
        await watched(185.0, document["duration"], None)
        return SimpleNamespace(as_summary=dict, findings=[])

    stages: list[tuple[int, str]] = []

    async def progress(value: int, stage: str) -> None:
        stages.append((value, stage))

    monkeypatch.setattr(audio_qc, "check_audio", heard)
    monkeypatch.setattr(frame_freeze, "check_frames", watched)
    plan = SimpleNamespace(document={"frames": 5550, "fps": 30, "duration": 185.0, "scenes": []}, narration_spans={})

    async def check(told) -> None:
        await render.check_render(
            final_path="final.mp4", stem=None, plan=plan, media=render._Media(), size=(1280, 720), fps=30, workdir=tmp_path, progress=told
        )

    await check(progress)
    assert stages == [
        (93, "Hearing the render: 0 of 2 narrations heard"),
        (93, "Hearing the render: 2 of 2 narrations heard"),
        (94, "Watching the render for frozen pictures: 0 of 185 s"),
        (94, "Watching the render for frozen pictures: 180 of 185 s, now 3:00.00 to 3:05.00, scene `s12` (outro)"),
        (94, "Watching the render for frozen pictures: 185 of 185 s"),
    ]

    # The render cancelled while it is checked stops there - never a check that could not run.
    def cancelled_at(percent: int):
        async def cancelled(value: int, _stage: str) -> None:
            if value == percent:
                raise compositor.RenderCancelledError("The render was cancelled.")

        return cancelled

    for percent in (93, 94):
        with pytest.raises(compositor.RenderCancelledError):
            await check(cancelled_at(percent))


def test_the_render_process_reads_its_lease_from_the_environment(monkeypatch):
    execution_id = uuid.uuid4()
    monkeypatch.delenv(frame_lease.EXECUTION_ENV, raising=False)
    monkeypatch.delenv(frame_lease.OWNER_ENV, raising=False)
    assert frame_lease.FrameLease.from_environment() is None
    monkeypatch.setenv(frame_lease.EXECUTION_ENV, str(execution_id))
    monkeypatch.setenv(frame_lease.OWNER_ENV, "host:12:abcd")
    keeper = frame_lease.FrameLease.from_environment()
    assert keeper is not None and (keeper.execution_id, keeper.owner) == (execution_id, "host:12:abcd")
    monkeypatch.setenv(frame_lease.EXECUTION_ENV, "not-a-uuid")
    assert frame_lease.FrameLease.from_environment() is None


# ── The worker's side: the lease handed down, a lost lease left alone ─────


async def test_the_worker_hands_the_render_process_its_lease(monkeypatch):
    seen: dict = {}

    async def supervise(command, **options):
        seen.update(options["env"])
        return render_process.RenderExit(kind="done", returncode=0)

    monkeypatch.setattr(render_process, "supervise", supervise)
    monkeypatch.setattr(render_process.jobs, "heartbeat", _noop)
    execution_id = uuid.uuid4()
    outcome = await render_process.run_isolated("r", lease=(execution_id, "host:1:abc"))
    assert outcome.kind == "done"
    assert seen[frame_lease.EXECUTION_ENV] == str(execution_id) and seen[frame_lease.OWNER_ENV] == "host:1:abc"
    # And its length: the render process renews it for as long as the reconciler does.
    assert seen[frame_lease.LEASE_ENV] == "180"


def test_the_render_process_reads_the_length_of_its_lease_from_the_environment(monkeypatch):
    monkeypatch.setenv(frame_lease.EXECUTION_ENV, str(uuid.uuid4()))
    monkeypatch.setenv(frame_lease.OWNER_ENV, "host:1:abc")
    monkeypatch.setenv(frame_lease.LEASE_ENV, "180")
    keeper = frame_lease.FrameLease.from_environment()
    assert keeper is not None and keeper.lease_seconds == 180 and keeper.interval == 60
    monkeypatch.delenv(frame_lease.LEASE_ENV)
    monkeypatch.setattr(frame_lease.settings, "orchestration_lease_seconds", 60)
    assert frame_lease.FrameLease.from_environment().lease_seconds == 60


async def _noop(*_args, **_kwargs):
    return None


async def test_a_render_that_lost_its_lease_ends_without_touching_its_row(monkeypatch):
    marks: list = []

    async def mark(*_args, **changes):
        marks.append(changes)

    monkeypatch.setattr(render_process.jobs, "mark", mark)
    body = (
        "import json, sys\n"
        "sys.stdout.write(json.dumps({'type': 'outcome', 'kind': 'lease_lost', 'error': 'gone'}) + '\\n')\n"
    )
    ended = await asyncio.wait_for(
        render_process.supervise([sys.executable, "-c", body], memory_limit=None, stall_seconds=30, poll_seconds=0.1),
        60,
    )
    assert (ended.kind, ended.error) == ("lease_lost", "gone")
    outcome = await render_process.settle("r", ended, last_attempt=True, ceiling=None, stall_seconds=30)
    assert (outcome.kind, outcome.error) == ("lease_lost", "gone") and marks == []


async def test_the_render_adapter_hands_its_lease_down_and_bails_when_it_is_gone(monkeypatch):
    from app.services.orchestration_adapters.video_studio import VideoRenderAdapter

    calls: list = []

    async def run_isolated(render_id, **options):
        calls.append(options)
        return runners.JobOutcome("lease_lost", "The render's lease is gone: Execution x is pending")

    monkeypatch.setattr(render_process, "run_isolated", run_isolated)
    execution = SimpleNamespace(id=uuid.uuid4(), payload={"render_id": "r1"}, attempt_count=1, max_attempts=2)
    ctx = SimpleNamespace(db=SimpleNamespace(commit=_noop), worker_id="host:1:abc", cancel_event=None)
    with pytest.raises(engine.LeaseLostError, match="lease is gone"):
        await VideoRenderAdapter().execute(ctx, execution)
    assert calls[0]["lease"] == (execution.id, "host:1:abc") and calls[0]["last_attempt"] is False


# ── The engine says what it saw ───────────────────────────────────────────


class _Rows:
    def __init__(self, rows):
        self.rows = rows

    async def execute(self, *_args, **_kwargs):
        return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: list(self.rows)))

    async def flush(self):
        return None


def _leased(**fields) -> OrchestrationExecution:
    now = datetime.now(UTC)
    values = dict(
        id=uuid.uuid4(),
        adapter="video_render",
        state="running",
        adapter_state=None,
        payload={"render_id": "r1"},
        run_at=now - timedelta(minutes=5),
        deadline_at=None,
        lease_owner="host:1:abc",
        lease_expires_at=now - timedelta(seconds=14),
        attempt_count=2,
        max_attempts=2,
        last_error=None,
        dedup_key=None,
        updated_at=now - timedelta(seconds=74),
    )
    values.update(fields)
    return OrchestrationExecution(**values)


async def test_an_expired_lease_says_when_its_holder_was_last_heard_from_never_a_crash(caplog):
    exhausted = _leased()
    retried = _leased(attempt_count=1)
    with caplog.at_level(logging.WARNING, logger=engine.__name__):
        dead = await engine.reclaim_expired_leases(_Rows([exhausted, retried]), limit=10)
    assert dead == [exhausted]
    assert exhausted.state == "dead_letter"
    assert exhausted.last_error == "Lease expired, last heartbeat 74 s ago, with no attempt left."
    assert retried.state == "pending"
    assert retried.last_error == "Lease expired, last heartbeat 74 s ago: run again (1 of 2 attempts used)."
    for row in (exhausted, retried):
        assert "crash" not in row.last_error
    assert exhausted.payload["render_id"] == "r1"
    expiry = engine.lease_expiry(exhausted)
    assert expiry is not None and (expiry.silent_seconds, expiry.owner, expiry.attempt) == (74, "host:1:abc", 2)
    assert expiry.describe() == "last heartbeat 74 s ago"
    assert "the lease of host:1:abc expired, last heartbeat 74 s ago -> dead_letter (attempt 2 of 2)" in caplog.text


async def test_an_expiry_of_an_earlier_attempt_is_not_the_current_ones():
    row = _leased(attempt_count=1)
    await engine.reclaim_expired_leases(_Rows([row]), limit=10)
    assert engine.lease_expiry(row) is not None
    row.attempt_count = 2  # the re-run claimed it, then ended another way
    assert engine.lease_expiry(row) is None
    row.payload = {engine.LEASE_EXPIRY_PAYLOAD_KEY: {"attempt": 2, "last_heartbeat_at": "not a date"}}
    assert engine.lease_expiry(row) is None


@pytest.fixture
def job(monkeypatch):
    # Its last progress 10 minutes before its lease lapsed: it had stopped moving.
    row = SimpleNamespace(
        status="running", progress=63, stage="Drawing frames (82%)", progress_at=datetime.now(UTC) - timedelta(minutes=10)
    )
    state = SimpleNamespace(row=row, marks=[])

    async def load(kind, job_id):
        return state.row

    async def mark(kind, job_id, **changes):
        state.marks.append(changes)

    monkeypatch.setattr(runners.jobs, "load", load)
    monkeypatch.setattr(runners.jobs, "mark", mark)
    return state


async def test_a_job_whose_lease_expired_says_where_it_was_and_when_it_was_last_heard_from(job):
    row = _leased()
    await engine.reclaim_expired_leases(_Rows([row]), limit=10)
    from app.services.orchestration_adapters.video_studio import VideoRenderAdapter

    await VideoRenderAdapter().on_dead_letter(SimpleNamespace(db=SimpleNamespace(commit=_noop)), row)
    assert job.marks[0]["status"] == "failed"
    assert job.marks[0]["error"] == (
        "Lease expired at 63% (Drawing frames (82%)), last heartbeat 74 s ago, with no attempt left."
    )


@pytest.fixture
def requeues(monkeypatch, job):
    """The render's execution queued again on a session of its own: ``(execution id, payload, last error)`` each time."""
    state = SimpleNamespace(calls=[], cancelled=[], woken=0, open=True)

    @contextlib.asynccontextmanager
    async def session():
        yield SimpleNamespace(commit=_noop)

    async def requeue_execution(db, *, execution_id, run_at=None):
        row = SimpleNamespace(id=execution_id, payload={"render_id": "r1"}, last_error=None)
        state.calls.append(row)
        return row

    async def cancel_execution(db, *, execution_id):
        state.cancelled.append(execution_id)

    async def mark_if_open(kind, job_id, **changes):
        job.marks.append(changes)
        return state.open

    async def wake():
        state.woken += 1

    from app.core import database

    monkeypatch.setattr(database, "async_session", session)
    monkeypatch.setattr(engine, "requeue_execution", requeue_execution)
    monkeypatch.setattr(engine, "cancel_execution", cancel_execution)
    monkeypatch.setattr(jobs, "mark_if_open", mark_if_open)
    monkeypatch.setattr(jobs, "wake", wake)
    return state


async def test_a_render_whose_lease_lapsed_while_it_still_moved_runs_again_instead_of_failing(job, requeues):
    """"Lease expired at 59% ..., with no attempt left" on a render drawing frames - resumed now."""
    from app.services.orchestration_adapters.video_studio import VideoRenderAdapter

    job.row.progress_at = datetime.now(UTC) - timedelta(seconds=3)
    row = _leased(deadline_at=datetime.now(UTC) + timedelta(hours=2))
    await engine.reclaim_expired_leases(_Rows([row]), limit=10)
    assert row.state == "dead_letter"
    await VideoRenderAdapter().on_dead_letter(SimpleNamespace(db=SimpleNamespace(commit=_noop)), row)
    [requeued] = requeues.calls
    assert requeued.id == row.id and requeued.payload[render_process.LEASE_RESUMES_KEY] == 1
    assert "while it still moved" in requeued.last_error
    # Never failed: queued again, its stage saying why, the worker woken to take it.
    assert all(mark.get("status") != "failed" for mark in job.marks)
    assert job.marks[0]["status"] == "pending" and job.marks[0]["stage"].startswith("Resuming after a lost lease at 63%")
    assert requeues.woken == 1 and requeues.cancelled == []


async def test_a_render_that_had_stopped_moving_still_fails_with_no_attempt_left(job, requeues):
    from app.services.orchestration_adapters.video_studio import VideoRenderAdapter

    row = _leased()
    await engine.reclaim_expired_leases(_Rows([row]), limit=10)
    await VideoRenderAdapter().on_dead_letter(SimpleNamespace(db=SimpleNamespace(commit=_noop)), row)
    assert requeues.calls == []
    assert job.marks[-1]["status"] == "failed" and "with no attempt left" in job.marks[-1]["error"]


async def test_a_render_resumed_after_lost_leases_as_many_times_as_allowed_fails_the_next(job, requeues):
    job.row.progress_at = datetime.now(UTC)
    row = _leased(payload={"render_id": "r1", render_process.LEASE_RESUMES_KEY: render_process.MAX_LEASE_RESUMES})
    await engine.reclaim_expired_leases(_Rows([row]), limit=10)
    assert not await render_process.resume_after_lost_lease(row, engine.lease_expiry(row))
    past = _leased(deadline_at=datetime.now(UTC) - timedelta(seconds=1))
    await engine.reclaim_expired_leases(_Rows([past]), limit=10)
    assert not await render_process.resume_after_lost_lease(past, engine.lease_expiry(past))
    assert requeues.calls == []


async def test_a_render_cancelled_while_it_was_queued_again_never_runs(job, requeues):
    job.row.progress_at = datetime.now(UTC)
    requeues.open = False
    row = _leased()
    await engine.reclaim_expired_leases(_Rows([row]), limit=10)
    assert await render_process.resume_after_lost_lease(row, engine.lease_expiry(row))
    assert requeues.cancelled == [row.id] and requeues.woken == 0


async def test_a_job_the_engine_gave_up_on_otherwise_keeps_its_error_and_where_it_was(job):
    await runners.dead_letter("render", "r1", "Deadline exceeded.")
    assert job.marks[0]["error"] == "Deadline exceeded. It was at 63% (Drawing frames (82%))."


# ── The worker's loop stays free ──────────────────────────────────────────


async def test_the_supervisors_proc_sampling_never_blocks_the_workers_event_loop(monkeypatch):
    def slow_sample(pid):
        time.sleep(0.3)  # a busy Chromium's smaps_rollup
        return {}, None

    monkeypatch.setattr(render_process, "_sample", slow_sample)
    ticks = 0

    async def tick():
        nonlocal ticks
        while True:
            await asyncio.sleep(0.02)
            ticks += 1

    ticker = asyncio.create_task(tick())
    body = "import time\ntime.sleep(1.2)\n"
    try:
        await asyncio.wait_for(
            render_process.supervise(
                [sys.executable, "-c", body], memory_limit=None, stall_seconds=0, poll_seconds=0.05
            ),
            60,
        )
    finally:
        ticker.cancel()
    # Blocked by each sample, the ticker would tick a few times a second.
    assert ticks >= 30


async def test_a_heartbeat_that_fails_is_said_with_how_long_the_lease_has_left(monkeypatch, caplog):
    async def failing(*_args, **_kwargs):
        raise ConnectionError("database unreachable")

    @contextlib.asynccontextmanager
    async def session():
        yield SimpleNamespace(commit=_noop)

    monkeypatch.setattr(orchestration_reconciler.settings, "orchestration_lease_seconds", 3)
    monkeypatch.setattr(orchestration_reconciler, "async_session", session)
    monkeypatch.setattr(orchestration_reconciler.engine, "extend_lease", failing)
    execution_id = uuid.uuid4()
    with caplog.at_level(logging.WARNING, logger=orchestration_reconciler.__name__):
        beat = asyncio.create_task(orchestration_reconciler._heartbeat(execution_id, "w"))
        await asyncio.sleep(1.3)
        beat.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await beat
    assert f"Heartbeat of execution {execution_id} failed (1 in a row" in caplog.text
    assert "the lease lasts 3 s" in caplog.text and "database unreachable" in caplog.text
