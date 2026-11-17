"""Scene previews run in a lane of their own, beside the whole renders.

One render slot per worker took the previews and the whole renders in turn:
an agent sweeping the scenes of one video delayed a 4K render of another for
as long as the sweep lasted, and a sweep waited behind a 20-minute 4K render.
What is proven:

* a scene preview is queued in the preview lane (``video_preview``), on
  ``VIDEO_STUDIO_PREVIEW_CONCURRENCY`` slots (2 by default) of its own, run by
  an adapter that runs it exactly as a render;
* a preview's process has a memory ceiling of its own, sized to a 720p30 draft,
  within the preview reserve and never above the worker's memory; an ultra
  render keeps its 16 GiB;
* a preview stopped at that ceiling runs again at once on one page,
  and fails only when one page does not fit either; a whole render never does;
* a whole render never takes the preview reserve (8 GiB, a hard
  floor), so a preview starts beside it at once on its pages - a 6 GiB one
  beside a 16 GiB render on 24 GiB -; one whose pages do not fit what is left
  is never drawn on one page for it: its worker says why, and the
  preview lane routes it or waits for its pages (``test_video_studio_preview_
  routing``); a whole render never waits for a preview, and a preview alone
  always starts;
* should the memory run out all the same, the kernel stops a preview's
  processes first, a render's next, the worker's own last.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from app.core.config import settings
from app.services.orchestration_adapters import video_studio as adapters
from app.services.video_studio import jobs, preview_routing, render_preflight, render_process, resources, runners

_GIB = 1 << 30
_MIB = 1 << 20


@pytest.fixture(autouse=True)
def _defaults(monkeypatch):
    monkeypatch.setattr(settings, "video_studio_render_memory_mb", 0)
    monkeypatch.setattr(settings, "video_studio_ultra_render_memory_mb", 16384)
    monkeypatch.setattr(settings, "video_studio_preview_memory_mb", 0)
    monkeypatch.setattr(settings, "video_studio_preview_reserve_mb", 8192)
    monkeypatch.setattr(settings, "video_studio_preview_concurrency", 2)
    monkeypatch.setattr(settings, "video_studio_render_pages", 2)
    monkeypatch.setattr(render_process, "_ATTEMPTS", {})
    monkeypatch.setattr(render_process, "_ADMITTED", {})


#: Scene previews of the sizes production measured: 6 GiB and 7.5 GiB on two pages, with screen inserts.
_SIX = render_process.PreviewSize(ceiling=6 * _GIB, one_page=int(3.5 * _GIB), inserts=True)
_LARGEST = render_process.PreviewSize(ceiling=int(7.5 * _GIB), one_page=int(4.2 * _GIB), inserts=True)


def _container(monkeypatch, limit: int | None, host: int | None = None) -> None:
    monkeypatch.setattr(resources, "memory_limit", lambda: limit)
    monkeypatch.setattr(resources, "host_memory", lambda: host)


def _running(render_id: str, ceiling: int, *, preview: bool = False) -> None:
    """A run of this worker, its process held to *ceiling*: a whole render, or a scene preview."""
    attempt = render_process._Attempt(preview=preview)
    attempt.ceiling = ceiling
    render_process._ATTEMPTS[render_id] = attempt


# ── The lane ──────────────────────────────────────────────────────────────


def test_a_preview_has_slots_of_its_own_run_as_a_render(monkeypatch):
    assert type(settings).model_fields["video_studio_preview_concurrency"].default == 2
    lane = adapters.VideoPreviewAdapter()
    assert lane.name == jobs.PREVIEW_ADAPTER == "video_preview"
    assert isinstance(lane, adapters.VideoRenderAdapter) and lane.lease_seconds == jobs.RENDER_LEASE_SECONDS
    monkeypatch.setattr(settings, "video_studio_preview_concurrency", 3)
    monkeypatch.setattr(settings, "video_studio_render_concurrency", 1)
    assert lane.concurrency_limit == 3 and adapters.VideoRenderAdapter().concurrency_limit == 1
    assert jobs.slots(jobs.PREVIEW_ADAPTER) == 3 and jobs.slots(jobs.RENDER_ADAPTER) == 1


def test_a_render_goes_to_the_lane_of_its_kind():
    assert jobs.adapter_of("render", SimpleNamespace(scene_id="intro")) == jobs.PREVIEW_ADAPTER
    assert jobs.adapter_of("render", SimpleNamespace(scene_id=None, quality="ultra")) == jobs.RENDER_ADAPTER
    assert jobs.adapter_of("take", SimpleNamespace(scene_id="intro")) == jobs.TAKE_ADAPTER
    # The recordings are the render queue's third lane.
    assert jobs.LANES == {jobs.RENDER_ADAPTER: "render", jobs.PREVIEW_ADAPTER: "preview", jobs.TAKE_ADAPTER: "recording"}


def test_a_queued_job_says_its_lane():
    preview = jobs.QueuePlace(running=2, ahead=3, per_worker=2, lane="preview")
    assert preview.as_dict() == {"position": 4, "ahead": 3, "running": 2, "per_worker": 2, "lane": "preview"}
    state = jobs.JobState(kind="render", id="r", status=jobs.STATE_QUEUED, percent=0, stage="Queued", last_progress_at=None, queue=preview)
    assert jobs.describe(state) == (
        "waiting for a worker: 2 scene previews being made now, 3 queued ahead of it in the preview lane "
        "(a worker makes 2 at a time)"
    )
    # An AI shot, an AI track: one lane each, none named.
    assert jobs.QueuePlace(running=0, ahead=0, per_worker=2).as_dict() == {
        "position": 1,
        "ahead": 0,
        "running": 0,
        "per_worker": 2,
    }


# ── A preview's memory ────────────────────────────────────────────────────


def test_a_previews_ceiling_is_sized_to_a_720p30_draft(monkeypatch):
    _container(monkeypatch, 24 * _GIB)
    needed = render_preflight.memory_needed((1280, 720), pages=2, x264_preset="veryfast")
    assert render_process.preview_memory_ceiling() == round(needed * render_process.PREVIEW_HEADROOM)
    assert 2 * _GIB < render_process.preview_memory_ceiling() < 3 * _GIB
    # Never above the worker's memory, on a small worker (a preview's is no render's ceiling).
    _container(monkeypatch, 2 * _GIB)
    assert render_process.memory_ceiling() == 0 and render_process.preview_memory_ceiling() == 2 * _GIB
    # Set, it is what it says.
    monkeypatch.setattr(settings, "video_studio_preview_memory_mb", 1500)
    assert render_process.preview_memory_ceiling() == 1500 * _MIB


def test_an_ultra_render_keeps_its_16_gib(monkeypatch):
    _container(monkeypatch, 24 * _GIB)
    assert render_process.memory_ceiling("ultra") == 16 * _GIB
    assert render_process.ceiling_of(SimpleNamespace(scene_id=None, quality="ultra")) == 16 * _GIB
    assert render_process.ceiling_of(SimpleNamespace(scene_id="intro", quality="draft")) == render_process.preview_memory_ceiling()


def test_a_whole_render_never_takes_the_preview_reserve(monkeypatch):
    """A render is admitted only where the worker's memory less the 8 GiB preview reserve covers it."""
    _container(monkeypatch, 24 * _GIB)
    assert render_process.preview_reserve() == 8 * _GIB
    # Copilot's worker sets 16 GiB, which the reserve leaves; more is never given.
    monkeypatch.setattr(settings, "video_studio_render_memory_mb", 16384)
    assert render_process.memory_ceiling() == render_process.memory_ceiling("ultra") == 16 * _GIB
    monkeypatch.setattr(settings, "video_studio_render_memory_mb", 20480)
    assert render_process.memory_ceiling() == render_process.memory_ceiling("ultra") == 16 * _GIB
    # So a render past 16 GiB is told the container that holds it beside the reserve.
    assert "give the worker's container 24 GiB or more (8 GiB of it stay with the worker's own jobs and its scene " in (
        render_process.memory_to_give(16 * _GIB, "ultra")
    )
    assert "on a container of 26 GiB or more (its scene previews keep 8 GiB of it)" in render_process.memory_to_give(
        18 * _GIB
    )
    # A hard floor: a small worker's default ceiling gives it up, and a reserve set to nothing keeps it.
    monkeypatch.setattr(settings, "video_studio_render_memory_mb", 0)
    _container(monkeypatch, 12 * _GIB)
    assert render_process.memory_ceiling() == 4 * _GIB
    monkeypatch.setattr(settings, "video_studio_preview_reserve_mb", 0)
    _container(monkeypatch, 24 * _GIB)
    monkeypatch.setattr(settings, "video_studio_render_memory_mb", 20480)
    assert render_process.memory_ceiling() == 16 * _GIB
    # A worker that draws no preview keeps none: a render keeps all but the worker's own 4 GiB.
    monkeypatch.setattr(settings, "video_studio_preview_concurrency", 0)
    assert render_process.memory_ceiling() == 20 * _GIB


def test_a_24_gib_worker_running_a_16_gib_render_admits_a_6_gib_preview_at_once(monkeypatch):
    _container(monkeypatch, 24 * _GIB)
    _running("ultra", render_process.memory_ceiling("ultra"))
    assert render_process.memory_held() == 16 * _GIB
    assert render_process.admit_preview("p1", _SIX) is None
    assert render_process.admission_of("p1") == render_process.PreviewAdmission(6 * _GIB, pages=2)  # on its own pages
    assert render_process.memory_held() == 22 * _GIB
    # The largest preview measured, and the reserve's worst case, start there at once too.
    render_process.release_preview("p1")
    assert render_process.admit_preview("p1", _LARGEST) is None and render_process.admission_of("p1").pages == 2
    render_process.release_preview("p1")
    worst = render_process.PreviewSize(ceiling=8 * _GIB, one_page=5 * _GIB, inserts=True)
    assert render_process.admit_preview("p1", worst) is None and render_process.admission_of("p1").pages == 2


def test_a_preview_whose_pages_do_not_fit_is_never_drawn_on_one_page_for_it(monkeypatch):
    """One page drew a 1080p scene with an insert at a frame a second beside a 4K render."""
    _container(monkeypatch, 24 * _GIB)
    _running("ultra", 16 * _GIB)
    assert render_process.admit_preview("p1", _SIX) is None
    # 2 GiB left: not its two pages - and never one page for it: its worker says why, the lane routes it or waits.
    assert render_process.admit_preview("p2", _SIX) == (
        "its 2 pages need 6.0 GiB, and its worker holds 22.0 GiB of its 24.0 GiB for 1 render (16.0 GiB) and "
        "1 scene preview (6.0 GiB) (2.0 GiB left)"
    )
    assert render_process.admission_of("p2") is None and render_process.memory_held() == 22 * _GIB
    # The first one ended: the next starts on its pages.
    render_process.release_preview("p1")
    assert render_process.admit_preview("p2", _SIX) is None and render_process.admission_of("p2").ceiling == 6 * _GIB


def test_a_preview_alone_always_starts_and_a_whole_render_never_waits(monkeypatch):
    # A worker too small for a preview beside anything: alone, it starts on its pages as before.
    _container(monkeypatch, 4 * _GIB)
    assert render_process.admit_preview("p1") is None and render_process.admission_of("p1").pages == 2
    # Beside it, the next waits for its pages: never one page.
    assert render_process.admit_preview("p2") is not None and render_process.admission_of("p2") is None
    # A preview's admission holds its memory until its process reports its own ceiling, never twice.
    for render_id in ("p1", "p2"):
        render_process.release_preview(render_id)
    _container(monkeypatch, 24 * _GIB)
    assert render_process.admit_preview("p1") is None
    held = render_process.memory_held()
    _running("p1", render_process.preview_memory_ceiling(), preview=True)
    assert render_process.memory_held() == held == render_process.preview_memory_ceiling()
    # Without a container limit nor a host memory read, nothing is bounded.
    _container(monkeypatch, None, host=None)
    _running("ultra", 16 * _GIB)
    assert render_process.admit_preview("p9") is None


def test_a_previews_sizes_on_its_pages_and_on_one(monkeypatch):
    _container(monkeypatch, 24 * _GIB)
    inserts = render_preflight.InsertLoad(videos=3, pictures=1)
    size = render_process.preview_size(inserts, quality="draft")
    assert size.ceiling == render_process.preview_memory_ceiling(inserts, quality="draft")
    assert size.one_page == render_process.preview_memory_ceiling(inserts, quality="draft", pages=1)
    needed = render_preflight.memory_needed((1280, 720), pages=1, x264_preset="veryfast", inserts=inserts)
    assert size.one_page == round(needed * render_process.PREVIEW_HEADROOM)
    assert size.one_page < size.ceiling <= render_process.preview_reserve() and size.pages == 2 and size.inserts
    assert not render_process.preview_size(quality="draft").inserts


def test_a_previews_processes_are_stopped_first_a_renders_next_never_the_worker(monkeypatch, tmp_path):
    assert render_process.process_env(preview=True)[render_process.OOM_SCORE_ENV] == "1000"
    assert render_process.process_env(preview=False)[render_process.OOM_SCORE_ENV] == "500"
    target = tmp_path / "oom_score_adj"
    target.write_text("0")
    monkeypatch.setattr(render_process, "_OOM_SCORE_ADJ_PATH", target)
    monkeypatch.setattr(render_process.sys, "platform", "linux")
    monkeypatch.setenv(render_process.OOM_SCORE_ENV, "1000")
    render_process.stopped_before_the_worker()
    assert target.read_text() == "1000"
    # Started by anything else, the process keeps the score it inherited.
    target.write_text("0")
    monkeypatch.delenv(render_process.OOM_SCORE_ENV)
    render_process.stopped_before_the_worker()
    assert target.read_text() == "0"


# ── The supervisor and the adapter ────────────────────────────────────────


async def test_the_supervisor_holds_a_preview_to_the_previews_ceiling_and_records_it(monkeypatch, tmp_path):
    """Stopped at its ceiling, a preview runs again at once on one page; stopped there too, it fails."""
    _container(monkeypatch, 24 * _GIB)
    seen: dict = {"runs": []}
    render_id = str(uuid.uuid4())

    async def supervise(command, **options):
        seen["runs"].append((options["memory_limit"], render_process.memory_held(), dict(options["env"])))
        return render_process.RenderExit(kind="memory", peak_memory=3 * _GIB, progress=40)

    async def load(_kind, _job_id):
        return SimpleNamespace(quality="draft", scene_id="intro", status="running")

    async def nothing(*_args, **_kwargs):
        return None

    async def mark_if_open(*_args, **changes):
        seen.setdefault("stages", []).append(changes.get("stage"))
        seen["error"] = changes.get("error")
        return True

    async def forget(_render_id):
        return None

    monkeypatch.setattr(render_process, "supervise", supervise)
    monkeypatch.setattr(render_process, "preview_inserts", nothing)
    monkeypatch.setattr(render_process.jobs, "load", load)
    monkeypatch.setattr(render_process.jobs, "heartbeat", nothing)
    monkeypatch.setattr(render_process.jobs, "mark_if_open", mark_if_open)
    monkeypatch.setattr(render_process.render_segments, "forget", forget)
    monkeypatch.setattr(render_process, "WORKDIR_ROOT", tmp_path)
    outcome = await render_process.run_isolated(render_id)
    ceiling = render_process.preview_memory_ceiling()
    (first_limit, first_held, first_env), (second_limit, _held, second_env) = seen["runs"]
    assert first_limit == second_limit == ceiling and first_held == ceiling
    # Its pages laid out in that ceiling.
    assert first_env[render_preflight.ROOM_ENV] == str(ceiling)
    assert render_preflight.ONE_PAGE_ENV not in first_env and second_env[render_preflight.ONE_PAGE_ENV] == "1"
    assert seen["stages"][0].startswith("Drawing again on one page: The preview used 3.0 GiB, past its")
    assert outcome.kind == "failed" and "The preview used 3.0 GiB, past its" in seen["error"]
    assert "drawn on one page after its ceiling stopped it on more" in seen["error"]
    assert "raise VIDEO_STUDIO_PREVIEW_MEMORY_MB" in seen["error"]
    assert render_id not in render_process._ATTEMPTS


async def test_a_preview_its_ceiling_stopped_draws_on_one_page_and_ends_there(monkeypatch, tmp_path):
    _container(monkeypatch, 24 * _GIB)
    runs: list[dict] = []

    async def supervise(command, **options):
        runs.append(dict(options["env"]))
        if len(runs) == 1:
            return render_process.RenderExit(kind="memory", peak_memory=3 * _GIB, progress=30)
        return render_process.RenderExit(kind="done", returncode=0)

    async def load(_kind, _job_id):
        return SimpleNamespace(quality="draft", scene_id="cat-soc", status="running")

    async def nothing(*_args, **_kwargs):
        return None

    async def mark_if_open(*_args, **_changes):
        return True

    async def forget(_render_id):
        return None

    monkeypatch.setattr(render_process, "supervise", supervise)
    monkeypatch.setattr(render_process, "preview_inserts", nothing)
    monkeypatch.setattr(render_process.jobs, "load", load)
    monkeypatch.setattr(render_process.jobs, "heartbeat", nothing)
    monkeypatch.setattr(render_process.jobs, "mark_if_open", mark_if_open)
    monkeypatch.setattr(render_process.render_segments, "forget", forget)
    monkeypatch.setattr(render_process, "WORKDIR_ROOT", tmp_path)
    outcome = await render_process.run_isolated(str(uuid.uuid4()))
    assert outcome.kind == "done" and len(runs) == 2 and runs[1][render_preflight.ONE_PAGE_ENV] == "1"
    # The process started so draws on one page, whatever the preview asks otherwise.
    monkeypatch.setenv(render_preflight.ONE_PAGE_ENV, "1")
    assert render_preflight.preview_drawn_pages() == 1
    monkeypatch.delenv(render_preflight.ONE_PAGE_ENV)
    assert render_preflight.preview_drawn_pages() == render_preflight.preview_pages() == 2


async def test_a_whole_render_its_ceiling_stopped_is_not_drawn_again_on_one_page(monkeypatch, tmp_path):
    _container(monkeypatch, 24 * _GIB)
    runs: list[dict] = []

    async def supervise(command, **options):
        runs.append(dict(options["env"]))
        return render_process.RenderExit(kind="memory", peak_memory=20 * _GIB, progress=30)

    async def load(_kind, _job_id):
        return SimpleNamespace(quality="high", scene_id=None, status="running")

    async def nothing(*_args, **_kwargs):
        return None

    async def mark_if_open(*_args, **_changes):
        return True

    async def forget(_render_id):
        return None

    monkeypatch.setattr(render_process, "supervise", supervise)
    monkeypatch.setattr(render_process.jobs, "load", load)
    monkeypatch.setattr(render_process.jobs, "heartbeat", nothing)
    monkeypatch.setattr(render_process.jobs, "mark_if_open", mark_if_open)
    monkeypatch.setattr(render_process.render_segments, "forget", forget)
    monkeypatch.setattr(render_process, "WORKDIR_ROOT", tmp_path)
    outcome = await render_process.run_isolated(str(uuid.uuid4()))
    assert outcome.kind == "failed" and len(runs) == 1


async def test_a_routed_preview_hands_its_note_to_its_process(monkeypatch, tmp_path):
    """Where a preview came to be drawn travels to its process, which puts it first in its answer's notes."""
    _container(monkeypatch, 24 * _GIB)
    render_id = str(uuid.uuid4())
    note = "Drawn on its 2 pages on worker w:2, routed there from worker w:1, where its 2 pages need 6.0 GiB."
    render_process._ADMITTED[render_id] = render_process.PreviewAdmission(6 * _GIB, pages=2, note=note)
    runs: list[tuple[int, dict]] = []

    async def supervise(command, **options):
        runs.append((options["memory_limit"], dict(options["env"])))
        return render_process.RenderExit(kind="done", returncode=0)

    async def load(_kind, _job_id):
        return SimpleNamespace(quality="standard", scene_id="cat-soc", status="running")

    async def nothing(*_args, **_kwargs):
        return None

    async def mark_if_open(*_args, **_changes):
        return True

    monkeypatch.setattr(render_process, "supervise", supervise)
    monkeypatch.setattr(render_process, "preview_inserts", nothing)
    monkeypatch.setattr(render_process.jobs, "load", load)
    monkeypatch.setattr(render_process.jobs, "heartbeat", nothing)
    monkeypatch.setattr(render_process.jobs, "mark_if_open", mark_if_open)
    monkeypatch.setattr(render_process.render_segments, "forget", nothing)
    monkeypatch.setattr(render_process, "WORKDIR_ROOT", tmp_path)
    outcome = await render_process.run_isolated(render_id)
    [(limit, env)] = runs
    # Held to its admission's ceiling, on its pages, its note handed to its process, first stopped.
    assert outcome.kind == "done" and limit == 6 * _GIB and render_preflight.ONE_PAGE_ENV not in env
    assert env[render_preflight.ADMISSION_NOTE_ENV] == note and env[render_process.OOM_SCORE_ENV] == "1000"
    monkeypatch.setenv(render_preflight.ADMISSION_NOTE_ENV, note)
    assert render_preflight.preview_admission_note() == note
    monkeypatch.delenv(render_preflight.ADMISSION_NOTE_ENV)
    assert render_preflight.preview_admission_note() is None


async def _noop():
    return None


@pytest.fixture
def lane(monkeypatch):
    world = SimpleNamespace(ran=[], marks=[], held_while_running=None, admissions={}, size=None, rooms=[], published=[])

    async def run_isolated(render_id, **_options):
        world.ran.append(render_id)
        world.held_while_running = render_process.memory_held()
        world.admissions[render_id] = render_process.admission_of(render_id)
        return runners.JobOutcome("done")

    async def no_wait(_render_id):
        return None

    async def mark_if_open(_kind, _job_id, **changes):
        world.marks.append(changes)
        return True

    async def size_of(_render_id):
        return world.size or render_process.preview_size()

    async def rooms(*, me, **_kwargs):
        return [room for room in world.rooms if room.worker != me]

    async def publish(worker_id, *, force=False):
        world.published.append((worker_id, force))

    monkeypatch.setattr(render_process, "run_isolated", run_isolated)
    monkeypatch.setattr(render_process, "preview_size_of", size_of)
    monkeypatch.setattr(preview_routing, "rooms", rooms)
    monkeypatch.setattr(preview_routing, "publish", publish)
    monkeypatch.setattr(jobs, "preview_waits", no_wait)
    monkeypatch.setattr(jobs, "mark_if_open", mark_if_open)
    monkeypatch.setattr(adapters.wake, "schedule", lambda *_args: None)
    return world


def _execution(render_id: str):
    return SimpleNamespace(
        id=uuid.uuid4(), payload={"render_id": render_id}, attempt_count=1, max_attempts=3, adapter_state=None
    )


def _ctx(worker_id: str = "w:1"):
    return SimpleNamespace(db=SimpleNamespace(commit=_noop), worker_id=worker_id, cancel_event=None, stopping=None)


async def test_the_preview_adapter_waits_for_its_pages_with_no_attempt_spent(lane, monkeypatch):
    _container(monkeypatch, 24 * _GIB)
    _running("ultra", 16 * _GIB)
    _running("other-preview", int(7.5 * _GIB), preview=True)
    execution = _execution("p1")
    result = await adapters.VideoPreviewAdapter().execute(_ctx(), execution)
    assert (result.kind, result.backoff_seconds, result.adapter_state, result.place, result.claim_for) == (
        "defer",
        preview_routing.WAIT_POLL_SECONDS,
        adapters.MEMORY_WAITING_STATE,
        "keep",
        None,
    )
    # Since when it waits and why, which its next claims read; its stage says it, no other worker having room.
    since = datetime.fromisoformat(result.payload_update[adapters.MEMORY_WAIT_KEY])
    assert result.payload_update[adapters.MEMORY_WHY_KEY].startswith("its 2 pages need ")
    assert lane.ran == [] and lane.marks[-1]["stage"].startswith(
        "Waiting for its pages on worker w:1 (no other worker has them free with a preview slot): its 2 pages need "
    )
    execution.adapter_state, execution.payload = result.adapter_state, {**execution.payload, **result.payload_update}
    assert adapters.memory_waited(execution)[1] == since.isoformat()
    # A wait of another kind in between starts it again.
    assert adapters.memory_waited(SimpleNamespace(payload=execution.payload, adapter_state=jobs.PREVIEW_WAITING_STATE))[0] == 0
    # Room again: it runs on its pages, its memory held while it runs and given back after, its answer saying it waited.
    del render_process._ATTEMPTS["other-preview"]
    execution.payload[adapters.MEMORY_WAIT_KEY] = (datetime.now(UTC) - timedelta(seconds=42)).isoformat()
    result = await adapters.VideoPreviewAdapter().execute(_ctx(), execution)
    assert result.kind == "completed" and lane.ran == ["p1"] and lane.admissions["p1"].pages == 2
    assert lane.admissions["p1"].note.startswith("Drawn on its 2 pages on worker w:1, after waiting 42 s for its pages, as ")
    assert "its 2 pages need " in lane.admissions["p1"].note
    assert lane.held_while_running == 16 * _GIB + render_process.preview_memory_ceiling()
    assert render_process._ADMITTED == {}
    # What it has free said as it started and as it ended, for the workers that route previews.
    assert lane.published[-2:] == [("w:1", True), ("w:1", True)]


async def test_the_render_adapter_never_waits_for_a_preview(lane, monkeypatch):
    _container(monkeypatch, 12 * _GIB)
    for index in range(2):
        _running(f"preview-{index}", render_process.preview_memory_ceiling(), preview=True)
    ctx = SimpleNamespace(db=SimpleNamespace(commit=_noop), worker_id="w:1", cancel_event=None, stopping=None)
    result = await adapters.VideoRenderAdapter().execute(ctx, _execution("whole"))
    assert result.kind == "completed" and lane.ran == ["whole"]
