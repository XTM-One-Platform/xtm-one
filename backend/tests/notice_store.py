"""The Redis commands :mod:`app.core.log_once` claims a notice with, faked for tests.

A test of a notice said once for the platform plays several processes in one:
they share one :class:`FakeNoticeRedis` (:func:`share_notices`), and
:func:`new_process` forgets what the current one decided, which is where
another uvicorn worker, a recycled one or a replica starts from.
"""

from __future__ import annotations

import asyncio

from app.core import log_once


class FakeNoticeRedis:
    """``SET NX EX``, ``GETDEL``, ``INCRBY``, ``EXPIRE`` and the release script: ``fail`` raises, ``hang`` never answers."""

    def __init__(self, *, fail: bool = False, hang: bool = False) -> None:
        self.fail = fail
        self.hang = hang
        self.values: dict[str, str] = {}
        self.ttls: dict[str, int] = {}
        self.commands = 0

    async def _answer(self) -> None:
        if self.hang:
            await asyncio.Event().wait()
        if self.fail:
            raise ConnectionError("Redis is unreachable")
        self.commands += 1

    async def set(self, key: str, value: str, *, nx: bool = False, ex: int | None = None) -> bool | None:
        await self._answer()
        if nx and key in self.values:
            return None
        self.values[key] = value
        if ex is not None:
            self.ttls[key] = ex
        return True

    async def getdel(self, key: str) -> str | None:
        await self._answer()
        self.ttls.pop(key, None)
        return self.values.pop(key, None)

    async def incrby(self, key: str, amount: int) -> int:
        await self._answer()
        total = int(self.values.get(key, "0")) + amount
        self.values[key] = str(total)
        return total

    async def expire(self, key: str, seconds: int) -> bool:
        await self._answer()
        self.ttls[key] = seconds
        return key in self.values

    async def eval(self, script: str, numkeys: int, *keys_and_args: str) -> int:
        await self._answer()
        assert script == log_once._RELEASE_IF_OWNER_LUA, "a notice runs no other script"
        key, token = keys_and_args
        if self.values.get(key) != token:
            return 0
        del self.values[key]
        self.ttls.pop(key, None)
        return 1

    def claims(self) -> list[str]:
        return [key for key in self.values if not key.endswith(":held")]

    def interval_elapsed(self) -> None:
        """Every claim expires, as Redis drops it at the end of its interval."""
        for key in self.claims():
            del self.values[key]
            self.ttls.pop(key, None)


def share_notices(monkeypatch, fake: FakeNoticeRedis | None = None) -> FakeNoticeRedis:
    """Make every notice claim go to *fake*, the Redis all the test's processes share."""
    store = fake or FakeNoticeRedis()

    async def _client() -> FakeNoticeRedis:
        return store

    monkeypatch.setattr(log_once, "shared_store", _client)
    return store


def new_process() -> None:
    """Start over as another process of the platform: no window, nothing counted."""
    log_once.forget()
