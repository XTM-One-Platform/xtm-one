"""An in-memory Redis for the memory tiers (``agent_memory`` / ``user_memory``).

Enough of the client for the real memory writers and purges to run against it:
the tests that check what a purge leaves behind, and whether a write still in
flight can bring deleted memory back (``memory_tombstones``), drive the real
code instead of recording calls.
"""

from __future__ import annotations

import asyncio
import fnmatch
from types import SimpleNamespace
from unittest.mock import AsyncMock


class MemoryRedis:
    """``written`` records every key a write touched, so a test can check that
    the purge clears everything the real writers produce."""

    def __init__(self, keys=()):
        self.data: dict[str, object] = dict.fromkeys(keys, "1")
        self.written: set[str] = set()

    def memory(self) -> set[str]:
        return {key for key in self.data if not key.startswith("memory_tombstone:")}

    def tombstones(self) -> set[str]:
        return {key for key in self.data if key.startswith("memory_tombstone:")}

    async def get(self, key):
        value = self.data.get(key)
        return None if isinstance(value, list) else value

    async def set(self, key, value, ex=None, nx=False):
        if nx and key in self.data:
            return None
        self.data[key] = str(value)
        self.written.add(key)
        return True

    async def delete(self, *keys):
        removed = [key for key in keys if key in self.data]
        for key in removed:
            del self.data[key]
        return len(removed)

    async def exists(self, *keys):
        return sum(key in self.data for key in keys)

    async def eval(self, script, numkeys, *keys_and_args):
        """The compare-and-delete of ``memory_tombstones.lift`` and the scripts of ``user_memory``."""
        from app.services.memory_tombstones import _LIFT_SCRIPT

        if script != _LIFT_SCRIPT:
            return await run_user_memory_script(self, script, keys_and_args)
        if numkeys != 1:
            raise NotImplementedError(script)
        key, deletion = keys_and_args
        if self.data.get(key) != deletion:
            return 0
        del self.data[key]
        return 1

    async def scan(self, _cursor, match=None, count=None):
        return 0, sorted(key for key in self.data if fnmatch.fnmatchcase(key, match))

    async def lpush(self, key, value):
        entries = self.data.setdefault(key, [])
        entries.insert(0, value)
        self.written.add(key)
        return len(entries)

    async def ltrim(self, key, start, end):
        if key in self.data:
            self.data[key] = self.data[key][start : end + 1]

    async def lrange(self, key, start, end):
        value = self.data.get(key)
        if not isinstance(value, list):
            return []
        return list(value)[start:] if end == -1 else list(value)[start : end + 1]

    async def lrem(self, key, _count, value):
        entries = self.data.get(key)
        if not isinstance(entries, list):
            return 0
        kept = [entry for entry in entries if entry != value]
        self.data[key] = kept
        return len(entries) - len(kept)

    async def expire(self, key, _seconds):
        return key in self.data

    async def incr(self, key):
        self.data[key] = str(int(self.data.get(key) or 0) + 1)
        self.written.add(key)
        return int(self.data[key])

    def pipeline(self, transaction=True):
        return MemoryPipeline(self)


async def run_user_memory_script(redis, script, keys_and_args) -> int:
    """A ``user_memory`` Lua script over a double's ``get`` / ``set`` / ``delete``."""
    from app.services.user_memory import _RELEASE_LEASE_SCRIPT, _SETTLE_COUNTER_SCRIPT, _WRITE_ENTRIES_SCRIPT

    if script == _WRITE_ENTRIES_SCRIPT:
        return await write_entries(redis, keys_and_args)
    if script == _RELEASE_LEASE_SCRIPT:
        lease, token = keys_and_args
        if await redis.get(lease) != token:
            return 0
        return await redis.delete(lease)
    if script == _SETTLE_COUNTER_SCRIPT:
        counter_key, generation_key, generation, counted, ttl = keys_and_args
        if (await redis.get(generation_key) or "0") != generation:
            return -1
        left = max(0, int(await redis.get(counter_key) or 0) - int(counted))
        await redis.set(counter_key, left, ex=int(ttl))
        return left
    raise NotImplementedError(script)


async def write_entries(redis, keys_and_args) -> int:
    """``user_memory._WRITE_ENTRIES_SCRIPT`` over a double's ``get`` / ``set`` / ``delete``."""
    (
        entries_key,
        profile_key,
        generation_key,
        expected,
        previous,
        payload,
        profile,
        ttl,
        generation,
        check_free_text,
        free_text,
    ) = keys_and_args
    if generation and (await redis.get(generation_key) or "0") != generation:
        return -1
    current = await redis.get(entries_key)
    if (current != previous) if expected == "1" else current:
        return 0
    if expected != "1" and check_free_text == "1" and (await redis.get(profile_key) or "") != free_text:
        return -2
    await redis.set(entries_key, payload, ex=int(ttl))
    if profile:
        await redis.set(profile_key, profile, ex=int(ttl))
    else:
        await redis.delete(profile_key)
    return 1


class MemoryPipeline:
    def __init__(self, redis: MemoryRedis):
        self._redis = redis
        self._queued: list[tuple[str, tuple, dict]] = []

    def __getattr__(self, name):
        def queue(*args, **kwargs):
            self._queued.append((name, args, kwargs))
            return self

        return queue

    async def execute(self):
        return [
            await getattr(self._redis, name)(*args, **kwargs)
            for name, args, kwargs in self._queued
        ]


def use_memory_redis(monkeypatch, redis: MemoryRedis) -> None:
    """Route every memory module's ``redis_client`` to *redis*."""
    import app.core.redis as redis_module
    from app.services import agent_memory, user_memory, user_memory_maintenance

    async def _client():
        return redis

    for module in (redis_module, agent_memory, user_memory, user_memory_maintenance):
        monkeypatch.setattr(module, "redis_client", _client)


def profile_memory(profile: str, short_term=()) -> dict:
    """A loaded user memory whose profile is *profile*, one statement per line."""
    from datetime import UTC, datetime

    from app.services.user_memory_entries import bullet_lines, new_entry

    now = datetime.now(UTC)
    return {
        "short_term": list(short_term),
        "entries": [new_entry(line, now=now).to_dict() for line in bullet_lines(profile)],
        "legacy_profile": "",
    }


def summarizer(content: str = "- durable summary"):
    """An LLM answering every consolidation with *content*."""
    return SimpleNamespace(
        chat=AsyncMock(
            return_value=SimpleNamespace(content=content, stop_reason="end_turn")
        )
    )


class ParkedLLM:
    """An LLM that holds every call until ``release()``: a consolidation parked
    in its LLM call, the way a detached one waits on the provider."""

    def __init__(self, content: str = "- written back after the purge"):
        self._content = content
        self._answered = asyncio.Event()
        self.waiting = 0

    async def chat(self, **_kwargs):
        self.waiting += 1
        await self._answered.wait()
        return SimpleNamespace(content=self._content, stop_reason="end_turn")

    async def parked(self, calls: int) -> None:
        while self.waiting < calls:
            await asyncio.sleep(0)

    def release(self) -> None:
        self._answered.set()
