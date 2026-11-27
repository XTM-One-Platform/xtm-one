"""A chat turn cut by a platform stop keeps its handover parent run.

Before: the shutdown sweep (``autonomous.fail_orphaned_in_process_runs``)
failed the chat-handover parent run of a turn the drain had just queued for its
automatic resume, and the resumed turn created a new parent: a red run in the
list, the delegated children under it, and a Resume click to make sense of it.

Now the cut record names the parent (``CutTurn.parent_run_id``), the sweep
keeps it (``awaiting_auto_resume``), the resumed turn adopts it, and the stale
sweep fails it only once its record left the queue with no adoption.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from app.services import autonomous, cut_turn_resume, in_process_runs
from app.services.cut_turn_resume import (
    AWAITING_AUTO_RESUME_KEY,
    AWAITING_SINCE_KEY,
    PARENT_ADOPTION_GRACE_SECONDS,
    PENDING_KEY,
    CutTurn,
    adoptable_parent_run,
    enqueue_cut_turn,
    queued_conversations,
    record_key,
    resuming_key,
)
from app.services.orchestration_adapters.assignment import execution_dedup_key

CONV = "11111111-1111-1111-1111-111111111111"
USER = "22222222-2222-2222-2222-222222222222"
MSG = "33333333-3333-3333-3333-333333333333"
AGENT = "44444444-4444-4444-4444-444444444444"


class _Redis:
    """The Redis calls the queue and the sweeps make, in memory."""

    def __init__(self) -> None:
        self.kv: dict[str, str] = {}
        self.sets: dict[str, set[str]] = {}
        self.fail = False

    def _check(self) -> None:
        if self.fail:
            raise ConnectionError("redis down")

    async def eval(self, script, numkeys, *keys_and_args):
        self._check()
        record_k, pending_k, value, _ttl, member = keys_and_args
        self.sets.setdefault(pending_k, set()).add(member)
        self.kv[record_k] = value
        return 1

    async def mget(self, *keys):
        self._check()
        return [self.kv.get(k) for k in keys]

    async def sismember(self, key, member):
        self._check()
        return member in self.sets.get(key, set())


@pytest.fixture
def redis(monkeypatch) -> _Redis:
    fake = _Redis()

    async def _client():
        return fake

    import app.core.redis as redis_module

    monkeypatch.setattr(cut_turn_resume, "_redis", _client)
    # The stale sweep's chat:processing probe: no live turn.
    monkeypatch.setattr(redis_module, "redis_client", _client)
    monkeypatch.setattr(cut_turn_resume, "_current_build", lambda: None)
    return fake


@pytest.fixture(autouse=True)
def _clean_registry():
    in_process_runs._tracked_run_ids.clear()
    in_process_runs._chat_parent_conversations.clear()
    yield
    in_process_runs._tracked_run_ids.clear()
    in_process_runs._chat_parent_conversations.clear()


def _queue(redis: _Redis, conv: str = CONV, parent: str | None = None) -> None:
    cut = CutTurn(conv, USER, MSG, "turn-1", "stop", 0, "2026-11-02T12:00:00+00:00", "web", parent_run_id=parent)
    redis.kv[record_key(conv)] = cut.to_json()
    redis.sets.setdefault(PENDING_KEY, set()).add(conv)


def _handover_run(*, conv: str = CONV, context: dict | None = None, age_seconds: int = 120):
    now = datetime.now(UTC)
    return SimpleNamespace(
        id=uuid.uuid4(),
        assignment_id=None,
        agent_id=uuid.UUID(AGENT),
        user_id=uuid.UUID(USER),
        status="running",
        created_at=now - timedelta(seconds=age_seconds),
        started_at=now - timedelta(seconds=age_seconds),
        completed_at=None,
        error=None,
        trigger_payload={"trigger_type": "chat_handover", "conversation_id": conv},
        trigger_context=context if context is not None else {"type": "chat_handover"},
        result_summary=None,
    )


class _Result:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return self

    def all(self):
        return list(self._rows)


class _DB:
    def __init__(self, results):
        self._results = list(results)
        self.commits = 0

    async def execute(self, stmt):
        return self._results.pop(0)

    async def commit(self):
        self.commits += 1


# ── The cut record ───────────────────────────────────────────────────────


def test_the_record_carries_the_parent_run():
    cut = CutTurn(CONV, USER, MSG, "turn-1", "stop", 0, "2026-11-02T12:00:00+00:00", "web", parent_run_id="p-1")

    assert CutTurn.from_raw(cut.to_json()) == cut
    assert CutTurn.from_raw(cut.to_json()).parent_run_id == "p-1"


def test_a_record_of_an_earlier_release_has_no_parent_run():
    old = json.dumps({"conversation_id": CONV, "user_id": USER, "user_message_id": MSG})

    cut = CutTurn.from_raw(old)

    assert cut is not None and cut.parent_run_id is None


async def test_enqueue_stores_the_parent_run(redis):
    assert await enqueue_cut_turn(
        CONV, user_id=USER, user_message_id=MSG, turn_id="t", cause="stop", parent_run_id=uuid.UUID(int=7)
    )

    assert CutTurn.from_raw(redis.kv[record_key(CONV)]).parent_run_id == str(uuid.UUID(int=7))


async def test_queued_conversations_reads_record_and_pending_set(redis):
    _queue(redis, CONV)
    other = str(uuid.uuid4())
    redis.kv[record_key(other)] = redis.kv[record_key(CONV)]  # record without its pending entry

    assert await queued_conversations([CONV, other, None]) == {CONV}


async def test_queued_conversations_counts_a_turn_another_process_took(redis):
    # The next process's sweep took the record (GETDEL) and is resuming it.
    redis.kv[resuming_key(CONV)] = "1"

    assert await queued_conversations([CONV]) == {CONV}


async def test_queued_conversations_is_none_when_redis_cannot_say(redis):
    redis.fail = True

    assert await queued_conversations([CONV]) is None


# ── The process registry ─────────────────────────────────────────────────


def test_the_registry_names_the_parent_of_a_conversation():
    in_process_runs.track_in_process_run("child")
    in_process_runs.track_in_process_run("p-1", conversation_id=CONV)
    in_process_runs.track_in_process_run("p-2", conversation_id=CONV)

    assert in_process_runs.chat_parent_run_for(CONV) == "p-2"
    assert in_process_runs.chat_parent_run_for("other") is None
    assert in_process_runs.chat_parent_run_for(None) is None

    in_process_runs.untrack_in_process_run("p-2")
    assert in_process_runs.chat_parent_run_for(CONV) == "p-1"


# ── The shutdown sweep ───────────────────────────────────────────────────


async def test_shutdown_keeps_the_parent_of_a_queued_turn(redis):
    _queue(redis)
    run = _handover_run()
    db = _DB([_Result([run]), _Result([])])

    failed = await autonomous.fail_orphaned_in_process_runs(db, {str(run.id)})

    assert failed == 0
    assert run.status == "running"
    assert run.trigger_context[AWAITING_AUTO_RESUME_KEY] == CONV
    assert run.trigger_context["type"] == "chat_handover"
    assert AWAITING_SINCE_KEY in run.trigger_context
    assert db.commits == 1


async def test_shutdown_fails_the_parent_of_a_turn_not_queued(redis):
    run = _handover_run()
    db = _DB([_Result([run]), _Result([])])

    failed = await autonomous.fail_orphaned_in_process_runs(db, {str(run.id)})

    assert failed == 1
    assert run.status == "failed"
    assert "shutdown" in run.error
    assert AWAITING_AUTO_RESUME_KEY not in run.trigger_context


async def test_shutdown_keeps_the_parent_of_a_turn_already_being_resumed(redis):
    """A rollout's next process took the cut turn before this process's shutdown sweep ran."""
    redis.kv[resuming_key(CONV)] = "1"
    run = _handover_run()
    db = _DB([_Result([run]), _Result([])])

    assert await autonomous.fail_orphaned_in_process_runs(db, {str(run.id)}) == 0
    assert run.status == "running"
    assert run.trigger_context[AWAITING_AUTO_RESUME_KEY] == CONV


async def test_shutdown_fails_the_parent_when_redis_cannot_say(redis):
    _queue(redis)
    redis.fail = True
    run = _handover_run()
    db = _DB([_Result([run]), _Result([])])

    assert await autonomous.fail_orphaned_in_process_runs(db, {str(run.id)}) == 1
    assert run.status == "failed"


async def test_shutdown_still_fails_a_consult_child_of_a_queued_turn(redis):
    _queue(redis)
    child = _handover_run(context={"type": "consult"})
    child.trigger_payload = {"trigger_type": "consult", "conversation_id": CONV}
    db = _DB([_Result([child]), _Result([])])

    assert await autonomous.fail_orphaned_in_process_runs(db, {str(child.id)}) == 1
    assert child.status == "failed"


async def test_shutdown_leaves_a_parent_with_a_live_execution(redis):
    _queue(redis)
    run = _handover_run()
    db = _DB([_Result([run]), _Result([(execution_dedup_key(run.id), "running")])])

    assert await autonomous.fail_orphaned_in_process_runs(db, {str(run.id)}) == 0
    assert AWAITING_AUTO_RESUME_KEY not in run.trigger_context


# ── The stale sweep ──────────────────────────────────────────────────────


def _awaiting(*, since: datetime) -> SimpleNamespace:
    return _handover_run(
        context={"type": "chat_handover", AWAITING_AUTO_RESUME_KEY: CONV, AWAITING_SINCE_KEY: since.isoformat()},
        age_seconds=7200,
    )


async def test_the_stale_sweep_leaves_a_kept_parent_while_its_turn_is_queued(redis, monkeypatch):
    monkeypatch.setattr(autonomous.settings, "stale_assignment_run_seconds", 60)
    _queue(redis)
    run = _awaiting(since=datetime.now(UTC) - timedelta(hours=2))
    db = _DB([_Result([run]), _Result([]), _Result([])])

    assert await autonomous._cleanup_stale_assignment_runs(db) == 0
    assert run.status == "running"


async def test_the_stale_sweep_leaves_a_kept_parent_when_redis_cannot_say(redis):
    redis.fail = True
    run = _awaiting(since=datetime.now(UTC) - timedelta(hours=2))
    db = _DB([_Result([run]), _Result([]), _Result([])])

    assert await autonomous._cleanup_stale_assignment_runs(db) == 0
    assert run.status == "running"


async def test_the_stale_sweep_waits_the_adoption_grace(redis):
    run = _awaiting(since=datetime.now(UTC) - timedelta(seconds=PARENT_ADOPTION_GRACE_SECONDS / 2))
    db = _DB([_Result([run]), _Result([]), _Result([])])

    assert await autonomous._cleanup_stale_assignment_runs(db) == 0
    assert run.status == "running"


async def test_the_stale_sweep_fails_a_parent_whose_resume_never_adopted_it(redis):
    run = _awaiting(since=datetime.now(UTC) - timedelta(seconds=PARENT_ADOPTION_GRACE_SECONDS + 5))
    db = _DB([_Result([run]), _Result([]), _Result([])])

    assert await autonomous._cleanup_stale_assignment_runs(db) == 1
    assert run.status == "failed"
    assert "never adopted" in run.error and CONV in run.error
    assert run.error.startswith(autonomous.STALE_ASSIGNMENT_CLEANUP_ERROR_PREFIX)
    assert db.commits == 1


async def test_the_stale_sweep_leaves_a_kept_parent_its_resumed_turn_still_runs(redis):
    """Past the grace, a live turn on the conversation (the resumed one) keeps it."""
    from app.core.utils import chat_processing_key

    redis.kv[chat_processing_key(CONV)] = "turn-token"
    run = _awaiting(since=datetime.now(UTC) - timedelta(seconds=PARENT_ADOPTION_GRACE_SECONDS + 5))
    db = _DB([_Result([run]), _Result([]), _Result([])])

    assert await autonomous._cleanup_stale_assignment_runs(db) == 0
    assert run.status == "running"


# ── Adoption ─────────────────────────────────────────────────────────────


def test_the_same_agent_users_running_parent_is_adoptable():
    run = _awaiting(since=datetime.now(UTC))

    assert adoptable_parent_run(run, agent_id=AGENT, user_id=USER, conversation_id=CONV)


@pytest.mark.parametrize(
    "change",
    [
        {"agent_id": uuid.uuid4()},
        {"user_id": uuid.uuid4()},
        {"status": "failed"},
        {"assignment_id": uuid.uuid4()},
        {"trigger_context": {"type": "consult"}},
        {"trigger_payload": {"trigger_type": "chat_handover", "conversation_id": str(uuid.uuid4())}},
        {"trigger_payload": None},
    ],
    ids=["other-agent", "other-user", "not-running", "an-assignment-run", "not-a-handover", "other-conv", "no-payload"],
)
def test_any_other_run_is_not_adopted(change):
    run = _awaiting(since=datetime.now(UTC))
    for key, value in change.items():
        setattr(run, key, value)

    assert not adoptable_parent_run(run, agent_id=AGENT, user_id=USER, conversation_id=CONV)


async def test_the_resume_hands_the_parent_to_the_turn(monkeypatch):
    """``resume_cut_turn`` sets the hand-off the turn adopts, and clears it after."""
    from contextlib import asynccontextmanager

    import app.core.database as database
    from app.services import parent_wake, turn_checkpoint

    conv = SimpleNamespace(
        id=uuid.UUID(CONV), user_id=uuid.UUID(USER), agent_id=None, channel="web",
        channel_thread_id=None, tool_overrides_json=None,
    )
    msg = SimpleNamespace(id=uuid.UUID(MSG), role="user", content="go on", sequence=3)
    answers = [conv, msg, None]

    class _R:
        def __init__(self, v):
            self._v = v

        def scalar_one_or_none(self):
            return self._v

    class _S:
        async def execute(self, stmt):
            return _R(answers.pop(0))

        async def commit(self):
            return None

    @asynccontextmanager
    async def _session():
        yield _S()

    monkeypatch.setattr(database, "async_session", _session)

    async def _stopped(*a, **kw):
        return False

    monkeypatch.setattr(parent_wake, "_stopped_since", _stopped)

    async def _load_replay(db, conversation_id, *, user_id=None, now=None, code_interpreter=False):
        return turn_checkpoint.ResumedWork(found=False), []

    monkeypatch.setattr(turn_checkpoint, "load_replay", _load_replay)
    seen = []

    async def _run(db, **kw):
        seen.append(cut_turn_resume.RESUMED_PARENT_RUN.get())
        return False

    monkeypatch.setattr(parent_wake, "_run_parent_wake_turn", _run)

    cut = CutTurn(CONV, USER, MSG, "turn-1", "stop", 0, "2026-11-02T12:00:00+00:00", "web", parent_run_id="p-9")
    assert await parent_wake.resume_cut_turn(cut) is True

    [handoff] = seen
    assert handoff is not None and handoff.run_id == "p-9" and handoff.adopted is False
    assert cut_turn_resume.RESUMED_PARENT_RUN.get() is None
