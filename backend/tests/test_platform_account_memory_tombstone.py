"""A re-created platform account lifts the deletion's memory tombstone.

An account a trusted platform JWT provisions has a deterministic id
(``uuid5(NAMESPACE_URL, "platform:<iss>:<email>")``), so after a deletion the
same person comes back under the same id. The deletion's account tombstone
(``memory_tombstones``) would then undo every Redis memory write of the
new account for its whole TTL; the provisioning INSERT lifts it instead.
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass
from types import SimpleNamespace

import pytest
from sqlalchemy import Select
from sqlalchemy.sql.dml import Insert

from app.api import deps
from app.core import xtm_auth
from app.services.agent_memory import store_short_term_memory
from app.services.memory_tombstones import account_tombstone, bury
from app.services.user_memory import store_user_memory
from tests.memory_redis_double import MemoryRedis, use_memory_redis

_ISS = "https://opencti.example.test"
_EMAIL = "analyst@example.com"
_USER_ID = uuid.uuid5(uuid.NAMESPACE_URL, f"platform:{_ISS}:{_EMAIL}")


@dataclass
class _Payload:
    iss: str
    email: str


class _Result:
    def __init__(self, value=None, *, rowcount: int | None = None):
        self._value = value
        if rowcount is not None:
            self.rowcount = rowcount

    def scalar_one_or_none(self):
        return self._value

    def scalar_one(self):
        return self._value

    def scalars(self):
        return self

    def all(self):
        return []


class _ProvisioningDb:
    """The first ``users`` lookup finds nobody, the INSERT inserts the row (or
    loses a race), and the re-fetch returns it."""

    def __init__(self, *, inserted: bool):
        self._inserted = inserted
        self._user_selects = 0
        self._row = None

    async def execute(self, stmt, *_args, **_kwargs):
        if isinstance(stmt, Insert) and stmt.table.name == "users":
            self._row = SimpleNamespace(**stmt.compile().params, is_admin=False)
            return _Result(rowcount=1 if self._inserted else 0)
        froms = stmt.get_final_froms() if isinstance(stmt, Select) else []
        if froms and getattr(froms[0], "name", None) == "users" and stmt.whereclause is not None:
            self._user_selects += 1
            return _Result(None if self._user_selects == 1 else self._row)
        return _Result(None)

    async def commit(self):
        pass


class _DownRedis(MemoryRedis):
    """Redis failing when the sign-in reads the tombstone, or only when it lifts it."""

    def __init__(self, *, from_step: str):
        super().__init__()
        self._from_step = from_step

    async def get(self, key):
        if self._from_step == "read":
            raise ConnectionError("redis is down")
        return await super().get(key)

    async def eval(self, *_args):
        raise ConnectionError("redis is down")


@pytest.fixture
def platform_jwt(monkeypatch):
    async def _verify(_token, _db):
        return _Payload(iss=_ISS, email=_EMAIL)

    monkeypatch.setattr(xtm_auth, "verify_xtm_jwt", _verify)
    monkeypatch.setattr(deps.jwt, "get_unverified_header", lambda _token: {"alg": "EdDSA"})


@pytest.fixture
def memory_redis(monkeypatch) -> MemoryRedis:
    redis = MemoryRedis()
    use_memory_redis(monkeypatch, redis)
    return redis


async def test_a_re_created_platform_account_lifts_the_deletion_tombstone(platform_jwt, memory_redis):
    uid, gone = str(_USER_ID), str(uuid.uuid4())
    # Two deleted accounts: this platform account, and one that does not come back.
    await bury(memory_redis, account_tombstone(uid), account_tombstone(gone))

    user = await deps._resolve_trusted_platform_jwt("dummy", _ProvisioningDb(inserted=True))

    assert user.id == _USER_ID
    assert memory_redis.tombstones() == {account_tombstone(gone)}
    # Through the real writers: the re-created account keeps its memory, the
    # account that did not come back still undoes every write.
    assert await store_user_memory(uid, agent_name="Analyst", summary="asked") == 1
    lane_agent = str(uuid.uuid4())
    assert await store_short_term_memory(lane_agent, source="chat", summary="s", scope_id=uid) == 1
    assert await store_user_memory(gone, agent_name="Analyst", summary="asked") == 0


async def test_an_account_the_insert_did_not_create_keeps_its_tombstone(platform_jwt, memory_redis):
    uid = str(_USER_ID)
    await bury(memory_redis, account_tombstone(uid))

    user = await deps._resolve_trusted_platform_jwt("dummy", _ProvisioningDb(inserted=False))

    assert user.id == _USER_ID
    assert memory_redis.tombstones() == {account_tombstone(uid)}


@pytest.mark.parametrize("from_step", ["read", "lift"])
async def test_a_redis_outage_never_fails_the_sign_in(platform_jwt, monkeypatch, caplog, from_step):
    redis = _DownRedis(from_step=from_step)
    await bury(redis, account_tombstone(str(_USER_ID)))
    use_memory_redis(monkeypatch, redis)
    with caplog.at_level("WARNING", logger=deps.logger.name):
        user = await deps._resolve_trusted_platform_jwt("dummy", _ProvisioningDb(inserted=True))
    assert user.id == _USER_ID
    assert any("memory tombstone" in r.getMessage() for r in caplog.records)
    # A tombstone that could not be checked stays.
    assert redis.tombstones() == {account_tombstone(str(_USER_ID))}


class _AccountDb:
    def __init__(self, *, exists: bool):
        self._exists = exists

    async def execute(self, *_args, **_kwargs):
        return _Result(_USER_ID if self._exists else None)


@pytest.mark.parametrize("recreated", [True, False], ids=["re-created", "gone"])
async def test_a_purge_lifts_the_tombstone_of_an_account_re_created_while_it_ran(memory_redis, recreated):
    """The deletion buries the tombstone AFTER its commit: a platform sign-in
    re-creating the account in between lifts nothing yet, so the purge
    re-reads the account and lifts the tombstone itself."""
    from app.api.v1 import users as users_module

    uid = str(_USER_ID)
    await users_module._clear_deleted_user_memory(uid, [], db=_AccountDb(exists=recreated))
    assert (account_tombstone(uid) in memory_redis.tombstones()) is (not recreated)
    # What the account writes next is kept once it exists again, undone while it is gone.
    stored = await store_user_memory(uid, agent_name="Analyst", summary="asked")
    assert stored == (1 if recreated else 0)


async def _delete_the_account_again() -> None:
    """A later deletion of the re-created account: committed, then purged."""
    from app.api.v1 import users as users_module

    await users_module._clear_deleted_user_memory(str(_USER_ID), [], db=_AccountDb(exists=False))


class _DeletedAgainAfterTheReRead:
    """The purge's re-read finds the re-created account, which is deleted again
    before that purge lifts anything."""

    async def execute(self, *_args, **_kwargs):
        await _delete_the_account_again()
        return _Result(_USER_ID)


class _DeletedAgainAfterTheInsert(_ProvisioningDb):
    """The sign-in's INSERT commits, and the account is deleted again before the
    sign-in lifts anything."""

    async def commit(self):
        await _delete_the_account_again()


async def test_an_older_purge_never_lifts_the_tombstone_of_a_later_deletion(memory_redis):
    from app.api.v1 import users as users_module

    uid = str(_USER_ID)
    await users_module._clear_deleted_user_memory(uid, [], db=_DeletedAgainAfterTheReRead())

    assert account_tombstone(uid) in memory_redis.tombstones()
    assert await store_user_memory(uid, agent_name="Analyst", summary="asked") == 0


async def test_a_sign_in_never_lifts_the_tombstone_of_a_deletion_after_its_insert(platform_jwt, memory_redis):
    uid = str(_USER_ID)
    await bury(memory_redis, account_tombstone(uid))

    await deps._resolve_trusted_platform_jwt("dummy", _DeletedAgainAfterTheInsert(inserted=True))

    assert account_tombstone(uid) in memory_redis.tombstones()
    assert await store_user_memory(uid, agent_name="Analyst", summary="asked") == 0


async def test_an_older_purge_never_puts_its_tombstone_back_over_a_later_deletion(
    platform_jwt, memory_redis, monkeypatch
):
    """An older purge has buried the tombstone when a sign-in reads it and
    re-creates the account, which is deleted and purged again while that
    sign-in waits to lift; then the older purge runs on."""
    from app.api.v1 import users as users_module
    from app.services import agent_memory

    uid = str(_USER_ID)
    parked, lifting = asyncio.Event(), asyncio.Event()
    lift = deps._lift_memory_tombstone
    scoped_purge = agent_memory.clear_user_scoped_memory
    sign_ins: list[asyncio.Task] = []

    async def _held_lift(user_id, deletion):
        parked.set()
        await lifting.wait()
        await lift(user_id, deletion)

    async def _scoped_purge_after_a_sign_in(user_id, **kwargs):
        if not sign_ins:
            sign_ins.append(
                asyncio.create_task(
                    deps._resolve_trusted_platform_jwt("dummy", _DeletedAgainAfterTheInsert(inserted=True))
                )
            )
            await parked.wait()
        await scoped_purge(user_id, **kwargs)

    monkeypatch.setattr(deps, "_lift_memory_tombstone", _held_lift)
    monkeypatch.setattr(agent_memory, "clear_user_scoped_memory", _scoped_purge_after_a_sign_in)

    await users_module._clear_deleted_user_memory(uid, [], db=_AccountDb(exists=False))
    lifting.set()
    await sign_ins[0]

    assert account_tombstone(uid) in memory_redis.tombstones()
    assert await store_user_memory(uid, agent_name="Analyst", summary="asked") == 0
