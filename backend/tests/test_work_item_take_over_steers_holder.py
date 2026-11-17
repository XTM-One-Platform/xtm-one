"""A take-over reaches the run it takes the item from, and that run writes nothing more (#11751).

The #11745 retest on build 1.261114.0-dev.72820b3: run B took an item over
(``take_over=true``) from a live background task H in a ``wait``. The claim
note was recorded, but H was never told (the stop was offered only to a
session opened on that same item, and a background task had none), and H's
next note on the item it no longer held was accepted silently. Now the stop
reaches any live session of the holder, a background task opens one on its
run ids, the pause ends on it, and the former holder's writes are refused.
Redis is a fake; nothing reaches the network.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime

import pytest

import app.services.work_item as wi_service
from app.services import work_item_note_steering as steering
from app.services.company_managed_writes import agent_writer
from app.services.work_item_preflight import finished_under_holder_text
from app.tools.agent_loop import drain_steering_queue
from app.tools.wait_policy import WAIT_REASON_STEERED, WaitBudget, WaitPolicy, execute_wait
from tests.test_work_item_cancel_under_live_holder import _item, world  # noqa: F401
from tests.test_work_item_note_steering import _Redis

TAKER = uuid.uuid4()
TAKER_RUN = uuid.uuid4()
AGENT = agent_writer(is_company_managed=False, agent_id=TAKER)


@pytest.fixture
def redis(monkeypatch):
    store = _Redis()

    async def _client():
        return store

    monkeypatch.setattr(steering, "redis_client", _client)
    monkeypatch.setattr(steering, "POLL_SECONDS", 0.01)
    return store


# -- The stop reaches the holder, a pause included ------------------------------------


@pytest.mark.asyncio
async def test_a_take_over_ends_the_holders_wait_with_the_stop(redis):
    """A background task H (task id + child run) waits; run B takes its item over."""
    item_id, task_id, child_run = str(uuid.uuid4()), str(uuid.uuid4()), str(uuid.uuid4())
    note = wi_service.finished_under_holder_note(
        "reset to open", AGENT, uuid.UUID(task_id), datetime(2026, 11, 14, 3, 5, 23, tzinfo=UTC)
    )
    async with steering.session_for_runs((task_id, child_run)) as queue:
        assert queue is not None

        async def _take_over_soon():
            await asyncio.sleep(0.05)
            await wi_service._signal_finished_to_holder(item_id, task_id, note, "Retest scratch")

        taker = asyncio.create_task(_take_over_soon())
        result = await execute_wait(
            {"duration": "30s"},
            budget=WaitBudget(WaitPolicy()),
            pending_input=lambda: not queue.empty(),
        )
        await taker
        assert result.reason == WAIT_REASON_STEERED
        assert result.elapsed < 5
        steered = drain_steering_queue(queue)
    assert len(steered) == 1
    assert f"STOP: item reset to open by agent {TAKER} at 2026-11-14T03:05:23Z while run {task_id} held it" in (
        steered[0].text
    )
    assert item_id in steered[0].text
    assert redis.values == {} and redis.lists == {}


@pytest.mark.asyncio
async def test_a_stop_reaches_a_run_whose_session_names_another_item(redis):
    """A run triggered by item X holds item Y it claimed mid-run: Y's stop reaches it."""
    run, triggered, held = str(uuid.uuid4()), str(uuid.uuid4()), str(uuid.uuid4())
    async with steering.session(run, {"work_item_id": triggered}) as queue:
        assert await steering.offer(held, run, "a plain note", writer_run_ids=[str(TAKER_RUN)]) is False
        assert await steering.offer(held, run, "STOP: taken", any_item=True) is True
        for _ in range(100):
            if not queue.empty():
                break
            await asyncio.sleep(0.01)
        assert ["STOP: taken" in m.text for m in drain_steering_queue(queue)] == [True]


@pytest.mark.asyncio
async def test_no_live_session_of_the_holder_relays_no_stop(redis):
    assert await steering.offer(str(uuid.uuid4()), str(uuid.uuid4()), "STOP: taken", any_item=True) is False
    assert redis.lists == {}


# -- The former holder writes nothing more -----------------------------------------------


def _taken_over(former: uuid.UUID, *, status="open", new_holder=None):
    note = wi_service.finished_under_holder_note(f"reset to {status}", AGENT, former, datetime.now(UTC))
    return _item(status=status, claimed_by_run_id=new_holder, claimed_by_agent_id=None, claim_notes=[note])


@pytest.mark.asyncio
async def test_the_former_holders_note_is_refused_with_the_stop(world):  # noqa: F811
    former = uuid.uuid4()
    item = _taken_over(former)
    session, _offers = world(item, "running")

    with pytest.raises(ValueError) as refused:
        await wi_service.update_work_item(
            str(item.id), user_id=str(item.user_id), note="still working", writer=AGENT, writer_run_ids=(former,)
        )
    text = str(refused.value)
    assert text.startswith("this run no longer holds this item. STOP: item reset to open by agent")
    assert f"while run {former} held it" in text and "It is now open" in text
    assert not session.committed and item.status == "open"


@pytest.mark.asyncio
async def test_the_former_holders_completion_is_refused_and_names_the_new_holder(world):  # noqa: F811
    former, new_holder = uuid.uuid4(), uuid.uuid4()
    item = _taken_over(former, status="in_progress", new_holder=new_holder)
    world(item, "running")

    with pytest.raises(ValueError, match=f"held by run {new_holder}"):
        await wi_service.complete_work_item(
            str(item.id), user_id=str(item.user_id), status="done", writer=AGENT, writer_run_ids=(former,)
        )
    assert item.status == "in_progress"


@pytest.mark.asyncio
async def test_the_next_holder_and_a_take_back_write_freely(world):  # noqa: F811
    former, new_holder = uuid.uuid4(), uuid.uuid4()
    item = _taken_over(former, status="in_progress", new_holder=new_holder)
    world(item, "completed")

    await wi_service.update_work_item(
        str(item.id), user_id=str(item.user_id), note="mine now", writer=AGENT, writer_run_ids=(new_holder,)
    )
    await wi_service.update_work_item(
        str(item.id),
        user_id=str(item.user_id),
        note="taken back",
        writer=AGENT,
        writer_run_ids=(former,),
        take_over=True,
    )
    assert item.status == "in_progress"


def test_a_second_take_over_does_not_hide_the_first_holders_stop():
    first, second = uuid.uuid4(), uuid.uuid4()
    notes = [
        wi_service.finished_under_holder_note("reset to open", AGENT, first, datetime.now(UTC)),
        wi_service.finished_under_holder_note("cancelled", AGENT, second, datetime.now(UTC)),
    ]
    view = {"claim_notes": notes}
    assert f"while run {first} held it" in finished_under_holder_text(view, [str(first)])
    assert f"while run {second} held it" in finished_under_holder_text(view, [str(second)])
    assert f"while run {second} held it" in finished_under_holder_text(view)
    assert finished_under_holder_text(view, [str(uuid.uuid4())]) is None
