"""A worker without ENTERPRISE_LICENSE keeps the background AI of a licensed deployment.

``ENTERPRISE_LICENSE`` is per process. Before the background gate a
worker deployed without it (the variable set on the platform container only)
ran every job anyway: nothing on the worker asked the licence. The gate asks it
at every run start, delegated task, evaluation and worker AI job, so that
worker resolved Community Edition and held all of them. ``licence_verdict``
lets it run on the licensed verdict the platform process publishes, signed with
the deployment's ``SECRET_KEY``.

Pinned here: the platform process publishes only when ``ENTERPRISE_LICENSE`` is
what licenses it, and publishes no license material; the worker runs on a valid
verdict and logs it once; Community Edition stays Community Edition; a forged,
expired or malformed verdict, a missing or unreachable Redis, and any process
that did not opt in change nothing.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.api.v1 import platform as platform_api
from app.core.config import settings
from app.services import agentic_gate, ai_state, licence_verdict
from app.services.agentic_gate import LICENSE_REQUIRED_CODE

BACKEND = Path(__file__).resolve().parent.parent

# Stands in for a license PEM: the verdict never reads it, only its presence.
_ENV_LICENSE = "-----BEGIN CERTIFICATE-----\nMIIB\n-----END CERTIFICATE-----\n"


class _FakeRedis:
    def __init__(self) -> None:
        self.store: dict[str, str] = {}
        self.writes = 0

    async def get(self, key):
        return self.store.get(key)

    async def set(self, key, value, ex=None):
        self.store[key] = value
        self.writes += 1
        return True


@pytest.fixture(autouse=True)
def _fresh_verdict_state(monkeypatch):
    monkeypatch.setattr(licence_verdict, "_publishing", False)
    monkeypatch.setattr(licence_verdict, "_accepting", False)
    monkeypatch.setattr(licence_verdict, "_last_publish_attempt", None)
    monkeypatch.setattr(licence_verdict, "_warned", False)


@pytest.fixture
def redis(monkeypatch):
    fake = _FakeRedis()

    async def _client():
        return fake

    monkeypatch.setattr("app.core.redis.redis_client", _client)
    return fake


def _process(monkeypatch, *, licensed: bool, env_license: str = "") -> None:
    """One process of the deployment: AI switched on, its OWN licence answer, and
    its own ``ENTERPRISE_LICENSE``. The gate's cached answer is dropped, as a
    fresh process has none."""

    async def _ai(_db):
        return True

    async def _licence(_db):
        return licensed

    monkeypatch.setattr(ai_state, "is_ai_enabled", _ai)
    monkeypatch.setattr(platform_api, "is_agentic_ai_licensed", _licence)
    monkeypatch.setattr(settings, "enterprise_license", env_license)
    ai_state.prime_cache(True)
    agentic_gate.invalidate_cache()
    agentic_gate._last_known = None


def _as_web(monkeypatch) -> None:
    monkeypatch.setattr(licence_verdict, "_publishing", True)
    monkeypatch.setattr(licence_verdict, "_accepting", False)


def _as_worker(monkeypatch) -> None:
    monkeypatch.setattr(licence_verdict, "_publishing", False)
    monkeypatch.setattr(licence_verdict, "_accepting", True)


async def _web_publishes(monkeypatch) -> None:
    _as_web(monkeypatch)
    _process(monkeypatch, licensed=True, env_license=_ENV_LICENSE)
    assert await agentic_gate.agentic_refusal(None) is None


@pytest.mark.asyncio
async def test_a_worker_without_the_license_runs_on_the_platforms_verdict(monkeypatch, redis, caplog):
    await _web_publishes(monkeypatch)

    _as_worker(monkeypatch)
    _process(monkeypatch, licensed=False)
    with caplog.at_level(logging.WARNING, logger=licence_verdict.__name__):
        assert await agentic_gate.agentic_refusal(None) is None
        agentic_gate.invalidate_cache()
        assert await agentic_gate.agentic_refusal(None) is None
    warnings = [r for r in caplog.records if r.name == licence_verdict.__name__]
    assert len(warnings) == 1, "said once per process, not per read"
    assert "ENTERPRISE_LICENSE" in warnings[0].getMessage()


@pytest.mark.asyncio
async def test_the_verdict_is_a_definitive_answer_not_a_transient_one(monkeypatch, redis):
    await _web_publishes(monkeypatch)
    _as_worker(monkeypatch)
    _process(monkeypatch, licensed=False)
    verdict = await agentic_gate.agentic_verdict(None)
    assert verdict.refusal is None and verdict.transient is False


@pytest.mark.asyncio
async def test_community_edition_stays_community_edition(monkeypatch, redis):
    _as_worker(monkeypatch)
    _process(monkeypatch, licensed=False)
    assert await agentic_gate.agentic_refusal(None) == LICENSE_REQUIRED_CODE
    assert redis.writes == 0


@pytest.mark.asyncio
async def test_the_platform_publishes_no_license_material(monkeypatch, redis):
    await _web_publishes(monkeypatch)
    stored = redis.store[licence_verdict.VERDICT_KEY]
    assert set(json.loads(stored)) == {"until", "sig"}
    assert "CERTIFICATE" not in stored and "MIIB" not in stored


@pytest.mark.asyncio
async def test_only_a_license_from_the_environment_is_published(monkeypatch, redis):
    """A license installed in Settings, or an Enterprise Edition platform, is
    read from the database by every process: nothing to share."""
    _as_web(monkeypatch)
    _process(monkeypatch, licensed=True, env_license="  ")
    assert await agentic_gate.agentic_refusal(None) is None
    assert redis.store == {}


@pytest.mark.asyncio
async def test_an_unlicensed_platform_publishes_nothing(monkeypatch, redis):
    _as_web(monkeypatch)
    _process(monkeypatch, licensed=False, env_license=_ENV_LICENSE)
    assert await agentic_gate.agentic_refusal(None) == LICENSE_REQUIRED_CODE
    assert redis.store == {}


@pytest.mark.asyncio
async def test_a_process_that_did_not_opt_in_neither_publishes_nor_accepts(monkeypatch, redis):
    _process(monkeypatch, licensed=True, env_license=_ENV_LICENSE)
    assert await agentic_gate.agentic_refusal(None) is None
    assert redis.store == {}

    await _web_publishes(monkeypatch)
    monkeypatch.setattr(licence_verdict, "_publishing", False)
    _process(monkeypatch, licensed=False)
    assert await agentic_gate.agentic_refusal(None) == LICENSE_REQUIRED_CODE


@pytest.mark.asyncio
async def test_publishing_is_rate_limited(monkeypatch, redis):
    clock = SimpleNamespace(now=1000.0)
    monkeypatch.setattr(
        licence_verdict,
        "time",
        SimpleNamespace(monotonic=lambda: clock.now, time=time.time),
    )
    _as_web(monkeypatch)
    monkeypatch.setattr(settings, "enterprise_license", _ENV_LICENSE)
    await licence_verdict.publish_licensed_verdict()
    await licence_verdict.publish_licensed_verdict()
    assert redis.writes == 1
    clock.now += licence_verdict.PUBLISH_INTERVAL_SECONDS + 1
    await licence_verdict.publish_licensed_verdict()
    assert redis.writes == 2


@pytest.mark.asyncio
async def test_a_verdict_signed_with_another_secret_key_is_refused(monkeypatch, redis):
    real_key = settings.secret_key
    monkeypatch.setattr(settings, "secret_key", "someone-elses-secret-key")
    await _web_publishes(monkeypatch)
    monkeypatch.setattr(settings, "secret_key", real_key)

    _as_worker(monkeypatch)
    _process(monkeypatch, licensed=False)
    assert await agentic_gate.agentic_refusal(None) == LICENSE_REQUIRED_CODE


@pytest.mark.asyncio
async def test_an_expired_verdict_is_refused(monkeypatch, redis):
    until = int(time.time()) - 1
    redis.store[licence_verdict.VERDICT_KEY] = json.dumps({"until": until, "sig": licence_verdict._signature(until)})
    _as_worker(monkeypatch)
    _process(monkeypatch, licensed=False)
    assert await agentic_gate.agentic_refusal(None) == LICENSE_REQUIRED_CODE


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "stored",
    [
        "not json",
        "[]",
        "42",
        json.dumps({"until": "soon", "sig": "x"}),
        # Ids of their own: a value holding the time differs between the
        # processes that collect it, and pytest-xdist needs one collection.
        pytest.param(json.dumps({"until": int(time.time()) + 600}), id="no-sig"),
        pytest.param(json.dumps({"until": int(time.time()) + 600, "sig": "\u00e9t\u00e9"}), id="non-ascii-sig"),
        # A lone high surrogate, which no codec encodes.
        pytest.param(json.dumps({"until": int(time.time()) + 600, "sig": "\ud800"}), id="lone-surrogate-sig"),
        # Numbers ``int()`` cannot take, or that are not integers at all.
        '{"until": 1e1000, "sig": "x"}',
        '{"until": NaN, "sig": "x"}',
        pytest.param(json.dumps({"until": float(int(time.time()) + 600), "sig": "x"}), id="float-until"),
        json.dumps({"until": True, "sig": "x"}),
        pytest.param(json.dumps({"until": int(time.time()) + 600, "sig": 7}), id="int-sig"),
        # Nested deeply enough to exhaust the parser's recursion. A short id:
        # pytest puts the test id in PYTEST_CURRENT_TEST, capped at 32767
        # characters on Windows.
        pytest.param("[" * 100_000 + "]" * 100_000, id="nested-100000-deep"),
    ],
)
async def test_a_malformed_verdict_is_refused(monkeypatch, redis, stored):
    redis.store[licence_verdict.VERDICT_KEY] = stored
    _as_worker(monkeypatch)
    _process(monkeypatch, licensed=False)
    assert await agentic_gate.agentic_refusal(None) == LICENSE_REQUIRED_CODE


@pytest.mark.asyncio
async def test_an_unreachable_redis_changes_neither_answer(monkeypatch):
    async def _down():
        raise ConnectionError("redis is down")

    monkeypatch.setattr("app.core.redis.redis_client", _down)
    _as_web(monkeypatch)
    _process(monkeypatch, licensed=True, env_license=_ENV_LICENSE)
    assert await agentic_gate.agentic_refusal(None) is None

    _as_worker(monkeypatch)
    _process(monkeypatch, licensed=False)
    assert await agentic_gate.agentic_refusal(None) == LICENSE_REQUIRED_CODE


@pytest.mark.asyncio
async def test_a_hanging_redis_is_bounded(monkeypatch):
    class _Hanging:
        async def get(self, key):
            await asyncio.sleep(30)

        async def set(self, key, value, ex=None):
            await asyncio.sleep(30)

    async def _client():
        return _Hanging()

    monkeypatch.setattr("app.core.redis.redis_client", _client)
    monkeypatch.setattr(licence_verdict, "REDIS_TIMEOUT_SECONDS", 0.05)
    _as_worker(monkeypatch)
    started = time.monotonic()
    assert await licence_verdict.peer_licensed_verdict() is False
    _as_web(monkeypatch)
    monkeypatch.setattr(settings, "enterprise_license", _ENV_LICENSE)
    await licence_verdict.publish_licensed_verdict()
    assert time.monotonic() - started < 5


def test_the_platform_shares_and_the_worker_accepts_at_startup():
    """The two opt-ins live where the two processes start: the web lifespan
    publishes, the SAQ worker startup accepts."""
    main_source = (BACKEND / "app" / "main.py").read_text(encoding="utf-8")
    worker_source = (BACKEND / "app" / "worker.py").read_text(encoding="utf-8")
    assert "share_licensed_verdicts()" in main_source
    assert "accept_licensed_verdicts()" not in main_source
    assert "accept_licensed_verdicts()" in worker_source
    assert "share_licensed_verdicts()" not in worker_source
