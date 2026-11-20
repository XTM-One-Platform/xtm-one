"""Unit tests for the Redis HA factory in ``app.core.redis``.

The factory runs at import time, so each test mutates ``settings`` and then
reloads the ``app.core.redis`` module to exercise a specific mode.

These tests are pure unit tests — no real Redis (or Sentinel) is
contacted; we patch the ``redis.asyncio`` constructors to capture the
arguments the factory builds.
"""

from __future__ import annotations

import importlib
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import app.core.config as _config_mod


def _settings():
    """Always return the *current* settings singleton.

    Another test may ``importlib.reload(app.core.config)`` which replaces
    the module-level ``settings`` object.  Accessing it via the config
    module (not a stale ``from … import settings`` binding) guarantees we
    see the live singleton.
    """
    return _config_mod.settings


@pytest.fixture(autouse=True)
def _restore_redis_settings():
    """Snapshot and restore Redis-related settings around each test."""
    s = _settings()
    snapshot = {
        "redis_mode": s.redis_mode,
        "redis_url": s.redis_url,
        "redis_sentinel_hosts": s.redis_sentinel_hosts,
        "redis_sentinel_service": s.redis_sentinel_service,
        "redis_sentinel_password": s.redis_sentinel_password,
        "redis_password": s.redis_password,
        "redis_username": s.redis_username,
        "redis_db": s.redis_db,
        "redis_ssl": s.redis_ssl,
        "redis_ssl_ca_certs": s.redis_ssl_ca_certs,
        "redis_ssl_cert_reqs": s.redis_ssl_cert_reqs,
        "redis_key_prefix": s.redis_key_prefix,
    }
    yield
    s = _settings()
    for k, v in snapshot.items():
        setattr(s, k, v)
    # Reload to leave the module in standalone (default) state.
    import app.core.redis as redis_module

    importlib.reload(redis_module)


def _load_redis_module():
    module_name = "app.core.redis"
    existing = sys.modules.get(module_name)
    if existing is None:
        return importlib.import_module(module_name)
    return importlib.reload(existing)


def test_parse_hosts_basic():
    redis_module = _load_redis_module()

    assert redis_module._parse_hosts("a:1,b:2", 26379) == [("a", 1), ("b", 2)]
    assert redis_module._parse_hosts("a , b:9", 26379) == [("a", 26379), ("b", 9)]
    assert redis_module._parse_hosts("", 26379) == []


def test_parse_hosts_invalid_port_raises():
    redis_module = _load_redis_module()

    with pytest.raises(ValueError):
        redis_module._parse_hosts("a:not-a-port", 26379)


def test_standalone_mode_uses_connection_pool():
    s = _settings()
    s.redis_mode = "standalone"
    s.redis_url = "redis://localhost:6379"

    with patch("redis.asyncio.ConnectionPool.from_url") as mock_from_url:
        mock_from_url.return_value = MagicMock()
        redis_module = _load_redis_module()

    assert mock_from_url.called
    call_kwargs = mock_from_url.call_args.kwargs
    assert call_kwargs["decode_responses"] is True
    assert call_kwargs["max_connections"] == s.redis_max_connections
    assert redis_module._pool is not None
    assert redis_module._sentinel is None


def test_sentinel_mode_builds_sentinel_client():
    s = _settings()
    s.redis_mode = "sentinel"
    s.redis_sentinel_hosts = "s1:26379, s2:26380, s3"
    s.redis_sentinel_service = "copilot-master"
    s.redis_sentinel_password = "sent-pw"
    s.redis_password = "data-pw"

    with patch("redis.asyncio.sentinel.Sentinel") as mock_sentinel_cls:
        instance = MagicMock()
        instance.master_for.return_value = MagicMock(name="master_client")
        mock_sentinel_cls.return_value = instance
        redis_module = _load_redis_module()

    args, kwargs = mock_sentinel_cls.call_args
    parsed_hosts = args[0]
    assert parsed_hosts == [("s1", 26379), ("s2", 26380), ("s3", 26379)]
    assert kwargs["sentinel_kwargs"]["password"] == "sent-pw"
    assert kwargs["password"] == "data-pw"
    assert kwargs["decode_responses"] is True
    assert kwargs["max_connections"] == s.redis_max_connections
    instance.master_for.assert_called_once_with(
        "copilot-master", redis_class=redis_module.Redis
    )
    assert redis_module._sentinel is instance


def test_sentinel_mode_applies_tls_to_sentinel_kwargs():
    """SSL kwargs must be present in sentinel_kwargs for TLS-backed Sentinels."""
    s = _settings()
    s.redis_mode = "sentinel"
    s.redis_sentinel_hosts = "s1:26379"
    s.redis_sentinel_service = "mymaster"
    s.redis_ssl = True
    s.redis_ssl_cert_reqs = "required"

    with patch("redis.asyncio.sentinel.Sentinel") as mock_sentinel_cls:
        instance = MagicMock()
        instance.master_for.return_value = MagicMock(name="master_client")
        mock_sentinel_cls.return_value = instance
        _load_redis_module()

    _, kwargs = mock_sentinel_cls.call_args
    sentinel_kw = kwargs["sentinel_kwargs"]
    assert sentinel_kw.get("ssl") is True
    assert sentinel_kw.get("ssl_cert_reqs") == "required"


def test_sentinel_mode_requires_hosts():
    s = _settings()
    s.redis_mode = "sentinel"
    s.redis_sentinel_hosts = ""

    # Pydantic validator rejects at settings level — but if the user bypasses
    # validation by mutating settings directly, the factory must also fail.
    with pytest.raises(RuntimeError):
        _load_redis_module()



def test_config_validator_rejects_unknown_mode():
    with pytest.raises(ValueError):
        _config_mod.Settings(redis_mode="garbage")  # type: ignore[call-arg]


def test_config_validator_rejects_sentinel_without_hosts():
    with pytest.raises(ValueError):
        _config_mod.Settings(redis_mode="sentinel", redis_sentinel_hosts="")  # type: ignore[call-arg]


def test_config_validator_rejects_cluster_mode():
    """Cluster mode was removed (SAQ is not cluster-safe). Pin the
    rejection so a future contributor doesn't silently re-add it
    without re-evaluating the SAQ ``BLMOVE`` / multi-key Lua
    constraints.
    """
    with pytest.raises(ValueError):
        _config_mod.Settings(redis_mode="cluster")  # type: ignore[call-arg]


def test_sentinel_client_accessor_raises_when_not_sentinel_mode():
    """Public ``sentinel_client()`` accessor must raise outside sentinel mode.

    Pins the contract that ``app.core.queue.get_queue()`` and any other
    consumer must branch on ``settings.redis_mode == "sentinel"`` before
    calling ``sentinel_client()``.
    """
    s = _settings()
    s.redis_mode = "standalone"
    s.redis_url = "redis://localhost:6379"

    with patch("redis.asyncio.ConnectionPool.from_url") as mock_from_url:
        mock_from_url.return_value = MagicMock()
        redis_module = _load_redis_module()

    with pytest.raises(RuntimeError):
        redis_module.sentinel_client()


def test_sentinel_client_accessor_returns_sentinel_instance_in_sentinel_mode():
    """``sentinel_client()`` must return the same ``Sentinel`` instance
    held in module-level state — so ``queue.get_queue()`` can call
    ``.master_for(...)`` on it without reaching into ``_sentinel``
    directly.
    """
    s = _settings()
    s.redis_mode = "sentinel"
    s.redis_sentinel_hosts = "s1:26379"
    s.redis_sentinel_service = "mymaster"

    with patch("redis.asyncio.sentinel.Sentinel") as mock_sentinel_cls:
        instance = MagicMock()
        instance.master_for.return_value = MagicMock(name="master_client")
        mock_sentinel_cls.return_value = instance
        redis_module = _load_redis_module()

    assert redis_module.sentinel_client() is instance



@pytest.mark.asyncio
async def test_redis_pubsub_client_sentinel_resolves_fresh_master():
    s = _settings()
    s.redis_mode = "sentinel"
    s.redis_sentinel_hosts = "s1:26379"
    s.redis_sentinel_service = "mymaster"

    with patch("redis.asyncio.sentinel.Sentinel") as mock_sentinel_cls:
        instance = MagicMock()
        instance.master_for.return_value = MagicMock(name="master_client")
        mock_sentinel_cls.return_value = instance
        redis_module = _load_redis_module()

        # First call during _build, then again via redis_pubsub_client.
        await redis_module.redis_pubsub_client()
        assert instance.master_for.call_count == 2



@pytest.mark.asyncio
async def test_close_pool_standalone():
    """close_pool() must aclose() the shared client and disconnect the pool."""
    s = _settings()
    s.redis_mode = "standalone"
    s.redis_url = "redis://localhost:6379"

    mock_pool = MagicMock()
    mock_pool.disconnect = AsyncMock()
    mock_client = MagicMock()
    mock_client.aclose = AsyncMock()

    with patch("redis.asyncio.ConnectionPool.from_url", return_value=mock_pool), patch(
        "redis.asyncio.Redis", return_value=mock_client
    ):
        redis_module = _load_redis_module()

    await redis_module.close_pool()
    mock_client.aclose.assert_called_once()
    mock_pool.disconnect.assert_called_once()


@pytest.mark.asyncio
async def test_close_pool_sentinel():
    """close_pool() must not crash in sentinel mode (no pool to disconnect)."""
    s = _settings()
    s.redis_mode = "sentinel"
    s.redis_sentinel_hosts = "s1:26379"
    s.redis_sentinel_service = "mymaster"

    mock_client = MagicMock()
    mock_client.aclose = AsyncMock()

    with patch("redis.asyncio.sentinel.Sentinel") as mock_sentinel_cls:
        instance = MagicMock()
        instance.master_for.return_value = mock_client
        mock_sentinel_cls.return_value = instance
        redis_module = _load_redis_module()

    await redis_module.close_pool()
    mock_client.aclose.assert_called_once()


@pytest.mark.asyncio
async def test_standalone_mode_without_a_prefix_builds_plain_clients():
    """No ``REDIS_KEY_PREFIX``: the clients are redis-py's own, unchanged."""
    import redis.asyncio as redis_asyncio

    s = _settings()
    s.redis_mode = "standalone"
    s.redis_url = "redis://localhost:6379"
    s.redis_key_prefix = ""
    redis_module = _load_redis_module()

    assert type(await redis_module.redis_client()) is redis_asyncio.Redis
    assert type(await redis_module.redis_pubsub_client()) is redis_asyncio.Redis


@pytest.mark.asyncio
async def test_standalone_mode_with_a_prefix_builds_prefixed_clients():
    from app.core.redis_key_prefix import prefixed_client_class

    s = _settings()
    s.redis_mode = "standalone"
    s.redis_url = "redis://localhost:6379"
    s.redis_key_prefix = "dev-xtmone1:"
    redis_module = _load_redis_module()

    prefixed = prefixed_client_class("dev-xtmone1:")
    assert isinstance(await redis_module.redis_client(), prefixed)
    assert isinstance(await redis_module.redis_pubsub_client(), prefixed)


@pytest.mark.asyncio
async def test_sentinel_mode_with_a_prefix_resolves_prefixed_clients():
    from app.core.redis_key_prefix import prefixed_client_class

    s = _settings()
    s.redis_mode = "sentinel"
    s.redis_sentinel_hosts = "s1:26379"
    s.redis_sentinel_service = "mymaster"
    s.redis_key_prefix = "dev-xtmone1:"

    with patch("redis.asyncio.sentinel.Sentinel") as mock_sentinel_cls:
        instance = MagicMock()
        instance.master_for.return_value = MagicMock(name="master_client")
        mock_sentinel_cls.return_value = instance
        redis_module = _load_redis_module()
        await redis_module.redis_pubsub_client()

    prefixed = prefixed_client_class("dev-xtmone1:")
    assert [c.kwargs["redis_class"] for c in instance.master_for.call_args_list] == [prefixed, prefixed]
