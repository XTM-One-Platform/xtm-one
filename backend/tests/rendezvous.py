"""Prove that callers overlap without a wall-clock bound.

"Two 0.15 s loaders overlapped if the pair took less than 0.4 s" fails
whenever a loaded runner delays a timer by the margin, and a sequential pair
can land under such a bound anyway. A rendezvous proves the overlap by
construction instead: each party waits until the others have arrived, so
concurrent callers meet however slow the machine is, while a sequential caller
never brings the next party in as long as one is parked.
"""

from __future__ import annotations

import asyncio

#: Only a caller that does not overlap its parties ever waits this long.
OVERLAP_TIMEOUT_SECONDS = 10.0


class Rendezvous:
    """Hold each arrival until *parties* of them are in flight together.

    A party that times out waiting is recorded in :attr:`missed` and opens the
    rendezvous, so the rest of a sequential run goes through at once instead of
    each waiting out its own timeout.
    """

    def __init__(self, parties: int, *, timeout: float = OVERLAP_TIMEOUT_SECONDS) -> None:
        self._parties = parties
        self._timeout = timeout
        self._arrived = 0
        self._open = asyncio.Event()
        self.missed: list[str] = []

    async def arrive(self, name: str) -> None:
        self._arrived += 1
        if self._arrived >= self._parties:
            self._open.set()
        try:
            await asyncio.wait_for(self._open.wait(), self._timeout)
        except TimeoutError:
            self.missed.append(name)
            self._open.set()
