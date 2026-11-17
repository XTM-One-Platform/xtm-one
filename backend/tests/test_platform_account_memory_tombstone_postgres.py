"""Only the sign-in whose INSERT re-creates a platform account lifts its tombstone.

``_resolve_trusted_platform_jwt`` lifts the deletion's memory tombstone when its
``INSERT ... ON CONFLICT DO NOTHING`` created the row, which it reads from the
driver's row count. The unit tests hand it that count; this runs the real
statement on real PostgreSQL: two sign-ins for the same deleted person race, the
one whose INSERT creates the row lifts the tombstone, the one whose INSERT finds
the row already there (``INSERT 0 0``) lifts nothing, and so does the sign-in of
an account that exists.

Skips rather than fails when PostgreSQL is unreachable, matching the other
``integration``-marked tests here.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

import pytest
from sqlalchemy import func, select

import app.models  # noqa: F401 - registers every table on Base.metadata
from app.api import deps
from app.core import xtm_auth
from app.models.user import User
from app.services.memory_tombstones import account_tombstone, bury
from tests.memory_redis_double import MemoryRedis, use_memory_redis
from tests.scratch_database import ALL_TABLES, scratch_database

pytestmark = pytest.mark.integration

_ISS = "https://opencti.example.test"
_EMAIL = "analyst@example.com"
_USER_ID = uuid.uuid5(uuid.NAMESPACE_URL, f"platform:{_ISS}:{_EMAIL}")


@dataclass
class _Payload:
    iss: str
    email: str


async def test_only_the_insert_that_re_creates_the_account_lifts_its_tombstone(monkeypatch):
    async def _verify(_token, _db):
        return _Payload(iss=_ISS, email=_EMAIL)

    monkeypatch.setattr(xtm_auth, "verify_xtm_jwt", _verify)
    monkeypatch.setattr(deps.jwt, "get_unverified_header", lambda _token: {"alg": "EdDSA"})
    redis = MemoryRedis()
    use_memory_redis(monkeypatch, redis)
    await bury(redis, account_tombstone(str(_USER_ID)), deletion="the-deletion")

    lifted: list[tuple[uuid.UUID, str]] = []
    lift = deps._lift_memory_tombstone

    async def _recorded_lift(user_id, deletion):
        lifted.append((user_id, deletion))
        await lift(user_id, deletion)

    monkeypatch.setattr(deps, "_lift_memory_tombstone", _recorded_lift)

    async with scratch_database(
        "platform_tombstone",
        tables=ALL_TABLES,
        extensions=("vector",),
        skip_without_extensions=True,
        refuse_other_databases=True,
    ) as scratch:
        # The first sign-in has looked the person up, found nobody and read
        # the tombstone; before its INSERT runs, a concurrent sign-in for the
        # same person re-creates the account on another connection.
        read_tombstone = deps._read_memory_tombstone
        winner: list[User] = []
        raced = False

        async def _read_then_a_concurrent_sign_in(user_id):
            nonlocal raced
            deletion = await read_tombstone(user_id)
            if not raced:
                raced = True
                async with scratch.sessions() as other:
                    winner.append(await deps._resolve_trusted_platform_jwt("dummy", other))
            return deletion

        monkeypatch.setattr(deps, "_read_memory_tombstone", _read_then_a_concurrent_sign_in)

        async with scratch.sessions() as db:
            loser = await deps._resolve_trusted_platform_jwt("dummy", db)
        async with scratch.sessions() as db:
            known = await deps._resolve_trusted_platform_jwt("dummy", db)
            accounts = await db.scalar(select(func.count()).select_from(User).where(User.email == _EMAIL))

    assert winner[0].id == loser.id == known.id == _USER_ID
    assert accounts == 1
    # Both read the tombstone before their INSERT: only the INSERT that
    # created the row lifts it.
    assert lifted == [(_USER_ID, "the-deletion")]
    assert account_tombstone(str(_USER_ID)) not in redis.tombstones()
