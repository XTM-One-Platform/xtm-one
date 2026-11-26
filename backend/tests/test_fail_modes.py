"""Fail-mode regression tests for rate limiting and quota enforcement.

Pins the SEC-10 / OBS-4 hardening contract:

* ``enforce_rate_limit`` — ``RATE_LIMIT_FAIL_MODE=closed`` turns a Redis
  failure into HTTP 503 (limits cannot be bypassed by degrading the backing
  store); the default ``open`` keeps the historical skip-the-check behaviour
  so a Redis hiccup never locks everyone out of authentication.
* Quota enforcement — ``QUOTA_FAIL_MODE=closed`` surfaces the shared
  ``QUOTA_UNAVAILABLE_MESSAGE`` refusal through each of the three
  enforcement sites (agent chat, general chat, assignment engine) when the
  quota backend raises; in Copilot / dev mode the default ``open`` proceeds
  unmetered (with an ERROR log) exactly as before. In XTM One mode quotas
  always fail closed, ``QUOTA_FAIL_MODE=open`` included.
* Sandbox-dispatch combined agentic+sandbox quota check (spec
  002-sandbox-quota-usage-tracking FR-019) — reads ``QUOTA_FAIL_MODE`` when
  the backend is unreachable; per-call-site coverage lives in
  ``test_opensandbox_sandbox_mode.py``, this file adds one representative
  case for parity with the other three enforcement sites above.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.core import config, rate_limit
from app.services.quota import QUOTA_UNAVAILABLE_MESSAGE, quota_fail_closed
from tests.agentic_gate_stub import stub_agentic_gate

# ── Helpers ──────────────────────────────────────────────────────────────


class _BrokenRedis:
    """Redis stub whose every operation fails (simulated outage)."""

    async def eval(self, *_a, **_kw):
        raise RuntimeError("redis is down")


def _patch_broken_redis(monkeypatch):
    async def _client():
        return _BrokenRedis()

    monkeypatch.setattr(rate_limit, "redis_client", _client)


def _request_stub():
    return SimpleNamespace(
        client=SimpleNamespace(host="203.0.113.20"), headers={}
    )


async def _raise_quota_backend_error(*_a, **_kw):
    raise RuntimeError("quota backend unreachable")


def _xtm_one_with_quota_fail_mode_open(monkeypatch, mode: str = "xtm_one") -> None:
    """XTM One mode with ``QUOTA_FAIL_MODE=open`` set, which it ignores."""
    monkeypatch.setattr(config.settings, "platform_mode", mode)
    monkeypatch.setattr(config.settings, "quota_fail_mode", "open")
    monkeypatch.setattr(config.settings, "_environment_fields", frozenset({"quota_fail_mode"}))


# ── quota_fail_closed helper ─────────────────────────────────────────────


@pytest.mark.parametrize("mode", ["xtm_one", "ai_disabled"])
def test_quota_fail_closed_is_always_true_in_xtm_one(monkeypatch, mode):
    _xtm_one_with_quota_fail_mode_open(monkeypatch, mode)
    assert quota_fail_closed() is True


def test_quota_fail_closed_parses_mode(monkeypatch):
    monkeypatch.setattr(config.settings, "platform_mode", "copilot")
    monkeypatch.setattr(config.settings, "quota_fail_mode", "open")
    assert quota_fail_closed() is False
    monkeypatch.setattr(config.settings, "quota_fail_mode", "closed")
    assert quota_fail_closed() is True
    # Whitespace / case tolerance — an env var like " Closed " must count.
    monkeypatch.setattr(config.settings, "quota_fail_mode", " Closed ")
    assert quota_fail_closed() is True


def test_fail_mode_settings_validate_at_startup():
    """A typo in RATE_LIMIT_FAIL_MODE / QUOTA_FAIL_MODE (e.g. ``close`` or
    ``strict``) must fail at startup instead of silently meaning "open" —
    for a security fail-closed flag, a typo degrading to fail-open is
    exactly the bypass the flag exists to prevent."""
    from pydantic import ValidationError

    cfg = config.Settings(
        _env_file=None, rate_limit_fail_mode=" Closed ", quota_fail_mode="OPEN"
    )
    assert cfg.rate_limit_fail_mode == "closed"
    assert cfg.quota_fail_mode == "open"

    with pytest.raises(ValidationError):
        config.Settings(_env_file=None, quota_fail_mode="close")
    with pytest.raises(ValidationError):
        config.Settings(_env_file=None, rate_limit_fail_mode="strict")


# ── Rate limiter fail modes ──────────────────────────────────────────────


async def test_rate_limit_fail_open_passes_on_redis_failure(monkeypatch):
    """Default mode: a Redis outage skips the check (historical contract)."""
    _patch_broken_redis(monkeypatch)
    monkeypatch.setattr(config.settings, "rate_limit_fail_mode", "open")
    monkeypatch.setattr(config.settings, "trust_proxy_headers", False)

    # No exception — the request goes through.
    await rate_limit.enforce_rate_limit(
        _request_stub(), "login", limit=10, window_seconds=60
    )


async def test_rate_limit_fail_closed_raises_503_on_redis_failure(monkeypatch):
    """RATE_LIMIT_FAIL_MODE=closed: a Redis outage must reject with 503 so
    the limiter cannot be bypassed by degrading the backing store."""
    _patch_broken_redis(monkeypatch)
    monkeypatch.setattr(config.settings, "rate_limit_fail_mode", "closed")
    monkeypatch.setattr(config.settings, "trust_proxy_headers", False)

    with pytest.raises(HTTPException) as exc:
        await rate_limit.enforce_rate_limit(
            _request_stub(), "login", limit=10, window_seconds=60
        )
    assert exc.value.status_code == 503


async def test_rate_limit_fail_closed_does_not_change_happy_path(monkeypatch):
    """Closed mode must only affect the Redis-failure branch — a healthy
    Redis still throttles with 429 past the limit and passes under it."""

    class _CountingRedis:
        def __init__(self):
            self.count = 0

        async def eval(self, _script, _numkeys, _key, _window):
            self.count += 1
            return self.count

    fake = _CountingRedis()

    async def _client():
        return fake

    monkeypatch.setattr(rate_limit, "redis_client", _client)
    monkeypatch.setattr(config.settings, "rate_limit_fail_mode", "closed")
    monkeypatch.setattr(config.settings, "trust_proxy_headers", False)

    await rate_limit.enforce_rate_limit(
        _request_stub(), "login", limit=2, window_seconds=60
    )
    await rate_limit.enforce_rate_limit(
        _request_stub(), "login", limit=2, window_seconds=60
    )
    with pytest.raises(HTTPException) as exc:
        await rate_limit.enforce_rate_limit(
            _request_stub(), "login", limit=2, window_seconds=60
        )
    assert exc.value.status_code == 429


# ── Quota fail-closed: agent chat ────────────────────────────────────────


async def test_agent_chat_quota_fail_closed_refuses_request(monkeypatch):
    """``run_agent_chat`` must return the shared quota-unavailable refusal
    (same tuple shape as the quota-exceeded reply) when the quota backend
    raises and QUOTA_FAIL_MODE=closed."""
    from app.services.agent_chat import run_agent_chat

    monkeypatch.setattr(config.settings, "quota_fail_mode", "closed")
    monkeypatch.setattr(
        "app.services.quota.check_agentic_quota", _raise_quota_backend_error
    )

    agent = SimpleNamespace(id=uuid.uuid4(), name="Test Agent")
    response, new_agent_id = await run_agent_chat(
        db=object(),  # unused before the quota gate (conversation_id=None)
        user_id=str(uuid.uuid4()),
        agent=agent,
        history=[],
        user_message="hello",
    )

    assert response == QUOTA_UNAVAILABLE_MESSAGE
    assert new_agent_id is None


async def test_agent_chat_refuses_in_xtm_one_even_with_quota_fail_mode_open(monkeypatch):
    from app.services.agent_chat import run_agent_chat

    _xtm_one_with_quota_fail_mode_open(monkeypatch)
    monkeypatch.setattr(
        "app.services.quota.check_agentic_quota", _raise_quota_backend_error
    )

    response, new_agent_id = await run_agent_chat(
        db=object(),  # unused before the quota gate (conversation_id=None)
        user_id=str(uuid.uuid4()),
        agent=SimpleNamespace(id=uuid.uuid4(), name="Test Agent"),
        history=[],
        user_message="hello",
    )

    assert response == QUOTA_UNAVAILABLE_MESSAGE
    assert new_agent_id is None


# ── Quota fail-closed: general chat ──────────────────────────────────────


async def test_general_chat_quota_fail_closed_refuses_request(monkeypatch):
    from app.services.general_chat import run_general_chat

    monkeypatch.setattr(config.settings, "quota_fail_mode", "closed")
    monkeypatch.setattr(
        "app.services.quota.check_agentic_quota", _raise_quota_backend_error
    )

    response, new_agent_id = await run_general_chat(
        db=object(),  # unused before the quota gate
        user_id=str(uuid.uuid4()),
        history=[],
        user_message="hello",
    )

    assert response == QUOTA_UNAVAILABLE_MESSAGE
    assert new_agent_id is None


async def test_general_chat_refuses_in_xtm_one_even_with_quota_fail_mode_open(monkeypatch):
    from app.services.general_chat import run_general_chat

    _xtm_one_with_quota_fail_mode_open(monkeypatch)
    monkeypatch.setattr(
        "app.services.quota.check_agentic_quota", _raise_quota_backend_error
    )

    response, new_agent_id = await run_general_chat(
        db=object(),  # unused before the quota gate
        user_id=str(uuid.uuid4()),
        history=[],
        user_message="hello",
    )

    assert response == QUOTA_UNAVAILABLE_MESSAGE
    assert new_agent_id is None


# ── Quota fail modes: assignment engine ──────────────────────────────────


class _FakeResult:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value

    def scalars(self):
        return self

    def all(self):
        return [self._value]


class _FakeSession:
    """Returns the queued scalar results in order: run, assignment, agent,
    then the run's context as the charge reads it back under its lock."""

    def __init__(self, results):
        self._results = list(results)
        self.commits = 0

    async def execute(self, _stmt):
        return _FakeResult(self._results.pop(0))

    async def commit(self):
        self.commits += 1

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_a):
        return False


class _StopAfterQuotaGate(BaseException):
    """Sentinel raised by the patched ``_publish_run_status`` to stop the
    engine right after the quota gate. BaseException-derived so no broad
    ``except Exception`` inside the engine can swallow it."""


def _build_assignment_run_stubs():
    run = SimpleNamespace(
        id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        assignment_id=uuid.uuid4(),
        agent_id=uuid.uuid4(),
        status="pending",
        error=None,
        started_at=None,
        is_company_managed=False,
        trigger_context=None,
    )
    assignment = SimpleNamespace(
        enabled=True,
        name="Test Assignment",
        user_id=None,
        is_company_managed=False,
        trigger_config={},
    )
    agent = SimpleNamespace(enabled=True, user_id=None)
    return run, assignment, agent


async def test_assignment_quota_fail_closed_fails_run(monkeypatch):
    """QUOTA_FAIL_MODE=closed: a quota-backend failure must fail the run
    with the shared refusal message (mirroring the quota-exceeded path)
    instead of executing unmetered."""
    from app.services import assignment_engine

    run, assignment, agent = _build_assignment_run_stubs()
    session = _FakeSession([run, assignment, agent])
    monkeypatch.setattr(assignment_engine, "async_session", lambda: session)
    stub_agentic_gate(monkeypatch)
    monkeypatch.setattr(config.settings, "quota_fail_mode", "closed")
    monkeypatch.setattr(
        "app.services.quota.check_agentic_quota", _raise_quota_backend_error
    )

    async def _must_not_publish(*_a, **_kw):
        raise AssertionError(
            "fail-closed must return before the run is marked running"
        )

    monkeypatch.setattr(assignment_engine, "_publish_run_status", _must_not_publish)

    await assignment_engine.execute_assignment_run(str(run.id))

    assert run.status == "failed"
    assert run.error == QUOTA_UNAVAILABLE_MESSAGE
    assert session.commits >= 1, "the failed status must be committed"


async def test_assignment_run_fails_in_xtm_one_even_with_quota_fail_mode_open(monkeypatch):
    from app.services import assignment_engine

    run, assignment, agent = _build_assignment_run_stubs()
    session = _FakeSession([run, assignment, agent])
    monkeypatch.setattr(assignment_engine, "async_session", lambda: session)
    stub_agentic_gate(monkeypatch)
    _xtm_one_with_quota_fail_mode_open(monkeypatch)
    monkeypatch.setattr(
        "app.services.quota.check_agentic_quota", _raise_quota_backend_error
    )

    async def _must_not_publish(*_a, **_kw):
        raise AssertionError("an XTM One run must not execute unmetered")

    monkeypatch.setattr(assignment_engine, "_publish_run_status", _must_not_publish)

    await assignment_engine.execute_assignment_run(str(run.id))

    assert run.status == "failed"
    assert run.error == QUOTA_UNAVAILABLE_MESSAGE


async def test_assignment_quota_fail_open_proceeds(monkeypatch):
    """Copilot mode's default: a quota-backend failure logs and the run
    proceeds (historical contract). The sentinel in ``_publish_run_status``
    proves the engine got past the quota gate and marked the run running."""
    from app.services import assignment_engine

    run, assignment, agent = _build_assignment_run_stubs()
    session = _FakeSession([run, assignment, agent])
    monkeypatch.setattr(assignment_engine, "async_session", lambda: session)
    stub_agentic_gate(monkeypatch)
    monkeypatch.setattr(config.settings, "platform_mode", "copilot")
    monkeypatch.setattr(config.settings, "quota_fail_mode", "open")
    monkeypatch.setattr(
        "app.services.quota.check_agentic_quota", _raise_quota_backend_error
    )

    async def _stop(*_a, **_kw):
        raise _StopAfterQuotaGate

    monkeypatch.setattr(assignment_engine, "_publish_run_status", _stop)

    with pytest.raises(_StopAfterQuotaGate):
        await assignment_engine.execute_assignment_run(str(run.id))

    assert run.status == "running", (
        "fail-open must let execution continue past the quota gate"
    )
    assert run.error != QUOTA_UNAVAILABLE_MESSAGE


# ── Quota fail modes: sandbox dispatch (spec 002 FR-019) ─────────────────


async def test_sandbox_dispatch_combined_quota_check_reads_quota_fail_mode_not_sandbox(
    monkeypatch,
):
    """The sandbox-dispatch combined-quota check must honor
    ``QUOTA_FAIL_MODE`` — the same setting the other three enforcement
    sites above already respect."""
    from app.services import custom_tool_executor as cte
    from app.services import quota as quota_module

    monkeypatch.setattr(
        quota_module, "check_agentic_quota", _raise_quota_backend_error
    )
    monkeypatch.setattr(config.settings, "quota_fail_mode", "closed")
    # Must not depend on the ambient disable_custom_tools default, which
    # varies by platform_mode (derives to is_xtm_one when unset) and by
    # environment (a local dev .env can override it) — pin it explicitly so
    # this test exercises the quota gate, not the disabled-tools guard.
    monkeypatch.setattr(config.settings, "disable_custom_tools", False)

    with pytest.raises(RuntimeError, match=QUOTA_UNAVAILABLE_MESSAGE):
        await cte.execute_js_tool_opensandbox(
            "console.log('hi');", {}, user_id=uuid.uuid4()
        )


async def test_sandbox_dispatch_refuses_in_xtm_one_even_with_quota_fail_mode_open(monkeypatch):
    from app.services import custom_tool_executor as cte
    from app.services import quota as quota_module

    monkeypatch.setattr(
        quota_module, "check_agentic_quota", _raise_quota_backend_error
    )
    _xtm_one_with_quota_fail_mode_open(monkeypatch)
    monkeypatch.setattr(config.settings, "disable_custom_tools", False)

    with pytest.raises(RuntimeError, match=QUOTA_UNAVAILABLE_MESSAGE):
        await cte.execute_js_tool_opensandbox(
            "console.log('hi');", {}, user_id=uuid.uuid4()
        )
