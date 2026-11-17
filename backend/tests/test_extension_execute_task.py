"""Tests for ``POST /api/v1/extension/execute-task``.

Covers:
  - Slug prefix restriction (only ``browser-*`` slugs allowed).
  - Quota enforcement (429 with Retry-After header).
  - Agent resolution (404 when missing or disabled).
  - Stateless execution via ``run_agent_chat``.
  - JSON fence stripping (```json … ```, ``` … ```).
  - ``{"response": …}`` envelope unwrapping.
  - Empty / non-JSON agent output handling.
  - 500 when the agentic loop raises.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import deps
from app.api.v1 import extension as extension_mod
from app.core.database import get_db
from app.services.quota import QuotaExceededError

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_VALID_BODY = {"agent_slug": "browser-scan-all", "content": '{"pageTitle":"t"}'}


class _ScalarOneResult:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


class _FakeDb:
    async def execute(self, _stmt):
        return _ScalarOneResult(
            SimpleNamespace(id=uuid.uuid4(), slug="browser-scan-all", enabled=True)
        )

    async def commit(self):
        return None


def _fake_user():
    return SimpleNamespace(id=uuid.uuid4(), is_admin=False)


def _client(fake_db=None, user=None):
    app = FastAPI()
    app.include_router(extension_mod.router, prefix="/api/v1/extension")
    app.dependency_overrides[deps.get_current_user] = lambda: user or _fake_user()
    app.dependency_overrides[deps.require_chat_enabled] = lambda: None

    db = fake_db or _FakeDb()

    async def _get_db():
        yield db

    app.dependency_overrides[get_db] = _get_db
    return TestClient(app, raise_server_exceptions=False)


def _client_ce_only(fake_db=None, user=None):
    """Test client that simulates a Community Edition deployment (chat gate active)."""
    from fastapi import HTTPException

    app = FastAPI()
    app.include_router(extension_mod.router, prefix="/api/v1/extension")
    app.dependency_overrides[deps.get_current_user] = lambda: user or _fake_user()

    async def _blocked():
        raise HTTPException(status_code=403, detail="Chat is unavailable on this deployment.")

    app.dependency_overrides[deps.require_chat_enabled] = _blocked

    db = fake_db or _FakeDb()

    async def _get_db():
        yield db

    app.dependency_overrides[get_db] = _get_db
    return TestClient(app, raise_server_exceptions=False)


# ---------------------------------------------------------------------------
# Slug restriction
# ---------------------------------------------------------------------------


def test_rejects_non_browser_slug():
    with _client() as c:
        resp = c.post(
            "/api/v1/extension/execute-task",
            json={"agent_slug": "opencti-assistant", "content": "{}"},
        )
    # Pydantic validation rejects it before the handler (pattern constraint)
    assert resp.status_code == 422


def test_rejects_slug_without_browser_prefix():
    """Even if Pydantic is bypassed, the handler enforces the prefix."""
    # The schema regex ``^browser-[a-z0-9-]+$`` already rejects this, so
    # we get a 422 from Pydantic validation.
    with _client() as c:
        resp = c.post(
            "/api/v1/extension/execute-task",
            json={"agent_slug": "malicious-agent", "content": "{}"},
        )
    assert resp.status_code == 422


def test_ce_only_deployment_returns_403():
    """execute-task must be blocked on Community Edition (no license, no EE platform)."""
    with _client_ce_only() as c:
        resp = c.post("/api/v1/extension/execute-task", json=_VALID_BODY)
    assert resp.status_code == 403


# ---------------------------------------------------------------------------
# Quota enforcement
# ---------------------------------------------------------------------------


def test_quota_exceeded_returns_429_with_retry_after():
    exc = QuotaExceededError("agentic", limit=100, current=100, period="daily")

    with (
        patch.object(extension_mod, "get_user_group_ids", new_callable=AsyncMock, return_value=[]),
        patch.object(extension_mod, "check_agentic_quota", new_callable=AsyncMock, side_effect=exc),
        patch.object(extension_mod, "seconds_until_period_reset", return_value=3600),
        _client() as c,
    ):
        resp = c.post("/api/v1/extension/execute-task", json=_VALID_BODY)

    assert resp.status_code == 429
    assert resp.headers["retry-after"] == "3600"


# ---------------------------------------------------------------------------
# Agent resolution
# ---------------------------------------------------------------------------


def test_agent_not_found_returns_404():
    class _MissDb:
        async def execute(self, _stmt):
            return _ScalarOneResult(None)

    with (
        patch.object(extension_mod, "get_user_group_ids", new_callable=AsyncMock, return_value=[]),
        _client(fake_db=_MissDb()) as c,
    ):
        resp = c.post("/api/v1/extension/execute-task", json=_VALID_BODY)

    assert resp.status_code == 404
    assert "not found" in resp.json()["detail"].lower()


# ---------------------------------------------------------------------------
# Successful execution + JSON parsing
# ---------------------------------------------------------------------------

_JSON_FENCE_CASES = [
    ("bare JSON", '{"entities": []}', {"entities": []}),
    ("```json fence", '```json\n{"entities": []}\n```', {"entities": []}),
    ("``` fence (no lang)", '```\n{"a": 1}\n```', {"a": 1}),
    ("response-envelope unwrap", '{"response": {"ok": true}}', {"ok": True}),
    ("response-envelope with extra keys kept", '{"response": 1, "x": 2}', {"response": 1, "x": 2}),
]


@pytest.mark.parametrize("label,agent_output,expected_data", _JSON_FENCE_CASES, ids=[c[0] for c in _JSON_FENCE_CASES])
def test_json_parsing(label, agent_output, expected_data):
    with (
        patch.object(extension_mod, "get_user_group_ids", new_callable=AsyncMock, return_value=[]),
        patch.object(extension_mod, "check_agentic_quota", new_callable=AsyncMock),
        patch.object(extension_mod, "increment_agentic_usage", new_callable=AsyncMock),
        patch.object(extension_mod, "run_agent_chat", new_callable=AsyncMock, return_value=(agent_output, None)),
        _client() as c,
    ):
        resp = c.post("/api/v1/extension/execute-task", json=_VALID_BODY)

    assert resp.status_code == 200
    assert resp.json()["data"] == expected_data


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("agent_output", ["", "   ", None], ids=["empty", "whitespace", "None"])
def test_empty_agent_output(agent_output):
    with (
        patch.object(extension_mod, "get_user_group_ids", new_callable=AsyncMock, return_value=[]),
        patch.object(extension_mod, "check_agentic_quota", new_callable=AsyncMock),
        patch.object(extension_mod, "increment_agentic_usage", new_callable=AsyncMock),
        patch.object(extension_mod, "run_agent_chat", new_callable=AsyncMock, return_value=(agent_output, None)),
        _client() as c,
    ):
        resp = c.post("/api/v1/extension/execute-task", json=_VALID_BODY)

    assert resp.status_code == 200
    body = resp.json()
    assert body["data"] is None
    assert "empty" in body["error"].lower()


def test_non_json_agent_output():
    with (
        patch.object(extension_mod, "get_user_group_ids", new_callable=AsyncMock, return_value=[]),
        patch.object(extension_mod, "check_agentic_quota", new_callable=AsyncMock),
        patch.object(extension_mod, "increment_agentic_usage", new_callable=AsyncMock),
        patch.object(extension_mod, "run_agent_chat", new_callable=AsyncMock, return_value=("not json at all", None)),
        _client() as c,
    ):
        resp = c.post("/api/v1/extension/execute-task", json=_VALID_BODY)

    assert resp.status_code == 200
    body = resp.json()
    assert body["data"] is None
    assert "invalid json" in body["error"].lower()


def test_agent_runtime_error_returns_500():
    with (
        patch.object(extension_mod, "get_user_group_ids", new_callable=AsyncMock, return_value=[]),
        patch.object(extension_mod, "check_agentic_quota", new_callable=AsyncMock),
        patch.object(extension_mod, "increment_agentic_usage", new_callable=AsyncMock),
        patch.object(extension_mod, "run_agent_chat", new_callable=AsyncMock, side_effect=RuntimeError("LLM broke")),
        _client() as c,
    ):
        resp = c.post("/api/v1/extension/execute-task", json=_VALID_BODY)

    assert resp.status_code == 500
    assert "browser-scan-all" in resp.json()["detail"]


# ---------------------------------------------------------------------------
# max_tokens parameter
# ---------------------------------------------------------------------------


def test_max_tokens_forwarded_to_run_agent_chat():
    """When max_tokens is provided, it is passed as max_tokens_override."""
    mock_chat = AsyncMock(return_value=('{"ok": true}', None))
    body = {**_VALID_BODY, "max_tokens": 25000}

    with (
        patch.object(extension_mod, "get_user_group_ids", new_callable=AsyncMock, return_value=[]),
        patch.object(extension_mod, "check_agentic_quota", new_callable=AsyncMock),
        patch.object(extension_mod, "increment_agentic_usage", new_callable=AsyncMock),
        patch.object(extension_mod, "run_agent_chat", mock_chat),
        _client() as c,
    ):
        resp = c.post("/api/v1/extension/execute-task", json=body)

    assert resp.status_code == 200
    _, kwargs = mock_chat.call_args
    assert kwargs["max_tokens_override"] == 25000


def test_max_tokens_none_when_omitted():
    """When max_tokens is not in the request, max_tokens_override is None."""
    mock_chat = AsyncMock(return_value=('{"ok": true}', None))

    with (
        patch.object(extension_mod, "get_user_group_ids", new_callable=AsyncMock, return_value=[]),
        patch.object(extension_mod, "check_agentic_quota", new_callable=AsyncMock),
        patch.object(extension_mod, "increment_agentic_usage", new_callable=AsyncMock),
        patch.object(extension_mod, "run_agent_chat", mock_chat),
        _client() as c,
    ):
        resp = c.post("/api/v1/extension/execute-task", json=_VALID_BODY)

    assert resp.status_code == 200
    _, kwargs = mock_chat.call_args
    assert kwargs["max_tokens_override"] is None


@pytest.mark.parametrize("value", [0, 999, 100_001, -1], ids=["zero", "below_min", "above_max", "negative"])
def test_max_tokens_validation_rejects_out_of_bounds(value):
    with _client() as c:
        resp = c.post(
            "/api/v1/extension/execute-task",
            json={**_VALID_BODY, "max_tokens": value},
        )
    assert resp.status_code == 422
