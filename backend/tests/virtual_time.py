"""An event loop on a clock only its own waits move, for tests that bound durations.

On the wall clock, a test that asserts how long a bounded call took turns red
whenever a loaded xdist worker stalls (a 0.6 s stall moved a take's cap to
another step, main CI run 41402176545; a kill-margin bound read 3.74 s for
3.5 s, main CI run 42008260135). On :class:`VirtualTimeLoop`, time passes only
when every task waits, by exactly the wait, so timeouts, deadlines and sleeps
fall at the same instants on any runner.

Run a coroutine on it with ``asyncio.run(coro, loop_factory=VirtualTimeLoop)``;
code that reads ``time.monotonic()`` reads ``asyncio.get_running_loop().time()``
instead once the test patches its module's clock to it.
"""

from __future__ import annotations

import asyncio
import selectors


class _IdleSkippingSelector(selectors.DefaultSelector):
    """A selector that, with nothing ready, moves its loop's clock to the next timer instead of waiting for it."""

    def __init__(self, loop: VirtualTimeLoop) -> None:
        super().__init__()
        self._loop = loop

    def select(self, timeout: float | None = None):
        ready = super().select(0)
        if ready or timeout == 0:
            return ready
        if timeout is None:
            return super().select(None)
        self._loop.virtual_now += timeout
        return []


class VirtualTimeLoop(asyncio.SelectorEventLoop):
    """An event loop on a clock only its own waits move.

    Work handed to a thread runs inline, so no thread finishes at a real
    instant the clock cannot see.
    """

    def __init__(self) -> None:
        self.virtual_now = 0.0
        super().__init__(_IdleSkippingSelector(self))

    def time(self) -> float:
        return self.virtual_now

    def run_in_executor(self, executor, func, *args):
        future = self.create_future()
        try:
            future.set_result(func(*args))
        except Exception as exc:
            future.set_exception(exc)
        return future
