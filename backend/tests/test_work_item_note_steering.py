"""A note written on a held work item reaches the run that holds it.

The run triggered by a work item opens a note session around its agent loop;
a note another writer adds to that item is offered to it, moved into the
loop's steering queue and injected at the next iteration as a steer marked as
a note on its held item. An unheld item, a note the holder wrote itself, and
a closed session relay nothing. Redis is a fake; nothing reaches the network.
"""

from __future__ import annotations

import asyncio
import uuid

import pytest

from app.services import work_item_note_steering as steering
from app.tools.agent_loop import SteeringMessage, SteeringQueue, drain_steering_queue

ITEM = str(uuid.uuid4())
RUN = str(uuid.uuid4())
OTHER_RUN = str(uuid.uuid4())


class _Redis:
    def __init__(self, *, fail: bool = False):
        self.values: dict[str, str] = {}
        self.lists: dict[str, list[str]] = {}
        self.fail = fail

    def _check(self):
        if self.fail:
            raise ConnectionError("redis down")

    async def set(self, key, value, ex=None, nx=False):
        self._check()
        self.values[key] = value
        return True

    async def get(self, key):
        self._check()
        return self.values.get(key)

    async def delete(self, *keys):
        self._check()
        return sum(1 for key in keys if self.values.pop(key, None) is not None or self.lists.pop(key, None) is not None)

    async def expire(self, key, seconds):
        self._check()
        return True

    async def rpush(self, key, value):
        self._check()
        self.lists.setdefault(key, []).append(value)
        return len(self.lists[key])

    async def lpop(self, key):
        self._check()
        items = self.lists.get(key) or []
        return items.pop(0) if items else None

    async def lrem(self, key, count, value):
        self._check()
        items = self.lists.get(key) or []
        if value in items:
            items.remove(value)
            return 1
        return 0


@pytest.fixture
def redis(monkeypatch):
    store = _Redis()

    async def _client():
        return store

    monkeypatch.setattr(steering, "redis_client", _client)
    monkeypatch.setattr(steering, "POLL_SECONDS", 0.01)
    return store


def _payload(item_id: str = ITEM) -> dict:
    return {"trigger_type": "work_item", "work_item_id": item_id}


async def _wait_for_items(queue, count: int) -> list:
    for _ in range(200):
        if queue.qsize() >= count:
            break
        await asyncio.sleep(0.01)
    return drain_steering_queue(queue)


@pytest.mark.asyncio
async def test_a_note_on_the_held_item_reaches_the_live_holder(redis):
    async with steering.session(RUN, _payload()) as queue:
        assert isinstance(queue, SteeringQueue)
        assert queue.interactive is False
        pushed = await steering.offer(
            ITEM, RUN, "COLLISION: #10328 exists, push nothing", title="Fix the finalizer", writer_run_ids=[OTHER_RUN]
        )
        assert pushed is True
        items = await _wait_for_items(queue, 1)
    assert len(items) == 1
    assert isinstance(items[0], SteeringMessage)
    assert "COLLISION: #10328 exists, push nothing" in items[0].text
    assert "New note on the work item you hold" in items[0].text
    assert ITEM in items[0].text and "Fix the finalizer" in items[0].text
    # Closed: marker and inbox gone.
    assert redis.values == {} and redis.lists == {}


@pytest.mark.asyncio
async def test_a_note_written_by_the_holder_itself_is_not_relayed(redis):
    async with steering.session(RUN, _payload()) as queue:
        assert await steering.offer(ITEM, RUN, "started", writer_run_ids=[RUN]) is False
        await asyncio.sleep(0.03)
        assert drain_steering_queue(queue) == []


@pytest.mark.asyncio
async def test_no_live_session_relays_nothing(redis):
    assert await steering.offer(ITEM, RUN, "a note", writer_run_ids=[OTHER_RUN]) is False
    assert redis.lists == {}


@pytest.mark.asyncio
async def test_a_note_on_another_item_is_not_pushed_to_the_holder_of_this_one(redis):
    other_item = str(uuid.uuid4())
    async with steering.session(RUN, _payload()) as queue:
        assert await steering.offer(other_item, RUN, "a note", writer_run_ids=[OTHER_RUN]) is False
        await asyncio.sleep(0.03)
        assert drain_steering_queue(queue) == []


@pytest.mark.asyncio
async def test_a_run_not_triggered_by_a_work_item_opens_no_session(redis):
    async with steering.session(RUN, {"trigger_type": "schedule"}) as queue:
        assert queue is None
    async with steering.session(None, _payload()) as queue:
        assert queue is None
    assert redis.values == {}


@pytest.mark.asyncio
async def test_an_unreachable_store_relays_nothing_and_never_raises(monkeypatch):
    store = _Redis(fail=True)

    async def _client():
        return store

    monkeypatch.setattr(steering, "redis_client", _client)
    async with steering.session(RUN, _payload()) as queue:
        assert queue is None
    assert await steering.offer(ITEM, RUN, "a note", writer_run_ids=[OTHER_RUN]) is False


@pytest.mark.asyncio
async def test_the_held_item_is_read_from_the_work_item_payload_too(redis):
    payload = {"trigger_type": "work_item", "work_item": {"id": ITEM}}
    assert steering.held_item_of(payload) == ITEM
    async with steering.session(RUN, payload) as queue:
        assert queue is not None
        assert await steering.offer(ITEM, RUN, "a note", writer_run_ids=[OTHER_RUN]) is True


def test_the_same_entry_is_taken_once_and_the_cap_holds(monkeypatch):
    monkeypatch.setattr(steering, "MAX_STEERED_NOTES", 2)
    queue = steering.HeldItemNoteQueue()
    entry = {"id": "a", "item": ITEM, "note": "one"}
    assert queue.put_entry(entry) is True
    assert queue.put_entry(entry) is True
    assert queue.put_entry({"id": "b", "item": ITEM, "note": "two"}) is True
    assert queue.put_entry({"id": "c", "item": ITEM, "note": "three"}) is False
    assert [m.text.split("\n")[1] for m in drain_steering_queue(queue)] == ["one", "two"]


def test_a_plain_steering_queue_stays_interactive():
    assert SteeringQueue().interactive is True
