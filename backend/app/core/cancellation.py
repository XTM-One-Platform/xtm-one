# SPDX-License-Identifier: Apache-2.0

"""Cancellation-resilient cleanup helpers.

Python 3.11+ tracks a per-task *cancellation count* via
:meth:`asyncio.Task.cancelling`, plus an internal "cancel is pending" flag.
Once a task is being cancelled, the **next** ``await`` inside any ``finally`` /
teardown block can immediately raise ``CancelledError`` — even though that
cleanup still needs to run to release resources cleanly (closing a DB session,
unwinding an ``AsyncExitStack``, issuing a transaction ROLLBACK).

This module offers two complementary primitives:

* :func:`uncancel_during_cleanup` — the *uncancel/recancel* dance. Lifts a
  cancellation that has **already been delivered** (caught) for the duration of
  a cleanup block, then re-applies it so the task still behaves as cancelled.
  Used where the caller catches ``CancelledError`` itself and re-raises after
  cleanup (e.g. the MCP client teardown).

* :func:`run_cleanup_to_completion` — runs a cleanup coroutine on a **shielded
  child task** and waits for it in a shielded anyio scope, absorbing
  cancellations delivered to the *waiting* task. Unlike the uncancel dance, this guarantees the wrapped
  coroutine runs to completion even when a cancellation is *requested but not
  yet delivered* (the asyncpg ROLLBACK case), because the child
  task has its own, un-cancelled state. Used by the DB session dependency.

* :func:`run_to_completion` — the same shielded child task for work that is
  not cleanup: its outcome must land (a rotated refresh token committed), and
  a cancellation that arrives while it runs is let through once it is done.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable, Coroutine
from contextlib import asynccontextmanager


@asynccontextmanager
async def uncancel_during_cleanup() -> AsyncIterator[None]:
    """Run a cleanup block with the task's *delivered* cancellation lifted.

    On entry, any pending cancellation count on the current task is drained
    with :meth:`asyncio.Task.uncancel`; on exit it is re-applied with
    :meth:`asyncio.Task.cancel` so the task is still observed as cancelled at
    its next checkpoint. ``cancel()`` is only re-applied when at least one
    cancellation was actually lifted, so wrapping a non-cancelled task never
    injects a cancellation the caller did not ask for.

    This is the right tool when the surrounding code **catches**
    ``CancelledError`` and re-raises after cleanup (the cancellation has
    already been delivered, so ``cancelling() > 0`` but the cleanup's awaits
    will not spuriously re-raise). For cleanup that must survive a *pending,
    undelivered* cancellation, use :func:`run_cleanup_to_completion`.

    No-op outside a running task (``current_task()`` is ``None``).
    """
    task = asyncio.current_task()
    lifted = 0
    if task is not None:
        while task.cancelling() > 0:
            task.uncancel()
            lifted += 1
    try:
        yield
    finally:
        if task is not None and lifted > 0:
            for _ in range(lifted):
                task.cancel()


async def run_cleanup_to_completion[T](coro: Coroutine[object, object, T]) -> T:
    """Run *coro* to completion, immune to cancellation of the awaiting task.

    The coroutine is scheduled as a child task and awaited via
    :func:`asyncio.shield`. A ``CancelledError`` delivered to the *waiting*
    task is absorbed and the wait resumes, so the wrapped cleanup — e.g. a DB
    ``session.close()`` and the asyncpg ROLLBACK it issues — always runs to
    completion and never leaves a pooled connection checked out in an
    inconsistent state.

    Because the child task carries its own cancellation state, this works even
    when a cancellation is *requested but not yet delivered* on the parent —
    the scenario where a plain ``await coro`` would raise ``CancelledError`` on
    its first checkpoint, and where the ``uncancel`` dance is insufficient on
    CPython builds that keep the "must cancel" flag set after ``uncancel()``.

    Absorbed cancellations are intentionally **not** re-raised: this is meant
    to be called from a ``finally`` block where the original exception (often
    the very ``CancelledError`` being propagated during request teardown)
    continues to unwind on its own once the cleanup is done. The cleanup's own
    return value is returned, and its non-cancellation exceptions propagate.

    The wait is inside a shielded anyio scope: a cancelled anyio scope (a
    streaming response whose client left) re-cancels its task on every loop
    tick, which turned the wait into a catch-and-retry spin at full CPU for as
    long as the cleanup ran. Shielded, the scope delivers nothing until the
    wait is over, then delivers its cancellation as usual.

    Each cancellation absorbed (``Task.cancel()``, which no anyio shield stops)
    is balanced with :meth:`asyncio.Task.uncancel` so the waiting task's
    cancellation count does not stay elevated after the helper returns (asyncio
    3.11+). Leaving it elevated would both confuse an enclosing
    structured-concurrency scope and, if a ``cancel()`` arrived while this
    coroutine was running rather than suspended, leave ``_must_cancel`` set so
    the *next* ``await`` re-raises immediately — turning the wait into a
    raise/catch spin until the cleanup finishes. Draining it keeps the loop a
    genuine wait.
    """
    task = asyncio.current_task()

    def _balance(_cancellation: asyncio.CancelledError) -> None:
        if task is not None:
            task.uncancel()

    return await _await_shielded(coro, _balance)


async def run_to_completion[T](coro: Coroutine[object, object, T]) -> T:
    """Run *coro* to completion, then let a cancellation it outlived through.

    For work whose outcome must land even when the awaiting task is cancelled
    while it runs: the refresh of a rotating OAuth refresh token, whose new
    token exists nowhere else once the authorization server answered.

    Two kinds of cancellation reach an asyncio task, and each is held off its
    own way while *coro* runs:

    - an anyio cancel scope (the MCP SDK's transports run in one): the wait is
      inside a shielded scope, so the scope does not deliver its cancellation
      until the shield is left, and then delivers it as usual - with the message
      and the count it recognises as its own on exit;
    - ``Task.cancel()`` and ``asyncio.timeout``, which no anyio shield stops:
      *coro* runs on a child task awaited through :func:`asyncio.shield`, the
      cancellation is caught while it finishes, then raised again unchanged
      (the task's cancellation count untouched, so ``asyncio.timeout`` still
      turns it into ``TimeoutError``).

    Unlike :func:`run_cleanup_to_completion`, nothing is dropped: the caller is
    cancelled as soon as *coro* is done. Its result is returned otherwise, and
    a non-cancellation exception of *coro* propagates.
    """
    caught: list[asyncio.CancelledError] = []
    result = await _await_shielded(coro, caught.append)
    if caught:
        raise caught[-1]
    return result


async def _await_shielded[T](
    coro: Coroutine[object, object, T],
    on_cancel: Callable[[asyncio.CancelledError], object],
) -> T:
    """Run *coro* on a task of its own and wait for it, whatever cancels the waiting task.

    The wait is inside a shielded anyio scope, so a cancelled scope delivers
    nothing until it is over (unshielded, it re-cancels the task on every loop
    tick: a catch-and-retry spin at full CPU). A ``Task.cancel()``, which no
    anyio shield stops, is handed to *on_cancel*, the caller's policy, and the
    wait resumes. A cancellation of *coro* itself (loop shutdown) is raised.
    """
    import anyio

    inner = asyncio.ensure_future(coro)
    with anyio.CancelScope(shield=True):
        while not inner.done():
            try:
                await asyncio.shield(inner)
            except asyncio.CancelledError as exc:
                if inner.cancelled():
                    raise
                on_cancel(exc)
    return inner.result()
