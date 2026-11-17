"""A quota resolution in flight never re-caches a config changed meanwhile.

``resolve_quota_config`` reads the config from PostgreSQL on a cache miss, then
writes the result to Redis. Every change commits, then drops the cached
resolution (the user, group and platform quota routes, a membership change); a
resolver that read the OLD config just before that commit and wrote the cache
just after the drop left the stale resolution cached for the whole TTL.

Two halves close it:

- every invalidation of this release replaces the config generation before it
  deletes, and a resolver writes only while the generation is still the one it
  read before the database: once such an invalidation has run, the old config is
  never published, not even for a moment;
- a written resolution is confirmed by a second read of the database and
  dropped when the two differ. That catches an invalidation that only deletes -
  the previous release's, which a rolling deploy still runs next to this one.
"""

from __future__ import annotations

import fnmatch
import json
import uuid
from types import SimpleNamespace

import pytest

from app.services import quota

_OLD = {"default": 10, "period": "monthly"}
_NEW = {"default": 3, "period": "monthly"}


class _FakeRedis:
    """The Redis commands the config cache uses, with the compare-and-set emulated."""

    def __init__(self) -> None:
        self.store: dict[str, str] = {}
        self.ops: list[tuple[str, str]] = []

    async def get(self, key):
        return self.store.get(key)

    async def set(self, key, value, ex=None):
        self.ops.append(("set", key))
        self.store[key] = value

    async def delete(self, *keys):
        for key in keys:
            self.ops.append(("delete", key))
            self.store.pop(key, None)

    async def scan_iter(self, match, count=None):
        for key in list(self.store):
            if fnmatch.fnmatch(key, match):
                yield key

    async def eval(self, script, numkeys, *args):
        assert script == quota._SET_IF_GENERATION_LUA
        keys, argv = args[:numkeys], args[numkeys:]
        if (self.store.get(keys[1]) or "") == str(argv[2]):
            self.ops.append(("set", keys[0]))
            self.store[keys[0]] = argv[0]
            return 1
        return 0


class _Db:
    """The user's own override: none, so the group level is read."""

    async def execute(self, _stmt):
        return SimpleNamespace(scalar_one_or_none=lambda: None)


@pytest.fixture
def fake_redis(monkeypatch):
    fake = _FakeRedis()

    async def _client():
        return fake

    monkeypatch.setattr(quota, "redis_client", _client)
    monkeypatch.setattr(quota.settings, "platform_mode", "copilot")

    async def _no_global(_db):
        return None

    monkeypatch.setattr(quota, "_load_global_quota_config", _no_global)
    return fake


def _cached(redis: _FakeRedis, user_id: uuid.UUID) -> dict | None:
    entry = redis.store.get(quota._config_cache_key(user_id))
    return None if entry is None else json.loads(entry)


async def _previous_release_drop(redis, user_id):
    await redis.delete(quota._config_cache_key(user_id))


async def _previous_release_drop_all(redis, _user_id):
    async for key in redis.scan_iter(match=f"{quota._CONFIG_CACHE_PREFIX}*", count=200):
        await redis.delete(key)


_THIS_RELEASE = [
    pytest.param(lambda _redis, uid: quota.invalidate_quota_cache(uid), id="the user's quota"),
    pytest.param(lambda _redis, uid: quota.invalidate_membership_quota_cache([uid]), id="a membership"),
    pytest.param(lambda _redis, _uid: quota.invalidate_quota_cache(), id="every quota"),
]
_PREVIOUS_RELEASE = [
    pytest.param(_previous_release_drop, id="the previous release's drop"),
    pytest.param(_previous_release_drop_all, id="the previous release's drop of every quota"),
]


def _group_quota(monkeypatch, *, reads: list[str] | None = None) -> dict:
    """The group's quota as committed; the test replaces it to commit a change."""
    committed = {"agentic": dict(_OLD)}

    async def _groups(_db, _uid):
        if reads is not None:
            reads.append("groups")
        return [dict(committed)]

    monkeypatch.setattr(quota, "_load_user_groups_with_quotas", _groups)
    return committed


def _changing_during_the_first_read(monkeypatch, redis, user_id, invalidate) -> None:
    committed = {"agentic": dict(_OLD)}

    async def _groups(_db, _uid):
        read = dict(committed)
        if committed["agentic"] == _OLD:
            # This resolver has read the old quota; the admin's change commits
            # and its invalidation runs before the resolver writes the cache.
            committed["agentic"] = dict(_NEW)
            await invalidate(redis, user_id)
        return [read]

    monkeypatch.setattr(quota, "_load_user_groups_with_quotas", _groups)


def _after_the_write(monkeypatch, redis, user_id, action) -> None:
    """Run *action* once, right after the resolver's write of the user's entry."""
    cache_key = quota._config_cache_key(user_id)
    real_set, real_eval = redis.set, redis.eval
    pending = [action]

    async def _fire(key):
        if key == cache_key and pending:
            await pending.pop()()

    async def _set(key, value, ex=None):
        await real_set(key, value, ex=ex)
        await _fire(key)

    async def _eval(script, numkeys, *args):
        result = await real_eval(script, numkeys, *args)
        await _fire(args[0])
        return result

    monkeypatch.setattr(redis, "set", _set)
    monkeypatch.setattr(redis, "eval", _eval)


@pytest.mark.parametrize("invalidate", _THIS_RELEASE + _PREVIOUS_RELEASE)
async def test_a_change_committed_during_a_resolution_is_not_undone(monkeypatch, fake_redis, invalidate):
    user_id = uuid.uuid4()
    _changing_during_the_first_read(monkeypatch, fake_redis, user_id, invalidate)

    await quota.resolve_quota_config(_Db(), fake_redis, user_id, "agentic")

    # The next check must not be answered from the config the change replaced.
    cached = _cached(fake_redis, user_id)
    assert cached is None or cached["agentic"] == _NEW
    assert await quota.resolve_quota_config(_Db(), fake_redis, user_id, "agentic") == _NEW


@pytest.mark.parametrize("invalidate", _THIS_RELEASE)
async def test_no_check_sees_the_replaced_config_once_its_invalidation_ran(monkeypatch, fake_redis, invalidate):
    user_id = uuid.uuid4()
    _changing_during_the_first_read(monkeypatch, fake_redis, user_id, invalidate)
    concurrent: list[dict | None] = []

    async def _concurrent_check():
        # Another request for the same user, while the resolver has not
        # confirmed anything yet: the admin's change has completed.
        concurrent.append(await quota.resolve_quota_config(_Db(), fake_redis, user_id, "agentic"))

    _after_the_write(monkeypatch, fake_redis, user_id, _concurrent_check)

    await quota.resolve_quota_config(_Db(), fake_redis, user_id, "agentic")

    assert concurrent in ([], [_NEW])
    assert _cached(fake_redis, user_id) in (None, {"agentic": _NEW})


async def test_a_change_committed_after_the_write_is_caught_by_the_second_read(monkeypatch, fake_redis):
    user_id = uuid.uuid4()
    committed = _group_quota(monkeypatch)

    async def _commit():
        # The change commits once the entry is written; its invalidation has
        # not run yet.
        committed["agentic"] = dict(_NEW)

    _after_the_write(monkeypatch, fake_redis, user_id, _commit)

    assert await quota.resolve_quota_config(_Db(), fake_redis, user_id, "agentic") == _NEW
    assert _cached(fake_redis, user_id) is None


async def test_an_undisturbed_resolution_is_cached_as_the_previous_release_reads_it(monkeypatch, fake_redis):
    user_id = uuid.uuid4()
    _group_quota(monkeypatch)

    assert await quota.resolve_quota_config(_Db(), fake_redis, user_id, "agentic") == _OLD

    # A replica of the previous release still reads this entry during a rolling
    # deploy, as ``json.loads(entry).get(quota_type)``: it holds the resolution
    # and nothing else.
    assert _cached(fake_redis, user_id) == {"agentic": _OLD}


async def test_a_cached_resolution_is_served_without_reading_the_database(monkeypatch, fake_redis):
    user_id = uuid.uuid4()
    reads: list[str] = []
    _group_quota(monkeypatch, reads=reads)

    first = await quota.resolve_quota_config(_Db(), fake_redis, user_id, "agentic")
    reads_on_a_miss = len(reads)
    second = await quota.resolve_quota_config(_Db(), fake_redis, user_id, "agentic")

    assert first == second == _OLD
    # The miss reads the config twice; the hit does not read it at all.
    assert reads_on_a_miss == 2
    assert len(reads) == reads_on_a_miss


async def test_an_entry_the_previous_release_cached_is_served(monkeypatch, fake_redis):
    user_id = uuid.uuid4()
    reads: list[str] = []
    _group_quota(monkeypatch, reads=reads)
    fake_redis.store[quota._config_cache_key(user_id)] = json.dumps({"agentic": _NEW})

    assert await quota.resolve_quota_config(_Db(), fake_redis, user_id, "agentic") == _NEW
    assert reads == []


async def test_a_second_read_that_fails_leaves_nothing_cached(monkeypatch, fake_redis):
    user_id = uuid.uuid4()
    reads = 0

    async def _groups(_db, _uid):
        nonlocal reads
        reads += 1
        if reads == 2:
            raise ConnectionError("the database dropped the connection")
        return [{"agentic": dict(_OLD)}]

    monkeypatch.setattr(quota, "_load_user_groups_with_quotas", _groups)

    # Unconfirmed, the entry may be the stale one: it goes, and the failure
    # surfaces like any failed read of the resolution.
    with pytest.raises(ConnectionError):
        await quota.resolve_quota_config(_Db(), fake_redis, user_id, "agentic")
    assert _cached(fake_redis, user_id) is None


async def test_a_stale_entry_that_cannot_be_dropped_never_fails_the_check(monkeypatch, fake_redis):
    user_id = uuid.uuid4()
    committed = _group_quota(monkeypatch)

    async def _commit():
        committed["agentic"] = dict(_NEW)

    async def _delete_fails(*_keys):
        raise ConnectionError("redis dropped the connection")

    _after_the_write(monkeypatch, fake_redis, user_id, _commit)
    monkeypatch.setattr(fake_redis, "delete", _delete_fails)

    # Answered from the second read; the entry is left to the change's own
    # invalidation, or to its TTL, like any drop that fails.
    assert await quota.resolve_quota_config(_Db(), fake_redis, user_id, "agentic") == _NEW


async def test_a_redis_that_refuses_the_script_still_caches_a_confirmed_resolution(monkeypatch, fake_redis):
    user_id = uuid.uuid4()
    _group_quota(monkeypatch)

    async def _no_scripts(*_args, **_kwargs):
        raise ConnectionError("NOPERM this user has no permissions to run the 'eval' command")

    monkeypatch.setattr(fake_redis, "eval", _no_scripts)

    # The plain write, confirmed by the second read as always.
    assert await quota.resolve_quota_config(_Db(), fake_redis, user_id, "agentic") == _OLD
    assert _cached(fake_redis, user_id) == {"agentic": _OLD}


@pytest.mark.parametrize("invalidate", _THIS_RELEASE)
async def test_every_invalidation_replaces_the_generation_before_it_deletes(fake_redis, invalidate):
    user_id = uuid.uuid4()
    cache_key = quota._config_cache_key(user_id)
    fake_redis.store[cache_key] = json.dumps({"agentic": _OLD})

    await invalidate(fake_redis, user_id)

    assert fake_redis.ops == [("set", quota._CONFIG_GENERATION_KEY), ("delete", cache_key)]


@pytest.mark.parametrize("invalidate", _THIS_RELEASE)
async def test_a_generation_that_cannot_be_replaced_never_skips_the_delete(monkeypatch, fake_redis, invalidate):
    user_id = uuid.uuid4()
    cache_key = quota._config_cache_key(user_id)
    fake_redis.store[cache_key] = json.dumps({"agentic": _OLD})
    real_set = fake_redis.set

    async def _set(key, value, ex=None):
        if key == quota._CONFIG_GENERATION_KEY:
            raise ConnectionError("redis dropped the connection")
        await real_set(key, value, ex=ex)

    monkeypatch.setattr(fake_redis, "set", _set)

    await invalidate(fake_redis, user_id)

    assert cache_key not in fake_redis.store


async def test_invalidating_everything_keeps_the_generation(fake_redis):
    fake_redis.store[quota._config_cache_key(uuid.uuid4())] = "{}"

    await quota.invalidate_quota_cache()
    first = fake_redis.store.get(quota._CONFIG_GENERATION_KEY)
    await quota.invalidate_quota_cache()

    # The scan over the cache prefix must not delete the generation, and each
    # invalidation writes a token of its own.
    assert set(fake_redis.store) == {quota._CONFIG_GENERATION_KEY}
    assert first and fake_redis.store[quota._CONFIG_GENERATION_KEY] != first
