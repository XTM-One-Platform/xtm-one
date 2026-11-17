"""A chat turn cut by a platform stop is queued and resumed by the next process.

``services/cut_turn_resume``: only a platform stop (``stop``, ``recycle``) is
queued, at most ``MAX_AUTO_RESUMES`` times per user message; the sweep of the
scheduler tick takes each cut turn atomically once its lock is free and hands
it to the resume runner; the resumed reply links back to the cut turn.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
from redis.exceptions import ResponseError

from app.core import worker_shutdown
from app.services import cut_turn_resume
from app.services.cut_turn_resume import (
    DEAD_LOCK_GRACE_SECONDS,
    MAX_AUTO_RESUME_SPAN_SECONDS,
    MAX_AUTO_RESUMES,
    MAX_TOTAL_AUTO_RESUMES,
    PENDING_KEY,
    RESUMED_FROM_META_KEY,
    RESUMING_FRAME_TYPE,
    CutTurn,
    enqueue_cut_turn,
    is_queued,
    record_key,
    resumable_cause,
    resumed_from_meta,
    resuming_frame,
    sweep_cut_turns,
    take_cut_turn,
)

CONV = "11111111-1111-1111-1111-111111111111"
USER = "22222222-2222-2222-2222-222222222222"
MSG = "33333333-3333-3333-3333-333333333333"


class _FakeRedis:
    """The Redis calls the queue makes, in memory."""

    def __init__(self) -> None:
        self.kv: dict[str, str] = {}
        self.sets: dict[str, set[str]] = {}
        self.ttl: dict[str, int] = {}
        self.fail = False
        #: Fail the queueing script before it writes anything (connection lost).
        self.fail_queue_script = False
        #: Fail the SADD inside the queueing script (WRONGTYPE...): it runs
        #: first, so the script stops before writing the record.
        self.fail_sadd_in_script = False

    def _check(self) -> None:
        if self.fail:
            raise ConnectionError("redis down")

    async def set(self, key, value, ex=None):
        self._check()
        self.kv[key] = value
        if ex is not None:
            self.ttl[key] = ex

    async def get(self, key):
        self._check()
        return self.kv.get(key)

    async def getdel(self, key):
        self._check()
        return self.kv.pop(key, None)

    async def sadd(self, key, member):
        self._check()
        self.sets.setdefault(key, set()).add(member)

    async def eval(self, script, numkeys, *keys_and_args):
        """The queueing script of ``enqueue_cut_turn``: SET with its TTL, then SADD, atomically."""
        self._check()
        assert script == cut_turn_resume._QUEUE_CUT_TURN and numkeys == 2
        if self.fail_queue_script:
            raise ConnectionError("redis down")
        record_k, pending_k, value, ttl, member = keys_and_args
        if self.fail_sadd_in_script:
            raise ResponseError("WRONGTYPE Operation against a key holding the wrong kind of value")
        self.sets.setdefault(pending_k, set()).add(member)
        self.kv[record_k] = value
        self.ttl[record_k] = int(ttl)
        return 1

    async def sismember(self, key, member):
        self._check()
        return member in self.sets.get(key, set())

    async def exists(self, key):
        self._check()
        return int(key in self.kv)

    async def delete(self, key):
        self._check()
        self.ttl.pop(key, None)
        return int(self.kv.pop(key, None) is not None)

    async def srem(self, key, member):
        self._check()
        self.sets.get(key, set()).discard(member)

    async def smembers(self, key):
        self._check()
        return {m.encode() for m in self.sets.get(key, set())}

    async def scard(self, key):
        self._check()
        return len(self.sets.get(key, set()))


@pytest.fixture(autouse=True)
def _running_process(monkeypatch):
    """No test of this module asked its process to stop, unless it says so;
    its records carry no build, unless it writes one (the same-build deferral)."""
    monkeypatch.setattr(worker_shutdown, "_request", None)
    monkeypatch.setattr(cut_turn_resume, "_current_build", lambda: None)


@pytest.fixture
def redis(monkeypatch) -> _FakeRedis:
    fake = _FakeRedis()

    async def _client():
        return fake

    monkeypatch.setattr(cut_turn_resume, "_redis", _client)
    return fake


async def _enqueue(cause: str | None = "stop", **kw) -> bool:
    return await enqueue_cut_turn(
        CONV, user_id=USER, user_message_id=MSG, turn_id="turn-1", cause=cause, **kw
    )


@pytest.mark.parametrize("cause", ["stop", "recycle"])
def test_a_platform_stop_is_resumable(cause):
    assert resumable_cause(cause)


@pytest.mark.parametrize("cause", [None, "", "oom", "crash", "unknown"])
def test_any_other_cut_is_not(cause):
    assert not resumable_cause(cause)


async def test_a_turn_cut_by_a_platform_stop_is_queued(redis, caplog):
    now = datetime(2026, 11, 1, 1, 0, tzinfo=UTC)

    assert await _enqueue("recycle", entry="rest", now=now)

    assert redis.sets[PENDING_KEY] == {CONV}
    assert redis.ttl[record_key(CONV)] == cut_turn_resume.RECORD_TTL_SECONDS
    record = json.loads(redis.kv[record_key(CONV)])
    assert record == {
        "conversation_id": CONV,
        "user_id": USER,
        "user_message_id": MSG,
        "turn_id": "turn-1",
        "cause": "recycle",
        "resumes": 0,
        "cut_at": now.isoformat(),
        "entry": "rest",
        "lock_token": None,
        "build": record["build"],
        "stalled": 0,
        "parent_run_id": None,
        "first_cut_at": now.isoformat(),
        "trace_id": None,
    }
    assert f"queued for its automatic resume 1 of {MAX_TOTAL_AUTO_RESUMES}" in caplog.text


async def test_a_turn_cut_otherwise_keeps_the_one_click(redis, caplog):
    assert not await _enqueue(None)

    assert redis.kv == {}
    assert "not a platform stop" in caplog.text


async def test_three_stalled_resumes_in_a_row_keep_the_one_click(redis, caplog):
    assert await _enqueue(resumes_made=MAX_AUTO_RESUMES - 1, stalled=MAX_AUTO_RESUMES - 1)
    redis.kv.clear()
    redis.sets.clear()

    assert not await _enqueue(resumes_made=MAX_AUTO_RESUMES, stalled=MAX_AUTO_RESUMES)

    assert redis.kv == {}
    assert "completed no tool round" in caplog.text


async def test_a_turn_that_advances_keeps_resuming_across_many_rollouts(redis):
    """67 rollouts in a day; a 30 min turn crosses several, each resume making progress."""
    assert await _enqueue(resumes_made=MAX_AUTO_RESUMES + 2, stalled=0)

    record = json.loads(redis.kv[record_key(CONV)])
    assert record["resumes"] == MAX_AUTO_RESUMES + 2
    assert record["stalled"] == 0


async def test_a_message_gets_at_most_the_ceiling_in_all(redis, caplog):
    assert await _enqueue(resumes_made=MAX_TOTAL_AUTO_RESUMES - 1, stalled=0)
    redis.kv.clear()
    redis.sets.clear()

    assert not await _enqueue(resumes_made=MAX_TOTAL_AUTO_RESUMES, stalled=0)

    assert redis.kv == {}
    assert "the most a message gets" in caplog.text


@pytest.mark.parametrize("resumes_made", [20, 50, MAX_TOTAL_AUTO_RESUMES - 1])
async def test_a_progressing_turn_survives_a_rollout_storm(redis, resumes_made):
    """21 cuts from 11:44Z to 14:49Z, each resume making progress, stopped at 20 of 20."""
    first = datetime(2026, 11, 2, 11, 44, 42, tzinfo=UTC)
    now = datetime(2026, 11, 2, 14, 49, 7, tzinfo=UTC)

    assert await _enqueue(resumes_made=resumes_made, stalled=0, first_cut_at=first.isoformat(), now=now)

    record = json.loads(redis.kv[record_key(CONV)])
    assert record["first_cut_at"] == first.isoformat()
    assert record["cut_at"] == now.isoformat()
    assert CutTurn.from_raw(redis.kv[record_key(CONV)]).first_cut == first.isoformat()


async def test_a_turn_past_the_span_keeps_the_one_click(redis, caplog):
    now = datetime(2026, 11, 2, 23, 0, tzinfo=UTC)
    first = now - timedelta(seconds=MAX_AUTO_RESUME_SPAN_SECONDS + 60)

    assert not await _enqueue(resumes_made=5, stalled=0, first_cut_at=first.isoformat(), now=now)

    assert redis.kv == {}
    assert "after its first cut" in caplog.text


async def test_the_span_does_not_lift_the_stalled_guard(redis, caplog):
    now = datetime(2026, 11, 2, 12, 0, tzinfo=UTC)

    assert not await _enqueue(
        resumes_made=MAX_AUTO_RESUMES, stalled=MAX_AUTO_RESUMES, first_cut_at=now.isoformat(), now=now
    )

    assert "completed no tool round" in caplog.text


def test_a_record_of_an_earlier_release_spans_from_its_cut():
    old = CutTurn.from_raw(
        json.dumps({"conversation_id": CONV, "user_id": USER, "cause": "stop", "cut_at": "2026-11-02T11:44:42+00:00"})
    )

    assert old is not None
    assert old.first_cut_at is None
    assert old.first_cut == "2026-11-02T11:44:42+00:00"


def test_a_record_of_an_earlier_release_counts_its_resumes_as_stalled():
    old = CutTurn.from_raw(
        json.dumps({"conversation_id": CONV, "user_id": USER, "resumes": 2, "cause": "stop"})
    )

    assert old is not None
    assert old.stalled is None
    assert old.stalled_resumes == 2


async def test_a_queue_that_cannot_be_written_costs_the_resume_never_the_turn(redis, caplog):
    redis.fail = True

    assert not await _enqueue()

    assert "Could not queue the cut turn" in caplog.text


async def test_a_cut_turn_is_taken_once(redis):
    await _enqueue()

    first = await take_cut_turn(CONV)
    second = await take_cut_turn(CONV)

    assert first is not None and first.user_message_id == MSG and first.cause == "stop"
    assert second is None
    assert redis.sets[PENDING_KEY] == set()


async def test_a_take_with_redis_down_takes_nothing(redis):
    await _enqueue()
    redis.fail = True

    assert await take_cut_turn(CONV) is None


@pytest.mark.parametrize("raw", [None, 42, b"not json", "{}", '{"conversation_id": "c"}'])
def test_an_unreadable_record_is_no_cut_turn(raw):
    assert CutTurn.from_raw(raw) is None


def test_a_record_round_trips():
    cut = CutTurn(CONV, USER, MSG, "turn-1", "stop", 2, "2026-11-01T01:00:00+00:00", "web")

    assert CutTurn.from_raw(cut.to_json().encode()) == cut


async def test_the_sweep_resumes_a_cut_turn_once_its_lock_is_free(redis):
    await _enqueue()
    resumed: list[CutTurn] = []
    active = {CONV: True}

    async def _resume(cut):
        resumed.append(cut)

    async def _is_active(cid):
        return active.get(cid, False)

    # The cut turn's process is still draining: it waits for the next tick.
    assert await sweep_cut_turns(_resume, is_turn_active=_is_active) == 0
    assert resumed == []
    assert redis.sets[PENDING_KEY] == {CONV}

    active[CONV] = False
    assert await sweep_cut_turns(_resume, is_turn_active=_is_active) == 1
    assert [c.conversation_id for c in resumed] == [CONV]

    # Taken: the next tick (or another worker) finds nothing.
    assert await sweep_cut_turns(_resume, is_turn_active=_is_active) == 0
    assert len(resumed) == 1


async def test_an_expired_record_leaves_the_queue(redis):
    await _enqueue()
    del redis.kv[record_key(CONV)]

    async def _resume(cut):
        raise AssertionError("nothing to resume")

    async def _idle(cid):
        return False

    assert await sweep_cut_turns(_resume, is_turn_active=_idle) == 0
    assert redis.sets[PENDING_KEY] == set()


async def test_a_resume_that_fails_is_logged_and_the_sweep_goes_on(redis, caplog):
    await _enqueue()

    async def _resume(cut):
        raise RuntimeError("boom")

    async def _idle(cid):
        return False

    assert await sweep_cut_turns(_resume, is_turn_active=_idle) == 0
    assert "Could not resume the cut turn" in caplog.text


async def test_a_sweep_with_redis_down_resumes_nothing(redis):
    redis.fail = True

    async def _resume(cut):
        raise AssertionError("nothing to resume")

    assert await sweep_cut_turns(_resume, is_turn_active=None) == 0


def test_the_resumed_reply_links_back_to_the_cut_turn():
    cut = CutTurn(CONV, USER, MSG, "turn-1", "stop", 1, "2026-11-01T01:00:00+00:00")

    meta = resumed_from_meta(cut, resumed_as="turn-2")

    assert meta[RESUMED_FROM_META_KEY] == {
        "turn_id": "turn-1",
        "cause": "stop",
        "cut_at": "2026-11-01T01:00:00+00:00",
        "resume": 2,
        "of": MAX_TOTAL_AUTO_RESUMES,
        "resumed_as": "turn-2",
    }


# ---------------------------------------------------------------------------
# The production case. The resume was swept by the scheduler tick,
# which runs once a minute fleet-wide on whichever worker wins its Redis
# window, including a worker of the pod being drained: there the resume
# started and was cut again within seconds (trace 266e62f0, resume 3 of 3,
# 1.1 s), and the cap ran out; a lock left by a killed process held the
# queue for its 300 s TTL.
# ---------------------------------------------------------------------------


async def _idle(cid):
    return False


async def _held(cid):
    return True


async def test_a_stopping_process_resumes_nothing(redis):
    """A resume started on a draining pod is cut again and spends the cap."""
    await _enqueue()

    async def _resume(cut):
        raise AssertionError("a stopping process must not resume")

    assert await sweep_cut_turns(_resume, is_turn_active=_idle, is_stopping=lambda: True) == 0
    assert redis.sets[PENDING_KEY] == {CONV}
    assert record_key(CONV) in redis.kv


async def test_the_sweep_of_a_process_asked_to_stop_reads_its_stop(redis):
    await _enqueue()
    worker_shutdown.note_shutdown(worker_shutdown.STOP, "SIGTERM")

    async def _resume(cut):
        raise AssertionError("a stopping process must not resume")

    assert await sweep_cut_turns(_resume, is_turn_active=_idle) == 0
    assert await cut_turn_resume.sweep_once_if_pending() == 0
    assert redis.sets[PENDING_KEY] == {CONV}


async def test_a_lock_left_by_the_cut_turn_s_dead_process_is_freed(redis, caplog):
    cut_at = datetime(2026, 11, 1, 11, 44, 46, tzinfo=UTC)
    await _enqueue(lock_token="tok-cut", now=cut_at)
    released: list[tuple[str, str]] = []
    resumed: list[CutTurn] = []

    async def _release(cid, token):
        released.append((cid, token))
        return True

    async def _resume(cut):
        resumed.append(cut)

    late = datetime.fromtimestamp(cut_at.timestamp() + DEAD_LOCK_GRACE_SECONDS + 1, tz=UTC)
    assert await sweep_cut_turns(_resume, is_turn_active=_held, release_dead_lock=_release, now=late) == 1
    assert released == [(CONV, "tok-cut")]
    assert [c.conversation_id for c in resumed] == [CONV]
    assert "Freed the turn lock" in caplog.text


async def test_a_lock_is_left_to_the_cut_process_s_drain(redis):
    cut_at = datetime(2026, 11, 1, 11, 44, 46, tzinfo=UTC)
    await _enqueue(lock_token="tok-cut", now=cut_at)

    async def _release(cid, token):
        raise AssertionError("within the drain the lock is the cut process's")

    async def _resume(cut):
        raise AssertionError("held")

    early = datetime.fromtimestamp(cut_at.timestamp() + DEAD_LOCK_GRACE_SECONDS - 5, tz=UTC)
    assert await sweep_cut_turns(_resume, is_turn_active=_held, release_dead_lock=_release, now=early) == 0
    assert redis.sets[PENDING_KEY] == {CONV}


async def test_a_lock_another_turn_took_is_never_freed(redis):
    """The person started a turn: its lock holds another token, the compare-and-delete refuses."""
    cut_at = datetime(2026, 11, 1, 11, 44, 46, tzinfo=UTC)
    await _enqueue(lock_token="tok-cut", now=cut_at)

    async def _release(cid, token):
        return False

    async def _resume(cut):
        raise AssertionError("another turn holds the conversation")

    late = datetime.fromtimestamp(cut_at.timestamp() + 600, tz=UTC)
    assert await sweep_cut_turns(_resume, is_turn_active=_held, release_dead_lock=_release, now=late) == 0
    assert redis.sets[PENDING_KEY] == {CONV}


async def test_a_record_with_no_lock_token_never_frees_a_lock(redis):
    await _enqueue(now=datetime(2026, 11, 1, 11, 0, tzinfo=UTC))

    async def _release(cid, token):
        raise AssertionError("no token, nothing to compare")

    async def _resume(cut):
        raise AssertionError("held")

    late = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)
    assert await sweep_cut_turns(_resume, is_turn_active=_held, release_dead_lock=_release, now=late) == 0


async def test_the_notice_reads_whether_the_turn_is_queued(redis):
    assert not await is_queued(CONV)
    await _enqueue()
    assert await is_queued(CONV)
    await take_cut_turn(CONV)
    # Taken, not yet holding the conversation: still resuming by itself.
    assert await is_queued(CONV)
    await cut_turn_resume.resume_settled(CONV)
    assert not await is_queued(CONV)


async def test_a_taken_cut_turn_still_reads_resuming_until_its_resume_settles(redis, monkeypatch):
    """Between the sweep's take and the resumed turn's lock, a refetch of the
    conversation offered Resume; the click made the resume stand down."""
    await _gate(monkeypatch, on=True)
    await _enqueue()

    taken = await take_cut_turn(CONV)

    assert taken is not None
    assert record_key(CONV) not in redis.kv
    assert redis.ttl[cut_turn_resume.resuming_key(CONV)] == cut_turn_resume.RESUMING_TTL_SECONDS
    assert await cut_turn_resume.resumes_by_itself(CONV)
    await cut_turn_resume.resume_settled(CONV)
    assert not await cut_turn_resume.resumes_by_itself(CONV)


async def test_a_take_of_nothing_leaves_no_resuming_marker(redis):
    assert await take_cut_turn(CONV) is None

    assert cut_turn_resume.resuming_key(CONV) not in redis.kv
    assert not await is_queued(CONV)


async def test_the_scheduled_resume_settles_its_marker_whatever_the_outcome(redis, monkeypatch):
    from app.services import parent_wake

    await _enqueue()
    taken = await take_cut_turn(CONV)
    seen: list[bool] = []

    async def _stands_down(cut, **kw):
        seen.append(await is_queued(cut.conversation_id))
        raise RuntimeError("boom")

    monkeypatch.setattr(parent_wake, "resume_cut_turn", _stands_down)

    with pytest.raises(RuntimeError):
        await parent_wake._resume_cut_turn_settled(taken)

    assert seen == [True]
    assert not await is_queued(CONV)


async def test_a_take_by_a_process_asked_to_stop_puts_the_record_back(redis):
    """Asked to stop between the sweep's start and its take: the resume would be
    cut in the drain or never start, and the take had removed the record."""
    await _enqueue(resumes_made=1, trace_id="t-hop-1")
    before = redis.kv[record_key(CONV)]
    checks = iter([False, True])

    async def _free(cid):
        return False

    async def _resume(cut):
        raise AssertionError("a stopping process starts no resume")

    handed = await sweep_cut_turns(_resume, is_turn_active=_free, is_stopping=lambda: next(checks))

    assert handed == 0
    assert await is_queued(CONV)
    assert json.loads(redis.kv[record_key(CONV)]) == json.loads(before)
    assert cut_turn_resume.resuming_key(CONV) not in redis.kv


async def test_the_record_keeps_the_trace_of_the_hop_it_resumes(redis):
    await _enqueue(trace_id="t-hop-1")

    taken = await take_cut_turn(CONV)

    assert taken is not None and taken.trace_id == "t-hop-1"
    # A record of an earlier release has none: the resume queries it.
    legacy = json.loads(taken.to_json())
    legacy.pop("trace_id")
    assert CutTurn.from_raw(json.dumps(legacy)).trace_id is None


async def test_a_failed_queue_write_leaves_neither_record_nor_member(redis, caplog):
    """Record and pending entry go together: a failed write, even beside a stale
    member of the conversation, queues nothing and keeps Resume."""
    redis.sets[PENDING_KEY] = {CONV}
    redis.fail_queue_script = True

    assert not await _enqueue()

    assert record_key(CONV) not in redis.kv
    assert not await is_queued(CONV)
    assert "Could not queue the cut turn" in caplog.text


async def test_a_sadd_failing_inside_the_script_leaves_no_record(redis, caplog):
    """A Lua error does not roll back a write: the SADD runs first, so its failure
    writes no record a stale member could pair with."""
    redis.sets[PENDING_KEY] = {CONV}
    redis.fail_sadd_in_script = True

    assert not await _enqueue()

    assert record_key(CONV) not in redis.kv
    assert not await is_queued(CONV)
    assert "Could not queue the cut turn" in caplog.text


def test_the_queueing_script_adds_the_entry_before_writing_the_record():
    script = cut_turn_resume._QUEUE_CUT_TURN
    assert script.index("redis.call('SADD'") < script.index("redis.call('SET'")
    assert "DEL" not in script


async def test_a_sadd_failure_keeps_an_earlier_queued_record(redis):
    """A later cut of a conversation already queued, with the SADD refused, neither
    overwrites nor deletes the queued record."""
    await _enqueue()
    queued = redis.kv[record_key(CONV)]
    redis.fail_sadd_in_script = True

    assert not await _enqueue()

    assert redis.kv[record_key(CONV)] == queued
    assert await is_queued(CONV)


async def test_a_failed_queue_write_never_touches_a_concurrent_stream_s_record(redis):
    """Two platform-chat streams of one conversation hold no lock: the one whose
    write fails leaves the record the other queued."""
    await _enqueue()
    queued = redis.kv[record_key(CONV)]
    redis.fail_queue_script = True

    assert not await _enqueue()

    assert redis.kv[record_key(CONV)] == queued
    assert await is_queued(CONV)


async def test_the_sweep_drops_a_stale_member_with_no_record(redis):
    redis.sets[PENDING_KEY] = {CONV}

    assert await take_cut_turn(CONV) is None

    assert CONV not in redis.sets[PENDING_KEY]


async def test_a_record_outside_the_pending_set_is_not_queued(redis):
    await _enqueue()
    redis.sets[PENDING_KEY].discard(CONV)

    assert not await is_queued(CONV)


async def _gate(monkeypatch, *, on: bool | None) -> list[int]:
    """Stub the agentic gate: on, off, or failing (None); counts its reads."""
    reads: list[int] = []

    async def _read():
        reads.append(1)
        if on is None:
            raise RuntimeError("database down")
        return on

    monkeypatch.setattr(cut_turn_resume, "_agentic_ai_on", _read)
    return reads


async def test_a_queued_turn_resumes_by_itself_while_agentic_ai_is_on(redis, monkeypatch):
    """The notice says "resuming by itself" only while the sweep can take it."""
    await _gate(monkeypatch, on=True)
    await _enqueue()

    assert await cut_turn_resume.resumes_by_itself(CONV)


async def test_a_queued_turn_keeps_resume_while_agentic_ai_is_off(redis, monkeypatch):
    """With AI off the queue waits (24 h), so the notice keeps Resume."""
    await _gate(monkeypatch, on=False)
    await _enqueue()

    assert await is_queued(CONV)
    assert not await cut_turn_resume.resumes_by_itself(CONV)


async def test_an_unreadable_gate_keeps_resume(redis, monkeypatch):
    await _gate(monkeypatch, on=None)
    await _enqueue()

    assert not await cut_turn_resume.resumes_by_itself(CONV)


async def test_a_turn_not_queued_never_reads_the_gate(redis, monkeypatch):
    reads = await _gate(monkeypatch, on=True)

    assert not await cut_turn_resume.resumes_by_itself(CONV)
    assert reads == []


async def test_an_empty_queue_costs_no_database_session(redis, monkeypatch):
    async def _no_db():
        raise AssertionError("an empty queue opens no session")

    monkeypatch.setattr(cut_turn_resume, "_agentic_ai_on", _no_db)
    assert await cut_turn_resume.sweep_once_if_pending() == 0


async def test_the_queue_waits_while_agentic_ai_is_off(redis, monkeypatch):
    await _enqueue()

    async def _off():
        return False

    monkeypatch.setattr(cut_turn_resume, "_agentic_ai_on", _off)
    assert await cut_turn_resume.sweep_once_if_pending() == 0
    assert redis.sets[PENDING_KEY] == {CONV}


async def test_the_sweep_loop_runs_at_once_and_survives_an_error(monkeypatch):
    import asyncio

    calls: list[int] = []

    async def _sweep():
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("boom")
        return 0

    channel_sweeps: list[int] = []

    async def _channel_sweep() -> int:
        channel_sweeps.append(1)
        return 0

    monkeypatch.setattr(cut_turn_resume, "sweep_once_if_pending", _sweep)
    monkeypatch.setattr(cut_turn_resume, "sweep_channel_turns_once", _channel_sweep)
    task = asyncio.create_task(cut_turn_resume.run_sweep_loop(interval=0.01))
    for _ in range(100):
        if len(calls) >= 2:
            break
        await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(calls) >= 2
    # The channel queues are drained by the same loop, after a failed web sweep too (#11924).
    assert len(channel_sweeps) >= 1


async def test_a_stop_cut_waits_for_a_process_of_the_new_build(redis):
    """Trace 266e62f0: resume 3 of 3 started on the build being rolled out and was cut after 1.1 s."""
    cut_at = datetime(2026, 11, 1, 11, 15, 47, tzinfo=UTC)
    await _enqueue(now=cut_at)
    redis.kv[record_key(CONV)] = json.dumps({**json.loads(redis.kv[record_key(CONV)]), "build": "ea795cd"})
    resumed: list[CutTurn] = []

    async def _resume(cut):
        resumed.append(cut)

    soon = datetime.fromtimestamp(cut_at.timestamp() + 37, tz=UTC)
    assert await sweep_cut_turns(_resume, is_turn_active=_idle, now=soon, build="ea795cd") == 0
    # A process of the new build takes it at once.
    assert await sweep_cut_turns(_resume, is_turn_active=_idle, now=soon, build="754a23b") == 1
    assert [c.conversation_id for c in resumed] == [CONV]


async def test_past_the_deferral_any_build_takes_a_stop_cut(redis):
    """A scale-in or a node drain brings no new build: the turn still resumes."""
    cut_at = datetime(2026, 11, 1, 11, 15, 47, tzinfo=UTC)
    await _enqueue(now=cut_at)
    redis.kv[record_key(CONV)] = json.dumps({**json.loads(redis.kv[record_key(CONV)]), "build": "ea795cd"})
    resumed: list[CutTurn] = []

    async def _resume(cut):
        resumed.append(cut)

    late = datetime.fromtimestamp(cut_at.timestamp() + cut_turn_resume.SAME_BUILD_DEFER_SECONDS + 1, tz=UTC)
    assert await sweep_cut_turns(_resume, is_turn_active=_idle, now=late, build="ea795cd") == 1


async def test_a_recycle_is_resumed_by_its_own_build_at_once(redis):
    await _enqueue(cause="recycle")
    redis.kv[record_key(CONV)] = json.dumps({**json.loads(redis.kv[record_key(CONV)]), "build": "ea795cd"})
    resumed: list[CutTurn] = []

    async def _resume(cut):
        resumed.append(cut)

    assert await sweep_cut_turns(_resume, is_turn_active=_idle, build="ea795cd") == 1


def test_a_drain_cut_with_no_recorded_cause_is_a_platform_stop():
    assert worker_shutdown.cut_cause() is None
    assert worker_shutdown.drain_cut_cause() == worker_shutdown.STOP
    assert resumable_cause(worker_shutdown.drain_cut_cause())


def test_a_recorded_cause_wins_over_the_default():
    worker_shutdown.note_shutdown(worker_shutdown.RECYCLE, "5000 requests")
    assert worker_shutdown.drain_cut_cause() == worker_shutdown.RECYCLE


def test_the_viewers_are_told_the_resume_started():
    cut = CutTurn(CONV, USER, MSG, "turn-1", "recycle", 0, "")

    assert resuming_frame(cut) == {
        "type": RESUMING_FRAME_TYPE,
        "cause": "recycle",
        "resume": 1,
        "of": MAX_TOTAL_AUTO_RESUMES,
    }
