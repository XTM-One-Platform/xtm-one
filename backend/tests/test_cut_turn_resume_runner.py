"""The resume runner of a turn cut by a platform stop.

``parent_wake.resume_cut_turn`` continues the SAME stored user message: no new
user message is stored, the history ends at that message, the cut turn's
checkpoint is replayed after it, the viewers are told the resume started and
the reply links back to the cut turn. A conversation whose person wrote or
stopped since the cut, or whose message is gone, is not resumed. A resume cut
again by the next platform stop is queued once more.
"""

from __future__ import annotations

import asyncio
import uuid
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from app.services import cut_turn_resume, parent_wake, turn_checkpoint
from app.services.cut_turn_resume import RESUMED_FROM_META_KEY, RESUMING_FRAME_TYPE, CutTurn

CONV = uuid.UUID("11111111-1111-1111-1111-111111111111")
USER = uuid.UUID("22222222-2222-2222-2222-222222222222")
MSG = uuid.UUID("33333333-3333-3333-3333-333333333333")


class _Result:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


class _Session:
    """Answers the runner's three reads in order: conversation, message, newer user message."""

    def __init__(self, answers, statements=None):
        self._answers = answers
        self._statements = statements

    async def execute(self, stmt):
        if self._statements is not None:
            self._statements.append(stmt)
        return _Result(self._answers.pop(0))

    async def commit(self):
        return None


def _conv():
    return SimpleNamespace(
        id=CONV,
        user_id=USER,
        agent_id=None,
        channel="web",
        channel_thread_id=None,
        tool_overrides_json=None,
    )


def _msg(role: str = "user"):
    return SimpleNamespace(id=MSG, role=role, content="research the quarterly risks", sequence=7)


def _cut(resumes: int = 0) -> CutTurn:
    return CutTurn(str(CONV), str(USER), str(MSG), "turn-1", "stop", resumes, "2026-11-01T01:00:00+00:00", "web")


@pytest.fixture
def env(monkeypatch):
    state = SimpleNamespace(
        answers=[_conv(), _msg(), None],
        statements=[],
        stopped=False,
        runs=[],
        refused=False,
        replay=["r"],
        unanswered=None,
    )

    @asynccontextmanager
    async def _session():
        yield _Session(state.answers, state.statements)

    import app.core.database as database

    monkeypatch.setattr(database, "async_session", _session)

    async def _stopped(db, conversation_id, since, **kw):
        return state.stopped

    monkeypatch.setattr(parent_wake, "_stopped_since", _stopped)

    async def _unanswered(db, conversation_id, after_sequence):
        return state.unanswered

    monkeypatch.setattr(parent_wake, "_left_unanswered_since_cut", _unanswered)

    async def _load_replay(db, conversation_id, *, user_id=None, now=None, code_interpreter=False):
        return turn_checkpoint.ResumedWork(found=bool(state.replay)), list(state.replay)

    monkeypatch.setattr(turn_checkpoint, "load_replay", _load_replay)

    async def _run(db, **kw):
        state.runs.append(kw)
        return state.refused

    monkeypatch.setattr(parent_wake, "_run_parent_wake_turn", _run)
    return state


async def test_a_cut_turn_continues_the_same_user_message(env):
    assert await parent_wake.resume_cut_turn(_cut(resumes=1)) is True

    [run] = env.runs
    assert run["wake_text"] == "research the quarterly risks"
    # No new user message: the stored one is continued (invariant 16).
    assert run["persist_user"] is False
    assert run["wake_from_platform"] is False
    assert run["history_before_sequence"] == 8
    assert run["replay"] == ["r"]
    assert run["user_id"] == str(USER)
    assert run["start_frame"]["type"] == RESUMING_FRAME_TYPE
    assert run["start_frame"]["resume"] == 2
    link = run["reply_meta_extra"][RESUMED_FROM_META_KEY]
    assert link["turn_id"] == "turn-1" and link["cause"] == "stop" and link["resume"] == 2
    assert callable(run["on_checkpoint"])


@pytest.mark.parametrize(
    "answers",
    [
        [None, _msg(), None],
        [_conv(), None, None],
        [_conv(), _msg(role="assistant"), None],
    ],
    ids=["conversation-gone", "message-gone", "not-a-user-message"],
)
async def test_a_cut_turn_whose_message_is_gone_is_not_resumed(env, answers):
    env.answers[:] = answers

    assert await parent_wake.resume_cut_turn(_cut()) is False
    assert env.runs == []


async def test_a_person_who_wrote_since_the_cut_is_not_answered_twice(env):
    env.answers[2:] = [uuid.uuid4(), None]

    assert await parent_wake.resume_cut_turn(_cut()) is False
    assert env.runs == []


def _newer_message_query(env) -> str:
    from sqlalchemy.dialects import postgresql

    return str(env.statements[2].compile(dialect=postgresql.dialect()))


async def test_any_visible_user_message_after_the_cut_turn_s_still_blocks_its_resume(env):
    # The cut record's time is set after the cut, so
    # it cannot tell a steer taken in before the cut from one written after
    # it. Every visible user message later in the conversation still counts.
    assert await parent_wake.resume_cut_turn(_cut()) is True

    query = _newer_message_query(env)
    assert "messages.sequence >" in query
    assert "messages.created_at" not in query


async def test_a_hidden_wake_after_the_cut_is_not_the_person_writing(env):
    # The background task the cut turn delegated is resumed on its own and
    # wakes the conversation with a hidden platform message, often before the
    # sweep takes the cut turn: that wake is not the person moving on.
    from sqlalchemy.dialects import postgresql

    assert await parent_wake.resume_cut_turn(_cut()) is True

    compiled = env.statements[2].compile(dialect=postgresql.dialect())
    assert "messages.metadata_json IS NULL" in str(compiled)
    assert any(
        isinstance(value, str) and '"hidden": true' in value for value in compiled.params.values()
    ), "a hidden message does not count"
    import json

    stored = json.dumps({"hidden": True, "source": parent_wake.PARENT_WAKE_SOURCE})
    assert parent_wake._HIDDEN_MESSAGE_MARK in stored, "the mark matches how a wake is stored"


def _row(role: str, sequence: int, *, content: str = "", meta: str | None = None):
    return SimpleNamespace(id=uuid.uuid4(), role=role, content=content, sequence=sequence, metadata_json=meta)


async def test_a_message_steered_into_the_cut_turn_is_answered_by_its_resume(env):
    # The person steered a message into the turn before the platform
    # stop cut it; the resume answers it, on the latest, instead of standing
    # down as if the person had written after the cut.
    steer = _row("user", 9, content="and the supply chain ones")
    env.answers[:] = [
        _conv(),
        _msg(),
        steer.id,
        _row("assistant", 10, meta='{"interrupted": true, "auto_resume": true}'),
        steer,
    ]

    assert await parent_wake.resume_cut_turn(_cut()) is True

    [run] = env.runs
    assert run["wake_text"] == "and the supply chain ones"
    assert run["history_before_sequence"] == 10
    assert run["persist_user"] is False


@pytest.mark.parametrize(
    ("reply", "latest"),
    [
        (None, None),
        (_row("assistant", 10, meta='{"answer": true}'), None),
        (_row("assistant", 10, meta='{"interrupted": true}'), _row("user", 11, content="after the cut")),
    ],
    ids=["no-reply-stored", "a-finished-reply", "written-after-the-cut"],
)
async def test_a_message_written_after_the_cut_still_stands_the_resume_down(env, reply, latest):
    env.answers[:] = [_conv(), _msg(), uuid.uuid4(), reply, latest]

    assert await parent_wake.resume_cut_turn(_cut()) is False
    assert env.runs == []


async def test_a_message_folded_into_the_cut_turn_after_the_cut_is_answered_by_its_resume(env):
    # Sent between the platform stop and the sweep, the message found the cut
    # turn's lock still held and was folded into a turn that no longer runs:
    # no turn of its own started, so the resume answers it instead of
    # standing down and leaving it unanswered (the stale Waiting card).
    late = _row("user", 11, content="what is missing?")
    # The steer check finds the interrupted reply but no user message before it.
    env.answers[:] = [_conv(), _msg(), late.id, _row("assistant", 10, meta='{"interrupted": true}'), None]
    env.unanswered = late

    assert await parent_wake.resume_cut_turn(_cut()) is True

    [run] = env.runs
    assert run["wake_text"] == "what is missing?"
    assert run["history_before_sequence"] == 12
    assert run["persist_user"] is False


class _Rows:
    """Answers the helper's reads in order."""

    def __init__(self, answers):
        self._answers = answers

    async def execute(self, stmt):
        return _Result(self._answers.pop(0))


@pytest.mark.parametrize(
    ("answers", "active", "expected"),
    [
        ([None], False, False),
        (["latest", uuid.uuid4()], False, False),
        (["latest", None], True, False),
        (["latest", None], False, True),
    ],
    ids=["no-message", "already-answered", "a-turn-runs", "left-unanswered"],
)
async def test_a_message_left_unanswered_since_the_cut_is_the_resume_s(monkeypatch, answers, active, expected):
    from app.services import chat_turn_guard

    latest = _row("user", 11, content="still there?")
    rows = [latest if a == "latest" else a for a in answers]

    async def _active(conversation_id):
        return active

    monkeypatch.setattr(chat_turn_guard, "is_turn_active", _active)

    found = await parent_wake._left_unanswered_since_cut(_Rows(rows), CONV, 7)

    assert (found is latest) is expected


async def test_a_resume_refused_for_a_newer_message_names_it(env, caplog):
    import logging

    newer = uuid.uuid4()
    env.answers[2:] = [newer, None]

    with caplog.at_level(logging.WARNING):
        assert await parent_wake.resume_cut_turn(_cut()) is False
    assert any(
        str(newer) in r.getMessage() and "turn cut at 2026-11-01T01:00:00+00:00" in r.getMessage()
        for r in caplog.records
    )


async def test_a_person_who_stopped_since_the_cut_is_not_resumed(env):
    env.stopped = True

    assert await parent_wake.resume_cut_turn(_cut()) is False
    assert env.runs == []


async def test_a_record_with_no_message_is_not_resumed(env):
    cut = CutTurn(str(CONV), str(USER), None, "turn-1", "stop", 0, "", "web")

    assert await parent_wake.resume_cut_turn(cut) is False
    assert env.runs == []


async def test_a_resume_refused_by_the_agentic_gate_keeps_the_one_click(env):
    env.refused = True

    assert await parent_wake.resume_cut_turn(_cut()) is False
    assert len(env.runs) == 1


async def test_a_resume_cut_again_by_a_platform_stop_is_queued_once_more(env, monkeypatch):
    from app.core import worker_shutdown
    from app.core.shutdown_finalizers import BACKGROUND_TURN_CANCEL_MESSAGE, drain_shutdown_finalizers

    queued: list[dict] = []

    async def _enqueue(conversation_id, **kw):
        queued.append({"conversation_id": conversation_id, **kw})
        return True

    monkeypatch.setattr(cut_turn_resume, "enqueue_cut_turn", _enqueue)
    monkeypatch.setattr(worker_shutdown, "_request", None)
    worker_shutdown.note_shutdown(worker_shutdown.STOP, "SIGTERM")

    async def _run(db, **kw):
        raise asyncio.CancelledError(BACKGROUND_TURN_CANCEL_MESSAGE)

    monkeypatch.setattr(parent_wake, "_run_parent_wake_turn", _run)

    with pytest.raises(asyncio.CancelledError):
        await parent_wake.resume_cut_turn(_cut(resumes=1))
    await drain_shutdown_finalizers(timeout=5)

    [call] = queued
    assert call["user_message_id"] == str(MSG)
    assert call["resumes_made"] == 2
    # It completed no tool round of its own: one more stalled resume.
    assert call["stalled"] == 2
    assert call["cause"] == worker_shutdown.STOP
    # Cut before it took the conversation: no lock of its own to name.
    assert call["lock_token"] is None


async def test_a_resume_cut_again_is_queued_once_by_its_own_teardown_with_its_lock(env, monkeypatch):
    """Queued while it still holds the conversation, never a second time after."""
    from app.core import worker_shutdown
    from app.core.shutdown_finalizers import drain_shutdown_finalizers
    from app.core.worker_shutdown import WIND_DOWN_CANCEL_MESSAGE, TurnWoundDown

    queued: list[dict] = []

    async def _enqueue(conversation_id, **kw):
        queued.append({"conversation_id": conversation_id, **kw})
        return True

    monkeypatch.setattr(cut_turn_resume, "enqueue_cut_turn", _enqueue)
    monkeypatch.setattr(worker_shutdown, "_request", None)
    worker_shutdown.note_shutdown(worker_shutdown.STOP, "SIGTERM")

    async def _run(db, *, on_cut, **kw):
        # The turn's teardown, before its interrupted close and its release.
        await on_cut("lock-token-1")
        await on_cut("lock-token-1")
        raise TurnWoundDown(WIND_DOWN_CANCEL_MESSAGE)

    monkeypatch.setattr(parent_wake, "_run_parent_wake_turn", _run)

    with pytest.raises(asyncio.CancelledError):
        await parent_wake.resume_cut_turn(_cut(resumes=1))
    await drain_shutdown_finalizers(timeout=5)

    [call] = queued
    assert call["lock_token"] == "lock-token-1"
    assert call["resumes_made"] == 2
    assert call["user_message_id"] == str(MSG)


async def test_a_resume_cut_after_progress_starts_its_stalled_count_over(env, monkeypatch):
    """A long turn that completes rounds between rollouts keeps resuming by itself."""
    from app.core import worker_shutdown
    from app.core.shutdown_finalizers import BACKGROUND_TURN_CANCEL_MESSAGE, drain_shutdown_finalizers
    from app.llm.base import ChatMessage
    from app.tools.base import ToolCall

    queued: list[dict] = []

    async def _enqueue(conversation_id, **kw):
        queued.append({"conversation_id": conversation_id, **kw})
        return True

    monkeypatch.setattr(cut_turn_resume, "enqueue_cut_turn", _enqueue)
    monkeypatch.setattr(worker_shutdown, "_request", None)
    worker_shutdown.note_shutdown(worker_shutdown.STOP, "SIGTERM")

    async def _flush(self, *, flush):
        self._closed = True

    monkeypatch.setattr(turn_checkpoint.TurnCheckpoint, "aclose", _flush)
    monkeypatch.setattr(turn_checkpoint.TurnCheckpoint, "_start_writer", lambda self: None)

    async def _run(db, *, on_checkpoint, **kw):
        on_checkpoint(
            [
                ChatMessage(role="assistant", content="", tool_calls=[ToolCall(id="c1", name="read_file", arguments={})]),
                ChatMessage(role="tool", content="ok", tool_call_id="c1"),
            ]
        )
        raise asyncio.CancelledError(BACKGROUND_TURN_CANCEL_MESSAGE)

    monkeypatch.setattr(parent_wake, "_run_parent_wake_turn", _run)

    with pytest.raises(asyncio.CancelledError):
        await parent_wake.resume_cut_turn(_cut(resumes=5))
    await drain_shutdown_finalizers(timeout=5)

    [call] = queued
    assert call["resumes_made"] == 6
    assert call["stalled"] == 0


async def test_the_sweep_hands_the_runner_on_detached(monkeypatch):
    started: list[CutTurn] = []

    async def _resume(cut, **kw):
        started.append(cut)
        return True

    monkeypatch.setattr(parent_wake, "resume_cut_turn", _resume)

    await parent_wake.schedule_cut_turn_resume(_cut())
    await asyncio.sleep(0)
    await asyncio.gather(*parent_wake._PARENT_WAKE_TASKS)

    assert [c.turn_id for c in started] == ["turn-1"]


async def _cut_again(monkeypatch, cut: CutTurn, *, opened: uuid.UUID | None) -> dict:
    """Cut *cut*'s resume by the drain, after it opened trace *opened* (or none); the record queued."""
    from app.core import worker_shutdown
    from app.core.shutdown_finalizers import BACKGROUND_TURN_CANCEL_MESSAGE, drain_shutdown_finalizers
    from app.services import langfuse_tracing

    queued: list[dict] = []

    async def _enqueue(conversation_id, **kw):
        queued.append({"conversation_id": conversation_id, **kw})
        return True

    monkeypatch.setattr(cut_turn_resume, "enqueue_cut_turn", _enqueue)
    monkeypatch.setattr(worker_shutdown, "_request", None)
    worker_shutdown.note_shutdown(worker_shutdown.STOP, "SIGTERM")

    async def _run(db, **kw):
        link = langfuse_tracing._next_root_trace.get()
        if opened is not None and link is not None:
            link.trace_id = opened
        raise asyncio.CancelledError(BACKGROUND_TURN_CANCEL_MESSAGE)

    monkeypatch.setattr(parent_wake, "_run_parent_wake_turn", _run)

    with pytest.raises(asyncio.CancelledError):
        await parent_wake.resume_cut_turn(cut)
    await drain_shutdown_finalizers(timeout=5)
    [call] = queued
    return call


async def test_a_resume_cut_again_names_its_own_trace_for_the_next_hop(env, monkeypatch):
    """The next resume links to this hop: before, it queried the latest trace
    before the cut and, with this hop not flushed yet, linked the hop before."""
    hop = uuid.uuid4()
    cut = CutTurn(
        str(CONV), str(USER), str(MSG), "turn-1", "stop", 1, "2026-11-01T01:00:00+00:00", "web", trace_id="t-hop-1"
    )

    call = await _cut_again(monkeypatch, cut, opened=hop)

    assert call["trace_id"] == str(hop)


async def test_a_resume_cut_before_its_trace_opened_keeps_the_trace_it_resumes(env, monkeypatch):
    cut = CutTurn(
        str(CONV), str(USER), str(MSG), "turn-1", "stop", 1, "2026-11-01T01:00:00+00:00", "web", trace_id="t-hop-1"
    )

    call = await _cut_again(monkeypatch, cut, opened=None)

    assert call["trace_id"] == "t-hop-1"


async def test_the_resume_links_the_trace_its_record_names_without_querying(env, monkeypatch):
    from app.services import langfuse_tracing

    async def _query(db, cut, **kw):
        raise AssertionError("the record names the cut trace")

    monkeypatch.setattr(parent_wake, "cut_turn_trace_id", _query, raising=False)
    monkeypatch.setattr(cut_turn_resume, "cut_turn_trace_id", _query)
    seen: list[dict] = []

    async def _run(db, **kw):
        link = langfuse_tracing._next_root_trace.get()
        seen.append(dict(link.metadata))
        return False

    monkeypatch.setattr(parent_wake, "_run_parent_wake_turn", _run)
    cut = CutTurn(
        str(CONV), str(USER), str(MSG), "turn-1", "stop", 2, "2026-11-01T01:00:00+00:00", "web", trace_id="t-hop-2"
    )

    assert await parent_wake.resume_cut_turn(cut) is True

    [meta] = seen
    assert meta[RESUMED_FROM_META_KEY]["trace_id"] == "t-hop-2"


async def test_a_resumed_hop_is_queued_by_the_drain_before_it_is_cancelled(env, monkeypatch):
    """xtm-one #7132 web-miss-6: a hop the drain cancels is queued first.

    Before, its next resume was queued only at the end of its unwinding: a
    hop whose unwinding outlived the drain (or the pod's kill) lost it, and
    the person had to click Resume.
    """
    from app.core import shutdown_finalizers, worker_shutdown
    from app.services import langfuse_tracing

    monkeypatch.setattr(shutdown_finalizers, "_background_turns", set())
    monkeypatch.setattr(shutdown_finalizers, "_before_cancel", {})
    queued: list[dict] = []

    async def _enqueue(conversation_id, **kw):
        queued.append({"conversation_id": conversation_id, **kw})
        return True

    monkeypatch.setattr(cut_turn_resume, "enqueue_cut_turn", _enqueue)
    monkeypatch.setattr(worker_shutdown, "_request", None)
    hop = uuid.uuid4()
    running = asyncio.Event()

    async def _run(db, **kw):
        langfuse_tracing._next_root_trace.get().trace_id = hop
        running.set()
        # Still running when the drain ends it: no teardown has queued it.
        await asyncio.sleep(3600)

    monkeypatch.setattr(parent_wake, "_run_parent_wake_turn", _run)
    cut = CutTurn(
        str(CONV), str(USER), str(MSG), "turn-1", "stop", 1, "2026-11-01T01:00:00+00:00", "web", trace_id="t-hop-1"
    )
    task = asyncio.create_task(parent_wake.resume_cut_turn(cut))
    shutdown_finalizers.track_background_turn(task)
    await running.wait()
    worker_shutdown.note_shutdown(worker_shutdown.STOP, "SIGTERM")

    await shutdown_finalizers._end_background_turns(None, None)

    # Queued before the cancellation reached the hop, naming the hop's trace.
    assert queued and queued[0]["resumes_made"] == 2
    assert queued[0]["trace_id"] == str(hop)
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_a_hop_that_finishes_after_the_hand_over_forgets_its_queued_resume(env, monkeypatch):
    from app.core import shutdown_finalizers

    forgotten: list[str] = []

    async def _forget(conversation_id, *, user_message_id):
        forgotten.append(str(user_message_id))
        return True

    async def _enqueue(conversation_id, **kw):
        return True

    monkeypatch.setattr(cut_turn_resume, "forget_cut_turn", _forget)
    monkeypatch.setattr(cut_turn_resume, "enqueue_cut_turn", _enqueue)
    monkeypatch.setattr(shutdown_finalizers, "_before_cancel", {})

    async def _run(db, **kw):
        # The drain hands it over, then the hop answers before it is cancelled.
        task = asyncio.current_task()
        await shutdown_finalizers._hand_over_before_cancel({task})
        return False

    monkeypatch.setattr(parent_wake, "_run_parent_wake_turn", _run)

    assert await parent_wake.resume_cut_turn(_cut(resumes=1)) is True
    assert forgotten == [str(MSG)]


async def test_a_resume_cut_before_its_turn_started_is_put_back_as_taken(monkeypatch):
    """The take removed the record: a drain cancellation while the resume still
    read the conversation lost it, and the person had to click Resume."""
    from app.core.shutdown_finalizers import BACKGROUND_TURN_CANCEL_MESSAGE

    put_back: list[CutTurn] = []

    async def _put_back(cut):
        put_back.append(cut)
        return True

    async def _settled(cid):
        return None

    async def _cut_while_reading(cut, *, on_start=None):
        raise asyncio.CancelledError(BACKGROUND_TURN_CANCEL_MESSAGE)

    monkeypatch.setattr(cut_turn_resume, "put_back_cut_turn", _put_back)
    monkeypatch.setattr(cut_turn_resume, "resume_settled", _settled)
    monkeypatch.setattr(parent_wake, "resume_cut_turn", _cut_while_reading)

    with pytest.raises(asyncio.CancelledError):
        await parent_wake._resume_cut_turn_settled(_cut(resumes=1))

    assert [(c.turn_id, c.resumes) for c in put_back] == [("turn-1", 1)]


async def test_a_resume_cut_after_its_turn_started_is_not_put_back(monkeypatch):
    """Once started, the turn's own cut handling queues it (with its progress)."""
    from app.core.shutdown_finalizers import BACKGROUND_TURN_CANCEL_MESSAGE

    put_back: list[CutTurn] = []

    async def _put_back(cut):
        put_back.append(cut)
        return True

    async def _settled(cid):
        return None

    async def _cut_in_its_turn(cut, *, on_start=None):
        on_start()
        raise asyncio.CancelledError(BACKGROUND_TURN_CANCEL_MESSAGE)

    monkeypatch.setattr(cut_turn_resume, "put_back_cut_turn", _put_back)
    monkeypatch.setattr(cut_turn_resume, "resume_settled", _settled)
    monkeypatch.setattr(parent_wake, "resume_cut_turn", _cut_in_its_turn)

    with pytest.raises(asyncio.CancelledError):
        await parent_wake._resume_cut_turn_settled(_cut())

    assert put_back == []


async def test_a_resume_cancelled_by_something_else_is_not_put_back(monkeypatch):
    put_back: list[CutTurn] = []

    async def _put_back(cut):
        put_back.append(cut)
        return True

    async def _settled(cid):
        return None

    async def _cancelled(cut, *, on_start=None):
        raise asyncio.CancelledError()

    monkeypatch.setattr(cut_turn_resume, "put_back_cut_turn", _put_back)
    monkeypatch.setattr(cut_turn_resume, "resume_settled", _settled)
    monkeypatch.setattr(parent_wake, "resume_cut_turn", _cancelled)

    with pytest.raises(asyncio.CancelledError):
        await parent_wake._resume_cut_turn_settled(_cut())

    assert put_back == []
