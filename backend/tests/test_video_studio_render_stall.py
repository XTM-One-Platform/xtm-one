"""A stalled render is resumed in place, its finished frames kept. Synthetic videos and processes only.

* the segments a render finished are kept for a resume of the same drawing,
  and dropped for another one; a segment still being written never counts;
* the supervisor: a stall resumes the render in the same working directory,
  its lease renewed, each stall recorded on the row with the frames kept;
  past ``MAX_STALL_RESUMES`` it fails as stalled, saying it was resumed; a
  lease lost meanwhile stops it without touching its row;
* ``get_video_jobs`` (the render's answer) says each stall;
* in Chromium: a render drawn again in the same working directory draws only
  the segment it had not finished, and the video has every frame.
"""

from __future__ import annotations

import asyncio
import math
import os
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.models.video_studio import JOB_CANCELLED, JOB_FAILED, JOB_PENDING, JOB_RUNNING, VideoRender
from app.services.orchestration_engine import LeaseLostError
from app.services.video_studio import compositor, encoder, jobs, render_process, timeline
from app.services.video_studio.spec import QUALITY_PRESETS, parse_video_spec, validate_video_spec

# ── The segments kept ─────────────────────────────────────────────────────


def _segment(directory: Path, first: int, end: int, size: int = 64) -> Path:
    path = directory / compositor.segment_name(first, end)
    path.write_bytes(b"x" * size)
    return path


def test_the_segments_of_the_same_drawing_are_kept_and_another_drawing_starts_anew(tmp_path):
    directory = tmp_path / compositor.SEGMENTS_DIR
    assert compositor.kept_segments(directory, "d1") == {} and compositor.frames_kept(tmp_path) == 0
    first, second = _segment(directory, 0, 300), _segment(directory, 300, 600)
    # One being written when the render stopped, and an empty one: neither is a finished segment.
    (directory / "seg-0000600-0000900.part.mp4").write_bytes(b"x" * 64)
    _segment(directory, 900, 1200, size=0)
    assert compositor.kept_segments(directory, "d1") == {(0, 300): first, (300, 600): second}
    assert compositor.frames_kept(tmp_path) == 600
    # Another drawing (the video saved meanwhile): nothing kept, the directory made anew.
    assert compositor.kept_segments(directory, "d2") == {} and not first.exists()
    assert compositor.frames_kept(tmp_path) == 0


def test_a_page_draws_its_frames_in_segments_of_a_few_seconds():
    assert compositor.segment_ranges(0, 700, 300) == [(0, 300), (300, 600), (600, 700)]
    assert compositor.segment_ranges(450, 451, 300) == [(450, 451)]
    document = {"frames": 10, "fps": 30, "scenes": []}
    preset = QUALITY_PRESETS["draft"]
    digest = compositor.drawing_digest(document, preset, (1280, 720), (0, 10))
    assert digest == compositor.drawing_digest(dict(document), preset, (1280, 720), (0, 10))
    assert digest != compositor.drawing_digest({**document, "frames": 11}, preset, (1280, 720), (0, 10))
    assert digest != compositor.drawing_digest(document, QUALITY_PRESETS["high"], (1920, 1080), (0, 10))


# ── The supervisor ────────────────────────────────────────────────────────


def _stalled() -> render_process.RenderExit:
    return render_process.RenderExit(
        kind="stalled", returncode=-9, peak_memory=2 << 30, progress=63, stage="Drawing frames (82%)", drawing=["scene 7 (clip)"]
    )


@pytest.fixture
def supervised(monkeypatch, tmp_path):
    """``run_isolated`` over processes a test scripts: each attempt's exit, the row, the lease."""
    world = SimpleNamespace(
        exits=[], workdirs=[], row=SimpleNamespace(status=JOB_RUNNING, stalls=None), marks=[], renewed=[], lost=None
    )
    monkeypatch.setattr(render_process, "WORKDIR_ROOT", tmp_path)

    async def supervise(command, **_options):
        workdir = command[-1]
        world.workdirs.append(workdir)
        # Each attempt finds what the one before finished; this one finishes one more segment.
        directory = Path(workdir) / compositor.SEGMENTS_DIR
        if not directory.exists():
            compositor.kept_segments(directory, "the drawing")
        done = len(compositor.kept_segments(directory, "the drawing"))
        _segment(directory, done * 300, (done + 1) * 300)
        return world.exits.pop(0)

    async def load(_kind, _job_id):
        return world.row

    async def mark(_kind, _job_id, **changes):
        world.marks.append(changes)
        for key, value in changes.items():
            setattr(world.row, key, value)

    async def mark_if_open(kind, job_id, **changes):
        if world.row.status not in (JOB_RUNNING, JOB_PENDING):
            return False
        await mark(kind, job_id, **changes)
        return True

    async def heartbeat(*_args, **_kwargs):
        return None

    async def renew(execution_id, owner, *, lease_seconds=None):
        if world.lost:
            raise LeaseLostError(world.lost)
        # Taken again for the render's own lease, never the engine's.
        assert lease_seconds == jobs.RENDER_LEASE_SECONDS
        world.renewed.append((execution_id, owner))

    monkeypatch.setattr(render_process, "supervise", supervise)
    monkeypatch.setattr(render_process.jobs, "load", load)
    monkeypatch.setattr(render_process.jobs, "mark", mark)
    monkeypatch.setattr(render_process.jobs, "mark_if_open", mark_if_open)
    monkeypatch.setattr(render_process.jobs, "heartbeat", heartbeat)
    monkeypatch.setattr(render_process, "renew", renew)
    return world


LEASE = (uuid.UUID("00000000-0000-0000-0000-0000000061f9"), "worker-1")


async def test_a_stalled_render_is_resumed_in_place_with_its_frames_kept(supervised):
    supervised.exits = [_stalled(), _stalled(), render_process.RenderExit(kind="done")]
    outcome = await render_process.run_isolated("r", last_attempt=True, lease=LEASE)
    assert outcome.kind == "done"
    # The same working directory every time, its lease taken again before each resume; removed at the end.
    assert len(set(supervised.workdirs)) == 1 and supervised.renewed == [LEASE, LEASE]
    assert not os.path.exists(supervised.workdirs[0])
    stalls = supervised.row.stalls
    assert [(s["resumed"], s["frames_kept"], s["progress"], s["seconds"]) for s in stalls] == [
        (True, 300, 63, 120),
        (True, 600, 63, 120),
    ]
    assert stalls[0]["stage"] == "Drawing frames (82%)" and stalls[0]["drawing"] == ["scene 7 (clip)"]
    assert supervised.marks[0]["stage"] == "Resuming after a stall at 63%: 300 frames kept"


async def test_a_render_still_stalling_past_its_resumes_fails_saying_so(supervised):
    supervised.exits = [_stalled() for _ in range(render_process.MAX_STALL_RESUMES + 1)]
    outcome = await render_process.run_isolated("r", last_attempt=False, lease=LEASE)
    assert outcome.kind == "failed" and supervised.row.status == JOB_FAILED
    error = supervised.marks[-1]["error"]
    assert error.startswith("The render stalled at 63% (Drawing frames (82%)), drawing scene 7 (clip): no progress for 120 s")
    assert error.endswith(f"It had been resumed in place {render_process.MAX_STALL_RESUMES} times after earlier stalls.")
    assert [s["resumed"] for s in supervised.row.stalls] == [True] * render_process.MAX_STALL_RESUMES + [False]


async def test_a_render_cancelled_while_it_stood_still_keeps_its_cancel(supervised):
    supervised.row.status, supervised.row.error = JOB_CANCELLED, "Replaced by a newer render of the same video."
    stall = await render_process.record_stall("r", _stalled(), stall_seconds=120, frames_kept=300, resumed=True)
    assert stall["frames_kept"] == 300 and supervised.marks == [] and supervised.row.stalls is None


async def test_a_lease_lost_while_it_stood_still_leaves_the_row_to_the_engine(supervised):
    supervised.exits, supervised.lost = [_stalled()], "Execution is dead_letter"
    outcome = await render_process.run_isolated("r", last_attempt=True, lease=LEASE)
    assert outcome.kind == "lease_lost" and "dead_letter" in outcome.error
    # Its working directory is kept for the attempt that may follow; the sweep removes it once the render ended.
    assert supervised.marks == [] and os.path.exists(supervised.workdirs[0])


# ── Attempts that resume ──────────────────────────────────────────


async def test_a_render_whose_lease_was_lost_resumes_its_next_attempt_where_it_stopped(supervised):
    supervised.exits = [render_process.RenderExit(kind="lease_lost", error="The render's lease is gone")]
    first = await render_process.run_isolated("r", last_attempt=False, lease=LEASE, attempt=(1, 3))
    assert first.kind == "lease_lost"
    workdir = Path(supervised.workdirs[0])
    # What the attempt finished stays for the next one: a segment, and the frames of a figure it was drawing.
    assert workdir == render_process.workdir_of("r") and compositor.frames_kept(workdir) == 300
    figure = workdir / "figure-s05-platform"
    figure.mkdir()
    for index in range(285):
        (figure / f"f{index:05d}.jpg").write_bytes(b"jpeg")

    supervised.exits = [render_process.RenderExit(kind="done")]
    second = await render_process.run_isolated("r", last_attempt=False, lease=LEASE, attempt=(2, 3))
    assert second.kind == "done"
    # The same directory: the second attempt finished the next segment, never the first again.
    assert supervised.workdirs == [str(workdir), str(workdir)]
    assert supervised.marks[0]["stage"] == "Resuming (attempt 2 of 3): 300 frames and 285 figure frames kept"
    assert not workdir.exists()


async def test_a_crash_runs_the_render_again_with_what_it_finished_kept(supervised):
    supervised.exits = [render_process.RenderExit(kind="crashed", returncode=-11, progress=40)]
    retried = await render_process.run_isolated("r", last_attempt=False, lease=LEASE, attempt=(1, 3))
    assert retried.kind == "retry" and supervised.row.status == JOB_PENDING
    assert compositor.frames_kept(supervised.workdirs[0]) == 300
    supervised.exits = [render_process.RenderExit(kind="crashed", returncode=-11, progress=40)]
    failed = await render_process.run_isolated("r", last_attempt=True, lease=LEASE, attempt=(3, 3))
    assert failed.kind == "failed" and failed.error.endswith("This was its last attempt (3 of 3).")
    assert not os.path.exists(supervised.workdirs[0])


async def test_a_newer_attempt_stops_the_one_before_it_on_this_worker_before_it_starts(supervised, monkeypatch):
    """The engine gave the execution to a new attempt while the one before still ran here: they share its directory."""
    events: list[str] = []
    started = asyncio.Event()

    async def supervise(command, *, superseded=None, **_options):
        supervised.workdirs.append(command[-1])
        if len(supervised.workdirs) == 1:
            events.append("first started")
            started.set()
            await superseded.wait()
            events.append("first stopped")
            return render_process.RenderExit(kind="superseded")
        events.append("second started")
        return render_process.RenderExit(kind="done")

    monkeypatch.setattr(render_process, "supervise", supervise)
    first = asyncio.create_task(render_process.run_isolated("r", last_attempt=False, lease=LEASE, attempt=(1, 3)))
    await asyncio.wait_for(started.wait(), 5)
    second = await asyncio.wait_for(render_process.run_isolated("r", last_attempt=False, lease=LEASE, attempt=(2, 3)), 5)
    stopped = await first
    assert events == ["first started", "first stopped", "second started"]
    assert stopped.kind == "lease_lost" and "newer attempt" in stopped.error and second.kind == "done"
    assert supervised.workdirs[0] == supervised.workdirs[1] and render_process._ATTEMPTS == {}


async def test_the_sweep_removes_the_working_directories_no_attempt_will_resume(monkeypatch, tmp_path):
    import contextlib

    from app.core import database

    monkeypatch.setattr(render_process, "WORKDIR_ROOT", tmp_path)
    ended, running, gone, here = (uuid.uuid4() for _ in range(4))

    def made(name: str, *, hours_old: float = 0.0) -> Path:
        path = tmp_path / f"xtm-render-{name}"
        path.mkdir()
        if hours_old:
            moment = path.stat().st_mtime - hours_old * 3600
            os.utime(path, (moment, moment))
        return path

    paths = {
        "ended": made(str(ended)),
        "running": made(str(running)),
        "gone": made(str(gone)),
        "here": made(str(here), hours_old=12),
        # Untouched past the 8 h an ultra render may run.
        "old": made("k3j2_x9a", hours_old=10),
        "fresh": made("p0q1_r2s"),
    }
    monkeypatch.setitem(render_process._ATTEMPTS, str(here), render_process._Attempt())

    class _Rows:
        def all(self):
            return [(ended, "ready"), (running, "running")]

    @contextlib.asynccontextmanager
    async def session():
        async def execute(_statement):
            return _Rows()

        yield SimpleNamespace(execute=execute)

    monkeypatch.setattr(database, "async_session", session)
    await render_process.sweep_workdirs()
    assert {name for name, path in paths.items() if path.exists()} == {"running", "here", "fresh"}


def test_the_render_answer_says_each_stall():
    stalls = [
        {"progress": 63, "stage": "Drawing frames (82%)", "seconds": 120, "frames_kept": 2400, "resumed": True},
        {"progress": 71, "stage": None, "seconds": 120, "frames_kept": 0, "resumed": False},
    ]
    assert render_process.stall_line(stalls) == (
        "Stalled 2 times: at 63% (Drawing frames (82%)), no progress for 120 s, resumed in place, 2400 frames kept; "
        "at 71%, no progress for 120 s, stopped: no resume left."
    )
    assert render_process.stall_line(None) == ""


def test_get_video_jobs_says_a_render_stalled_and_was_resumed(monkeypatch):
    from app.services.builtin_tools import video_studio as tool
    from app.services.video_studio import jobs, projects
    from app.tools.base import ToolCall

    project = SimpleNamespace(id=uuid.uuid4(), title="Launch film", revision=3)
    running = VideoRender(
        id=uuid.uuid4(),
        project_id=project.id,
        revision=3,
        quality="high",
        status=JOB_RUNNING,
        progress=64,
        stage="Drawing frames (83%)",
        stalls=[{"progress": 63, "stage": "Drawing frames (82%)", "seconds": 120, "frames_kept": 2400, "resumed": True}],
    )

    async def visible(*_args, **_kwargs):
        return project

    async def manages(*_args, **_kwargs):
        return True

    async def start(*_args, **_kwargs):
        return projects.StartedRender(running, [], reused=True)

    async def waited(_kind, _ids, **_kwargs):
        return [running]

    async def states(kind, rows):
        return [jobs.job_state(kind, row) for row in rows]

    async def nothing(*_args, **_kwargs):
        return None

    class _Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return False

        async def get(self, *_args, **_kwargs):
            return SimpleNamespace(id=uuid.uuid4(), email="maker@example.com", role="user")

        async def commit(self):
            return None

    monkeypatch.setattr(projects, "load_visible_project", visible)
    monkeypatch.setattr(projects, "can_manage_project", manages)
    monkeypatch.setattr(projects, "start_render", start)
    monkeypatch.setattr(jobs, "wake", nothing)
    monkeypatch.setattr(jobs, "wait_for", waited)
    monkeypatch.setattr(jobs, "job_states", states)
    monkeypatch.setattr(tool, "async_session", lambda: _Session())
    monkeypatch.setattr(tool, "_wait_budget", lambda _default: 0.0)
    _defs, executor = tool.build_video_studio_tools(user_id=str(uuid.uuid4()))
    answer = asyncio.run(
        executor.execute_tool(ToolCall(id="r", name="render_video", arguments={"video_id": str(project.id)}))
    )
    assert "Stalled 1 time: at 63% (Drawing frames (82%)), no progress for 120 s, resumed in place, 2400 frames kept." in (
        answer.content
    )
    assert projects.serialize_render(running)["stalls"] == running.stalls


# ── In Chromium: a resume draws only what it had not finished ─────────────


def _document() -> dict:
    effective = validate_video_spec(
        {
            "title": "Resume",
            "freestyle": True,
            "scenes": [
                {"id": "open", "type": "title", "title": "Hello", "duration": 2},
                {"id": "claim", "type": "statement", "lines": ["One platform"], "duration": 2},
            ],
        }
    )
    from app.services.video_studio.themes import build_theme

    plan = timeline.plan(
        parse_video_spec(effective), timeline.PlanMedia(), fps=30, theme=build_theme(mode="dark", background_style="grid")
    )
    return plan.document


@pytest.mark.chromium
async def test_a_render_drawn_again_in_its_working_directory_draws_only_what_it_had_not_finished(tmp_path, monkeypatch):
    if not encoder.encoder_available():
        pytest.skip("ffmpeg is not installed")
    from app.services.builtin_tools.html_render import font_face_css

    monkeypatch.setattr(compositor, "SEGMENT_SECONDS", 1)
    document = _document()
    drawn: list[int] = []

    async def draw(doc: dict) -> str:
        return await compositor.render_frames(
            doc,
            compositor.CompositorAssets(),
            preset=QUALITY_PRESETS["draft"],
            output_size=(640, 360),
            workdir=str(tmp_path),
            font_css=font_face_css(),
            pages=1,
            on_frame=lambda: drawn.append(1),
        )

    try:
        video = await draw(document)
    except Exception as exc:
        text = str(exc)
        if "Executable doesn't exist" not in text and "playwright install" not in text.lower():
            raise
        if os.environ.get("REQUIRE_CHROMIUM") == "1":
            pytest.fail(f"Playwright's Chromium is required here and is not installed: {exc}")
        pytest.skip("Playwright's Chromium is not installed (python -m playwright install chromium)")
    total = int(document["frames"])
    assert len(drawn) == total and (await encoder.probe(video)).frame_count == total
    segments = sorted((tmp_path / compositor.SEGMENTS_DIR).glob("seg-*.mp4"))
    assert len(segments) == math.ceil(total / 30)
    # The render stopped while it wrote its last segment: only that one is drawn again.
    segments[-1].unlink()
    drawn.clear()
    tracker = compositor.DrawProgress()
    video = await compositor.render_frames(
        document,
        compositor.CompositorAssets(),
        preset=QUALITY_PRESETS["draft"],
        output_size=(640, 360),
        workdir=str(tmp_path),
        font_css=font_face_css(),
        pages=1,
        on_frame=lambda: drawn.append(1),
        tracker=tracker,
    )
    assert len(drawn) == total - 30 * (len(segments) - 1) and tracker.done == total
    assert (await encoder.probe(video)).frame_count == total
    # Another drawing (the video changed): nothing kept, every frame drawn.
    drawn.clear()
    await draw({**document, "background": {**(document.get("background") or {}), "seed": 7}})
    assert len(drawn) == total
