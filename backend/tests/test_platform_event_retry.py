"""A platform event delivery that errors is kept and delivered again, never lost (#2101).

``platform_events._dispatch_assignments`` hands a delivery that raises (the
DB pool exhausted, a dropped connection) or unreadable subscriptions to
``platform_event_retry``; the scheduler tick delivers them again with
back-off; an abandoned delivery is counted on the assignment with its reason.
"""

from __future__ import annotations

import json
import time
import uuid
from types import SimpleNamespace
from typing import Any

import pytest

from app.services import platform_event_retry as retry
from app.services import platform_events as pe

EVENT = "work_item.note_added"


class _FakeRedis:
    def __init__(self) -> None:
        self.kv: dict[str, str] = {}
        self.zset: dict[str, dict[str, float]] = {}

    async def set(self, key: str, value: str, ex: int | None = None, nx: bool = False) -> bool:
        self.kv[key] = value
        return True

    async def get(self, key: str) -> str | None:
        return self.kv.get(key)

    async def delete(self, key: str) -> int:
        return 1 if self.kv.pop(key, None) is not None else 0

    async def zadd(self, key: str, mapping: dict[str, float]) -> int:
        self.zset.setdefault(key, {}).update(mapping)
        return len(mapping)

    async def zrangebyscore(self, key: str, lo: float, hi: float, start: int = 0, num: int | None = None) -> list[str]:
        members = sorted((s, m) for m, s in self.zset.get(key, {}).items() if lo <= s <= hi)
        return [m for _, m in members][start : (start + num) if num else None]

    async def zrem(self, key: str, member: str) -> int:
        return 1 if self.zset.get(key, {}).pop(member, None) is not None else 0

    def make_due(self) -> None:
        for members in self.zset.values():
            for m in members:
                members[m] = time.time() - 1


@pytest.fixture
def world(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """Two subscribers of one event; the delivery outcome of each is scripted."""
    import app.core.database as database

    redis = _FakeRedis()

    async def fake_redis() -> _FakeRedis:
        return redis

    class _Session:
        async def __aenter__(self) -> _Session:
            if state.subscriptions_fail > 0:
                state.subscriptions_fail -= 1
                raise TimeoutError("QueuePool limit reached, connection timed out")
            return self

        async def __aexit__(self, *exc: object) -> bool:
            return False

    a, b = uuid.uuid4(), uuid.uuid4()
    agent = uuid.uuid4()
    subs = [
        (
            pe.Subscription(assignment_id=a, agent_id=agent, events=(EVENT,), filter={}, scope_all=True),
            object(),
            object(),
        ),
        (
            pe.Subscription(assignment_id=b, agent_id=agent, events=(EVENT,), filter={}, scope_all=True),
            object(),
            object(),
        ),
    ]
    state = SimpleNamespace(a=a, b=b, fail={b: 1}, subscriptions_fail=0, runs=[], counted=[], reasons=[], redis=redis)

    async def subscriptions(db: Any, name: str) -> list[tuple[pe.Subscription, Any, Any]]:
        return subs

    async def deliver(name: str, payload: Any, sub: pe.Subscription, assignment: Any, agent_row: Any) -> str:
        if state.fail.get(sub.assignment_id, 0) > 0:
            state.fail[sub.assignment_id] -= 1
            raise TimeoutError("QueuePool limit reached, connection timed out")
        state.runs.append(sub.assignment_id)
        return f"ok run r-{sub.assignment_id}"

    async def readable(sub: Any, payload: Any, cache: Any) -> bool:
        return True

    async def counted(assignment_id: Any, count: int) -> None:
        state.counted.append((assignment_id, count))

    async def reasons(assignment_id: Any, values: Any) -> None:
        state.reasons.append((assignment_id, dict(values)))

    from app.services import integration_event_filters

    monkeypatch.setattr(retry, "_redis", fake_redis)
    monkeypatch.setattr(database, "async_session", _Session)
    monkeypatch.setattr(pe, "_subscriptions", subscriptions)
    monkeypatch.setattr(pe, "_deliver", deliver)
    monkeypatch.setattr(pe, "can_read_workspace", readable)
    monkeypatch.setattr(pe, "can_read_object", readable)
    monkeypatch.setattr(integration_event_filters, "record_filtered_events", counted)
    monkeypatch.setattr(integration_event_filters, "record_filter_reasons", reasons)
    return state


def _payload() -> dict[str, Any]:
    return {"event_id": "e1", "object_type": "work_item", "object_id": "w1"}


@pytest.mark.asyncio
async def test_a_failed_delivery_is_kept_and_delivered_again(world: SimpleNamespace) -> None:
    outcomes = await pe._dispatch_assignments(EVENT, _payload())
    # The other subscriber got its run; the failing one is kept, not lost.
    assert outcomes == [f"ok run r-{world.a}"] and world.runs == [world.a]
    stored = list(world.redis.zset[retry.PENDING_KEY])
    assert stored == [retry.entry_id(EVENT, "e1", world.b)]
    entry = json.loads(world.redis.kv[f"{retry.ENTRY_KEY_PREFIX}{stored[0]}"])
    assert entry["assignment_id"] == str(world.b) and entry["attempt"] == 1 and "QueuePool" in entry["error"]
    # Not due yet: nothing delivered.
    assert await retry.process_due() == 0
    world.redis.make_due()
    assert await retry.process_due() == 1
    # Only the failing subscriber is delivered again: no second run for the other.
    assert world.runs == [world.a, world.b]
    assert world.redis.zset[retry.PENDING_KEY] == {} and world.counted == []


@pytest.mark.asyncio
async def test_unreadable_subscriptions_are_tried_again_not_read_as_no_subscriber(world: SimpleNamespace) -> None:
    world.fail = {}
    world.subscriptions_fail = 1
    assert await pe._dispatch_assignments(EVENT, _payload()) == []
    assert list(world.redis.zset[retry.PENDING_KEY]) == [retry.entry_id(EVENT, "e1", None)]
    world.redis.make_due()
    assert await retry.process_due() == 2
    assert sorted(map(str, world.runs)) == sorted([str(world.a), str(world.b)])


@pytest.mark.asyncio
async def test_a_delivery_failing_every_attempt_is_abandoned_and_counted(world: SimpleNamespace) -> None:
    world.fail = {world.b: retry.MAX_ATTEMPTS + 5}
    await pe._dispatch_assignments(EVENT, _payload())
    for _ in range(retry.MAX_ATTEMPTS):
        world.redis.make_due()
        await retry.process_due()
    assert world.redis.zset[retry.PENDING_KEY] == {}
    assert [(str(a), n) for a, n in world.counted] == [(str(world.b), 1)]
    ((assignment_id, reasons),) = world.reasons
    assert str(assignment_id) == str(world.b)
    (reason,) = reasons
    assert reason.startswith(retry.ABANDONED_REASON_PREFIX) and "QueuePool" in reason
    assert world.runs == [world.a]


@pytest.mark.asyncio
async def test_a_delivery_that_cannot_be_kept_is_counted_at_once(
    world: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def no_redis() -> None:
        return None

    monkeypatch.setattr(retry, "_redis", no_redis)
    await pe._dispatch_assignments(EVENT, _payload())
    assert [(str(a), n) for a, n in world.counted] == [(str(world.b), 1)]
    (reason,) = world.reasons[0][1]
    assert reason.startswith(retry.ABANDONED_REASON_PREFIX)


def test_the_back_off_grows_and_is_bounded() -> None:
    delays = list(retry.RETRY_DELAYS_SECONDS)
    assert delays == sorted(delays)
    assert len(delays) == retry.MAX_ATTEMPTS


def test_abandoned_reason_is_one_bounded_line() -> None:
    reason = retry.abandoned_reason("line one\n  line two " + "x" * 500)
    assert "\n" not in reason and len(reason) < 140
