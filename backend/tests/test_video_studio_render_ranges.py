"""A render past one worker's cap drawn in scene ranges across workers, and a revision's work reused.

The fixture is a 130 s video of 30 scenes - statements, recordings, AI clips
and figures - about the length of the brand film whose ultra render a
2-minute video was refused for.

* on the documented worker (8 CPUs, request and limit; a 24 GiB container;
  ``VIDEO_STUDIO_RENDER_PAGES=0``) its ultra render fits under the cap, drawn
  whole; on a worker too small for that it is drawn in scene ranges that each
  fit, and only past every range it is refused - the refusal carrying every
  number and the quality that fits;
* the ranges are cut where a scene starts; the render draws the ranges no
  worker took, waits for the others, and restores a range drawn elsewhere
  from its stored segments - never drawing it twice;
* a range draws from what the render published (its plan and its files, a
  downloaded file by its id), never prepares the render again, and leaves a
  range it cannot read to the render;
* a deadline the render pushes out moves the reconciler's timeout of its task;
* a second render of the same revision at another quality speaks no narration
  and opens no figure page again, composes its score once and follows its
  screen inserts once.
"""

from __future__ import annotations

import asyncio
import itertools
import json
import math
import struct
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.core.config import settings
from app.services import presentation_export
from app.services.file_storage import FileNotFoundInStorageError
from app.services.video_studio import (
    compositor,
    encoder,
    figures,
    jobs,
    music,
    projects,
    render,
    render_checks,
    render_preflight,
    render_process,
    render_ranges,
    resources,
    timeline,
    voice,
    voice_check,
)
from app.services.video_studio.insert_tracking import InsertTrack
from app.services.video_studio.screen_insert import Footage, InsertSource, KeySpec
from app.services.video_studio.spec import Voiceover, parse_video_spec, validate_video_spec

_GIB = 1 << 30
USER = SimpleNamespace(id=uuid.uuid4(), role="user")
PROJECT = SimpleNamespace(id=uuid.uuid4(), revision=7, title="Brand film", scene_previews=None)
ARCH = {
    "type": "architecture",
    "nodes": [{"id": "a", "label": "OpenCTI", "row": 1, "col": 1}, {"id": "b", "label": "OpenAEV", "row": 1, "col": 2}],
    "edges": [{"source": "a", "target": "b"}],
}


@pytest.fixture(autouse=True)
def _worker(monkeypatch):
    monkeypatch.setattr(settings, "video_studio_render_pages", 0)
    monkeypatch.setattr(settings, "video_studio_render_max_ranges", 8)
    monkeypatch.setattr(settings, "video_studio_render_stall_seconds", 120)


# ── The 130 s, 30-scene fixture ──────────────────────────────────────────


def _film() -> dict:
    """30 scenes of every kind: six rounds of statement, recording, AI clip, figure, statement."""
    scenes: list[dict] = []
    for round_ in range(6):
        scenes += [
            {"id": f"r{round_}-claim", "type": "statement", "lines": ["Every alert", "*triaged* first"], "duration": 4},
            {"id": f"r{round_}-console", "type": "recording", "take_id": str(uuid.UUID(int=round_ + 1)), "trim_end": 8},
            {"id": f"r{round_}-shot", "type": "clip", "clip_id": str(uuid.UUID(int=round_ + 101))},
            {"id": f"r{round_}-platform", "type": "figure", "block_spec": ARCH, "duration": 5},
            {"id": f"r{round_}-proof", "type": "statement", "lines": ["In *minutes*"], "duration": 3},
        ]
    return validate_video_spec({"title": "Brand film", "freestyle": True, "scenes": scenes})


def _plan(effective: dict):
    spec = parse_video_spec(effective)
    clips = {
        scene.id: timeline.ClipMedia(duration=4.0, width=1920, height=1080, fps=24.0, frame_count=96)
        for scene in spec.scenes
        if scene.type == "clip"
    }
    return timeline.estimate_plan(spec, {}, clips)


@pytest.fixture
def film(monkeypatch):
    effective = _film()

    async def estimate(_db, spec, **_kwargs):
        return _plan(spec)

    monkeypatch.setattr(projects, "estimate", estimate)
    return effective


class _Session:
    """The start's reads: no render in flight; ``begin_nested`` a savepoint that does nothing."""

    def __init__(self):
        self.added = []

    async def execute(self, _statement, _params=None):
        return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: []), all=lambda: [])

    async def get(self, *_args, **_kwargs):
        return None

    def begin_nested(self):
        session = self

        class _Savepoint:
            async def __aenter__(self):
                return session

            async def __aexit__(self, *_exc):
                return False

        return _Savepoint()

    def add(self, row):
        self.added.append(row)

    async def flush(self):
        return None


def _capacity(monkeypatch, *, cpus: int, container_gib: float | None, rates=None):
    """The worker the pre-flight reads (``publish_worker``): its CPUs, each quality's memory ceiling."""
    limit = int(container_gib * _GIB) if container_gib else None
    monkeypatch.setattr(resources, "memory_limit", lambda: limit)
    monkeypatch.setattr(resources, "host_memory", lambda: 64 * _GIB)
    published = {
        "pages": int(settings.video_studio_render_pages),
        "cpus": cpus,
        "memory_ceilings": {quality: render_process.memory_ceiling(quality) for quality in ("draft", "standard", "high", "ultra")},
    }

    async def capacity():
        return published

    async def measured(_db, _quality, **_kwargs):
        return rates

    monkeypatch.setattr(render_preflight, "worker_capacity", capacity)
    monkeypatch.setattr(render_preflight, "rates_of", measured)
    return published


def test_the_fixture_is_a_130_second_video_of_30_scenes_of_every_kind(film):
    plan = _plan(film)
    kinds = {scene.type for scene in parse_video_spec(film).scenes}
    assert len(plan.document["scenes"]) == 30 and kinds == {"statement", "recording", "clip", "figure"}
    assert 120 <= plan.duration <= 140, plan.duration


# ── The documented worker, an undersized one, a refusal ──────────────────


def test_its_ultra_render_fits_under_the_cap_on_the_documented_worker(film, monkeypatch):
    # docs/docs/deployment/configuration.md: 8 CPUs (request = limit), a 24 GiB container, the pages left at 0.
    published = _capacity(monkeypatch, cpus=8, container_gib=24)
    # The ultra ceiling: 16 GiB, the container keeping 4 GiB for the worker's own jobs.
    assert published["memory_ceilings"]["ultra"] == 16 * _GIB
    said = asyncio.run(render_preflight.preflight(_Session(), film, quality="ultra"))
    # CPUs less one, 7 pages wanted; 16 GiB holds what each page's measured peak and margin fit at 2160p,
    # the other CPUs left to their encoders' threads.
    drawn = render_preflight.layout((3840, 2160), x264_preset="slow", ceiling=16 * _GIB, cpus=8)
    assert render_preflight.auto_pages(8) == 7 and (said.pages, said.cpus) == (drawn.pages, 8) and 1 < said.pages < 7
    assert said.problems == [] and not said.refuses and said.ranges == 1
    # Within the 4 h on one worker; its slowest page's run is the render's time, on the pages its memory opens.
    assert said.expected_seconds < said.cap_seconds / 3 and said.full_pages_seconds < said.expected_seconds, said.line()
    # The memory that would draw on all 7 is said - the platform's sizing, once -, never a reason to refuse.
    assert any(f"holds {said.pages} of its 7 pages" in text for text in said.infra_once())
    # Every quality fits it, at its own memory.
    for quality in ("draft", "standard", "high"):
        assert not asyncio.run(render_preflight.preflight(_Session(), film, quality=quality)).problems


def test_a_scene_preview_draws_on_its_own_two_pages_whatever_the_render_draws_on(monkeypatch):
    # Seconds of 720p beside a whole render: the pages, and so the memory ceiling, of the preview lane.
    monkeypatch.setattr(settings, "video_studio_preview_memory_mb", 0)
    monkeypatch.setattr(settings, "video_studio_render_pages", 2)
    sized_for = render_process.preview_memory_ceiling()
    for pages in (0, 8):
        monkeypatch.setattr(settings, "video_studio_render_pages", pages)
        assert render_preflight.preview_pages() == render_preflight.PREVIEW_PAGES == 2
        assert render_process.preview_memory_ceiling() == sized_for
    # Fewer pages asked of every render: a preview asks no more.
    monkeypatch.setattr(settings, "video_studio_render_pages", 1)
    assert render_preflight.preview_pages() == 1


def test_on_a_worker_too_small_to_draw_it_whole_it_is_drawn_in_scene_ranges_that_each_fit(film, monkeypatch):
    # Two CPUs (one page) drawing a third as fast as CI: about 7 h on one worker.
    slow = render_preflight.Rates(render_preflight.DEFAULT_PAGE_RATE / 3, measured=3, measured_quality=3)
    _capacity(monkeypatch, cpus=2, container_gib=24, rates=slow)
    said = asyncio.run(render_preflight.preflight(_Session(), film, quality="ultra"))
    assert said.seconds > render_preflight.RENDER_DEADLINE_SECONDS and said.ranges >= 2
    assert said.range_seconds < render_preflight.RENDER_DEADLINE_SECONDS and not said.problems
    assert f"it is drawn in {said.ranges} scene ranges" in said.line()
    # A worker that would draw it whole is said too.
    assert said.cpus_needed is None or said.cpus_needed > 2
    kept = said.as_dict()
    assert kept["ranges"] == said.ranges and kept["one_worker_seconds"] > kept["expected_seconds"]
    assert "Drawn in" in render_preflight.said_of(SimpleNamespace(preflight=kept, measured=None))


def test_a_refusal_says_every_input_the_cpus_needed_and_the_highest_quality_that_fits(film, monkeypatch):
    monkeypatch.setattr(settings, "video_studio_render_max_ranges", 1)
    # One CPU drawing a sixth as fast as CI: past the 8 h of an ultra render, not split.
    slow = render_preflight.Rates(render_preflight.DEFAULT_PAGE_RATE / 6, measured=3, measured_quality=3)
    _capacity(monkeypatch, cpus=1, container_gib=24, rates=slow)
    said = asyncio.run(render_preflight.preflight(_Session(), film, quality="ultra"))
    assert said.refuses and said.cpus_needed and said.cpus_needed > 1
    [problem] = [p for p in said.problems if "may run" in p]
    # The estimate's inputs: the frames, a frame's seconds on a page - its slowest scenes' - and their source,
    # the pages, the CPUs.
    assert f"{said.frames:,} frames at 3840x2160" in problem
    assert said.slowest and f"its slowest scenes ({render_preflight.scene_words(said.slowest)}," in problem
    assert f"{said.slowest_seconds:.2f} s a frame on each of 1 page (1 CPU)" in problem
    assert "never measured here: weighed by its frames' kinds, measured on 3 ultra renders of this platform" in problem
    # The duration against the cap, what the worker needs, then the quality that fits: one message.
    assert "past the 8 h a render at ultra may run" in problem
    assert f"the worker needs {said.cpus_needed} CPUs - a CPU request and limit of {said.cpus_needed}" in problem
    assert "Meanwhile high (1080p60) fits this worker now" in problem and "never made for them" in problem
    assert problem.index("needs") < problem.index("Meanwhile")
    assert said.fits["quality"] == "high" and said.as_dict()["cpus_needed"] == said.cpus_needed


def test_start_render_refuses_with_those_numbers_and_force_is_audited(film, monkeypatch):
    monkeypatch.setattr(settings, "video_studio_render_max_ranges", 1)
    slow = render_preflight.Rates(render_preflight.DEFAULT_PAGE_RATE / 6, measured=3, measured_quality=3)
    _capacity(monkeypatch, cpus=1, container_gib=24, rates=slow)
    audits = []

    async def none(*_args, **_kwargs):
        return []

    async def effective(*_args, **_kwargs):
        return SimpleNamespace(spec=film)

    async def nothing(*_args, **_kwargs):
        return None

    async def audit(_db, **kwargs):
        audits.append(kwargs)

    monkeypatch.setattr(render_checks, "gate", none)
    monkeypatch.setattr(projects, "effective_spec", effective)
    monkeypatch.setattr(jobs, "renders_in_flight", none)
    monkeypatch.setattr(jobs, "enqueue", nothing)
    monkeypatch.setattr("app.services.audit.log_audit", audit)
    with pytest.raises(render_checks.RenderGateError) as refused:
        asyncio.run(projects.start_render(_Session(), PROJECT, USER, quality="ultra"))
    [reason] = refused.value.reasons
    assert reason.code == "preflight" and "s a frame on each of 1 page (1 CPU)" in reason.message
    assert "CPUs - a CPU request and limit of" in reason.message and "Meanwhile high (1080p60)" in reason.message
    started = asyncio.run(projects.start_render(_Session(), PROJECT, USER, quality="ultra", force=True))
    assert [r["code"] for r in started.render.gate_override["reasons"]] == ["preflight"]
    assert audits[-1]["details"]["forced"] is True and started.render.preflight["cpus_needed"]


# ── The ranges ────────────────────────────────────────────────────────────


def test_ranges_are_cut_where_a_scene_starts_of_about_the_same_frames(film):
    document = _plan(film).document
    planned = render_ranges.plan_ranges(document, 3)
    starts = {math.floor(float(scene["start"]) * float(document["fps"])) for scene in document["scenes"]}
    assert len(planned) == 3 and planned[0].first == 0 and planned[-1].end == int(document["frames"])
    assert all(a.end == b.first for a, b in itertools.pairwise(planned))
    assert all(r.first in starts or r.first == 0 for r in planned)
    sizes = [r.frames for r in planned]
    assert max(sizes) - min(sizes) < int(document["frames"]) * 0.2
    assert planned[0].scenes[0] == "r0-claim" and planned[-1].scenes[-1] == "r5-proof"
    # One scene cannot be cut.
    one = {"fps": 30, "frames": 300, "scenes": [{"id": "only", "start": 0.0, "end": 10.0}]}
    assert len(render_ranges.plan_ranges(one, 4)) == 1


def _row(part: int, first: int, end: int, *, status: str = "pending", drawn_by=None, execution_id=None):
    return SimpleNamespace(
        id=uuid.uuid4(),
        render_id=uuid.uuid4(),
        digest="d" * 64,
        part=part,
        first_frame=first,
        end_frame=end,
        scenes=[f"s{part}"],
        status=status,
        frames_done=0,
        drawn_by=drawn_by,
        execution_id=execution_id,
        error=None,
        measured=None,
    )


def test_the_render_draws_what_no_worker_took_and_restores_what_one_drew(monkeypatch):
    other = uuid.uuid4()
    rows = [_row(0, 0, 100), _row(1, 100, 200, execution_id=other), _row(2, 200, 300, execution_id=uuid.uuid4())]
    claimed: set[uuid.UUID] = set()
    reads = []

    async def rows_of(_render_id):
        reads.append(1)
        # Range 1 is taken by another worker at once, then drawn; range 2 no worker takes.
        rows[1].status, rows[1].drawn_by = ("running", "worker-b") if len(reads) < 3 else ("ready", "worker-b")
        return rows

    async def execution_states(_ids):
        return {other: "running"}

    async def claim(range_id, *, by, execution_id=None, take_over=False):
        row = next(r for r in rows if r.id == range_id)
        if row.status != "pending" and not take_over:
            return False
        claimed.add(range_id)
        row.status, row.drawn_by = "running", by
        return True

    async def nothing(*_args, **_kwargs):
        return None

    drawn: list[tuple[int, bool]] = []

    async def draw(row, mine):
        drawn.append((row.part, mine))
        return f"range-{row.part}.mp4"

    said = []

    async def report(frames, stage):
        said.append((frames, stage))

    async def no():
        return False

    monkeypatch.setattr(render_ranges, "rows_of", rows_of)
    monkeypatch.setattr(render_ranges, "execution_states", execution_states)
    monkeypatch.setattr(render_ranges, "claim", claim)
    monkeypatch.setattr(render_ranges, "extend_own_deadline", nothing)
    beats = []
    videos = asyncio.run(
        render_ranges.draw_all(
            "r", rows, draw=draw, report=report, cancelled=no, heartbeat=lambda: beats.append(1), wall_seconds=60, wait_seconds=0
        )
    )
    # Its own first, then the one no worker took, then the one drawn elsewhere restored - each once.
    assert drawn == [(0, True), (2, True), (1, False)]
    assert videos == ["range-0.mp4", "range-1.mp4", "range-2.mp4"]
    assert {r.part for r in rows if r.id in claimed} == {0, 2}


@pytest.mark.parametrize("state", ["dead_letter", "pending"])
def test_the_render_takes_over_a_range_whose_worker_stopped(monkeypatch, state):
    """Its execution ended, or went back to the queue: handed back, or taken back from a worker gone.

    Waiting for a worker to claim a range back hung a render that held its
    lane's only slot: the range execution queued behind it never ran.
    """
    stopped = uuid.uuid4()
    rows = [_row(0, 0, 100, status="ready"), _row(1, 100, 200, status="running", drawn_by="worker-b", execution_id=stopped)]

    async def rows_of(_render_id):
        return rows

    async def execution_states(_ids):
        return {stopped: state}

    taken = []

    async def claim(range_id, *, by, execution_id=None, take_over=False):
        taken.append(take_over)
        return take_over

    async def nothing(*_args, **_kwargs):
        return None

    drawn = []

    async def draw(row, mine):
        drawn.append((row.part, mine))
        return "v.mp4"

    async def no():
        return False

    monkeypatch.setattr(render_ranges, "rows_of", rows_of)
    monkeypatch.setattr(render_ranges, "execution_states", execution_states)
    monkeypatch.setattr(render_ranges, "claim", claim)
    monkeypatch.setattr(render_ranges, "extend_own_deadline", nothing)
    asyncio.run(
        render_ranges.draw_all(
            "r", rows, draw=draw, report=nothing, cancelled=no, heartbeat=lambda: None, wall_seconds=60, wait_seconds=0
        )
    )
    # Its worker's execution is over: the render takes it and draws what its stored segments lack.
    assert taken == [True] and sorted(drawn) == [(0, False), (1, True)]


def test_a_range_its_worker_draws_is_left_to_it():
    running = _row(1, 100, 200, status="running", drawn_by="worker-b", execution_id=uuid.uuid4())
    assert not render_ranges._claimable(running, "running")
    assert not render_ranges._claimable(running, None)


# ── The bundle a range draws from ─────────────────────────────────────────


class _Store:
    def __init__(self):
        self.objects: dict[str, bytes] = {}
        self.uploads: list[str] = []

    async def upload_path(self, path, name, _content_type=None):
        self.objects[name] = Path(path).read_bytes()
        self.uploads.append(name)
        return name

    async def upload_to(self, data, name, _content_type=None):
        self.objects[name] = bytes(data)
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
        return [name for name in self.objects if name.startswith(prefix)]

    async def read_range(self, name, offset, length):
        if name not in self.objects:
            raise FileNotFoundInStorageError(name)
        return self.objects[name][offset : offset + length]

    async def list_objects_modified(self, prefix):
        return [(name, datetime.now(UTC)) for name in self.objects if name.startswith(prefix)]

    async def delete(self, name):
        self.objects.pop(name, None)


@pytest.fixture
def store(monkeypatch):
    from app.services import file_storage as storage_module

    found = _Store()
    monkeypatch.setattr(storage_module, "file_storage", found)
    return found


def test_a_range_draws_from_what_the_render_published_a_downloaded_file_by_its_id(store, monkeypatch, tmp_path):
    made = tmp_path / "render"
    made.mkdir()
    take, slide, picture = made / "take-a.mp4", made / "slide-s2.png", made / "insert-0.png"
    for path in (take, slide, picture):
        path.write_bytes(path.name.encode())
    track = InsertTrack(4, (((0.1, 0.1), (0.9, 0.1), (0.9, 0.9), (0.1, 0.9)),), (0.1, 0.1, 0.9, 0.9))
    assets = compositor.CompositorAssets()
    assets.sources["take/s1"] = compositor.FrameSource(str(take), 30.0)
    assets.images["image/s2"] = str(slide)
    assets.inserts["insert/s1.0"] = InsertSource(
        str(picture),
        "image",
        ((0.2, 0.2), (0.8, 0.2), (0.8, 0.8), (0.2, 0.8)),
        track=track,
        key=KeySpec("blue", 0.25, 2.0),
        footage=Footage(str(take), "video", 30.0, "take/s1"),
        first_frame=4,
        plate=(4, 9, 14),
        logo="logo",
        label="Scene s1: its screen insert 0",
    )
    document = {"fps": 60, "frames": 600, "width": 1920, "height": 1080, "scenes": []}
    take_file = str(uuid.uuid4())
    bundle = asyncio.run(
        render_ranges.publish(
            "r1", document=document, assets=assets, origins={str(take): take_file}, quality="ultra", size=(3840, 2160)
        )
    )
    # The recording is named by its file id, never stored again; the slide and the picture the render made, once.
    assert len(store.uploads) == 2 and not any("take-a" in name for name in store.uploads)
    again = asyncio.run(
        render_ranges.publish(
            "r1", document=document, assets=assets, origins={str(take): take_file}, quality="ultra", size=(3840, 2160)
        )
    )
    assert again.digest == bundle.digest and len(store.uploads) == 2

    downloaded = []

    async def download_file(file_id, path):
        downloaded.append(file_id)
        Path(path).write_bytes(b"take")
        return SimpleNamespace(id=file_id)

    monkeypatch.setattr("app.services.video_studio.assets.download_file", download_file)
    read = asyncio.run(render_ranges.read("r1", bundle.digest))
    rebuilt = asyncio.run(render_ranges.fetch(read, tmp_path / "range-1"))
    assert downloaded == [take_file] and read.document == document and read.size == (3840, 2160)
    insert = rebuilt.inserts["insert/s1.0"]
    assert insert.track == track and insert.key == KeySpec("blue", 0.25, 2.0) and insert.first_frame == 4
    # The frames its clean surface is read on: a range reads the same plate as the render.
    assert insert.plate == (4, 9, 14)
    assert insert.footage.path == rebuilt.sources["take/s1"].path and Path(rebuilt.images["image/s2"]).read_bytes() == b"slide-s2.png"
    # The render and the range name every file by what it holds - the recording by its file id, what the render made
    # by its content - so both read the same segments' digest, whatever their paths.
    from app.services.video_studio.spec import QUALITY_PRESETS

    assets.origins[str(take)] = f"file:{take_file}"
    preset = QUALITY_PRESETS["ultra"]
    mine = compositor.drawing_inputs(document, preset, (3840, 2160), (0, 600), assets)
    assert compositor.drawing_inputs(document, preset, (3840, 2160), (0, 600), rebuilt) == mine
    assert {"source take/s1", "image image/s2", "insert insert/s1.0"} <= set(mine)
    # Another release draws a frame differently: it never draws from this bundle.
    monkeypatch.setattr(settings, "app_version", "another")
    with pytest.raises(render_ranges.BundleError, match="release"):
        asyncio.run(render_ranges.read("r1", bundle.digest))


def test_a_range_worker_draws_its_frames_and_never_prepares_the_render_again(store, monkeypatch, tmp_path):
    row = _row(1, 300, 600)
    finished = []

    async def find(_render_id, _digest, _part):
        return row

    async def load(_kind, _job_id):
        return SimpleNamespace(status="running")

    async def claim(_range_id, *, by, execution_id=None, take_over=False):
        return True

    async def read(_render_id, digest):
        return render_ranges.Bundle(digest, {"fps": 60, "frames": 600, "scenes": []}, "high", (1920, 1080), {}, {})

    async def fetch(_bundle, _workdir):
        return compositor.CompositorAssets()

    asked = {}

    async def render_frames(document, assets, **kwargs):
        asked.update(kwargs)
        kwargs["tracker"].done = 300
        return str(tmp_path / "video.mp4")

    async def finish(range_id, *, by, status="ready", error=None, measured=None):
        finished.append((status, measured))
        return True

    async def gathered(*_args, **_kwargs):
        raise AssertionError("a range never gathers the render's media")

    monkeypatch.setattr(render_ranges, "find", find)
    monkeypatch.setattr(jobs, "load", load)
    monkeypatch.setattr(render_ranges, "claim", claim)
    monkeypatch.setattr(render_ranges, "read", read)
    monkeypatch.setattr(render_ranges, "fetch", fetch)
    monkeypatch.setattr(render_ranges, "finish", finish)
    monkeypatch.setattr(compositor, "render_frames", render_frames)
    monkeypatch.setattr(render, "_gather_media", gathered)
    outcome = asyncio.run(render_ranges.run_range("r1", 1, "d" * 64, workdir=tmp_path))
    assert outcome.kind == "done" and asked["frame_range"] == (300, 600) and asked["render_id"] == "r1"
    assert finished[0][0] == "ready" and finished[0][1]["frames"] == 300

    # A bundle it cannot read: the range is left to the render, never failed for the render.
    async def unreadable(_render_id, _digest):
        raise render_ranges.BundleError("the render's drawing inputs are gone")

    monkeypatch.setattr(render_ranges, "read", unreadable)
    finished.clear()
    assert asyncio.run(render_ranges.run_range("r1", 1, "d" * 64, workdir=tmp_path)).kind == "done"
    assert finished[0][0] == "failed"


def _three_scenes() -> dict:
    from app.services.video_studio.themes import build_theme

    effective = validate_video_spec(
        {
            "title": "Ranges",
            "freestyle": True,
            "scenes": [
                {"id": "open", "type": "title", "title": "Hello", "duration": 2},
                {"id": "claim", "type": "statement", "lines": ["One platform"], "duration": 2},
                {"id": "proof", "type": "statement", "lines": ["Every *alert*"], "duration": 2},
            ],
        }
    )
    plan = timeline.plan(parse_video_spec(effective), timeline.PlanMedia(), fps=30, theme=build_theme(mode="dark", background_style="grid"))
    return json.loads(json.dumps(plan.document, default=str))


def _psnr(video: str, reference: str) -> float:
    """The average PSNR of *video* against *reference*, frame by frame, in dB (``inf`` when identical)."""
    import re
    import shutil
    import subprocess

    out = subprocess.run(  # noqa: S603
        [shutil.which("ffmpeg") or "ffmpeg", "-hide_banner", "-i", video, "-i", reference, "-lavfi", "psnr", "-f", "null", "-"],
        check=True,
        capture_output=True,
        text=True,
        timeout=120,
    ).stderr
    found = re.search(r"average:(inf|[\d.]+)", out)
    assert found, out[-500:]
    return float(found.group(1))


@pytest.mark.chromium
async def test_two_workers_draw_two_scene_ranges_the_render_joins_into_every_frame(store, monkeypatch, tmp_path):
    import os

    from app.services.builtin_tools.html_render import font_face_css
    from app.services.video_studio.spec import QUALITY_PRESETS
    from tests.test_video_studio_render_pages import _frames

    if not encoder.encoder_available():
        pytest.skip("ffmpeg is not installed")
    monkeypatch.setattr(compositor, "SEGMENT_SECONDS", 1)
    document, preset, size = _three_scenes(), QUALITY_PRESETS["draft"], (640, 360)

    async def draw(workdir, *, frame_range=None, render_id=None, tracker=None, drawn=None):
        return await compositor.render_frames(
            document,
            compositor.CompositorAssets(),
            preset=preset,
            output_size=size,
            workdir=str(workdir),
            font_css=font_face_css(),
            pages=1,
            frame_range=frame_range,
            render_id=render_id,
            tracker=tracker,
            on_frame=(lambda: drawn.append(1)) if drawn is not None else None,
        )

    try:
        whole = await draw(tmp_path / "whole")
    except Exception as exc:
        text = str(exc)
        if "Executable doesn't exist" not in text and "playwright install" not in text.lower():
            raise
        if os.environ.get("REQUIRE_CHROMIUM") == "1":
            pytest.fail(f"Playwright's Chromium is required here and is not installed: {exc}")
        pytest.skip("Playwright's Chromium is not installed (python -m playwright install chromium)")
    expected = _frames(whole)
    first, second = render_ranges.plan_ranges(document, 2)
    assert first.end == second.first and second.scenes[0] in ("claim", "proof")

    # Worker B draws the second range from what the render published, nothing else.
    bundle = await render_ranges.publish(
        "r1", document=document, assets=compositor.CompositorAssets(), origins={}, quality="draft", size=size
    )
    row = _row(1, second.first, second.end)
    ended = []

    async def find(_render_id, _digest, _part):
        return row

    async def load(_kind, _job_id):
        return SimpleNamespace(status="running")

    async def claim(_range_id, *, by, execution_id=None, take_over=False):
        return True

    async def moved(_range_id, *, by, frames_done):
        return True

    async def finish(_range_id, *, by, status="ready", error=None, measured=None):
        ended.append(status)
        return True

    async def not_cancelled(_kind, _job_id):
        return False

    for name, fake in (("find", find), ("claim", claim), ("progress", moved), ("finish", finish)):
        monkeypatch.setattr(render_ranges, name, fake)
    monkeypatch.setattr(jobs, "load", load)
    monkeypatch.setattr(jobs, "is_cancelled", not_cancelled)
    assert (await render_ranges.run_range("r1", 1, bundle.digest, workdir=tmp_path / "worker-b")).kind == "done"
    assert ended == ["ready"]

    # The render draws the first range itself, then restores the second from its stored segments: nothing drawn.
    joined = tmp_path / "render"
    own = await draw(joined / "range-0", frame_range=(first.first, first.end), render_id="r1")
    drawn: list[int] = []
    restored = compositor.DrawProgress()
    theirs = await draw(joined / "range-1", frame_range=(second.first, second.end), render_id="r1", tracker=restored, drawn=drawn)
    assert drawn == [] and restored.kept == second.frames
    Path(own).replace(joined / "range-000.mp4")
    Path(theirs).replace(joined / "range-001.mp4")
    video = str(joined / "video.mp4")
    await encoder.concat([str(joined / "range-000.mp4"), str(joined / "range-001.mp4")], out_path=video, workdir=str(joined))
    # Every frame of the whole render, joined by stream copy. Each range cuts its own segments, so its key frames fall
    # elsewhere than the whole render's: the frames are the same picture, compared by PSNR rather than by bytes.
    assert len(_frames(video)) == len(expected) == int(document["frames"])
    assert _psnr(video, whole) > 40.0


def test_a_range_process_is_started_with_its_part_and_digest(monkeypatch, tmp_path):
    import io

    command, _backend = render_process._command("r1", str(tmp_path), part=2, digest="d" * 64)
    assert command[-3:] == ["--range", "2", "d" * 64]
    seen = {}

    async def run(render_id, _channel, workdir, scene_range=None):
        seen.update(render_id=render_id, workdir=workdir, scene_range=scene_range)

    monkeypatch.setattr(render_process, "_run", run)
    monkeypatch.setattr(render_process, "status_channel", io.StringIO)
    monkeypatch.setattr(render_process, "die_with_the_worker", lambda: None)
    monkeypatch.setattr("app.core.logging.setup_logging", lambda **_kwargs: None)
    assert render_process.main(command[3:]) == 0
    assert seen == {"render_id": "r1", "workdir": tmp_path, "scene_range": (2, "d" * 64)}
    # Its working directory is its render's: it goes once the render ended.
    assert render_process.range_workdir("r1", 2).name == "xtm-render-r1-range-2"


def test_the_render_adapter_runs_a_range_execution_as_a_range_and_ends_only_the_range(monkeypatch):
    from app.services.orchestration_adapters import video_studio as adapters
    from app.services.video_studio.runners import JobOutcome

    ran = []

    async def run_range_isolated(render_id, part, digest, **_kwargs):
        ran.append((render_id, part, digest))
        return JobOutcome("done")

    async def run_isolated(*_args, **_kwargs):
        raise AssertionError("a range is never run as its render")

    failed = []

    async def find(render_id, digest, part):
        return SimpleNamespace(id=f"{render_id}:{digest}:{part}")

    async def fail_open(range_id, error):
        failed.append((range_id, error))

    async def commit():
        return None

    monkeypatch.setattr(render_process, "run_range_isolated", run_range_isolated)
    monkeypatch.setattr(render_process, "run_isolated", run_isolated)
    monkeypatch.setattr(render_ranges, "find", find)
    monkeypatch.setattr(render_ranges, "fail_open", fail_open)
    ctx = SimpleNamespace(db=SimpleNamespace(commit=commit), worker_id="worker-b", cancel_event=None, stopping=None)
    execution = SimpleNamespace(
        id=uuid.uuid4(), payload={"render_id": "r1", "range": 2, "digest": "d"}, attempt_count=1, max_attempts=3, last_error="x"
    )
    adapter = adapters.VideoRenderAdapter()
    assert asyncio.run(adapter.execute(ctx, execution)).kind == "completed" and ran == [("r1", 2, "d")]
    # Given up on: the range is failed for the render to finish, the render's own row untouched.
    asyncio.run(adapter.on_dead_letter(ctx, execution))
    assert failed == [("r1:d:2", "x")]


def test_ranges_say_where_each_stands(store):
    rows = [
        _row(0, 0, 100, status="ready", drawn_by="render"),
        _row(1, 100, 200, status="running", drawn_by="worker-b"),
        _row(2, 200, 300),
    ]
    rows[1].frames_done = 45
    said = render_ranges.describe_ranges(rows)
    assert said.startswith("Drawn in 3 scene ranges: 1 (scene s0) drawn; 2 (scene s1) 45% on worker-b; 3 (scene s2) waiting")


# ── The deadline, per range ───────────────────────────────────────────────


def test_a_running_execution_pushes_its_own_deadline_out_never_in_and_only_its_owner(monkeypatch):
    from app.services import orchestration_engine as engine

    now = datetime.now(UTC)
    row = SimpleNamespace(id=uuid.uuid4(), state="running", lease_owner="worker-a", deadline_at=now + timedelta(hours=1), updated_at=now)

    async def locked(_db, _execution_id, **_kwargs):
        return row

    async def flush():
        return None

    monkeypatch.setattr(engine, "_locked_get", locked)
    db = SimpleNamespace(flush=flush)
    later = now + timedelta(hours=4)
    assert asyncio.run(engine.extend_deadline(db, execution_id=row.id, worker_id="worker-a", deadline_at=later)) == later
    # Never in.
    asyncio.run(engine.extend_deadline(db, execution_id=row.id, worker_id="worker-a", deadline_at=now))
    assert row.deadline_at == later
    # Another worker's, or an execution that ended: the lease is lost.
    with pytest.raises(engine.LeaseLostError):
        asyncio.run(engine.extend_deadline(db, execution_id=row.id, worker_id="worker-b", deadline_at=later))
    row.state = "completed"
    with pytest.raises(engine.LeaseLostError):
        asyncio.run(engine.extend_deadline(db, execution_id=row.id, worker_id="worker-a", deadline_at=later))


def test_a_deadline_the_render_pushed_out_moves_its_tasks_timeout():
    from app.services import orchestration_reconciler as reconciler

    async def scenario():
        execution_id = uuid.uuid4()
        async with asyncio.timeout(60) as scope:
            reconciler._deadline_scopes[execution_id] = scope
            try:
                first = scope.when()
                reconciler._follow_deadline(execution_id, datetime.now(UTC) + timedelta(hours=4))
                pushed = scope.when()
                # Never in.
                reconciler._follow_deadline(execution_id, datetime.now(UTC) + timedelta(seconds=5))
                return first, pushed, scope.when()
            finally:
                reconciler._deadline_scopes.pop(execution_id, None)

    first, pushed, kept = asyncio.run(scenario())
    assert pushed > first + 3 * 3600 and kept == pushed


# ── A second render of the same revision at another quality ───────────────


def _wav_bytes(seconds: float = 1.0, rate: int = 48000) -> bytes:
    pcm = b"\x00\x00" * round(seconds * rate)
    header = b"RIFF" + struct.pack("<I", 36 + len(pcm)) + b"WAVE"
    header += b"fmt " + struct.pack("<IHHIIHH", 16, 1, 1, rate, rate * 2, 2, 16)
    return header + b"data" + struct.pack("<I", len(pcm)) + pcm


class _Provider:
    def __init__(self):
        self.spoken = 0

    def speech_pronunciation(self, _model):
        from app.llm.speech import PronunciationSupport

        return PronunciationSupport()

    async def synthesize_speech(self, _request):
        self.spoken += 1
        return SimpleNamespace(audio=_wav_bytes(), voice="alloy", notes=())


class _FigurePage:
    def __init__(self, world):
        self.world = world

    async def evaluate(self, script, arg=None):
        if "describe" in script:
            return {"steps": [{"labels": ["OpenCTI"]}, {"labels": ["OpenAEV"]}]}
        if "plan" in script:
            return {"active": [[0.0, arg["length"]]]}
        return None

    async def screenshot(self, *, path, **_kw):
        Path(path).write_bytes(b"jpeg")
        self.world.shot += 1


@pytest.fixture
def studio(store, monkeypatch):
    world = SimpleNamespace(opened=0, shot=0, rescaled=[], provider=_Provider())

    @asynccontextmanager
    async def open_page(_url, **_kw):
        world.opened += 1
        yield _FigurePage(world)

    async def frames_to_mezzanine(frames, *, out_path, **_kw):
        Path(out_path).write_bytes(b"mp4:" + str(len(frames)).encode())

    async def probe(_path):
        return SimpleNamespace(width=1920, height=1080)

    async def rescale(source, out, *, width, height, fps, **_kw):
        world.rescaled.append((Path(source).parent.name.rsplit("-", 1)[-1], width, height, fps))
        Path(out).write_bytes(Path(source).read_bytes())

    async def condition_voice(raw, out):
        Path(out).write_bytes(Path(raw).read_bytes())

    monkeypatch.setattr(presentation_export, "open_render_page", open_page)
    monkeypatch.setattr(encoder, "frames_to_mezzanine", frames_to_mezzanine)
    monkeypatch.setattr(encoder, "probe", probe)
    monkeypatch.setattr(encoder, "rescale", rescale)
    monkeypatch.setattr(encoder, "condition_voice", condition_voice)
    return world


def _speak(world, workdir: Path):
    target = SimpleNamespace(
        config=SimpleNamespace(provider="openai", id="speech-1"), model="tts", voice="alloy", provider=world.provider
    )
    speaker = voice_check.Speaker(target, Voiceover(voice="alloy"), None, "no speech-to-text here")
    line = voice.Line("r0-claim", "Every alert, triaged first.", None, None)
    workdir.mkdir(parents=True, exist_ok=True)
    return asyncio.run(voice_check.speak_checked(speaker, line, workdir=str(workdir)))


def _figure(workdir: Path, *, scale: int, fps: int):
    source = figures.source_from_spec(SimpleNamespace(block_spec=ARCH))
    scene = SimpleNamespace(id="r0-platform", reveal="even", build="sequence", camera="follow", dwell=0.8, motion="drift")
    return asyncio.run(
        figures.draw(
            source,
            figures.figure_document(source, scheme="dark", palette={}),
            user=None,
            scene=scene,
            length=2.0,
            words=None,
            fps=fps,
            viewport=(1920, 1080),
            scale=scale,
            workdir=workdir,
            video_id=str(PROJECT.id),
        )
    )


def test_a_second_render_of_the_same_revision_at_another_quality_speaks_and_draws_nothing_again(studio, tmp_path):
    # The ultra render: the narration spoken, the figure drawn at twice the size, 60 frames a second.
    first = _speak(studio, tmp_path / "ultra")
    _figure(tmp_path / "ultra", scale=2, fps=60)
    assert first.path and studio.provider.spoken == 1
    assert studio.opened == 1 and studio.shot == 120
    # The high render of the same revision: no TTS call, no figure page - the figure rescaled from the ultra one.
    second = _speak(studio, tmp_path / "high")
    path, clip = _figure(tmp_path / "high", scale=1, fps=60)
    assert studio.provider.spoken == 1 and second.duration == pytest.approx(first.duration)
    assert studio.opened == 1 and studio.shot == 120 and studio.rescaled == [("2x60", 1920, 1080, 60)]
    assert clip.fps == 60 and Path(path).is_file()
    # A draft after it takes the least sharp drawing that holds its frames: the high one, every other frame.
    _figure(tmp_path / "draft", scale=1, fps=30)
    assert studio.opened == 1 and studio.rescaled[-1] == ("1x60", 1920, 1080, 30)
    # A sharper quality than any kept is drawn: an upscale would blur it.
    _figure(tmp_path / "ultra-120", scale=3, fps=60)
    assert studio.opened == 2


def test_a_built_in_score_is_composed_once_per_bed(store, monkeypatch, tmp_path):
    from app.services.video_studio import score

    composed = []

    def render_score(ref, duration, cues, out_path):
        composed.append(ref)
        Path(out_path).write_bytes(_wav_bytes())

    async def prepare_bed(source, out, **_kw):
        Path(out).write_bytes(Path(source).read_bytes())

    monkeypatch.setattr(score, "render_score", render_score)
    monkeypatch.setattr(encoder, "prepare_bed", prepare_bed)
    bed = music.Bed(kind="track", ref="pulse", duration=130.0, cues=((0.0, "card", False), (4.0, "words", True)))
    for name in ("ultra", "high"):
        (tmp_path / name).mkdir()
        asyncio.run(music.make_bed(bed, str(tmp_path / name / "bed.wav"), source_path=None, workdir=tmp_path / name))
    assert composed == ["pulse"] and (tmp_path / "high" / "bed.wav").read_bytes() == _wav_bytes()
    # Another worker, its disk empty: taken from the store.
    monkeypatch.setattr("app.services.video_studio.render_cache.ROOT", tmp_path / "other-worker")
    (tmp_path / "other").mkdir()
    asyncio.run(music.make_bed(bed, str(tmp_path / "other" / "bed.wav"), source_path=None, workdir=tmp_path / "other"))
    assert composed == ["pulse"]


def test_screen_inserts_are_followed_once_per_footage_and_quad(store, monkeypatch, tmp_path):
    from app.services.video_studio import insert_tracking, screen_insert

    followed = []

    async def track_all(path, asked, **_kw):
        followed.append(path)
        return [
            InsertTrack(ask.first, tuple(tuple(map(tuple, ask.quad)) for _ in range(ask.last - ask.first + 1)), (0.1, 0.1, 0.9, 0.9))
            for ask in asked
        ]

    monkeypatch.setattr(insert_tracking, "track_all", track_all)
    quad = [[0.1, 0.1], [0.9, 0.1], [0.9, 0.9], [0.1, 0.9]]
    insert = {"type": "screen_insert", "source": {"file_id": str(uuid.uuid4())}, "quad": quad, "track": True, "start": 0.25, "duration": 0.5}
    spec = parse_video_spec(
        {"title": "T", "scenes": [{"id": "demo", "type": "clip", "clip_id": str(uuid.uuid4()), "overlays": [insert]}]}
    )
    key = timeline.insert_key("demo", 0)

    def follow(footage: str, origins: dict) -> tuple[dict, object]:
        payload = {"src": key, "quad": quad, "t0": 0.25, "t1": 0.75}
        document = {"scenes": [{"id": "demo", "type": "clip", "src": "clip/demo", "time_map": [[0.0, 0.25], [2.0, 2.25]], "inserts": [payload]}]}
        media = render._Media()
        media.assets.sources["clip/demo"] = compositor.FrameSource(footage, 24.0)
        media.assets.inserts[key] = screen_insert.InsertSource("p.png", "image", tuple(map(tuple, quad)))
        token = render._ORIGINS.set(origins)
        try:
            asyncio.run(render._track_inserts(spec, document, media))
        finally:
            render._ORIGINS.reset(token)
        return payload, media.assets.inserts[key].track

    clip = str(tmp_path / "clip-demo.mp4")
    first, track = follow(clip, {clip: "file-1"})
    # The next render of the same revision, at another quality: the same footage, the same insert - nothing followed.
    second, again = follow(str(tmp_path / "other" / "clip-demo.mp4"), {str(tmp_path / "other" / "clip-demo.mp4"): "file-1"})
    assert followed == [clip] and second == first and again == track and first["track"]["from"] == track.first
    # Footage the render made itself (a figure) has no file to name: followed every time.
    follow(str(tmp_path / "figure.mp4"), {})
    assert len(followed) == 2 and json.loads(json.dumps(list(track.box))) == [0.1, 0.1, 0.9, 0.9]
