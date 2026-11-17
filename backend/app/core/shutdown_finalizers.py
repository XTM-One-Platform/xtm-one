# SPDX-License-Identifier: Apache-2.0

"""Work a request owes before its process exits on a graceful shutdown.

uvicorn's bounded drain (``--timeout-graceful-shutdown``) cancels the request
tasks still running when it expires, then sends the lifespan shutdown at once:
it does not wait for them. A request that catches that cancellation to store
what it did races the lifespan teardown (the Redis pool, the database engine)
and, past it, the cancellation ``asyncio.run`` delivers to every task still
alive when the server returns.

Three registries close that window:

- the request tasks in flight (:func:`track_request_task`, entered by
  ``GracefulShutdownDrainMiddleware`` for every HTTP and WebSocket scope);
- the finalizers a cancelled request starts (:func:`start_shutdown_finalizer`),
  each a task of its own, so the request's cancellation never reaches it;
- the background agent turns, outside any request (:func:`track_background_turn`:
  a parent wake, a channel message). uvicorn's drain never waits for them, so
  :func:`drain_shutdown_finalizers` gives them the requests' grace, then cancels
  them as the drain cancels a request.

The lifespan shutdown awaits all three (:func:`drain_shutdown_finalizers`),
bounded by :data:`SHUTDOWN_DRAIN_SECONDS`, before it tears anything down.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Awaitable, Callable, Coroutine, Iterator
from typing import Any

from app.core.utils import GRACEFUL_SHUTDOWN_CANCEL_HINT, format_exception_message

logger = logging.getLogger(__name__)

#: How long the lifespan shutdown waits for the cancelled requests and the
#: finalizers they started.
SHUTDOWN_DRAIN_SECONDS = 15.0

#: The cancellation of a background turn its grace did not see end: uvicorn's
#: drain message, so the turn stores what a request cut by the drain stores.
BACKGROUND_TURN_CANCEL_MESSAGE = f"Task cancelled, {GRACEFUL_SHUTDOWN_CANCEL_HINT} (background turn)"

_request_tasks: set[asyncio.Task] = set()
_finalizers: set[asyncio.Task] = set()
_background_turns: set[asyncio.Task] = set()
#: What a background turn hands over before the drain cancels it, by task.
_before_cancel: dict[asyncio.Task, Callable[[], Awaitable[None]]] = {}

#: How long the drain waits for each turn's hand-over before cancelling it.
BEFORE_CANCEL_SECONDS = 3.0


@contextlib.contextmanager
def track_request_task() -> Iterator[None]:
    """Count the current task as a request in flight until the block exits."""
    task = asyncio.current_task()
    if task is None:
        yield
        return
    _request_tasks.add(task)
    try:
        yield
    finally:
        _request_tasks.discard(task)


def track_background_turn(task: asyncio.Task) -> None:
    """Count *task*, agent work outside any request, until it finishes."""
    _background_turns.add(task)
    task.add_done_callback(_background_turns.discard)


@contextlib.contextmanager
def before_cancel(hand_over: Callable[[], Awaitable[None]]) -> Iterator[None]:
    """Await *hand_over* before the shutdown drain cancels the current background turn.

    A background turn the drain cancels hands over what it owes (a cut chat
    turn queues its next resume) at the end of its unwinding: a slow
    unwinding, a turn that absorbed the cancellation and ran past the drain
    (then cancelled with no message by ``asyncio.run``), or the pod's kill
    lost it. Registered here, the hand-over runs first, bounded by
    :data:`BEFORE_CANCEL_SECONDS`, while the turn still runs.
    """
    task = asyncio.current_task()
    if task is None:
        yield
        return
    _before_cancel[task] = hand_over
    try:
        yield
    finally:
        if _before_cancel.get(task) is hand_over:
            _before_cancel.pop(task, None)


async def _hand_over_before_cancel(tasks: set[asyncio.Task]) -> None:
    hand_overs = [_before_cancel.pop(task) for task in tasks if task in _before_cancel]
    if not hand_overs:
        return

    async def _one(hand_over: Callable[[], Awaitable[None]]) -> None:
        try:
            await asyncio.wait_for(hand_over(), BEFORE_CANCEL_SECONDS)
        except Exception as exc:
            logger.warning("Shutdown: a background turn's hand-over failed: %s", format_exception_message(exc))

    await asyncio.gather(*(_one(h) for h in hand_overs))


def background_turns_in_flight() -> int:
    return sum(1 for task in _background_turns if not task.done())


async def _run(coro: Coroutine[Any, Any, Any], name: str) -> None:
    try:
        await coro
    except asyncio.CancelledError:
        logger.warning("Shutdown finalizer %s was cancelled before it finished", name)
        raise
    except Exception as exc:
        logger.warning("Shutdown finalizer %s failed: %s", name, format_exception_message(exc))


def start_shutdown_finalizer(coro: Coroutine[Any, Any, Any], *, name: str) -> asyncio.Task:
    """Run *coro* as a task the lifespan shutdown waits for.

    A task of its own, created from the caller's context: the request that
    starts it is being cancelled, and its cancellation must not reach the
    write. A failure is logged, never raised.
    """
    task = asyncio.create_task(_run(coro, name), name=f"shutdown-finalizer:{name}")
    _finalizers.add(task)
    task.add_done_callback(_finalizers.discard)
    return task


async def wait_for_shutdown_finalizer(task: asyncio.Task, timeout: float) -> bool:
    """Wait up to *timeout* seconds for *task*, never cancelling it.

    True when it finished. A task still running is left to the lifespan drain.
    """
    try:
        await asyncio.wait_for(asyncio.shield(task), timeout)
    except TimeoutError:
        return False
    return True


async def _end_background_turns(current: asyncio.Task | None, grace_until: float | None) -> None:
    """Let the background turns run until *grace_until*, then cancel the rest."""
    running = {task for task in _background_turns if task is not current and not task.done()}
    if running and grace_until is not None:
        left = grace_until - time.monotonic()
        if left > 0:
            await asyncio.wait(running, timeout=left)
        running = {task for task in running if not task.done()}
    if not running:
        return
    logger.warning(
        "Shutdown: cancelling %d background agent turn(s) still running (a parent "
        "wake or a channel message); they close as interrupted",
        len(running),
    )
    await _hand_over_before_cancel(running)
    for task in running:
        if not task.done():
            task.cancel(msg=BACKGROUND_TURN_CANCEL_MESSAGE)


async def drain_shutdown_finalizers(
    timeout: float = SHUTDOWN_DRAIN_SECONDS, *, background_grace_until: float | None = None
) -> int:
    """Wait for the requests in flight and the finalizers they start, bounded.

    Called by the lifespan shutdown, after uvicorn has either seen every
    request finish or cancelled the rest. The background turns first get until
    *background_grace_until* (``time.monotonic()``; the requests' drain, when
    the worker knows it) and are then cancelled with
    :data:`BACKGROUND_TURN_CANCEL_MESSAGE`. A task still unwinding may start a
    finalizer while this waits, so it waits again until every registry is
    empty or *timeout* is spent. Returns how many tasks were still running at
    the deadline.
    """
    current = asyncio.current_task()
    await _end_background_turns(current, background_grace_until)
    deadline = time.monotonic() + timeout
    while True:
        pending = {
            task
            for task in (*_request_tasks, *_finalizers, *_background_turns)
            if task is not current and not task.done()
        }
        if not pending:
            return 0
        left = deadline - time.monotonic()
        if left <= 0:
            logger.warning(
                "Shutdown: %d request(s), background turn(s) or finalizer(s) still "
                "running after %.0fs; the process exits without them",
                len(pending),
                timeout,
            )
            return len(pending)
        await asyncio.wait(pending, timeout=left)
