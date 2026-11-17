"""One compaction per conversation across processes.

A precompute runs outside the turn lock, so the next turn could start on
another process while it still ran and compact the same messages again: a second
compactor call and a second fact flush for one cycle. Whoever compacts now holds
a Redis claim on the conversation; a precompute that finds it held leaves the
compaction to its holder, and a turn waits for the holder and catches up with
the state it persisted.
"""

from __future__ import annotations

import asyncio
import contextlib
import types
import uuid

import pytest

import app.services.conversation_memory as cm
from tests.test_conversation_compaction_precompute import _Compactor, _history

CONV = str(uuid.uuid4())


class _FakeRedis:
    """The four commands the claim uses, over a dict (TTL is not simulated)."""

    def __init__(self) -> None:
        self.data: dict[str, str] = {}
        self.expiries: list[tuple[str, int]] = []

    async def set(self, key, value, nx=False, ex=None):
        if nx and key in self.data:
            return None
        self.data[key] = value
        return True

    async def exists(self, key):
        return int(key in self.data)

    async def eval(self, script, numkeys, key, token, *args):
        if self.data.get(key) != token:
            return 0
        if "'del'" in script:
            del self.data[key]
        else:
            self.expiries.append((key, int(args[0])))
        return 1


class _DeadRedis:
    async def set(self, *args, **kwargs):
        raise ConnectionError("redis down")

    async def exists(self, *args, **kwargs):
        raise ConnectionError("redis down")

    async def eval(self, *args, **kwargs):
        raise ConnectionError("redis down")


@pytest.fixture(autouse=True)
def _clean():
    cm._precomputed.clear()
    yield
    cm._precomputed.clear()
    for task in cm._claim_renewers.values():
        task.cancel()
    cm._claim_renewers.clear()


@pytest.fixture()
def redis(monkeypatch):
    fake = _FakeRedis()

    async def _client():
        return fake

    monkeypatch.setattr(cm, "_claim_redis", _client)
    monkeypatch.setattr(cm, "_CLAIM_POLL_SECS", 0.01)
    return fake


@pytest.fixture()
def db(monkeypatch):
    """The stored compaction state, and every persist."""
    box: dict = {"state": None, "persisted": []}

    async def _load(conversation_id):
        return box["state"]

    async def _persist(conversation_id, **kwargs):
        box["persisted"].append(kwargs)

    monkeypatch.setattr(cm, "_load_stored_compaction", _load)
    monkeypatch.setattr(cm, "_persist_compaction_state", _persist)
    return box


def _state_covering(history, through_index: int) -> dict:
    return {
        "directives": "- from the other process",
        "summary": "- recap elsewhere",
        "through_sequence": history[through_index].db_sequence,
        "compacted_count": through_index + 1,
    }


@pytest.mark.asyncio
async def test_a_turn_waits_for_a_compaction_running_elsewhere_and_catches_up(redis, db):
    history = _history(cm.SUMMARIZE_THRESHOLD + 2)
    through = len(history) - cm.KEEP_RECENT - 1
    redis.data[cm._claim_key(CONV)] = "other-process"
    llm = _Compactor()

    turn = asyncio.create_task(cm.manage_conversation_memory(llm, list(history), conversation_id=CONV))
    await asyncio.sleep(0.05)
    assert not turn.done()

    db["state"] = _state_covering(history, through)
    del redis.data[cm._claim_key(CONV)]
    result = await asyncio.wait_for(turn, timeout=5)

    assert llm.calls == []
    assert db["persisted"] == []
    assert "from the other process" in (result[0].content or "")
    assert result[1:] == history[through + 1 :]
    assert redis.data == {}


@pytest.mark.asyncio
async def test_a_turn_waits_again_when_another_holder_takes_the_claim_first(redis, db):
    history = _history(cm.SUMMARIZE_THRESHOLD + 2)
    through = len(history) - cm.KEEP_RECENT - 1
    key = cm._claim_key(CONV)
    redis.data[key] = "first-holder"
    llm = _Compactor()

    turn = asyncio.create_task(cm.manage_conversation_memory(llm, list(history), conversation_id=CONV))
    await asyncio.sleep(0.05)
    # The first holder releases without persisting anything and a second one
    # takes the claim before the waiting turn claims again.
    real_exists = redis.exists
    handed_over = False

    async def _exists(k):
        nonlocal handed_over
        if not handed_over and k == key:
            handed_over = True
            redis.data[key] = "second-holder"
            return 0
        return await real_exists(k)

    redis.exists = _exists
    del redis.data[key]
    await asyncio.sleep(0.05)
    assert not turn.done()
    assert llm.calls == []

    db["state"] = _state_covering(history, through)
    del redis.data[key]
    result = await asyncio.wait_for(turn, timeout=5)

    assert llm.calls == []
    assert result[1:] == history[through + 1 :]


@pytest.mark.asyncio
async def test_past_the_longest_a_holder_runs_the_turn_compacts_unclaimed(redis, db, monkeypatch):
    monkeypatch.setattr(cm, "_claim_wait_cap_secs", lambda: 0.1)
    redis.data[cm._claim_key(CONV)] = "stuck-holder"
    llm = _Compactor()

    result = await asyncio.wait_for(
        cm.manage_conversation_memory(llm, _history(cm.SUMMARIZE_THRESHOLD + 2), conversation_id=CONV),
        timeout=5,
    )

    assert len(llm.calls) == 1
    assert cm._is_compaction_message(result[0])
    assert redis.data == {cm._claim_key(CONV): "stuck-holder"}


@pytest.mark.asyncio
async def test_a_compaction_persisted_just_before_the_claim_is_not_repeated(redis, db):
    history = _history(cm.SUMMARIZE_THRESHOLD + 2)
    through = len(history) - cm.KEEP_RECENT - 1
    db["state"] = _state_covering(history, through)
    llm = _Compactor()

    result = await cm.manage_conversation_memory(llm, list(history), conversation_id=CONV)

    assert llm.calls == []
    assert result[1:] == history[through + 1 :]
    assert redis.data == {}


@pytest.mark.asyncio
async def test_a_precompute_leaves_a_claimed_compaction_to_its_holder(redis, db, monkeypatch):
    loads: list[str] = []

    async def _load(session, conversation_id, limit=50):
        loads.append(str(conversation_id))
        return _history(cm.SUMMARIZE_THRESHOLD + 2)

    @contextlib.asynccontextmanager
    async def _session():
        yield object()

    monkeypatch.setattr("app.channels.router.get_conversation_history", _load)
    monkeypatch.setattr("app.core.database.async_session", _session)
    redis.data[cm._claim_key(CONV)] = "other-process"
    llm = _Compactor()

    task = cm.precompute_compaction(
        llm,
        conversation_id=CONV,
        agent_id=None,
        user_id=None,
        memory_persistent_enabled=False,
        model=None,
    )

    assert await task is None
    assert llm.calls == []
    assert loads == []
    assert redis.data == {cm._claim_key(CONV): "other-process"}


@pytest.mark.asyncio
async def test_the_claim_is_held_while_compacting_and_released_after(redis, db):
    history = _history(cm.SUMMARIZE_THRESHOLD + 2)
    llm = _Compactor()
    llm.release.clear()

    turn = asyncio.create_task(cm.manage_conversation_memory(llm, list(history), conversation_id=CONV))
    for _ in range(50):
        if llm.calls:
            break
        await asyncio.sleep(0.01)
    assert cm._claim_key(CONV) in redis.data
    assert len(cm._claim_renewers) == 1

    llm.release.set()
    result = await turn

    assert len(llm.calls) == 1
    assert cm._is_compaction_message(result[0])
    assert len(db["persisted"]) == 1
    assert redis.data == {}
    assert cm._claim_renewers == {}


@pytest.mark.asyncio
async def test_a_failed_compaction_releases_its_claim(redis, db, monkeypatch):
    async def _boom(*args, **kwargs):
        raise RuntimeError("compactor exploded")

    monkeypatch.setattr(cm, "_compact_history", _boom)

    with pytest.raises(RuntimeError):
        await cm.manage_conversation_memory(
            _Compactor(), _history(cm.SUMMARIZE_THRESHOLD + 2), conversation_id=CONV
        )
    assert redis.data == {}
    assert cm._claim_renewers == {}


@pytest.mark.asyncio
async def test_a_turn_cancelled_before_it_compacts_releases_its_claim(redis, monkeypatch):
    """Stopped on "Summarizing" or its socket closed: a claim left behind is
    renewed for as long as the process lives, and every later compaction of the
    conversation waits on it."""
    reading = asyncio.Event()

    async def _load(conversation_id):
        reading.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(cm, "_load_stored_compaction", _load)
    turn = asyncio.create_task(
        cm.manage_conversation_memory(
            _Compactor(), _history(cm.SUMMARIZE_THRESHOLD + 2), conversation_id=CONV
        )
    )
    await asyncio.wait_for(reading.wait(), timeout=5)
    assert cm._claim_key(CONV) in redis.data

    turn.cancel()
    with pytest.raises(asyncio.CancelledError):
        await turn

    assert redis.data == {}
    assert cm._claim_renewers == {}


@pytest.mark.asyncio
async def test_a_claim_taken_as_its_caller_is_cancelled_is_taken_back(redis):
    """Cancelled while its SET is in flight: the claim may land after the
    caller is gone, with nobody left holding its token."""
    landed = asyncio.Event()
    answer = asyncio.Event()
    real_set = redis.set

    async def _slow_set(key, value, nx=False, ex=None):
        result = await real_set(key, value, nx=nx, ex=ex)
        landed.set()
        await answer.wait()
        return result

    redis.set = _slow_set
    claim = asyncio.create_task(cm._claim_compaction(CONV))
    await asyncio.wait_for(landed.wait(), timeout=5)
    claim.cancel()
    with pytest.raises(asyncio.CancelledError):
        await claim
    answer.set()
    await asyncio.gather(*list(cm._orphan_claim_releases))

    assert redis.data == {}
    assert cm._claim_renewers == {}


@pytest.mark.asyncio
async def test_a_claim_is_released_only_by_its_owner(redis):
    held, token = await cm._claim_compaction(CONV)
    assert held is False and token

    redis.data[cm._claim_key(CONV)] = "someone-else"
    await cm._release_compaction_claim(CONV, token)

    assert redis.data == {cm._claim_key(CONV): "someone-else"}


@pytest.mark.asyncio
async def test_a_held_claim_is_renewed_while_the_holder_runs(redis, monkeypatch):
    # Awaits the renewal itself: a fixed sleep raced it on a loaded worker.
    renewed = asyncio.Event()
    real_eval = redis.eval

    async def _eval(script, numkeys, key, token, *args):
        result = await real_eval(script, numkeys, key, token, *args)
        if args:
            renewed.set()
        return result

    redis.eval = _eval
    monkeypatch.setattr(cm, "_CLAIM_RENEW_SECS", 0.01)
    _, token = await cm._claim_compaction(CONV)
    await asyncio.wait_for(renewed.wait(), timeout=5)
    await cm._release_compaction_claim(CONV, token)

    assert (cm._claim_key(CONV), cm._CLAIM_TTL_SECS) in redis.expiries
    assert redis.data == {}


@pytest.mark.asyncio
async def test_a_claim_is_renewed_no_longer_than_a_turn_waits_on_it(redis, monkeypatch):
    """A holder stuck past it, or one whose release was lost, stops holding
    the conversation up: its claim then expires on its own."""
    # The renewer's clock is a fake one that each renewal moves one second on,
    # so the count does not depend on how loaded the worker is.
    clock = [0.0]
    real_eval = redis.eval

    async def _eval(script, numkeys, key, token, *args):
        result = await real_eval(script, numkeys, key, token, *args)
        if args:
            clock[0] += 1
        return result

    redis.eval = _eval
    monkeypatch.setattr(cm, "time", types.SimpleNamespace(monotonic=lambda: clock[0]))
    monkeypatch.setattr(cm, "_CLAIM_RENEW_SECS", 0)
    monkeypatch.setattr(cm, "_claim_wait_cap_secs", lambda: 3)
    _, token = await cm._claim_compaction(CONV)

    await asyncio.wait_for(cm._claim_renewers[token], timeout=5)

    assert redis.expiries == [(cm._claim_key(CONV), cm._CLAIM_TTL_SECS)] * 3
    await cm._release_compaction_claim(CONV, token)
    assert redis.data == {}


@pytest.mark.asyncio
async def test_without_redis_a_turn_compacts_on_its_own(db, monkeypatch):
    async def _client():
        return _DeadRedis()

    monkeypatch.setattr(cm, "_claim_redis", _client)
    llm = _Compactor()

    result = await cm.manage_conversation_memory(
        llm, _history(cm.SUMMARIZE_THRESHOLD + 2), conversation_id=CONV
    )

    assert len(llm.calls) == 1
    assert cm._is_compaction_message(result[0])
    assert cm._claim_renewers == {}
