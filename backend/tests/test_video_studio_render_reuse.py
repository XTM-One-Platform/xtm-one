"""One render per revision and quality, scene previews queued per video, a preview's poster. Synthetic rows only.

* ``start_render`` answers the render of the current revision at the same
  quality still queued or running - one the engine runs again after a lost
  lease included - without gating, cancelling, queueing or auditing again;
  a twin started while the gate ran is found under the lock; another quality
  replaces it as before; ``render_video`` says it followed the one in flight;
* a start of a revision and quality whose render no worker resumed brings
  that render back as the same job, from its kept segments;
* a queue place counts the previews due before it, read on the start's own
  session, and the start answers it (the previews of every video go in the
  order they were asked: ``test_video_studio_job_state``);
* a scene preview's poster is its first settled frame: the player never asks
  for a poster that is not there.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from app.models.video_studio import JOB_FAILED, JOB_PENDING, JOB_RUNNING, VideoRender
from app.services.video_studio import jobs, projects, render_checks

USER = SimpleNamespace(id=uuid.UUID("00000000-0000-0000-0000-0000000061b1"), role="user", email="maker@example.com")
CONVERSATION = uuid.UUID("00000000-0000-0000-0000-0000000061c0")


def _project() -> SimpleNamespace:
    return SimpleNamespace(id=uuid.uuid4(), revision=7, title="Launch film", scene_previews=None)


class _StartDb:
    """The start's reads: the render of the revision in flight (the n-th time it is asked), nothing else."""

    def __init__(self, in_flight: VideoRender | None = None, *, found_on: int = 1) -> None:
        self.in_flight = in_flight
        self.found_on = found_on
        self.asked: list[str] = []
        self.added: list = []

    async def execute(self, statement, _params=None):
        sql = str(statement)
        self.asked.append(sql)
        twin = "video_renders.quality = " in sql and "video_renders.revision = " in sql
        if twin and len([s for s in self.asked if "video_renders.quality = " in s]) >= self.found_on and self.in_flight:
            rows = [self.in_flight]
        else:
            rows = []
        return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: rows))

    def add(self, row) -> None:
        self.added.append(row)

    async def flush(self) -> None:
        return None


@pytest.fixture
def starting(monkeypatch):
    world = SimpleNamespace(gated=0, audits=[], enqueued=[], cancelled=[])

    async def gate(*_args, **_kwargs):
        world.gated += 1
        return []

    async def effective(*_args, **_kwargs):
        return SimpleNamespace(spec={"scenes": []})

    async def none(*_args, **_kwargs):
        return []

    async def enqueue(_db, kind, row, **kwargs):
        world.enqueued.append((kind, row, kwargs))

    async def cancel(_db, kind, row, **kwargs):
        world.cancelled.append(row)

    async def audit(_db, **kwargs):
        world.audits.append(kwargs)

    monkeypatch.setattr(render_checks, "gate", gate)
    monkeypatch.setattr(projects, "effective_spec", effective)
    monkeypatch.setattr(jobs, "renders_in_flight", none)
    monkeypatch.setattr(jobs, "enqueue", enqueue)
    monkeypatch.setattr(jobs, "cancel", cancel)
    monkeypatch.setattr("app.services.audit.log_audit", audit)
    return world


def _running(project, *, quality: str = "high", owed: uuid.UUID | None = None) -> VideoRender:
    return VideoRender(
        id=uuid.uuid4(),
        project_id=project.id,
        user_id=USER.id,
        revision=project.revision,
        quality=quality,
        status=JOB_RUNNING,
        progress=63,
        stage="Drawing frames (82%)",
        owed_conversation_id=owed,
    )


# ── 1: one render per revision and quality ────────────────────────────────


def test_a_relaunch_on_a_revision_in_flight_answers_the_same_render(starting):
    project = _project()
    running = _running(project)
    db = _StartDb(running)
    started = asyncio.run(projects.start_render(db, project, USER, quality="high", owed_to=CONVERSATION))
    assert started.render is running and started.reused and started.superseded == []
    # Nothing cancelled, queued, gated or audited again; the asking chat is told how it ends.
    assert (starting.gated, starting.enqueued, starting.cancelled, starting.audits, db.added) == (0, [], [], [], [])
    assert running.owed_conversation_id == CONVERSATION
    # Read for this revision at this quality, whole renders only.
    asked = db.asked[0].split("WHERE", 1)[1]
    assert "video_renders.scene_id IS NULL" in asked and "video_renders.status IN" in asked


def test_a_render_owed_to_another_chat_stays_owed_to_it(starting):
    project = _project()
    other = uuid.uuid4()
    running = _running(project, owed=other)
    asyncio.run(projects.start_render(_StartDb(running), project, USER, quality="high", owed_to=CONVERSATION))
    assert running.owed_conversation_id == other


def test_a_render_taken_back_from_a_lost_lease_is_the_one_answered(starting):
    """The engine runs it again on its row: pending again, its stage saying why - the same render."""
    project = _project()
    again = _running(project)
    again.status, again.stage = JOB_PENDING, "Starting again: The render process was killed by signal 9"
    started = asyncio.run(projects.start_render(_StartDb(again), project, USER, quality="high"))
    assert started.render.id == again.id and started.reused


def test_a_twin_started_while_the_gate_ran_is_found_under_the_lock(starting):
    project = _project()
    twin = _running(project)
    db = _StartDb(twin, found_on=2)
    started = asyncio.run(projects.start_render(db, project, USER, quality="high"))
    assert started.render is twin and started.reused and starting.gated == 1 and db.added == []
    assert "FOR UPDATE" in [s for s in db.asked if "video_renders.quality = " in s][1]


def test_another_quality_is_a_new_render(starting):
    project = _project()
    db = _StartDb(None)
    started = asyncio.run(projects.start_render(db, project, USER, quality="draft"))
    assert not started.reused and db.added == [started.render] and started.render.quality == "draft"
    assert starting.gated == 1 and starting.audits


def test_a_relaunch_brings_back_the_render_no_worker_resumed_as_the_same_job(starting, monkeypatch):
    """The render a stopped worker left, failed when no worker took it, is queued again - never a new one."""
    project = _project()
    left = _running(project, quality="ultra")
    left.status, left.progress, left.error = JOB_FAILED, 73, "No worker resumed it in 10 min after it went back."
    left.finished_at = left.progress_at = datetime.now(UTC)
    again: list = []

    async def found(_db, _project, *, quality):
        return left if quality == "ultra" else None

    async def enqueue_again(_db, row, **kwargs):
        again.append((row, kwargs))

    monkeypatch.setattr(projects, "render_left_unresumed", found)
    monkeypatch.setattr(jobs, "enqueue_again", enqueue_again)
    db = _StartDb(None)
    started = asyncio.run(projects.start_render(db, project, USER, quality="ultra", owed_to=CONVERSATION))
    assert started.render is left and started.relaunched and not started.reused
    # The same row on its own execution: nothing inserted, nothing queued anew.
    assert db.added == [] and starting.enqueued == [] and again == [(left, {"owed_to": CONVERSATION})]
    assert (left.status, left.error, left.finished_at, left.progress_at) == (JOB_PENDING, None, None, None)
    assert left.stage == "Queued again first: resumes from the segments it kept at 73%"
    # Gated and audited like any start; it reads queued, never running at 73%.
    assert starting.gated == 1 and starting.audits[0]["details"]["resumes_kept_segments_of"] == str(left.id)
    assert jobs.job_state("render", left).status == jobs.STATE_QUEUED
    # Another quality is a new render.
    other = asyncio.run(projects.start_render(_StartDb(None), project, USER, quality="high"))
    assert not other.relaunched and other.render is not left


def _render_video_answer(monkeypatch, started: projects.StartedRender) -> str:
    from app.services.builtin_tools import video_studio as tool
    from app.tools.base import ToolCall

    project = SimpleNamespace(id=started.render.project_id, title="Launch film", revision=7)
    running = started.render

    async def visible(*_args, **_kwargs):
        return project

    async def manages(*_args, **_kwargs):
        return True

    async def start(*_args, **_kwargs):
        return started

    async def waited(_kind, _ids, **_kwargs):
        return [running]

    async def nothing(*_args, **_kwargs):
        return None

    async def states(kind, rows):
        return [jobs.job_state(kind, row) for row in rows]

    class _Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return False

        async def get(self, *_args, **_kwargs):
            return SimpleNamespace(id=USER.id, email=USER.email, role="user")

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
    _defs, executor = tool.build_video_studio_tools(user_id=str(USER.id))
    call = ToolCall(id="r", name="render_video", arguments={"video_id": str(project.id), "quality": "high"})
    answer = asyncio.run(executor.execute_tool(call))
    assert not answer.is_error, answer.content
    return answer.content


def test_render_video_says_it_follows_the_render_in_flight(monkeypatch):
    running = _running(_project())
    said = _render_video_answer(monkeypatch, projects.StartedRender(running, [], reused=True))
    assert f"render `{running.id}`, queued or running, is the one followed" in said
    assert "replaced the render" not in said and "queued again as the same render" not in said


def test_render_video_says_the_render_no_worker_resumed_is_queued_again_as_the_same_render(monkeypatch):
    left = _running(_project())
    said = _render_video_answer(monkeypatch, projects.StartedRender(left, [], relaunched=True))
    assert f"Render `{left.id}` of this revision at high, which no worker resumed after its worker stopped" in said
    assert "queued again as the same render, first in the queue: it resumes from the segments it kept" in said


# ── 4: a preview's queue place ────────────────────────────────────────────


def test_a_queue_place_is_read_on_the_starts_own_session(monkeypatch):
    """Before the start commits: the lane as the start's own session sees it, the executions it flushed
    included, dealt as a free slot would deal it (``preview_lane.place_of``)."""
    from app.services.video_studio import preview_lane

    now = datetime.now(UTC)

    def execution(seconds: int) -> SimpleNamespace:
        at = now + timedelta(seconds=seconds)
        return SimpleNamespace(id=uuid.uuid4(), adapter=jobs.PREVIEW_ADAPTER, adapter_state=None, payload={}, run_at=at, created_at=at)

    first, second, making, own = execution(0), execution(1), execution(-60), execution(2)
    theirs, mine = uuid.uuid4(), uuid.uuid4()
    preview = VideoRender(id=uuid.uuid4(), scene_id="hook", status=JOB_PENDING, execution_id=own.id)
    read_on: list = []

    class _Db:
        async def get(self, _model, key):
            return own if key == own.id else None

        async def execute(self, _statement):
            # The lane's last draw times (``preview_lane.draw_seconds``): none drawn yet.
            return SimpleNamespace(all=lambda: [])

    db = _Db()

    async def read_lane(session):
        read_on.append(session)
        video_of = {first.id: theirs, second.id: theirs, making.id: mine, own.id: mine}
        return preview_lane.Lane([first, second, own], [making], video_of, {})

    async def titles(_db, _videos):
        return {theirs: "Event loop", mine: "Brand film"}

    monkeypatch.setattr(preview_lane, "read_lane", read_lane)
    monkeypatch.setattr(preview_lane, "_titles", titles)
    place = asyncio.run(jobs.queue_place("render", preview, db=db))
    assert read_on == [db]
    # Its video already makes one: the other video's first goes before it, then its turn.
    assert (place.running, place.ahead, place.position, place.lane) == (1, 1, 2, "preview")
    assert [(v["title"], v["previews"], v["being_made"], v["own"]) for v in place.as_dict()["videos_ahead"]] == [
        ("Brand film", 1, 1, True),
        ("Event loop", 1, 0, False),
    ]
    # No preview drawn yet: no wait is estimated, and none is said.
    assert place.as_dict()["expected_wait_seconds"] is None and place.wait_words() == ""


def test_a_preview_start_answers_its_queue_place(monkeypatch):
    place = jobs.QueuePlace(running=1, ahead=0, per_worker=1)
    project = SimpleNamespace(id=uuid.uuid4(), revision=2, title="Brand film", spec={"scenes": [{"id": "hook"}]})

    class _Db:
        async def execute(self, statement, _params=None):
            return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: []))

        def add(self, _row):
            return None

        async def flush(self):
            return None

    async def effective(*_args, **_kwargs):
        return SimpleNamespace(spec={"scenes": [{"id": "hook"}]})

    async def nothing(*_args, **_kwargs):
        return None

    async def placed(*_args, **_kwargs):
        return place

    monkeypatch.setattr(projects, "effective_spec", effective)
    monkeypatch.setattr(jobs, "enqueue", nothing)
    monkeypatch.setattr(jobs, "queue_place", placed)
    monkeypatch.setattr("app.services.audit.log_audit", nothing)
    started = asyncio.run(projects.start_scene_render(_Db(), project, USER, scene="hook"))
    assert started.queue is place and started.queue.position == 1


# ── 8: a scene preview's poster ───────────────────────────────────────────


def test_a_scene_previews_poster_is_its_first_settled_frame(monkeypatch):
    from app.api.v1 import video_studio as api
    from app.api.v1.schemas.video_studio import MediaTokenRequest

    first, middle, last = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    preview = VideoRender(id=uuid.uuid4(), scene_id="hook", file_id=uuid.uuid4(), frame_file_ids=[str(first), str(middle), str(last)])
    poster = uuid.uuid4()
    whole = VideoRender(id=uuid.uuid4(), file_id=uuid.uuid4(), poster_file_id=poster)
    rendered = {"render": preview}

    async def visible(_db, _render_id, _user):
        return rendered["render"], SimpleNamespace(title="Brand film")

    monkeypatch.setattr(api, "_visible_render", visible)
    body = MediaTokenRequest(kind="render", id=str(preview.id), asset="poster")
    assert asyncio.run(api._media_file(None, body, USER))[0] == first
    rendered["render"] = whole
    assert asyncio.run(api._media_file(None, body, USER))[0] == poster
