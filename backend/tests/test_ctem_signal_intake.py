# SPDX-License-Identifier: Apache-2.0

"""#2584: the queued intake of the platform signal source webhook.

A fake Redis runs the two scripts' semantics (store and queue once, pop and
take in one step), so the queue, its coalescing and the drain are checked
without a server.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from app.services import ctem_signal_intake as intake
from app.services.ctem_signal_listeners import OPENCTI_EXTENSION


class _FakeRedis:
    def __init__(self) -> None:
        self.queue: list[str] = []
        self.entries: dict[str, str] = {}

    async def eval(self, script: str, numkeys: int, *args: Any) -> Any:
        del numkeys
        if script == intake._ENQUEUE_LUA:
            _queue, _entries, key, value = args
            added = 0 if key in self.entries else 1
            self.entries[key] = value
            if added:
                self.queue.append(key)
            return [added, len(self.queue)]
        if script == intake._POP_LUA:
            if not self.queue:
                return None
            key = self.queue.pop(0)
            return self.entries.pop(key, None)
        raise AssertionError("unknown script")

    async def llen(self, _key: str) -> int:
        return len(self.queue)


@pytest.fixture
def fake_redis(monkeypatch: pytest.MonkeyPatch) -> _FakeRedis:
    redis = _FakeRedis()

    async def _client() -> _FakeRedis:
        return redis

    monkeypatch.setattr("app.core.redis.redis_client", _client)
    return redis


def _vulnerability(object_id: str, description: str, *, event_key: str = "update") -> intake.Delivery:
    return intake.Delivery(
        provider="opencti",
        event_key=event_key,
        payload={
            "data": {
                "type": "vulnerability",
                "id": "vulnerability--stix",
                "description": description,
                "extensions": {OPENCTI_EXTENSION: {"id": object_id}},
            }
        },
        verified=True,
        integration_id="11111111-1111-1111-1111-111111111111",
        source_id="22222222-2222-2222-2222-222222222222",
    )


def test_updates_of_one_object_share_one_entry_key() -> None:
    first = intake.entry_key(_vulnerability("obj-1", "a"))
    second = intake.entry_key(_vulnerability("obj-1", "b"))
    other = intake.entry_key(_vulnerability("obj-2", "a"))
    assert first == second
    assert first != other


def test_a_delete_or_an_openaev_delivery_is_never_folded() -> None:
    deleted = _vulnerability("obj-1", "a", event_key="delete")
    assert intake.entry_key(deleted) != intake.entry_key(deleted)
    openaev = intake.Delivery("openaev", "scenario", {"data": {}}, True, "i", "s")
    assert intake.entry_key(openaev) != intake.entry_key(openaev)


def test_a_delivery_round_trips_through_its_stored_form() -> None:
    delivery = _vulnerability("obj-1", "a")
    assert intake.Delivery.from_json(delivery.as_json()) == delivery
    assert intake.Delivery.from_json("not json") is None


def test_repeated_updates_coalesce_into_the_latest(fake_redis: _FakeRedis) -> None:
    async def scenario() -> None:
        assert await intake.enqueue(_vulnerability("obj-1", "first"))
        assert await intake.enqueue(_vulnerability("obj-1", "latest"))
        assert await intake.enqueue(_vulnerability("obj-2", "other"))
        assert await intake.depth() == 2
        popped = await intake._pop()
        assert isinstance(popped, intake.Delivery)
        assert popped.payload["data"]["description"] == "latest"

    asyncio.run(scenario())


def test_the_drain_records_every_queued_delivery_on_the_intake_session(
    fake_redis: _FakeRedis, monkeypatch: pytest.MonkeyPatch
) -> None:
    recorded: list[tuple[str, Any]] = []

    async def _record(delivery: intake.Delivery, *, session_factory: Any = None) -> bool:
        recorded.append((delivery.payload["data"]["description"], session_factory))
        return True

    monkeypatch.setattr(intake, "record", _record)
    from app.core import database

    async def scenario() -> None:
        for index in range(5):
            await intake.enqueue(_vulnerability(f"obj-{index}", f"d{index}"))
        task = asyncio.create_task(intake.run_drain_loop(concurrency=2, idle_seconds=0.01))
        for _ in range(200):
            if len(recorded) == 5:
                break
            await asyncio.sleep(0.01)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())
    assert sorted(description for description, _ in recorded) == [f"d{index}" for index in range(5)]
    assert all(factory is database.intake_session for _, factory in recorded)
    assert fake_redis.queue == []


def test_an_unreachable_queue_answers_false(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _client() -> Any:
        raise ConnectionError("down")

    monkeypatch.setattr("app.core.redis.redis_client", _client)
    assert asyncio.run(intake.enqueue(_vulnerability("obj-1", "a"))) is False
