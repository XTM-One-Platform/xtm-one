"""A render in flight when its worker stops is handed to the next one, never failed. Synthetic processes and rows.

Render 41a8ae71 of the brand film (20 Oct 2026) failed "Lease expired at 38%,
no attempt left": a deploy stopped its worker, whose lease then expired.

* the supervisor stops the render process once the worker stops, and a
  worker already stopping starts none: the attempt ends ``handover``, its
  working directory kept and its stored segments too, the row saying where
  it stood;
* the render adapter hands the execution back to the queue - due now, its
  attempt unspent - and does the same when torn down while the worker stops,
  never for a timeout;
* each segment closed is stored beside the render with its screen inserts'
  audits and evidence frames, an attempt on another worker takes the segments
  of the same drawing from there with them, and they go once the render ended
  or is gone;
* an encoder flushing its frames says it still works while it does, so the
  lease of a 2160p render outlives the flush.
"""

from __future__ import annotations

import asyncio
import json
import sys
import textwrap
import time
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.core.config import settings
from app.models.video_studio import JOB_FAILED, JOB_RUNNING
from app.services.file_storage import FileNotFoundInStorageError
from app.services.orchestration_adapters import video_studio as adapters
from app.services.video_studio import compositor, encoder, jobs, render_process, render_segments, runners
from app.services.video_studio.spec import QUALITY_PRESETS


def _child(body: str) -> list[str]:
    prelude = "import json, sys, time\ndef report(event):\n    sys.stdout.write(json.dumps(event) + '\\n')\n    sys.stdout.flush()\n"
    return [sys.executable, "-c", prelude + textwrap.dedent(body)]


async def test_the_supervisor_stops_the_render_process_once_the_worker_stops():
    stopping = asyncio.Event()
    progress = {"type": "progress", "progress": 38, "stage": "Drawing frames (33%)", "drawing": ["scene 4 (figure)"]}
    asyncio.get_running_loop().call_later(0.5, stopping.set)
    started = time.monotonic()
    ended = await asyncio.wait_for(
        render_process.supervise(
            _child(f"report({json.dumps(progress)})\ntime.sleep(120)\n"),
            memory_limit=None,
            stall_seconds=60,
            stopping=stopping,
            poll_seconds=0.1,
        ),
        30,
    )
    assert ended.kind == "handed_over" and ended.progress == 38 and time.monotonic() - started < 15


@pytest.fixture
def attempt(monkeypatch, tmp_path):
    """One attempt run by ``run_isolated``: its supervisor's exit given, the row's writes and the store's forgets kept."""
    world = SimpleNamespace(marks=[], forgotten=[], supervised=[], exit=None)

    async def supervise(command, **_options):
        world.supervised.append(command[-1])
        Path(command[-1], "segments").mkdir(exist_ok=True)
        return world.exit

    async def mark_if_open(_kind, _job_id, **changes):
        world.marks.append(changes)
        return True

    async def forget(render_id):
        world.forgotten.append(render_id)
        return 0

    async def nothing(*_args, **_kwargs):
        return None

    monkeypatch.setattr(render_process, "WORKDIR_ROOT", tmp_path)
    monkeypatch.setattr(render_process, "supervise", supervise)
    monkeypatch.setattr(render_process, "sweep_workdirs", nothing)
    monkeypatch.setattr(jobs, "mark_if_open", mark_if_open)
    monkeypatch.setattr(jobs, "heartbeat", nothing)
    monkeypatch.setattr(render_segments, "forget", forget)
    return world


async def test_a_handed_over_attempt_keeps_its_work_and_says_where_it_stood(attempt):
    attempt.exit = render_process.RenderExit(kind="handed_over", progress=38, stage="Drawing frames (33%)")
    render_id = str(uuid.uuid4())
    outcome = await render_process.run_isolated(render_id, stopping=asyncio.Event())
    assert outcome.kind == "handover" and "Handed to the next worker at 38%" in outcome.error
    assert render_process.workdir_of(render_id).is_dir() and attempt.forgotten == []
    # The stop is said for what it was: asked for, never "a restart or a deploy".
    assert attempt.marks[-1]["stage"].startswith("Handed to the next worker at 38%: its worker was asked to stop (")
    assert "first in the queue" in attempt.marks[-1]["stage"]
    # An attempt that ends the render clears what it kept, here and in the store.
    attempt.exit = render_process.RenderExit(kind="done")
    assert (await render_process.run_isolated(render_id)).kind == "done"
    assert not render_process.workdir_of(render_id).exists() and attempt.forgotten == [render_id]


async def test_a_worker_already_stopping_starts_no_render_process(attempt):
    stopping = asyncio.Event()
    stopping.set()
    outcome = await render_process.run_isolated(str(uuid.uuid4()), stopping=stopping)
    assert outcome.kind == "handover" and attempt.supervised == []


def _execution():
    return SimpleNamespace(id=uuid.uuid4(), payload={"render_id": str(uuid.uuid4())}, attempt_count=3, max_attempts=3)


@pytest.fixture
def adapter(monkeypatch):
    world = SimpleNamespace(outcome=None, raises=None, deferred=[])

    async def run_isolated(_render_id, **options):
        world.options = options
        if world.raises is not None:
            raise world.raises
        return world.outcome

    async def no_wait(_render_id):
        return None

    async def defer(_db, **kwargs):
        world.deferred.append(kwargs)

    class _Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return False

        async def commit(self):
            return None

    monkeypatch.setattr(render_process, "run_isolated", run_isolated)
    monkeypatch.setattr(jobs, "preview_waits", no_wait)
    monkeypatch.setattr("app.services.orchestration_engine.defer", defer)
    monkeypatch.setattr("app.core.database.async_session", _Session)
    return world


async def _noop():
    return None


async def test_the_render_adapter_hands_a_stopping_workers_render_back_its_attempt_unspent(adapter):
    """Even on its last attempt: a deploy is no failure of the render."""
    stopping = asyncio.Event()
    adapter.outcome = runners.JobOutcome("handover", "Handed to the next worker at 38%.")
    ctx = SimpleNamespace(db=SimpleNamespace(commit=_noop), worker_id="old:1", cancel_event=None, stopping=stopping)
    result = await adapters.VideoRenderAdapter().execute(ctx, _execution())
    assert adapter.options["stopping"] is stopping
    assert (result.kind, result.backoff_seconds, result.adapter_state) == ("defer", 0.0, adapters.HANDED_OVER_STATE)
    assert result.error == "Handed to the next worker at 38%."
    # First in the queue, the stopping worker and its version recorded for what resumes it.
    assert result.place == "head"
    handed = result.payload_update[adapters.HANDED_OVER_KEY]
    assert handed["worker"] == "old:1" and handed["version"] == settings.app_version


async def test_a_render_torn_down_as_its_worker_stops_is_handed_back_never_on_a_timeout(adapter):
    stopping = asyncio.Event()
    ctx = SimpleNamespace(db=SimpleNamespace(commit=_noop), worker_id="old:1", cancel_event=None, stopping=stopping)
    adapter.raises = asyncio.CancelledError()
    # An execution timeout cancels it too: never handed back, the reconciler fails it.
    with pytest.raises(asyncio.CancelledError):
        await adapters.VideoRenderAdapter().execute(ctx, _execution())
    assert adapter.deferred == []
    stopping.set()
    execution = _execution()
    with pytest.raises(asyncio.CancelledError):
        await adapters.VideoRenderAdapter().execute(ctx, execution)
    [handed] = adapter.deferred
    assert handed["execution_id"] == execution.id and handed["worker_id"] == "old:1"
    assert handed["delay_seconds"] == 0.0 and handed["adapter_state"] == adapters.HANDED_OVER_STATE
    assert handed["place"] == "head" and handed["payload_update"][adapters.HANDED_OVER_KEY]["worker"] == "old:1"


# ── The segments kept beside the render ───────────────────────────────────


class _Store:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}

    async def upload_path(self, path, name, _content_type=None):
        self.objects[name] = Path(path).read_bytes()
        return name

    async def download(self, name):
        if name not in self.objects:
            raise FileNotFoundInStorageError(name)
        return self.objects[name]

    async def download_to_path(self, name, path, *, missing_ok=False):
        if name not in self.objects:
            raise FileNotFoundInStorageError(name)
        Path(path).write_bytes(self.objects[name])

    async def list_object_names(self, prefix, *, recursive=True):
        names = [name for name in self.objects if name.startswith(prefix)]
        if recursive:
            return names
        return sorted({prefix + name[len(prefix) :].split("/", 1)[0] + "/" for name in names})

    async def delete(self, name):
        self.objects.pop(name, None)


@pytest.fixture
def store(monkeypatch):
    from app.services import file_storage as storage_module

    kept = _Store()
    monkeypatch.setattr(storage_module, "file_storage", kept)
    return kept


_DOCUMENT = {"width": 1920, "height": 1080, "fps": 30, "frames": 600, "duration": 20.0, "scenes": []}


async def test_another_worker_resumes_from_the_segments_kept_beside_the_render(store, tmp_path, monkeypatch):
    preset = QUALITY_PRESETS["draft"]
    digest = compositor.drawing_digest(_DOCUMENT, preset, (1280, 720), (0, 600))
    first = tmp_path / "worker-a" / compositor.SEGMENTS_DIR
    (first / compositor.EVIDENCE_DIR).mkdir(parents=True)
    # Its segments as the compositor cuts them: a tenth of its frames each at most.
    pieces = compositor.segment_ranges(0, 600, compositor.segment_frames(600, 30))
    assert pieces[0] == (0, 60) and len(pieces) == 10
    # What the matted screen insert of the first segment showed, and the frame that proves it.
    shown = {
        "insert/s02.0": {
            "frames": {"12": {"covered": 0.3, "logo_visible": 1.0}},
            "evidence": {"covered": {"badness": 0.3, "frame": 12, "file": "insert-s02-0-covered-0000012.jpg"}},
        }
    }
    (first / compositor.EVIDENCE_DIR / "insert-s02-0-covered-0000012.jpg").write_bytes(b"jpeg")
    (first / (Path(compositor.segment_name(*pieces[0])).stem + compositor.AUDIT_SUFFIX)).write_text(json.dumps(shown))
    for a, b in pieces:
        segment = first / compositor.segment_name(a, b)
        segment.write_bytes(f"{a}-{b}".encode())
        await render_segments.keep("r1", digest, segment)
    # A segment of another drawing (the video edited meanwhile) is never taken.
    store.objects[render_segments.prefix_of("r1", "older") + compositor.segment_name(*pieces[0])] = b"stale"
    joined: list = []

    async def concat(parts, *, out_path, workdir):
        joined.extend(Path(part).read_bytes() for part in parts)
        Path(out_path).write_bytes(b"video")

    monkeypatch.setattr(encoder, "concat", concat)
    audits: dict = {}
    video = await compositor.render_frames(
        _DOCUMENT,
        compositor.CompositorAssets(),
        preset=preset,
        output_size=(1280, 720),
        workdir=str(tmp_path / "worker-b"),
        font_css="",
        pages=1,
        render_id="r1",
        audits=audits,
    )
    # Nothing left to draw: no browser launched, the kept segments joined.
    assert joined == [f"{a}-{b}".encode() for a, b in pieces] and Path(video).read_bytes() == b"video"
    # The insert's audit came back with its segment, its evidence frame on this worker's disk.
    assert audits["insert/s02.0"]["frames"] == {12: {"covered": 0.3, "logo_visible": 1.0}}
    proof = audits["insert/s02.0"]["evidence"]["covered"][0]
    assert Path(proof["path"]).parent.parent == tmp_path / "worker-b" / compositor.SEGMENTS_DIR
    assert Path(proof["path"]).read_bytes() == b"jpeg"


async def test_another_worker_takes_back_its_segments_stored_under_an_earlier_digest(store, tmp_path, monkeypatch):
    """A file the render made itself, made again on the next worker, changes the digest of the whole drawing;
    each segment it stored whose own frames are drawn from the same inputs is taken back, never drawn again."""
    preset = QUALITY_PRESETS["draft"]
    pieces = compositor.segment_ranges(0, 600, compositor.segment_frames(600, 30))
    windows = compositor.WindowDigests(_DOCUMENT, preset, (1280, 720), compositor.CompositorAssets())
    earlier = render_segments.prefix_of("r1", "earlier-digest")
    for a, b in pieces:
        name = compositor.segment_name(a, b)
        record = {"revision": 3, "attempt": "started 2026-11-01T18:00:00+00:00", "render": "r1", "window": windows.of(a, b)}
        store.objects[earlier + Path(name).stem + compositor.DRAWN_SUFFIX] = json.dumps(record).encode()
        store.objects[earlier + name] = f"{a}-{b}".encode()
    # One drawn from other inputs (its window differs): never taken, the one of the same window is.
    other = render_segments.prefix_of("r1", "aaa-other")
    store.objects[other + Path(compositor.segment_name(*pieces[0])).stem + compositor.DRAWN_SUFFIX] = json.dumps(
        {"revision": 2, "window": "another"}
    ).encode()
    store.objects[other + compositor.segment_name(*pieces[0])] = b"stale"
    joined: list = []

    async def concat(parts, *, out_path, workdir):
        joined.extend(Path(part).read_bytes() for part in parts)
        Path(out_path).write_bytes(b"video")

    monkeypatch.setattr(encoder, "concat", concat)
    tracker = compositor.DrawProgress()
    await compositor.render_frames(
        _DOCUMENT,
        compositor.CompositorAssets(),
        preset=preset,
        output_size=(1280, 720),
        workdir=str(tmp_path / "worker-c"),
        font_css="",
        pages=1,
        render_id="r1",
        tracker=tracker,
    )
    # Nothing drawn again: no browser launched, the segments of the worker before joined in order.
    assert joined == [f"{a}-{b}".encode() for a, b in pieces]
    assert tracker.kept == 600 and tracker.inherited_frames == 0
    assert [run["frames"] for run in tracker.taken_back] == [[0, 600]]
    # Stored again under this attempt's digest: the worker after it restores them by the strict rule.
    digest = compositor.drawing_digest(_DOCUMENT, preset, (1280, 720), (0, 600), compositor.CompositorAssets())
    assert all(store.objects[render_segments.prefix_of("r1", digest) + compositor.segment_name(a, b)] for a, b in pieces)


async def test_nothing_is_taken_back_when_the_render_stored_nothing(store, tmp_path):
    windows = compositor.WindowDigests(_DOCUMENT, QUALITY_PRESETS["draft"], (1280, 720))
    assert await render_segments.take_back("r1", "d", tmp_path, (0, 600), windows, set()) == []


async def test_the_segments_go_once_the_render_ended_or_is_gone(store, monkeypatch):
    from app.core import database

    running, ended, gone = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    for render_id in (running, ended, gone):
        store.objects[render_segments.prefix_of(str(render_id), "d") + "seg-0000000-0000300.mp4"] = b"x"
    statuses = {running: JOB_RUNNING, ended: JOB_FAILED}

    class _Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return False

        async def execute(self, _statement):
            # (id, status, relaunchable, the status of the render that replaced it at its quality).
            return SimpleNamespace(all=lambda: [(render_id, status, None, None) for render_id, status in statuses.items()])

    monkeypatch.setattr(database, "async_session", _Session)
    assert await render_segments.sweep() == 2
    assert list(store.objects) == [render_segments.prefix_of(str(running), "d") + "seg-0000000-0000300.mp4"]
    assert await render_segments.forget(str(running)) == 1 and store.objects == {}


async def test_a_render_no_worker_resumed_keeps_its_segments_for_its_relaunch(store, monkeypatch):
    """Failed because no worker took it, a relaunch of the same revision and quality resumes from them."""
    from app.core import database

    left, ended, replaced = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    for render_id in (left, ended, replaced):
        store.objects[render_segments.prefix_of(str(render_id), "d") + "seg-0000000-0000300.mp4"] = b"x"
    asked: list[str] = []

    class _Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return False

        async def execute(self, statement):
            asked.append(str(statement))
            # (id, status, relaunchable, its successor's status): two failed, only one left by a stopped worker within
            # the day; one replaced by the render of a newer revision still drawing, which takes its segments.
            return SimpleNamespace(
                all=lambda: [(left, JOB_FAILED, True, None), (ended, JOB_FAILED, None, None), (replaced, "cancelled", None, JOB_RUNNING)]
            )

    monkeypatch.setattr(database, "async_session", _Session)
    assert await render_segments.sweep() == 1
    assert sorted(store.objects) == sorted(
        render_segments.prefix_of(str(kept), "d") + "seg-0000000-0000300.mp4" for kept in (left, replaced)
    )
    assert "LEFT OUTER JOIN orchestration_executions" in asked[0]
    assert "video_renders_1.quality = video_renders.quality" in asked[0]


async def test_an_encoder_flushing_says_it_still_works_until_it_exits(monkeypatch):
    # At 2160p the lookahead of a segment's encoder outlasts the lease once its frames stop.
    monkeypatch.setattr(encoder, "WAIT_REPORT_SECONDS", 0.02)

    class _Process:
        returncode = 0
        stdin = SimpleNamespace(close=lambda: None)

        async def wait(self):
            await asyncio.sleep(0.15)
            return 0

    pipe = encoder.FramePipeEncoder(out_path="seg.mp4", fps=60, frame_format="jpeg", crf=18, preset="slow", width=3840, height=2160)
    pipe._process = _Process()
    waited = []

    async def on_wait():
        waited.append(time.monotonic())

    await pipe.finish(on_wait=on_wait)
    assert 3 <= len(waited) <= 8
    # One that never exits is killed at its timeout, whatever it said meanwhile.
    pipe._process = _Process()
    pipe._process.kill = lambda: waited.append("killed")
    with pytest.raises(encoder.EncoderError, match="did not finish in time"):
        await pipe.finish(timeout=0.05, on_wait=on_wait)
    assert waited[-1] == "killed"
