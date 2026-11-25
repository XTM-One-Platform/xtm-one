"""A terminal write is refused over a note its run has not seen (#2266).

A note relayed to the run holding an item stays pending until the loop
injects it or the run reads the item again; while it is pending, a completion
or a terminal status write of that run is refused with the note named. Before
#2266 the run finalised the item over a "do not close" written a second before.
Redis is a fake; nothing reaches the network or the database.
"""

from __future__ import annotations

import json
import uuid

import pytest

from app.services import work_item as work_item_mod
from app.services import work_item_note_steering as steering

ITEM = str(uuid.uuid4())
RUN = str(uuid.uuid4())
OTHER_RUN = str(uuid.uuid4())


class _Redis:
    def __init__(self):
        self.values: dict[str, str] = {}
        self.lists: dict[str, list[str]] = {}
        self.hashes: dict[str, dict[str, str]] = {}

    async def set(self, key, value, ex=None, nx=False):
        self.values[key] = value
        return True

    async def get(self, key):
        return self.values.get(key)

    async def delete(self, *keys):
        gone = 0
        for key in keys:
            for store in (self.values, self.lists, self.hashes):
                if store.pop(key, None) is not None:
                    gone += 1
        return gone

    async def expire(self, key, seconds):
        return True

    async def rpush(self, key, value):
        self.lists.setdefault(key, []).append(value)
        return len(self.lists[key])

    async def lpop(self, key):
        items = self.lists.get(key) or []
        return items.pop(0) if items else None

    async def lrem(self, key, count, value):
        items = self.lists.get(key) or []
        if value in items:
            items.remove(value)
            return 1
        return 0

    async def hset(self, key, field, value):
        self.hashes.setdefault(key, {})[field] = value
        return 1

    async def hdel(self, key, *fields):
        stored = self.hashes.get(key) or {}
        return sum(1 for field in fields if stored.pop(field, None) is not None)

    async def hgetall(self, key):
        return dict(self.hashes.get(key) or {})


@pytest.fixture
def redis(monkeypatch):
    store = _Redis()

    async def _client():
        return store

    monkeypatch.setattr(steering, "redis_client", _client)
    monkeypatch.setattr(steering, "POLL_SECONDS", 0.01)
    return store


async def _held(redis: _Redis, run: str = RUN, item: str = ITEM) -> None:
    await redis.set(steering.live_key(run), json.dumps({"run": run, "item": item}))


async def test_a_relayed_note_is_pending_for_its_holder(redis):
    await _held(redis)
    assert await steering.offer(ITEM, RUN, "do not close", writer_run_ids=[OTHER_RUN])
    assert await steering.unseen_notes([RUN], ITEM) == ["do not close"]
    assert await steering.unseen_notes([OTHER_RUN], ITEM) == []
    assert await steering.unseen_notes([RUN], str(uuid.uuid4())) == []


async def test_the_holder_own_note_is_never_pending(redis):
    await _held(redis)
    assert not await steering.offer(ITEM, RUN, "my own note", writer_run_ids=[RUN])
    assert await steering.unseen_notes([RUN], ITEM) == []


async def test_completion_refused_over_an_unseen_note_then_passes_after_a_read(redis):
    await _held(redis)
    await steering.offer(ITEM, RUN, "do not close, the partner still waits", writer_run_ids=[OTHER_RUN])

    with pytest.raises(ValueError) as refused:
        await work_item_mod._refuse_terminal_over_unseen_notes(ITEM, "cancelled", [RUN])
    assert "do not close, the partner still waits" in str(refused.value)
    assert "get_work_item" in str(refused.value)

    # The run reads the item again: the note is seen, the completion goes.
    await steering.mark_seen([RUN], item_id=ITEM)
    await work_item_mod._refuse_terminal_over_unseen_notes(ITEM, "cancelled", [RUN])


async def test_a_note_the_loop_injected_is_seen(redis):
    await _held(redis)
    queue = steering.HeldItemNoteQueue([RUN])
    await steering.offer(ITEM, RUN, "COLLISION: #12 already open", writer_run_ids=[OTHER_RUN])
    await _with_queue(queue).pump()
    items = [queue.get_nowait() for _ in range(queue.qsize())]
    assert len(items) == 1
    assert await steering.unseen_notes([RUN], ITEM) == ["COLLISION: #12 already open"]
    await queue.admit(items)
    assert await steering.unseen_notes([RUN], ITEM) == []
    await work_item_mod._refuse_terminal_over_unseen_notes(ITEM, "done", [RUN])


def _with_queue(queue: steering.HeldItemNoteQueue) -> steering._Session:
    session = steering._Session([RUN], ITEM)
    session.queue = queue
    return session


async def test_non_terminal_writes_and_people_are_not_held(redis):
    await _held(redis)
    await steering.offer(ITEM, RUN, "do not close", writer_run_ids=[OTHER_RUN])
    # A note or a park is not a finish: it goes.
    await work_item_mod._refuse_terminal_over_unseen_notes(ITEM, "blocked", [RUN])
    await work_item_mod._refuse_terminal_over_unseen_notes(ITEM, None, [RUN])
    # A person (no run ids) completes as before.
    await work_item_mod._refuse_terminal_over_unseen_notes(ITEM, "done", None)


async def test_the_store_down_never_holds_a_write(monkeypatch):
    async def _down():
        raise ConnectionError("redis down")

    monkeypatch.setattr(steering, "redis_client", _down)
    assert await steering.unseen_notes([RUN], ITEM) == []
    await work_item_mod._refuse_terminal_over_unseen_notes(ITEM, "done", [RUN])
    await steering.mark_seen([RUN], item_id=ITEM)


async def test_closing_the_session_drops_pending_notes(redis):
    queue_session = steering._Session([RUN], ITEM)
    await queue_session.pump()
    await steering.offer(ITEM, RUN, "late note", writer_run_ids=[OTHER_RUN])
    assert await steering.unseen_notes([RUN], ITEM) == ["late note"]
    await queue_session.close()
    assert await steering.unseen_notes([RUN], ITEM) == []
