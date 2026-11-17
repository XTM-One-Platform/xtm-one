"""A Video Studio job resumes the conversation that started it once it ends.

A chat turn never holds a job: ``render_video`` waits for its render only
until its own deadline, then the agent polls ``get_video_jobs``. A turn that
ended first left a render that finished 10 minutes later with nobody to say
so. These tests pin the wake (``services/video_studio/wake.py``) without a
database: who is owed what is ``jobs``' business, proven against PostgreSQL
in ``test_video_studio_job_wake_postgres.py``.

* a job a chat turn starts is owed to its conversation (``jobs.enqueue``);
  one started anywhere else, or cancelled, to nobody;
* the worker, done with a job, waits the grace, then resumes the conversation
  with the outcome of every owed job that ended - once, by the last of a
  batch - as ``get_video_jobs`` words it, through the task wake's turn;
* nothing is woken for a job still running, owed to nobody, or already told;
* the tools say the conversation is resumed only where it is.
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from app.models.orchestration import OrchestrationExecution
from app.models.video_studio import JOB_CANCELLED, JOB_FAILED, JOB_PENDING, JOB_READY, JOB_RUNNING, VideoRender
from app.services import parent_wake
from app.services.builtin_tools import video_studio as tool
from app.services.orchestration_adapters import video_studio as adapters
from app.services.video_studio import jobs, wake
from app.tools.base import ToolCall, ToolResult

CONVERSATION = uuid.uuid4()
USER = uuid.uuid4()


def _render(status: str, *, owed: uuid.UUID | None = CONVERSATION) -> VideoRender:
    return VideoRender(
        id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        user_id=USER,
        revision=1,
        quality="high",
        status=status,
        owed_conversation_id=owed,
        created_at=datetime.now(UTC),
    )


# ── Who a job is owed to ──────────────────────────────────────────────────


def test_a_job_is_owed_to_the_conversation_that_starts_it(monkeypatch):
    async def create_execution(_db, **_kwargs):
        return SimpleNamespace(id=uuid.uuid4())

    monkeypatch.setattr("app.services.orchestration_engine.create_execution", create_execution)
    render = _render(JOB_PENDING, owed=uuid.uuid4())

    asyncio.run(jobs.enqueue(object(), "render", render, owed_to=CONVERSATION))
    assert render.owed_conversation_id == CONVERSATION

    # Started again from the editor: the earlier conversation is owed nothing.
    asyncio.run(jobs.enqueue(object(), "render", render, generation=1))
    assert render.owed_conversation_id is None


def test_a_cancelled_job_is_owed_to_nobody(monkeypatch):
    async def cancel_execution(_db, **_kwargs):
        return None

    monkeypatch.setattr("app.services.orchestration_engine.cancel_execution", cancel_execution)
    render = _render(JOB_RUNNING)
    render.execution_id = uuid.uuid4()

    assert asyncio.run(jobs.cancel(object(), "render", render)) is True
    assert (render.status, render.owed_conversation_id) == (JOB_CANCELLED, None)


def test_a_report_to_no_conversation_marks_nothing(monkeypatch):
    def refuse():
        raise AssertionError("no session is opened")

    monkeypatch.setattr(jobs, "async_session", refuse)
    ended = _render(JOB_READY)
    asyncio.run(jobs.mark_told("render", [ended], None))
    # A job another conversation is owed, read here, stays owed to it.
    asyncio.run(jobs.mark_told("render", [ended], uuid.uuid4()))
    assert ended.owed_conversation_id == CONVERSATION
    # A job still running is not told yet.
    running = _render(JOB_RUNNING)
    asyncio.run(jobs.mark_told("render", [running], CONVERSATION))
    assert running.owed_conversation_id == CONVERSATION


def test_count_phrase():
    assert jobs.count_phrase({"render": 1}) == "1 render"
    assert jobs.count_phrase({"render": 2, "clip": 1}) == "2 renders and 1 AI shot"
    assert jobs.count_phrase({"take": 1, "clip": 3, "music": 1}) == "1 recording, 3 AI shots and 1 AI track"
    assert jobs.count_phrase({}) == ""


# ── The wake ──────────────────────────────────────────────────────────────


@pytest.fixture
def owed(monkeypatch):
    """The rows ``jobs`` would read and claim, the report ``get_video_jobs`` would give, the wakes run."""
    world = SimpleNamespace(rows={}, in_flight=False, claimed={}, claims=0, wakes=[], reports=[])

    async def load(_kind, job_id):
        return world.rows.get(str(job_id))

    async def in_flight(conversation_id):
        assert conversation_id == CONVERSATION
        return world.in_flight

    async def claim(conversation_id):
        assert conversation_id == CONVERSATION
        world.claims += 1
        taken, world.claimed = world.claimed, {}
        return taken

    async def outcome(claimed, *, user_id, conversation_id):
        world.reports.append((claimed, user_id, conversation_id))
        return "Render `r1` is ready. Include this marker in your reply: [[VIDEO:v1]]", ["poster"]

    async def wake_conversation(**kwargs):
        world.wakes.append(kwargs)

    monkeypatch.setattr(jobs, "load", load)
    monkeypatch.setattr(jobs, "owed_jobs_in_flight", in_flight)
    monkeypatch.setattr(jobs, "claim_owed_jobs", claim)
    monkeypatch.setattr(wake, "_outcome", outcome)
    monkeypatch.setattr(parent_wake, "wake_conversation", wake_conversation)
    return world


def test_an_ended_job_resumes_its_conversation_with_the_outcome(owed):
    render = _render(JOB_READY)
    first = datetime.now(UTC) - timedelta(minutes=14)
    owed.rows[str(render.id)] = render
    owed.claimed = {"render": [(str(render.id), first)], "clip": [(str(uuid.uuid4()), first + timedelta(minutes=1))]}

    assert asyncio.run(wake.wake_owed_conversation("render", str(render.id))) is True

    [woken] = owed.wakes
    assert woken["conversation_id"] == str(CONVERSATION)
    assert woken["user_id"] == str(USER)
    assert woken["label"] == wake.WAKE_LABEL
    assert woken["result_files"] == ["poster"]
    # A Stop recorded after the first of them was started keeps it idle.
    assert woken["delegated_at"] == first
    text = woken["wake_text"]
    assert text.startswith("[Platform] The Video Studio work you started in this conversation ended")
    assert "1 render and 1 AI shot" in text
    assert "[[VIDEO:v1]]" in text
    [(claimed, user_id, conversation_id)] = owed.reports
    assert (user_id, conversation_id) == (str(USER), CONVERSATION)
    assert set(claimed) == {"render", "clip"}


@pytest.mark.parametrize(
    ("status", "owed_to", "in_flight"),
    [
        (JOB_RUNNING, CONVERSATION, False),  # not ended: a retry, or the run is not done
        (JOB_PENDING, CONVERSATION, False),
        (JOB_CANCELLED, CONVERSATION, False),  # nobody's news
        (JOB_READY, None, False),  # started from the editor, or already told
        (JOB_READY, CONVERSATION, True),  # the last job of the batch wakes it
        (JOB_FAILED, CONVERSATION, True),
    ],
)
def test_nothing_is_woken_before_its_time(owed, status, owed_to, in_flight):
    render = _render(status, owed=owed_to)
    owed.rows[str(render.id)] = render
    owed.in_flight = in_flight
    owed.claimed = {"render": [(str(render.id), datetime.now(UTC))]}

    assert asyncio.run(wake.wake_owed_conversation("render", str(render.id))) is False
    assert owed.wakes == [] and owed.claims == 0


def test_a_job_already_told_or_claimed_wakes_nothing(owed):
    """The turn that waited reported it, or a sibling's wake claimed it first."""
    render = _render(JOB_READY)
    owed.rows[str(render.id)] = render
    owed.claimed = {}

    assert asyncio.run(wake.wake_owed_conversation("render", str(render.id))) is False
    assert owed.claims == 1 and owed.wakes == []


def test_the_worker_schedules_the_wake_after_the_grace(monkeypatch):
    seen: list[tuple[str, str]] = []

    async def wake_owed(kind, job_id):
        seen.append((kind, job_id))
        return True

    monkeypatch.setattr(wake, "wake_owed_conversation", wake_owed)
    monkeypatch.setattr(wake, "WAKE_GRACE_SECONDS", 0.05)

    async def scenario():
        wake.schedule("render", "r-1")
        assert seen == []  # a turn still waiting reports first
        await asyncio.sleep(0.3)

    asyncio.run(scenario())
    assert seen == [("render", "r-1")]


def test_a_failed_wake_is_logged_never_raised(monkeypatch, caplog):
    async def broken(_kind, _job_id):
        raise RuntimeError("database gone")

    monkeypatch.setattr(wake, "wake_owed_conversation", broken)
    asyncio.run(wake._wake_after("clip", "c-1", 0))
    assert "Could not resume the conversation of Video Studio clip c-1" in caplog.text


def test_scheduling_outside_an_event_loop_is_a_no_op():
    wake.schedule("render", "r-1")


@pytest.mark.parametrize("kind", ["take", "clip", "music"])
def test_every_adapter_schedules_the_wake_when_its_run_returns(monkeypatch, kind):
    scheduled: list[tuple[str, str]] = []
    monkeypatch.setattr(wake, "schedule", lambda k, job_id: scheduled.append((k, str(job_id))))
    adapter = {
        "take": adapters.VideoTakeAdapter,
        "clip": adapters.VideoClipAdapter,
        "music": adapters.VideoMusicAdapter,
    }[kind]()

    async def run(_job_id, _cancel_event, **_slot):
        return SimpleNamespace(kind="done", error=None)

    monkeypatch.setattr(adapter, "_run", run)

    class _Db:
        async def commit(self):
            return None

    ctx = SimpleNamespace(db=_Db(), cancel_event=None, worker_id="w1")
    execution = OrchestrationExecution(payload={f"{kind}_id": "job-1"})
    asyncio.run(adapter.execute(ctx, execution))
    assert scheduled == [(kind, "job-1")]


def test_the_dead_letter_schedules_the_wake(monkeypatch):
    scheduled: list[tuple[str, str]] = []
    marked: list[tuple] = []
    monkeypatch.setattr(wake, "schedule", lambda k, job_id: scheduled.append((k, str(job_id))))

    async def dead_letter(kind, job_id, error, *, lease=None):
        marked.append((kind, job_id, error))

    monkeypatch.setattr("app.services.video_studio.runners.dead_letter", dead_letter)
    monkeypatch.setattr("app.services.orchestration_engine.lease_expiry", lambda _execution: None)

    class _Db:
        async def commit(self):
            return None

    execution = OrchestrationExecution(payload={"render_id": "r-9"}, last_error="Deadline exceeded")
    asyncio.run(adapters.VideoRenderAdapter().on_dead_letter(SimpleNamespace(db=_Db()), execution))
    assert marked == [("render", "r-9", "Deadline exceeded")]
    assert scheduled == [("render", "r-9")]


# ── What the tools say ────────────────────────────────────────────────────


def test_the_resumed_note_is_said_only_in_a_conversation(monkeypatch):
    """A render still running says the conversation is resumed - in a chat, not in an assignment."""
    running = _render(JOB_RUNNING)
    running.stage, running.progress, running.progress_at = "Drawing frames", 40, datetime.now(UTC)
    told: list = []

    async def wait_for_jobs(groups, *, timeout, told_to=None):
        told.append(told_to)
        return {"render": [running]}, jobs.WAIT_BUDGET

    @contextlib.asynccontextmanager
    async def session():
        yield None

    async def find_user(_db, _user_id):
        return SimpleNamespace(id=USER)

    async def visible(_db, _video_id, _user):
        return SimpleNamespace(id=running.project_id)

    monkeypatch.setattr(jobs, "wait_for_jobs", wait_for_jobs)
    monkeypatch.setattr(tool, "async_session", session)
    monkeypatch.setattr(tool, "_user", find_user)
    monkeypatch.setattr(tool, "_wait_budget", lambda _default: 0.0)
    monkeypatch.setattr("app.services.video_studio.projects.load_visible_project", visible)

    async def answer(conversation_id: str | None) -> str:
        _defs, executor = tool.build_video_studio_tools(user_id=str(USER), conversation_id=conversation_id)
        result = await executor.execute_tool(
            ToolCall(id="c1", name="get_video_jobs", arguments={"render_ids": [str(running.id)]})
        )
        return result.content

    note = "If your turn ends first, this conversation is resumed with the outcome when the work ends."
    assert note in asyncio.run(answer(str(CONVERSATION)))
    assert note not in asyncio.run(answer(None))
    # The visibility read tells nobody; the wait that reports the jobs tells the conversation, if any.
    assert told == [None, CONVERSATION, None, None]


def test_the_wake_reports_through_get_video_jobs(monkeypatch):
    """The outcome is worded by the tool the agent polls with, files included."""
    calls: list = []

    class _Executor:
        async def execute_tool(self, call):
            calls.append(call)
            return ToolResult(tool_call_id=call.id, content="Render `r1` is ready.", files=["poster"])

    monkeypatch.setattr(tool, "build_video_studio_tools", lambda **kwargs: ([], _Executor()))
    claimed = {"render": [("r1", datetime.now(UTC))], "take": [("t1", datetime.now(UTC))]}

    text, files = asyncio.run(wake._outcome(claimed, user_id=str(USER), conversation_id=CONVERSATION))

    assert (text, files) == ("Render `r1` is ready.", ["poster"])
    [call] = calls
    assert call.name == "get_video_jobs"
    assert call.arguments == {"render_ids": ["r1"], "recording_ids": ["t1"]}
