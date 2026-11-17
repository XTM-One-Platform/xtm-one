"""The security audit's Redis check reports what Redis answers.

``_check_infrastructure`` called ``redis_client()`` without ``await``: the
coroutine has no ``ping``, so every audit reported "Redis connection failed"
as a critical finding and scored the platform ``at_risk``.
"""

from __future__ import annotations

import asyncio
import logging
import warnings
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.api.v1 import security_audit
from app.core.redis import Redis

_LOGGER = security_audit.logger.name


def _redis_findings(findings: list[security_audit.AuditFinding]) -> list[security_audit.AuditFinding]:
    return [f for f in findings if f.title.startswith("Redis connection")]


def _install_redis(monkeypatch, ping: AsyncMock) -> AsyncMock:
    """Replace ``redis_client`` with the real shape: awaited, returning a client whose ``ping`` is awaited."""
    client = MagicMock(spec=Redis)
    client.ping = ping
    factory = AsyncMock(return_value=client)
    monkeypatch.setattr(security_audit, "redis_client", factory)
    return factory


async def test_a_reachable_redis_passes(monkeypatch):
    ping = AsyncMock(return_value=True)
    factory = _install_redis(monkeypatch, ping)
    findings: list[security_audit.AuditFinding] = []
    with warnings.catch_warnings():
        warnings.simplefilter("error", RuntimeWarning)  # "coroutine ... was never awaited"
        await security_audit._check_infrastructure(findings)
    (finding,) = _redis_findings(findings)
    assert finding.severity == "pass"
    factory.assert_awaited_once_with()
    ping.assert_awaited_once_with()


@pytest.mark.parametrize("where", ["client", "ping"])
async def test_an_unreachable_redis_is_still_a_critical_finding(monkeypatch, caplog, where):
    factory = _install_redis(monkeypatch, AsyncMock(side_effect=ConnectionError("Error 111 connecting to redis:6379")))
    if where == "client":
        factory.side_effect = ConnectionError("Error 111 connecting to redis:6379")
    findings: list[security_audit.AuditFinding] = []
    with caplog.at_level(logging.WARNING, logger=_LOGGER):
        await security_audit._check_infrastructure(findings)
    (finding,) = _redis_findings(findings)
    assert finding.severity == "critical"
    assert "Error 111 connecting to redis:6379" in caplog.text


async def test_a_redis_that_never_answers_fails_within_the_bound(monkeypatch, caplog):
    cancelled = asyncio.Event()

    async def _never_answers() -> bool:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise
        return True

    _install_redis(monkeypatch, AsyncMock(side_effect=_never_answers))
    monkeypatch.setattr(security_audit, "_REDIS_PING_TIMEOUT_SECONDS", 0.05)
    findings: list[security_audit.AuditFinding] = []
    with caplog.at_level(logging.WARNING, logger=_LOGGER):
        await asyncio.wait_for(security_audit._check_infrastructure(findings), timeout=5)
    (finding,) = _redis_findings(findings)
    assert finding.severity == "critical"
    # The unanswered ping is cancelled, not left running: redis-py drops the
    # connection of a cancelled command rather than returning it mid-reply.
    assert cancelled.is_set()
    # A timeout has an empty message; the log still says what happened.
    assert "TimeoutError" in caplog.text


@pytest.mark.parametrize(
    ("mode", "named", "not_named"),
    [
        ("standalone", "REDIS_URL", "REDIS_SENTINEL"),
        ("sentinel", "REDIS_SENTINEL_HOSTS", "REDIS_URL"),
    ],
)
async def test_the_recommendation_names_the_settings_of_the_redis_mode(monkeypatch, mode, named, not_named):
    _install_redis(monkeypatch, AsyncMock(side_effect=ConnectionError("refused")))
    monkeypatch.setattr(security_audit.settings, "redis_mode", mode)
    findings: list[security_audit.AuditFinding] = []
    await security_audit._check_infrastructure(findings)
    (finding,) = _redis_findings(findings)
    assert named in finding.recommendation
    assert not_named not in finding.recommendation
