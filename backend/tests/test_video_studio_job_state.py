"""What the caller is told of a Video Studio job is what the worker last wrote.

* ``get_video_jobs`` reads every job from its row: a progress the worker
  writes is what the next poll reports - status, percent, stage, last
  progress time - and a job the worker wrote a heartbeat for is never
  ``queued``. Its wait returns as soon as one job finishes.
* ``generate_video_clip`` answers ``{clip_id, status, waited_seconds,
  completion, reason, start_frame_file_id}`` whether the shot came back inside the call or goes on
  in the background, and a background answer says why: the inline budget ran
  out while the shot waited for a worker, was queued at the provider, was
  rate limited.
* A queued job's place is read from the executions the worker claims:
  a scene preview queued within its head start is ahead of a whole
  render, one queued after the render waited longer is behind it.

Synthetic jobs only. The job tables and their executions are an in-memory
store that hands every session fresh copies of the committed rows and writes
them back on commit, so the worker's writes and the poller's reads go through
separate sessions as they do on PostgreSQL; ``WHERE`` clauses are evaluated by
SQLAlchemy itself.
"""

from __future__ import annotations

import asyncio
import json
import re
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
import sqlalchemy as sa
from sqlalchemy.orm.evaluator import _EvaluatorCompiler

from app.core.config import settings
from app.llm.video import VideoJobState, VideoProviderBusyError, VideoResult
from app.models.orchestration import OrchestrationExecution
from app.models.video_studio import JOB_PENDING, JOB_READY, JOB_RUNNING, VideoClip, VideoMusic, VideoRender, VideoTake
from app.services.builtin_tools import video_studio as tool
from app.services.video_studio import assets, clips, content_drift, encoder, jobs, projects, runners
from app.tools.base import ToolCall

USER_ID = uuid.uuid4()
PROJECT_ID = uuid.uuid4()
_STORED_MODELS = (VideoTake, VideoRender, VideoClip, VideoMusic, OrchestrationExecution)


class _Result:
    def __init__(self, rows: list | None = None, scalar: int | None = None):
        self._rows, self._scalar = rows or [], scalar

    def scalars(self):
        return self

    def all(self):
        return list(self._rows)

    def scalar_one(self):
        return self._scalar


class _Store:
    """The committed job rows. A session reads copies and writes back what it loaded on commit."""

    def __init__(self):
        self.rows: dict[tuple[type, uuid.UUID], dict] = {}

    def put(self, row) -> None:
        mapper = sa.inspect(type(row))
        self.rows[(type(row), row.id)] = {attr.key: getattr(row, attr.key) for attr in mapper.column_attrs}

    def copy(self, model, key):
        values = self.rows.get((model, key))
        return None if values is None else model(**values)

    def of(self, model) -> list:
        return [self.copy(m, key) for (m, key) in list(self.rows) if m is model]

    @asynccontextmanager
    async def session(self):
        yield _Session(self)


class _Session:
    """A session over the store, with SQLAlchemy's identity map: a row it loaded or added is the one it reads again."""

    def __init__(self, store: _Store):
        self.store = store
        self.loaded: list = []

    def _own(self, row):
        """The session's instance of *row* when it holds one, else *row*, held from now on."""
        held = next((kept for kept in self.loaded if type(kept) is type(row) and kept.id == row.id), None)
        if held is None:
            self.loaded.append(row)
        return held or row

    async def get(self, model, key):
        row = self.store.copy(model, uuid.UUID(str(key)))
        return self._own(row) if row is not None else None

    async def execute(self, statement):
        entity = statement.column_descriptions[0]["entity"]
        if entity is None:
            table = statement.get_final_froms()[0]
            model = next(m for m in _STORED_MODELS if m.__tablename__ == table.name)
        else:
            model = entity
        where = statement.whereclause
        test = _EvaluatorCompiler(model).process(where) if where is not None else (lambda _row: True)
        # What a flushed session reads: its own instances as it changed them, the rows it added not committed yet.
        held = {(type(row), row.id): row for row in self.loaded}
        candidates = [held.get((model, row.id), row) for row in self.store.of(model)]
        candidates += [row for row in self.loaded if type(row) is model and (model, row.id) not in self.store.rows]
        rows = [row for row in candidates if test(row)]
        if entity is None:
            return _Result(scalar=len(rows))
        return _Result(rows=[self._own(row) for row in rows])

    def add(self, row) -> None:
        # The id PostgreSQL's insert would give it at the flush.
        if getattr(row, "id", None) is None:
            row.id = uuid.uuid4()
        self.loaded.append(row)

    async def flush(self) -> None:
        return None

    async def commit(self) -> None:
        for row in self.loaded:
            self.store.put(row)


@pytest.fixture
def store(monkeypatch):
    """The job tables in memory, behind every session the jobs, the runners and the tools open."""
    jobs_store = _Store()
    for module in (jobs, runners, tool):
        monkeypatch.setattr(module, "async_session", jobs_store.session)

    async def create_execution(_db, **kwargs):
        """The engine's insert: a ``pending`` execution, due at ``run_at`` or now."""
        now = jobs.datetime.now(UTC)
        execution = OrchestrationExecution(
            id=uuid.uuid4(),
            adapter=kwargs["adapter"],
            state="pending",
            payload=kwargs["payload"],
            run_at=kwargs.get("run_at") or now,
            deadline_at=kwargs.get("deadline_at"),
            attempt_count=0,
            max_attempts=kwargs.get("max_attempts") or 3,
            dedup_key=kwargs.get("dedup_key"),
            created_at=now,
            updated_at=now,
        )
        jobs_store.put(execution)
        return execution

    async def find_user(_db, _user_id):
        return SimpleNamespace(id=USER_ID)

    async def visible(_db, _video_id, _user):
        return SimpleNamespace(id=PROJECT_ID)

    async def nothing(*_args, **_kwargs):
        return None

    monkeypatch.setattr("app.services.orchestration_engine.create_execution", create_execution)
    monkeypatch.setattr(tool, "_user", find_user)
    monkeypatch.setattr(projects, "load_visible_project", visible)
    monkeypatch.setattr(jobs, "wake", nothing)
    monkeypatch.setattr(jobs, "_WAIT_POLL_SECONDS", 0.05)
    return jobs_store


def _executor():
    _definitions, executor = tool.build_video_studio_tools(None, str(USER_ID))
    return executor


async def _call(name: str, **arguments):
    return await _executor().execute_tool(ToolCall(id="c1", name=name, arguments=arguments))


def _head(content: str) -> dict:
    """The JSON object a Video Studio answer opens with."""
    decoder = json.JSONDecoder()
    value, _end = decoder.raw_decode(content)
    return value


async def _enqueue_render(store: _Store) -> VideoRender:
    """A render as ``render_video`` queues it: its row and its execution, in one transaction."""
    render = VideoRender(
        id=uuid.uuid4(),
        project_id=PROJECT_ID,
        user_id=USER_ID,
        revision=1,
        quality="high",
        status=JOB_PENDING,
        progress=0,
        stage="Queued",
        created_at=datetime.now(UTC),
    )
    async with store.session() as db:
        db.add(render)
        await jobs.enqueue(db, "render", render)
        await db.commit()
    return render


# ── Gap 1: the poller reads what the worker writes ────────────────────────


def test_the_next_poll_reads_the_progress_the_worker_wrote(store, monkeypatch):
    monkeypatch.setattr(tool, "_wait_budget", lambda _default: 0.0)

    async def scenario():
        render = await _enqueue_render(store)
        before = _head((await _call("get_video_jobs", render_ids=[str(render.id)])).content)
        # The worker path: the supervisor takes the render, its process writes a progress.
        await jobs.heartbeat("render", render.id, "Starting the render process")
        await jobs.mark("render", render.id, status=JOB_RUNNING, progress=34, stage="Drawing frames")
        after = _head((await _call("get_video_jobs", render_ids=[str(render.id)])).content)
        return render, before, after

    render, before, after = asyncio.run(scenario())
    queued = before["jobs"][0]
    assert (queued["kind"], queued["id"], queued["status"], queued["percent"]) == (
        "render",
        str(render.id),
        "queued",
        0,
    )
    assert queued["last_progress_at"] is None
    assert queued["queue"] == {"position": 1, "running": 0, "ahead": 0, "per_worker": 1, "lane": "render"}
    running = after["jobs"][0]
    assert (running["status"], running["percent"], running["stage"]) == ("running", 34, "Drawing frames")
    assert running["last_progress_at"] is not None and running["last_progress_seconds_ago"] <= 5
    assert running["reason"] is None
    assert after["returned_because"] == "the wait budget of 0 s ran out"


def test_a_job_the_worker_has_started_is_never_reported_queued(store):
    """Between the worker taking a render and its process writing a status, and
    while a render that crashed waits to start again, the row is ``pending`` -
    with a heartbeat, so it reads as running, with the stage that says why."""

    async def scenario():
        render = await _enqueue_render(store)
        await jobs.heartbeat("render", render.id, "Starting the render process")
        started = jobs.job_state("render", store.copy(VideoRender, render.id))
        await jobs.mark("render", render.id, stage="Starting again: the render process was killed", progress=0)
        await jobs.heartbeat("render", render.id, "Starting the render process")
        retrying = jobs.job_state("render", store.copy(VideoRender, render.id))
        return started, retrying

    started, retrying = asyncio.run(scenario())
    assert (started.status, started.stage) == ("running", "Starting the render process")
    # A second attempt keeps the stage that says why it starts again.
    assert (retrying.status, retrying.stage) == ("running", "Starting again: the render process was killed")


def test_a_running_render_says_when_its_lease_was_last_renewed_before_it_expires(store, monkeypatch):
    """While it runs, a render reports its execution's lease; a missed renewal is said before the lease expires."""
    monkeypatch.setattr(tool, "_wait_budget", lambda _default: 0.0)
    monkeypatch.setattr(settings, "orchestration_lease_seconds", 60)

    async def renewed(render: VideoRender, *, seconds_ago: int, attempt: int) -> None:
        """The worker's claim of the render's execution, its lease last renewed *seconds_ago*."""
        async with store.session() as db:
            execution = await db.get(OrchestrationExecution, render.execution_id)
            execution.state, execution.lease_owner, execution.attempt_count = "running", "worker-1", attempt
            execution.updated_at = datetime.now(UTC) - timedelta(seconds=seconds_ago)
            execution.lease_expires_at = execution.updated_at + timedelta(seconds=60)
            await db.commit()

    async def scenario():
        render = await _enqueue_render(store)
        await jobs.mark("render", render.id, status=JOB_RUNNING, progress=11, stage="Drawing figure 1/1 (285/588 frames)")
        await renewed(render, seconds_ago=5, attempt=1)
        fresh = (await _call("get_video_jobs", render_ids=[str(render.id)])).content
        await renewed(render, seconds_ago=45, attempt=2)
        late = (await _call("get_video_jobs", render_ids=[str(render.id)])).content
        return fresh, late

    fresh, late = asyncio.run(scenario())
    lease = _head(fresh)["jobs"][0]["lease"]
    assert 5 <= lease["last_heartbeat_seconds_ago"] <= 6 and 53 <= lease["expires_in_seconds"] <= 55
    assert (lease["attempt"], lease["max_attempts"], lease["holder"]) == (1, 3, "worker-1")
    # Its lease and its heartbeat are always said; a missed renewal only when there was one.
    assert "; its lease (worker worker-1) was renewed 5 s ago and expires in 5" in fresh
    assert "a renewal was missed" not in fresh and "attempt 1 of" not in fresh
    lease = _head(late)["jobs"][0]["lease"]
    assert 45 <= lease["last_heartbeat_seconds_ago"] <= 46 and lease["attempt"] == 2
    said = late.split("\n\n", 1)[1]
    assert "Drawing figure 1/1 (285/588 frames)" in said and ", attempt 2 of 3; its lease (worker worker-1) was renewed 4" in said
    assert "s ago and expires in 1" in said and "its worker may have stalled, and it runs again by itself" in said


# ── The render queue: what the worker takes first ────────────────


@pytest.fixture
def clock(monkeypatch):
    """The time the jobs read, moved by the test; the store's insert reads it too."""
    now = SimpleNamespace(value=datetime(2026, 10, 20, 10, 24, tzinfo=UTC))

    class _ClockedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return now.value if tz is None else now.value.astimezone(tz)

    monkeypatch.setattr(jobs, "datetime", _ClockedDatetime)
    return now


def test_the_queue_place_of_a_render_is_the_order_its_lane_takes_it_in(store, clock):
    """Scene previews have a lane of their own: a whole render's place counts the whole renders before
    it and nothing else, a preview's the previews; each place names its lane, what runs in it and its slots."""
    names: dict[uuid.UUID, str] = {}

    async def queue(name: str, *, preview: bool, after: timedelta) -> VideoRender:
        clock.value += after
        render = VideoRender(
            id=uuid.uuid4(),
            project_id=PROJECT_ID,
            user_id=USER_ID,
            revision=1,
            quality="draft" if preview else "high",
            status=JOB_PENDING,
            progress=0,
            stage="Queued",
            scene_id="claim" if preview else None,
            created_at=clock.value,
        )
        async with store.session() as db:
            db.add(render)
            await jobs.enqueue(db, "render", render)
            await db.commit()
        names[render.execution_id] = name
        return render

    def execution(render: VideoRender) -> OrchestrationExecution:
        return store.copy(OrchestrationExecution, render.execution_id)

    async def places(renders: dict[str, VideoRender]) -> dict[str, tuple[str, int, int, int]]:
        found = {}
        for name, render in renders.items():
            place = await jobs.queue_place("render", store.copy(VideoRender, render.id))
            found[name] = (place.lane, place.running, place.ahead, place.per_worker)
        return found

    async def told(render: VideoRender) -> str:
        return jobs.describe((await jobs.job_states("render", [store.copy(VideoRender, render.id)]))[0])

    async def scenario():
        renders = {"whole": await queue("whole", preview=False, after=timedelta(0))}
        for name in ("early-1", "early-2"):
            renders[name] = await queue(name, preview=True, after=timedelta(minutes=1))
        renders["late"] = await queue("late", preview=True, after=timedelta(minutes=6))
        renders["newest"] = await queue("newest", preview=False, after=timedelta(seconds=1))
        lanes = {names[e.id]: e.adapter for e in store.of(OrchestrationExecution)}
        queued = await places(renders)
        said = (await told(renders["whole"]), await told(renders["early-2"]))
        # The worker takes the whole render and the first preview; the second loses its lease, queued again now.
        for name in ("whole", "early-1"):
            taken = execution(renders[name])
            taken.state = "running"
            store.put(taken)
        # The whole render starts, its pre-flight expecting 20 min.
        started = store.copy(VideoRender, renders["whole"].id)
        started.status, started.started_at, started.preflight = JOB_RUNNING, clock.value, {"expected_seconds": 1200}
        store.put(started)
        clock.value += timedelta(seconds=30)
        requeued = execution(renders["early-2"])
        requeued.run_at = clock.value
        store.put(requeued)
        later = await places({name: renders[name] for name in ("newest", "early-2", "late")})
        behind = await told(renders["newest"])
        return lanes, queued, said, later, behind

    lanes, queued, said, later, behind = asyncio.run(scenario())
    render_lane, preview_lane = jobs.RENDER_ADAPTER, jobs.PREVIEW_ADAPTER
    assert lanes == {
        "whole": render_lane, "newest": render_lane, "early-1": preview_lane, "early-2": preview_lane, "late": preview_lane
    }
    # No preview is ever ahead of a whole render, whenever it was asked; previews keep the order they were asked in.
    assert queued == {
        "whole": ("render", 0, 0, 1),
        "newest": ("render", 0, 1, 1),
        "early-1": ("preview", 0, 0, 2),
        "early-2": ("preview", 0, 1, 2),
        "late": ("preview", 0, 2, 2),
    }
    assert said == (
        "waiting for a worker: 0 renders being made now, 0 queued ahead of it in the render lane (a worker makes 1 at a time)",
        "waiting for a worker: 1 of this video's previews ahead (a worker makes 2 at a time)",
    )
    assert later == {"newest": ("render", 1, 0, 1), "early-2": ("preview", 1, 1, 2), "late": ("preview", 1, 0, 2)}
    # Behind it, the newer render says the time it has left and when it starts itself.
    assert behind == (
        "waiting for a worker: 1 render being made now, 0 queued ahead of it in the render lane (a worker makes 1 at a "
        "time); the render being made ends in about 20 min (by its pre-flight's estimate) - this one starts in about 20 min"
    )


def test_the_preview_lane_is_dealt_round_robin_whatever_the_order_asked(store, clock, monkeypatch):
    """The preview lane is dealt one preview per video in turn (``preview_lane``), never in asked order.

    Three videos ask a preview each, in turn, four times, through the start
    every preview takes (``projects.start_scene_render``): asked round-robin
    already, every preview keeps the place it was given (the previews
    of the video asked last no longer go first). Then a sweep of one video's
    four scenes is followed by another video's single preview: dealt into
    the first round it lands second, not fifth, and each scene of the sweep
    after the first moves back one place exactly. A preview waiting for its
    video's cap keeps its place (``defer(place="keep")``: its ``run_at``
    stays, only ``not_before`` moves), and a preview a stopping worker handed
    back goes first in its lane, counted with the renders being made:
    nobody behind it is pushed back.
    """
    from app.services.orchestration_engine import NOT_BEFORE_PAYLOAD_KEY

    scenes = [{"id": f"s{n}", "type": "title", "title": f"Scene {n}"} for n in range(1, 5)]

    async def effective(_db, _project, _user):
        return SimpleNamespace(spec={"title": "Film", "scenes": scenes}, template=None)

    async def nothing(*_args, **_kwargs):
        return None

    monkeypatch.setattr(projects, "effective_spec", effective)
    monkeypatch.setattr(projects, "_lock_scene_preview_start", nothing)
    monkeypatch.setattr("app.services.audit.log_audit", nothing)
    user = SimpleNamespace(id=USER_ID, role="user")

    async def preview(video: SimpleNamespace, scene: str) -> VideoRender:
        clock.value += timedelta(seconds=20)
        async with store.session() as db:
            started = await projects.start_scene_render(db, video, user, scene=scene)
            await db.commit()
        return started.render

    async def position(render: VideoRender) -> int:
        return (await jobs.queue_place("render", store.copy(VideoRender, render.id))).position

    async def scenario():
        videos = [
            SimpleNamespace(id=uuid.uuid4(), revision=1, title=f"Film {n}", spec={"title": "Film", "scenes": scenes})
            for n in range(3)
        ]
        asked: list[VideoRender] = []
        seen: dict[uuid.UUID, list[int]] = {}
        for scene in scenes:
            for video in videos:
                asked.append(await preview(video, scene["id"]))
                for render in asked:
                    seen.setdefault(render.id, []).append(await position(render))
        # The first preview waits for its video's cap: claimable 15 s later, in its place.
        waiting = store.copy(OrchestrationExecution, asked[0].execution_id)
        waiting.payload = {**waiting.payload, NOT_BEFORE_PAYLOAD_KEY: (clock.value + timedelta(seconds=15)).isoformat()}
        store.put(waiting)
        # A preview a stopping worker handed back, in the same lane: first in the queue.
        fourth = SimpleNamespace(id=uuid.uuid4(), revision=1, title="Film 4", spec={"title": "Film", "scenes": scenes})
        handed = await preview(fourth, scenes[0]["id"])
        execution = store.copy(OrchestrationExecution, handed.execution_id)
        execution.run_at = min(e.run_at for e in store.of(OrchestrationExecution)) - timedelta(seconds=1)
        execution.adapter_state = jobs.HANDED_OVER_STATE
        store.put(execution)
        for render in asked:
            seen[render.id].append(await position(render))
        place = await jobs.queue_place("render", store.copy(VideoRender, asked[0].id))
        return asked, seen, place

    async def sweep():
        for execution in store.of(OrchestrationExecution):
            execution.state = "completed"
            store.put(execution)
        film, other = (
            SimpleNamespace(id=uuid.uuid4(), revision=1, title=title, spec={"title": "Film", "scenes": scenes})
            for title in ("Sweep", "Teaser")
        )
        swept = [await preview(film, scene["id"]) for scene in scenes]
        before = [await position(render) for render in swept]
        single = await preview(other, scenes[0]["id"])
        return before, [await position(render) for render in swept], await position(single)

    asked, seen, place = asyncio.run(scenario())
    # Asked round-robin across the three videos: dealt so, never a step back for any of them.
    assert [seen[render.id][0] for render in asked] == list(range(1, 13))
    for render in asked:
        assert set(seen[render.id]) == {seen[render.id][0]}, seen[render.id]
    # The handed-back render is counted with the renders being made, not ahead of the queued ones.
    assert (place.running, place.ahead) == (1, 0)
    before, after, single = asyncio.run(sweep())
    assert before == [1, 2, 3, 4] and single == 2
    assert after == [1, 3, 4, 5]


def test_every_kind_reports_status_percent_stage_heartbeat_and_reason():
    now = datetime.now(UTC)
    clip = VideoClip(
        id=uuid.uuid4(),
        status=JOB_RUNNING,
        progress=40,
        stage="Generating at the provider",
        provider_state="in_progress",
        provider_eta_at=now + timedelta(seconds=90),
        progress_at=now,
    )
    music = VideoMusic(id=uuid.uuid4(), status="failed", progress=60, error="The prompt names an artist.")
    take = VideoTake(id=uuid.uuid4(), status=JOB_READY, progress=100)
    states = [
        jobs.job_state("clip", clip, now=now),
        jobs.job_state("music", music, now=now),
        jobs.job_state("take", take, now=now),
    ]
    keys = {"kind", "id", "status", "percent", "stage", "last_progress_at", "last_progress_seconds_ago", "reason"}
    for state in states:
        assert keys <= set(state.as_dict(now))
    shot = states[0].as_dict(now)
    assert (shot["status"], shot["percent"], shot["provider_state"]) == ("running", 40, "in_progress")
    assert 85 <= shot["estimated_remaining_seconds"] <= 90
    assert jobs.describe(states[0]) == "the provider is generating it (in_progress, 40%, about 90 s left)"
    assert (states[1].status, states[1].reason) == ("failed", "The prompt names an artist.")
    recording = states[2].as_dict(now)
    assert (recording["kind"], recording["status"], recording["percent"]) == ("recording", "ready", 100)


def test_the_wait_returns_as_soon_as_one_job_finishes(store):
    async def scenario():
        first, second = await _enqueue_render(store), await _enqueue_render(store)

        async def worker():
            await asyncio.sleep(0.2)
            await jobs.mark("render", first.id, status=JOB_READY, progress=100)

        finishing = asyncio.create_task(worker())
        started = asyncio.get_running_loop().time()
        rows, why = await jobs.wait_for_jobs({"render": [str(first.id), str(second.id)]}, timeout=30)
        await finishing
        return rows, why, asyncio.get_running_loop().time() - started

    rows, why, waited = asyncio.run(scenario())
    assert why == jobs.WAIT_ONE_DONE and waited < 5
    assert [row.status for row in rows["render"]] == [JOB_READY, JOB_PENDING]


def test_the_job_tools_declare_live_state():
    """The tools that report a job's or a session's state are never replayed by the agent loop's cache."""
    live = {d.name for d in tool._definitions(["high"]) if d.live_state}
    assert live == {
        "get_video_jobs",
        "get_video",
        "get_video_recording",
        "list_video_recordings",
        "list_browser_profiles",
        "check_browser_profile_session",
        "snapshot_browser_profile_page",
    }


# ── Gap 2: generate_video_clip says how it completed and why ──────────────


class _Provider:
    """A video provider whose job the test drives: queued, then generating, never done unless told."""

    def __init__(self, *, busy: int | None = None, finish: bool = False, retry_after: float | None = None):
        self.busy = busy
        self.finish = finish
        self.retry_after = retry_after
        self.advance = asyncio.Event()

    def takes_negative_prompt(self, model):
        return True

    async def generate_video(self, request, *, progress=None, on_job=None, resume_job=None, on_state=None):
        if self.busy is not None:
            raise VideoProviderBusyError(
                f"The video provider is unavailable (HTTP {self.busy})", status_code=self.busy, retry_after=self.retry_after
            )
        await on_job("video_1")
        if self.finish:
            return VideoResult(data=b"MP4", job_id="video_1")
        await on_state(VideoJobState("queued"))
        await self.advance.wait()
        await progress(40)
        await on_state(VideoJobState("in_progress", 40))
        await asyncio.Event().wait()


@pytest.fixture
def shots(store, monkeypatch):
    """``generate_video_clip`` on the real tool, queue and worker; *provider* decides how the shot goes."""
    world = SimpleNamespace(provider=_Provider(), worker=None, workers=True)
    monkeypatch.setattr(settings, "video_studio_clip_inline_wait_seconds", 1)

    async def plan(_db, _user, **asked):
        return SimpleNamespace(label="Test provider", notes=[], prompt=asked["prompt"])

    async def queue(db, user, planned, **_kwargs):
        clip = VideoClip(
            id=uuid.uuid4(),
            user_id=user.id,
            provider="openai",
            model="sora-2",
            prompt=planned.prompt,
            params={"provider_id": str(uuid.uuid4()), "duration_seconds": 8},
            status=JOB_PENDING,
            progress=0,
            created_at=datetime.now(UTC),
        )
        db.add(clip)
        await jobs.enqueue(db, "clip", clip)
        world.clip_id = str(clip.id)
        return clip

    async def resolve(_db, _user_id, *, model=None, provider_id=None, **_kwargs):
        return world.provider, model, SimpleNamespace(id=provider_id, provider="openai")

    async def wake():
        if world.workers:
            world.worker = asyncio.create_task(runners.run_clip(world.clip_id))

    async def store_path(path, **_kwargs):
        return SimpleNamespace(id=uuid.uuid4(), size=len(Path(path).read_bytes()))

    async def probe(_path):
        return SimpleNamespace(duration=8.0, width=1280, height=720, has_audio=False)

    async def extract_frame(_path, *, at, out_path, width):
        Path(out_path).write_bytes(b"JPG")

    async def content_check(*_args, **_kwargs):
        return content_drift.ContentCheck(True)

    monkeypatch.setattr(content_drift, "check", content_check)
    monkeypatch.setattr(clips, "plan_clip", plan)
    monkeypatch.setattr(clips, "queue_clip", queue)
    monkeypatch.setattr(clips, "resolve", resolve)
    monkeypatch.setattr(jobs, "wake", wake)
    monkeypatch.setattr(assets, "store_path", store_path)
    monkeypatch.setattr(encoder, "probe", probe)
    monkeypatch.setattr(encoder, "extract_frame", extract_frame)
    return world


async def _stop(world) -> None:
    if world.worker is not None and not world.worker.done():
        world.worker.cancel()
        await asyncio.gather(world.worker, return_exceptions=True)


_SHAPE = {
    "clip_id",
    "status",
    "waited_seconds",
    "completion",
    "reason",
    "start_frame_file_id",
    # The shot's own frames and what they measure: null until it is made.
    "first_frame_file_id",
    "mid_frame_file_id",
    "last_frame_file_id",
    "frame_qc",
    # How its drifting camera was locked off, or why not: null until it is made, or when not asked.
    "stabilisation",
    # Whether its composited logo shows more than once: null for a shot whose logo was not composited.
    "logo_check",
    # The logo drawn onto its subject: null for a shot without the tracked path.
    "logo_tracked",
    # Its text check: the words checked, the ones the start picture holds unreadable; null until checked.
    "text_check",
    # The flat screen its first frame shows: its bezel and how flat it is; null for a take showing none.
    "surface",
    # What stands in front of a screen insert's screen over the take: null for a shot carrying none.
    "foreground_overlap",
    # How it is asked to look like something: start frame, reference images, both, or the prompt alone.
    "mode",
    # The model that made the take: a fallback one when the shot falls back.
    "model",
}


def test_a_shot_past_the_inline_budget_says_it_went_to_background_and_why(shots, monkeypatch):
    monkeypatch.setattr(tool, "_wait_budget", lambda _default: 0.0)

    async def scenario():
        answer = await _call("generate_video_clip", prompt="A slow orbit around a dark globe")
        # The provider moves on after the call returned: a poll reports what it says now.
        shots.provider.advance.set()
        await asyncio.sleep(0.1)
        poll = await _call("get_video_jobs", clip_ids=[shots.clip_id])
        await _stop(shots)
        return answer, poll

    answer, poll = asyncio.run(scenario())
    head = _head(answer.content)
    assert set(head) == _SHAPE and not answer.is_error
    assert (head["clip_id"], head["status"], head["completion"]) == (shots.clip_id, "generating", "background")
    assert head["waited_seconds"] >= 1
    assert head["reason"] == "inline wait budget of 1 s exceeded: the provider has it queued"
    shot = _head(poll.content)["jobs"][0]
    assert (shot["kind"], shot["status"], shot["provider_state"]) == ("clip", "running", "in_progress")
    assert shot["estimated_remaining_seconds"] is not None and shot["last_progress_at"] is not None


def test_a_shot_waiting_for_a_worker_or_rate_limited_says_so(shots):
    async def ask():
        answer = await _call("generate_video_clip", prompt="Particles over a city at night")
        await _stop(shots)
        return _head(answer.content)

    shots.workers = False
    waiting = asyncio.run(ask())
    assert waiting["completion"] == "background"
    assert waiting["reason"] == (
        "inline wait budget of 1 s exceeded: waiting for a worker: 0 AI shots being made now, "
        "0 queued ahead of it (a worker makes 4 at a time)"
    )
    shots.workers = True
    shots.provider = _Provider(busy=429)
    limited = asyncio.run(ask())
    # Waited for, never failed: when it is asked next, and how many times it was turned away - the call does
    # not wait for a shot held in the queue.
    assert re.fullmatch(
        r"inline wait skipped: provider busy, next try at \d\d:\d\d UTC \(in \d+ s\), 1 try: "
        r"openai \(sora-2\) - The video provider is unavailable \(HTTP 429\) - it starts by itself then, "
        r"nothing failed",
        limited["reason"],
    )


def test_a_shot_made_inside_the_call_answers_the_same_shape(shots):
    shots.provider = _Provider(finish=True)

    async def ask():
        answer = await _call("generate_video_clip", prompt="Waves of light over a map")
        await _stop(shots)
        return answer

    answer = asyncio.run(ask())
    head = _head(answer.content)
    assert set(head) == _SHAPE
    assert (head["status"], head["completion"], head["reason"]) == ("ready", "inline", None)
    assert f"Clip `{head['clip_id']}` is ready" in answer.content


def test_a_rate_limit_and_an_outage_both_hold_the_shot_and_spend_no_attempt(shots):
    """A 429 or a 503 when the job is created started nothing upstream: waited out, no attempt spent."""
    shots.workers = False

    async def run(provider):
        shots.provider = provider
        await _call("generate_video_clip", prompt="A dolly across a server room")
        return await runners.run_clip(shots.clip_id)

    held = asyncio.run(run(_Provider(busy=429, retry_after=23.0)))
    assert (held.kind, held.busy) == ("hold", True) and 23.0 <= held.wait <= 25.3 and "HTTP 429" in held.error
    waited = asyncio.run(run(_Provider(busy=503)))
    assert (waited.kind, waited.busy) == ("hold", True) and 30.0 <= waited.wait <= 33.0 and "HTTP 503" in waited.error


def test_the_frames_a_made_shot_answers_with_are_files_of_the_conversation(shots, monkeypatch):
    """``read_file_content`` opens what the answer names: the frames go through the conversation's copies."""
    shots.provider = _Provider(finish=True)
    frames = {"first_frame_file_id": "f1", "mid_frame_file_id": "f2", "last_frame_file_id": "f3"}
    linked: list = []

    async def keep_frames(clip_id, *_args, params, **_kwargs):
        params["frame_qc"] = {**frames, "motion_ssim": 0.9}
        await jobs.mark("clip", clip_id, params=params)
        return params["frame_qc"]

    async def file_refs(file_ids, *, show=True, **_kwargs):
        if not show:
            linked.append(list(file_ids))
            return tool._Pictures(ids=[f"conv-{f}" for f in file_ids], copies={f: f"conv-{f}" for f in file_ids})
        return tool._Pictures()

    monkeypatch.setattr(runners, "_keep_frames", keep_frames)
    monkeypatch.setattr(tool, "_file_refs", file_refs)

    async def ask():
        answer = await _call("generate_video_clip", prompt="Waves of light over a map")
        await _stop(shots)
        return answer

    head = _head(asyncio.run(ask()).content)
    assert head["status"] == "ready" and linked == [["f1", "f2", "f3"]]
    assert [head[name] for name in frames] == ["conv-f1", "conv-f2", "conv-f3"]
    assert head["frame_qc"] == {"motion_ssim": 0.9}


def test_the_inline_budget_is_one_setting_capped_by_the_call_deadline(monkeypatch):
    from app.tools.tool_events import bind_tool_event_sink, reset_tool_event_sink

    monkeypatch.setattr(settings, "video_studio_clip_inline_wait_seconds", 200)
    assert jobs.clip_inline_wait_budget() == (200.0, False)
    binding = bind_tool_event_sink(None, timeout=60)
    try:
        budget, capped = jobs.clip_inline_wait_budget()
    finally:
        reset_tool_event_sink(binding)
    assert capped and 50 <= budget <= 52
