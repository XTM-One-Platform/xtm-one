"""A render in a process of its own: its crash, its stall and its memory are its own.

The supervisor runs stand-in render processes - a few lines of Python that
report like ``render_process`` does, then stall, crash, grow or finish - and
must say how each one ended, at once, with where it was and its peak memory.
``settle`` then turns that into the render row and the job outcome: one retry
after a crash, never after a stall or the memory ceiling.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import textwrap
import time
from types import SimpleNamespace

import pytest

from app.core.config import settings
from app.models.video_studio import JOB_CANCELLED, JOB_FAILED, JOB_PENDING, JOB_READY, JOB_RUNNING
from app.services.video_studio import render_process, resources

posix = pytest.mark.skipif(os.name != "posix", reason="signals and process groups")
linux = pytest.mark.skipif(not sys.platform.startswith("linux"), reason="reads /proc")

_DRAWING = ["scene 7 (clip)", "scene 8 (statement)"]


def _child(body: str) -> list[str]:
    """A stand-in render process: *body* runs after a ``report(event)`` helper is defined."""
    prelude = textwrap.dedent(
        """
        import json, os, signal, sys, time
        def report(event):
            sys.stdout.write(json.dumps(event) + "\\n")
            sys.stdout.flush()
        """
    )
    return [sys.executable, "-c", prelude + textwrap.dedent(body)]


def _progress(value: int = 34) -> str:
    event = {"type": "progress", "progress": value, "stage": "Drawing frames (29%)", "drawing": _DRAWING}
    return f"report({json.dumps(event)})\n"


class _StartupFreeClock:
    """``render_process``'s ``time``, its ``monotonic`` standing still until *attempt*'s first progress is read.

    ``supervise`` arms the stall clock when it spawns the process; a stand-in is a fresh interpreter whose
    start alone can outlast a 1 s stall interval on a loaded runner. Measured from the first progress the
    supervisor read, the interval under test is the render's own, whatever the interpreter's start took.
    """

    def __init__(self, attempt: render_process._Attempt) -> None:
        self._attempt = attempt
        self._origin = time.monotonic()
        self._moving_since: float | None = None

    def monotonic(self) -> float:
        if not self._attempt.progress:
            return self._origin
        if self._moving_since is None:
            self._moving_since = time.monotonic()
        return self._origin + time.monotonic() - self._moving_since

    def __getattr__(self, name: str):
        return getattr(time, name)


async def _supervise_from_first_progress(monkeypatch, body: str, **options) -> render_process.RenderExit:
    """``_supervise`` with the stall interval counted from the first progress the process reports."""
    attempt = render_process._Attempt()
    monkeypatch.setattr(render_process, "time", _StartupFreeClock(attempt))
    return await _supervise(body, attempt=attempt, **options)


async def _supervise(body: str, **options) -> render_process.RenderExit:
    options.setdefault("memory_limit", None)
    options.setdefault("stall_seconds", 30.0)
    options.setdefault("poll_seconds", 0.1)
    return await asyncio.wait_for(render_process.supervise(_child(body), **options), 60)


async def test_a_render_that_finishes_ends_with_its_own_outcome():
    ended = await _supervise(_progress() + 'report({"type": "outcome", "kind": "done", "error": None})\n')
    assert ended.kind == "done" and ended.returncode == 0 and ended.progress == 34


async def test_a_failure_the_render_reported_is_retried_by_the_engine_not_settled_here():
    ended = await _supervise('report({"type": "outcome", "kind": "retry", "error": "The provider was down"})\n')
    assert (ended.kind, ended.error) == ("retry", "The provider was down")


@posix
async def test_a_render_killed_by_a_signal_is_a_crash_named_with_its_signal_and_where_it_was():
    started = time.monotonic()
    ended = await _supervise(_progress() + "os.kill(os.getpid(), signal.SIGKILL)\n")
    assert time.monotonic() - started < 15  # seen at once, never through a lease
    assert ended.kind == "crashed" and ended.returncode == -9
    message = ended.describe(ceiling=None, stall_seconds=120)
    assert "was killed by signal 9 (SIGKILL)" in message
    assert "at 34% (Drawing frames (29%)), drawing scene 7 (clip) and scene 8 (statement)" in message
    assert "its memory peaked at" in message


async def test_a_render_that_exits_without_a_result_is_a_crash_with_its_code():
    ended = await _supervise(_progress() + "sys.exit(3)\n")
    assert ended.kind == "crashed"
    assert "exited with code 3" in ended.describe(ceiling=None, stall_seconds=120)


async def test_a_render_whose_progress_stands_still_is_stopped_as_stalled(monkeypatch):
    started = time.monotonic()
    ended = await _supervise_from_first_progress(monkeypatch, _progress(40) + "time.sleep(120)\n", stall_seconds=1.0)
    assert time.monotonic() - started < 15
    assert ended.kind == "stalled"
    message = ended.describe(ceiling=None, stall_seconds=1.0)
    assert message.startswith("The render stalled at 40% (Drawing frames (29%))") and "no progress for 1 s" in message


async def test_a_render_that_keeps_moving_is_never_taken_for_a_stalled_one(monkeypatch):
    body = (
        "for i in range(12):\n    report({'type': 'progress', 'progress': 10 + i, 'stage': 'x'})\n    time.sleep(0.2)\n"
    )
    body += 'report({"type": "outcome", "kind": "done", "error": None})\n'
    ended = await _supervise_from_first_progress(monkeypatch, body, stall_seconds=1.0)
    assert ended.kind == "done" and ended.progress == 21


@linux
async def test_a_render_past_its_memory_ceiling_is_stopped_with_its_peak():
    body = _progress() + "hog = b'x' * (400 * 1024 * 1024)\ntime.sleep(120)\n"
    ended = await _supervise(body, memory_limit=200 * 1024 * 1024)
    assert ended.kind == "memory" and ended.peak_memory > 200 * 1024 * 1024
    message = ended.describe(ceiling=200 * 1024 * 1024, stall_seconds=120)
    assert "past its 200 MiB memory ceiling" in message and "drawing scene 7 (clip)" in message


@posix
async def test_a_cancelled_render_is_asked_to_stop_then_killed():
    body = "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n" + _progress() + "time.sleep(120)\n"
    cancel = asyncio.Event()
    cancel.set()
    ended = await _supervise(body, cancel_event=cancel, cancel_grace_seconds=0.5)
    assert ended.kind == "cancelled"


@linux
async def test_what_a_crashed_render_started_does_not_outlive_it(tmp_path):
    marker = tmp_path / "orphan.pid"
    body = f"""
        import subprocess
        orphan = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"], start_new_session=True)
        open({str(marker)!r}, "w").write(str(orphan.pid))
        report({{"type": "progress", "progress": 50, "stage": "x"}})
        time.sleep(0.6)
        os.kill(os.getpid(), signal.SIGKILL)
    """
    ended = await _supervise(body)
    assert ended.kind == "crashed"
    orphan = int(marker.read_text())
    for _ in range(50):
        if not _alive(orphan):
            break
        await asyncio.sleep(0.1)
    assert not _alive(orphan)


async def test_a_process_one_sample_saw_is_killed_when_a_later_sample_misses_it(monkeypatch):
    """A sample taken as the render dies finds its children already reparented."""
    orphan = 2**22 + 7
    samples = iter([{orphan: 42}])

    def sample(pid):
        return next(samples, {pid: 1}), None

    killed: list[dict] = []
    monkeypatch.setattr(render_process, "_sample", sample)
    monkeypatch.setattr(render_process, "_kill", lambda process, members: killed.append(dict(members)))
    await _supervise("time.sleep(0.6)\n")
    assert killed[-1].get(orphan) == 42


def _alive(pid: int) -> bool:
    """Running, not a zombie left for a parent that never reaps it."""
    try:
        with open(f"/proc/{pid}/stat") as stat:
            return stat.read().rsplit(")", 1)[1].split()[0] not in ("Z", "X")
    except OSError:
        return False


# ── Settling the render row ───────────────────────────────────────────────


@pytest.fixture
def row(monkeypatch):
    state = SimpleNamespace(status=JOB_RUNNING, marks=[])

    async def load(kind, job_id):
        return SimpleNamespace(status=state.status)

    async def mark_if_open(kind, job_id, **changes):
        if state.status not in (JOB_PENDING, JOB_RUNNING):
            return False
        state.marks.append(changes)
        state.status = changes.get("status", state.status)
        return True

    monkeypatch.setattr(render_process.jobs, "load", load)
    monkeypatch.setattr(render_process.jobs, "mark_if_open", mark_if_open)
    return state


def _crash(**fields) -> render_process.RenderExit:
    values = {
        "kind": "crashed",
        "returncode": -9,
        "peak_memory": 5 << 30,
        "progress": 34,
        "stage": "Drawing frames (29%)",
    }
    values.update(fields)
    return render_process.RenderExit(drawing=list(_DRAWING), **values)


async def test_a_first_crash_runs_the_render_once_more_at_the_same_quality(row):
    outcome = await render_process.settle("r", _crash(), last_attempt=False, ceiling=None, stall_seconds=120)
    assert outcome.kind == "retry" and "signal 9 (SIGKILL)" in outcome.error
    assert row.status == JOB_PENDING and row.marks[0]["stage"].startswith(
        "Starting again: The render process was killed"
    )


async def test_a_crash_on_the_last_attempt_fails_the_render_with_its_peak_memory_and_the_scene_drawn(row):
    outcome = await render_process.settle(
        "r", _crash(), last_attempt=True, ceiling=None, stall_seconds=120, attempt=(3, 3)
    )
    assert outcome.kind == "failed"
    error = row.marks[0]["error"]
    assert row.status == JOB_FAILED
    assert "peaked at 5.0 GiB" in error and "drawing scene 7 (clip) and scene 8 (statement)" in error
    assert error.endswith("This was its last attempt (3 of 3).")


@pytest.mark.parametrize("kind", ["memory", "stalled"])
async def test_the_memory_ceiling_and_a_stall_fail_the_render_at_once(row, kind):
    outcome = await render_process.settle(
        "r", _crash(kind=kind), last_attempt=False, ceiling=4 << 30, stall_seconds=120
    )
    assert outcome.kind == "failed" and row.status == JOB_FAILED


async def test_a_render_that_finished_before_its_process_went_down_is_left_as_it_is(row):
    row.status = JOB_READY
    outcome = await render_process.settle("r", _crash(), last_attempt=True, ceiling=None, stall_seconds=120)
    assert outcome.kind == "done" and row.marks == []


async def test_a_cancelled_render_says_cancelled(row):
    outcome = await render_process.settle(
        "r", _crash(kind="cancelled"), last_attempt=True, ceiling=None, stall_seconds=120
    )
    assert outcome.kind == "done" and row.status == JOB_CANCELLED


async def test_a_render_replaced_while_its_process_went_down_stays_replaced(row, monkeypatch):
    """The row read in progress, cancelled by a newer render before the failure is written."""
    async def load(kind, job_id):
        loaded = SimpleNamespace(status=row.status)
        row.status = JOB_CANCELLED  # replaced right after the read
        return loaded

    monkeypatch.setattr(render_process.jobs, "load", load)
    outcome = await render_process.settle("r", _crash(), last_attempt=True, ceiling=None, stall_seconds=120)
    assert outcome.kind == "done" and row.status == JOB_CANCELLED and row.marks == []
    row.status = JOB_RUNNING
    outcome = await render_process.settle("r", _crash(), last_attempt=False, ceiling=None, stall_seconds=120)
    assert outcome.kind == "done" and row.status == JOB_CANCELLED and row.marks == []


# ── The ceiling ───────────────────────────────────────────────────────────


def test_the_memory_ceiling_is_the_configured_one_or_a_share_of_the_container(monkeypatch):
    monkeypatch.setattr(settings, "video_studio_render_memory_mb", 0)
    monkeypatch.setattr(settings, "video_studio_preview_reserve_mb", 8192)
    monkeypatch.setattr(resources, "memory_limit", lambda: 24 << 30)
    assert render_process.memory_ceiling() == int((24 << 30) * 0.6)
    # A worker that draws no preview keeps no reserve: its renders have the share of a small container too.
    monkeypatch.setattr(settings, "video_studio_preview_concurrency", 0)
    monkeypatch.setattr(resources, "memory_limit", lambda: 8 << 30)
    assert render_process.memory_ceiling() == int((8 << 30) * 0.6)
    monkeypatch.setattr(resources, "memory_limit", lambda: None)
    monkeypatch.setattr(resources, "host_memory", lambda: None)
    assert render_process.memory_ceiling() == 4096 << 20
    monkeypatch.setattr(settings, "video_studio_render_memory_mb", 3000)
    assert render_process.memory_ceiling() == 3000 << 20


def test_the_render_process_runs_the_render_module_from_the_backend_root(tmp_path):
    command, cwd = render_process._command("abc", str(tmp_path))
    assert command[1:] == ["-m", "app.services.video_studio.render_process", "abc", str(tmp_path)]
    assert os.path.isdir(os.path.join(cwd, "app"))


async def test_the_working_directory_of_a_killed_render_is_removed(monkeypatch):
    seen: list[str] = []

    async def killed(command, **_options):
        workdir = command[-1]
        seen.append(workdir)
        with open(os.path.join(workdir, "chunk000.mp4"), "wb") as chunk:
            chunk.write(b"x" * 1024)
        return render_process.RenderExit(kind="memory", returncode=-9, peak_memory=6 << 30)

    async def settled(render_id, ended, **_options):
        return render_process.JobOutcome("failed", ended.kind)

    monkeypatch.setattr(render_process, "supervise", killed)
    monkeypatch.setattr(render_process, "settle", settled)
    outcome = await render_process.run_isolated("r")
    assert outcome.kind == "failed" and seen and not os.path.exists(seen[0])
