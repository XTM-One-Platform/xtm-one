"""The page-action wait of ``act_on_page`` against a real Redis.

The answer reaches the waiting turn through Redis because the POST carrying it
may land on another replica; only the pending action may receive it.
"""

import asyncio
import uuid
from contextlib import contextmanager

import pytest

pytestmark = pytest.mark.integration


@contextmanager
def _rebound_redis():
    # Same reason as ``tests/test_chat_approval_resume_postgres.py``: the
    # module-level client binds to the first test's loop.
    from app.core import redis as redis_module

    saved = (
        getattr(redis_module, "_shared_client", None),
        getattr(redis_module, "_pool", None),
        getattr(redis_module, "_sentinel", None),
    )
    redis_module._build_client()
    try:
        yield
    finally:
        (
            redis_module._shared_client,
            redis_module._pool,
            redis_module._sentinel,
        ) = saved


@pytest.fixture
def fresh_redis():
    with _rebound_redis():
        yield


def _conv() -> str:
    return str(uuid.uuid4())


@pytest.mark.asyncio
async def test_an_answer_pushed_by_another_client_reaches_the_wait(fresh_redis):
    from app.services import chat_turn_guard as g

    conv = _conv()
    await g.set_pending_page_action(conv, "a1")
    waiter = asyncio.create_task(g.await_page_action_result(conv, "a1", timeout=5))
    await asyncio.sleep(0.1)

    assert await g.push_page_action_result(conv, {"action_id": "a1", "ok": True})
    assert await waiter == {"action_id": "a1", "ok": True}
    await g.clear_pending_page_action(conv)


@pytest.mark.asyncio
async def test_an_answer_to_another_action_is_refused(fresh_redis):
    from app.services import chat_turn_guard as g

    conv = _conv()
    await g.set_pending_page_action(conv, "a2")
    assert not await g.push_page_action_result(conv, {"action_id": "a1", "ok": True})
    await g.clear_pending_page_action(conv)


@pytest.mark.asyncio
async def test_the_wait_ends_at_its_timeout(fresh_redis):
    from app.services import chat_turn_guard as g

    conv = _conv()
    await g.set_pending_page_action(conv, "a1")
    assert await g.await_page_action_result(conv, "a1", timeout=0.3) is None
    await g.clear_pending_page_action(conv)


@pytest.mark.asyncio
async def test_nothing_is_delivered_once_the_wait_is_cleared(fresh_redis):
    from app.services import chat_turn_guard as g

    conv = _conv()
    await g.set_pending_page_action(conv, "a1")
    await g.clear_pending_page_action(conv)
    assert not await g.push_page_action_result(conv, {"action_id": "a1", "ok": True})


@pytest.mark.asyncio
async def test_one_run_holds_a_conversations_page_until_it_releases_it(fresh_redis):
    from app.services import chat_turn_guard as g

    conv = _conv()
    assert await g.claim_page_action_run(conv, "run-a", ttl_seconds=30)
    assert not await g.claim_page_action_run(conv, "run-b", ttl_seconds=30)
    # A release with another run's token leaves the claim alone: a run that
    # outlived its claim must not free the page for the one that took it.
    await g.release_page_action_run(conv, "run-b")
    assert not await g.claim_page_action_run(conv, "run-b", ttl_seconds=30)
    await g.release_page_action_run(conv, "run-a")
    assert await g.claim_page_action_run(conv, "run-b", ttl_seconds=30)
    await g.release_page_action_run(conv, "run-b")


@pytest.mark.asyncio
async def test_the_wait_returns_as_soon_as_the_answer_is_pushed(fresh_redis):
    # A blocking read, not a poll: the answer is not held until a next tick.
    from app.services import chat_turn_guard as g

    conv = _conv()
    await g.set_pending_page_action(conv, "a1")
    waiter = asyncio.create_task(g.await_page_action_result(conv, "a1", timeout=10, block_seconds=5))
    await asyncio.sleep(0.2)
    start = asyncio.get_running_loop().time()
    assert await g.push_page_action_result(conv, {"action_id": "a1", "ok": True})
    assert await waiter == {"action_id": "a1", "ok": True}
    assert asyncio.get_running_loop().time() - start < 1
    await g.clear_pending_page_action(conv)
