"""A work item is dispatched to its inbox when it becomes dispatchable, not at the next tick.

Synthetic fixtures only: an in-memory Redis stand-in (SET NX EX, GETDEL,
EXPIRE, the compare-and-delete unlock, PUBLISH) and a counting pass.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from app.services import work_item_dispatch_wake as wake


class _FakeRedis:
    def __init__(self) -> None:
        self.store: dict[str, str] = {}
        self.published: list[tuple[str, str]] = []

    async def set(self, key: str, value: str, nx: bool = False, ex: int | None = None) -> bool | None:
        if nx and key in self.store:
            return None
        self.store[key] = value
        return True

    async def getdel(self, key: str) -> str | None:
        return self.store.pop(key, None)

    async def eval(self, script: str, numkeys: int, key: str, token: str, *args: Any) -> int:
        if self.store.get(key) != token:
            return 0
        if "'expire'" in script:
            return 1
        del self.store[key]
        return 1

    async def publish(self, channel: str, message: str) -> int:
        self.published.append((channel, message))
        return 1


@pytest.fixture
def redis(monkeypatch) -> _FakeRedis:
    fake = _FakeRedis()

    async def _client() -> _FakeRedis:
        return fake

    monkeypatch.setattr(wake, "_redis", _client)
    return fake


async def test_a_nudge_sets_pending_and_publishes(redis):
    await wake.notify_dispatch()
    assert redis.store[wake.PENDING_KEY] == "1"
    assert redis.published == [(wake.DISPATCH_WAKE_CHANNEL, "1")]


async def test_a_nudge_never_raises_when_redis_is_down(monkeypatch):
    async def _down() -> Any:
        raise ConnectionError("redis down")

    monkeypatch.setattr(wake, "_redis", _down)
    await wake.notify_dispatch()


async def test_a_pending_nudge_runs_one_pass_and_frees_the_lock(redis):
    passes: list[int] = []

    async def _dispatch() -> int:
        passes.append(1)
        return 2

    redis.store[wake.PENDING_KEY] = "1"
    assert await wake.dispatch_pass(dispatch=_dispatch) == 2
    assert passes == [1]
    assert wake.LOCK_KEY not in redis.store
    assert wake.PENDING_KEY not in redis.store


async def test_no_pending_nudge_runs_nothing(redis):
    async def _dispatch() -> int:
        raise AssertionError("no pass without a nudge")

    assert await wake.dispatch_pass(dispatch=_dispatch) == 0
    assert wake.LOCK_KEY not in redis.store


async def test_a_nudge_during_a_pass_runs_one_more_pass_by_the_holder(redis):
    passes: list[int] = []

    async def _dispatch() -> int:
        passes.append(1)
        if len(passes) == 1:
            # Another process creates an item while this pass runs: it nudges
            # but cannot take the lock, so the holder must run again.
            await wake.notify_dispatch()
            assert await wake.dispatch_pass(dispatch=_dispatch) == 0
        return 1

    redis.store[wake.PENDING_KEY] = "1"
    assert await wake.dispatch_pass(dispatch=_dispatch) == 2
    assert passes == [1, 1]


async def test_a_holder_runs_at_most_max_passes(redis):
    passes: list[int] = []

    async def _dispatch() -> int:
        passes.append(1)
        redis.store[wake.PENDING_KEY] = "1"
        return 0

    redis.store[wake.PENDING_KEY] = "1"
    await wake.dispatch_pass(dispatch=_dispatch)
    assert len(passes) == wake.MAX_PASSES
    # What is left is the next nudge's or the tick's: the nudge stays pending.
    assert redis.store.get(wake.PENDING_KEY) == "1"


async def test_a_failing_pass_frees_the_lock(redis):
    async def _dispatch() -> int:
        raise RuntimeError("database gone")

    redis.store[wake.PENDING_KEY] = "1"
    assert await wake.dispatch_pass(dispatch=_dispatch) == 0
    assert wake.LOCK_KEY not in redis.store


async def test_a_lock_another_process_took_after_expiry_is_never_released(redis):
    async def _dispatch() -> int:
        redis.store[wake.LOCK_KEY] = "someone-else"
        return 0

    redis.store[wake.PENDING_KEY] = "1"
    await wake.dispatch_pass(dispatch=_dispatch)
    assert redis.store[wake.LOCK_KEY] == "someone-else"


async def test_a_holder_that_lost_its_lock_stops_and_leaves_the_nudge(redis):
    """No renewal of a lock another process owns, and no pending nudge consumed for it."""
    passes: list[int] = []

    async def _dispatch() -> int:
        passes.append(1)
        redis.store[wake.LOCK_KEY] = "someone-else"
        redis.store[wake.PENDING_KEY] = "1"
        return 0

    redis.store[wake.PENDING_KEY] = "1"
    await wake.dispatch_pass(dispatch=_dispatch)
    assert passes == [1]
    assert redis.store[wake.LOCK_KEY] == "someone-else"
    assert redis.store[wake.PENDING_KEY] == "1"


class _Session:
    def __init__(self) -> None:
        self.rolled_back = False

    async def __aenter__(self) -> _Session:
        return self

    async def __aexit__(self, *exc: Any) -> None:
        return None

    async def rollback(self) -> None:
        self.rolled_back = True


@pytest.fixture
def consumers(monkeypatch):
    """The two work-item consumers of the tick, the AI gate and the session, stubbed."""
    import app.core.database as database
    import app.services.agentic_gate as gate
    import app.services.autonomous as autonomous
    import app.services.flow_triggers as flow_triggers

    calls: dict[str, Any] = {"paths": [], "flows": 0, "session": _Session(), "refusal": None, "flow_error": None}

    async def _assignments(db: Any, *, path: str = "tick") -> int:
        calls["paths"].append(path)
        return 1

    async def _flows(db: Any) -> int:
        calls["flows"] += 1
        if calls["flow_error"] is not None:
            raise calls["flow_error"]
        return 2

    async def _refusal(db: Any) -> Any:
        return calls["refusal"]

    monkeypatch.setattr(autonomous, "_process_work_item_assignments", _assignments)
    monkeypatch.setattr(flow_triggers, "process_work_item_flows", _flows)
    monkeypatch.setattr(gate, "agentic_refusal", _refusal)
    monkeypatch.setattr(database, "async_session", lambda: calls["session"])
    return calls


async def test_the_wake_pass_serves_inboxes_and_flow_work_item_triggers(consumers):
    """Flow trigger.work_item nodes are dispatched on a wake too, not only at the tick."""
    assert await wake._default_dispatch() == 3
    assert consumers["paths"] == ["wake"]
    assert consumers["flows"] == 1


async def test_a_failing_flow_consumer_keeps_the_inbox_dispatches(consumers):
    consumers["flow_error"] = RuntimeError("flow table gone")
    assert await wake._default_dispatch() == 1
    assert consumers["session"].rolled_back


async def test_ai_off_holds_the_inboxes_but_not_the_deterministic_flows(consumers):
    """As in the tick: the inbox consumer is gated, the flow consumer holds back AI graphs itself."""
    consumers["refusal"] = "AI is switched off"
    assert await wake._default_dispatch() == 2
    assert consumers["paths"] == []
    assert consumers["flows"] == 1


def test_only_a_status_back_to_open_reopens():
    assert wake.reopens([("work_item.status_changed", {"status": "open"})])
    assert wake.reopens([("work_item.claim_released", {"status": "open"})])
    assert not wake.reopens([("work_item.status_changed", {"status": "done"})])
    assert not wake.reopens([("work_item.note_added", {"status": "open"})])
    assert not wake.reopens([])


def test_the_lag_is_measured_from_the_last_activity():
    from prometheus_client import REGISTRY

    def _sum() -> float:
        return REGISTRY.get_sample_value("xtmone_work_item_dispatch_lag_seconds_sum", {"path": "wake"}) or 0.0

    now = datetime(2026, 11, 1, 23, 0, tzinfo=UTC)
    before = _sum()
    wake.observe_lag({"last_activity_at": (now - timedelta(seconds=3)).isoformat()}, "wake", now=now)
    assert _sum() == pytest.approx(before + 3.0)
    wake.observe_lag({"created_at": "not a date"}, "wake", now=now)
    wake.observe_lag({}, "wake", now=now)
    assert _sum() == pytest.approx(before + 3.0)


async def test_the_loop_runs_a_pass_at_start_and_on_each_nudge(monkeypatch, redis):
    """A nudge published before the subscription is still served; then one pass per burst."""

    class _PubSub:
        def __init__(self) -> None:
            self.messages: list[dict | None] = [{"data": "1"}, {"data": "1"}, None]
            self.reads = 0

        async def get_message(self, ignore_subscribe_messages: bool = True, timeout: float = 0) -> dict | None:
            self.reads += 1
            if self.messages:
                return self.messages.pop(0)
            raise asyncio.CancelledError

    pubsub = _PubSub()

    async def _subscribe() -> tuple[_PubSub, None]:
        return pubsub, None

    calls: list[int] = []

    async def _pass(*, dispatch: Any = None) -> int:
        calls.append(1)
        return 0

    monkeypatch.setattr(wake, "_subscribe", _subscribe)
    monkeypatch.setattr(wake, "dispatch_pass", _pass)
    with pytest.raises(asyncio.CancelledError):
        await wake.run_wake_loop()
    # One pass at start, one for the burst of two buffered nudges.
    assert calls == [1, 1]
