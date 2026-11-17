# Copyright (c) 2025-2026 Filigran SAS
#
# This file is part of the XTM One Enterprise Edition ("EE") and is
# licensed under the XTM One Enterprise Edition License (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://github.com/XTM-One-Platform/xtm-one/blob/main/LICENSE
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.

"""Deletion tombstones for the Redis memory tiers (``agent_memory`` / ``user_memory``).

A memory writer can still be in flight when the account or agent it writes for
is deleted: ``consolidate_memory`` and ``consolidate_user_memory`` are detached
from the chat turn and store their result after an LLM call. A purge that runs
while one of them waits would be undone by its late write.

The purge therefore buries a tombstone BEFORE deleting any key, and every
writer checks the tombstones AFTER its write, deleting what it wrote when one
is there. Whatever the interleaving, a write either lands before the purge's
delete (and is deleted by it) or after the tombstone (and undoes itself).

The tombstones live outside the ``agent_mem:`` / ``user_mem:`` key families,
so no memory purge scan removes them.
"""

from __future__ import annotations

from redis.asyncio import Redis

#: Longer than any in-flight write can take (a consolidation is at most three
#: LLM calls). Agent and local account ids are random UUIDs, so an outlived
#: tombstone blocks nothing; a platform account comes back under its old
#: deterministic id, and its re-creation lifts the tombstone (:func:`lift`).
TOMBSTONE_TTL = 86_400


def account_tombstone(user_id: str) -> str:
    return f"memory_tombstone:account:{user_id}"


def agent_tombstone(agent_id: str) -> str:
    return f"memory_tombstone:agent:{agent_id}"


async def bury(redis: Redis, *tombstones: str, deletion: str | None = None) -> None:
    """Mark the memory behind *tombstones* as deleted, before it is purged.

    They name *deletion*: only a :func:`lift` for that deletion removes them."""
    pipe = redis.pipeline()
    for tombstone in tombstones:
        pipe.set(tombstone, deletion or 1, ex=TOMBSTONE_TTL)
    await pipe.execute()


async def buried_by(redis: Redis, tombstone: str) -> str | None:
    """The deletion *tombstone* names, or None when it is not there."""
    return await redis.get(tombstone)


# Atomic: a GET-then-DEL would race a later deletion of the re-created
# account and remove the tombstone that deletion just buried.
_LIFT_SCRIPT = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
    return redis.call('DEL', KEYS[1])
end
return 0
"""


async def lift(redis: Redis, tombstone: str, deletion: str) -> None:
    """Remove *tombstone* while it still names *deletion*: what that deletion
    buried exists again, and writes for it stay."""
    await redis.eval(_LIFT_SCRIPT, 1, tombstone, deletion)


async def undo_if_buried(redis: Redis, tombstones: tuple[str, ...], keys: tuple[str, ...]) -> bool:
    """After a write: delete *keys* when any of *tombstones* is set."""
    if not await redis.exists(*tombstones):
        return False
    await redis.delete(*keys)
    return True
