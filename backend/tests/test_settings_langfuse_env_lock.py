"""Tests for env-managed Langfuse config locking.

When Langfuse is seeded from ``LANGFUSE_*`` environment variables it is
re-applied to the DB row on every startup (env always wins), so the Settings
→ Usage and quotas → Observability Langfuse card must be read-only — mirroring the env-seeded LLM
provider lock.  These tests pin:

1. ``_langfuse_env_managed`` reflecting the live env keys.
2. ``GET /settings/langfuse`` reporting ``is_from_env``.
3. ``PUT /settings/langfuse`` returning 403 when env-managed.
"""

from types import SimpleNamespace

import pytest

from app.api.v1.schemas.settings import LangfuseConfigRequest
from app.api.v1.settings import (
    _langfuse_env_managed,
    get_langfuse_config,
    update_langfuse_config,
)
from app.core.config import settings as app_settings


class _Result:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


class _MockSession:
    def __init__(self, results):
        self._results = list(results)

    async def execute(self, _statement):
        return self._results.pop(0)


def _set_langfuse_env(monkeypatch, public_key: str, secret_key: str) -> None:
    monkeypatch.setattr(app_settings, "langfuse_public_key", public_key)
    monkeypatch.setattr(app_settings, "langfuse_secret_key", secret_key)


# ---------------------------------------------------------------------------
# 1. _langfuse_env_managed
# ---------------------------------------------------------------------------


def test_langfuse_env_managed_false_without_keys(monkeypatch):
    _set_langfuse_env(monkeypatch, "", "")
    assert _langfuse_env_managed() is False


def test_langfuse_env_managed_requires_both_keys(monkeypatch):
    _set_langfuse_env(monkeypatch, "pk-lf-123", "")
    assert _langfuse_env_managed() is False
    _set_langfuse_env(monkeypatch, "", "sk-lf-123")
    assert _langfuse_env_managed() is False


def test_langfuse_env_managed_true_with_both_keys(monkeypatch):
    _set_langfuse_env(monkeypatch, "pk-lf-123", "sk-lf-456")
    assert _langfuse_env_managed() is True


def test_langfuse_env_managed_ignores_whitespace_only_keys(monkeypatch):
    # Whitespace-only keys would not seed a usable config in
    # _seed_langfuse_from_env (which strips), so they must not lock the UI /
    # 403 the PUT either. Both blank and one-blank cases are "not env-managed".
    _set_langfuse_env(monkeypatch, "   ", "\t\n")
    assert _langfuse_env_managed() is False
    _set_langfuse_env(monkeypatch, "pk-lf-123", "   ")
    assert _langfuse_env_managed() is False


# ---------------------------------------------------------------------------
# 2. GET reports is_from_env
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_langfuse_config_reports_env_managed(monkeypatch):
    _set_langfuse_env(monkeypatch, "pk-lf-123", "sk-lf-456")
    db = _MockSession([_Result(None)])

    resp = await get_langfuse_config(user=SimpleNamespace(), db=db)

    assert resp.is_from_env is True


@pytest.mark.asyncio
async def test_get_langfuse_config_not_env_managed_without_keys(monkeypatch):
    _set_langfuse_env(monkeypatch, "", "")
    db = _MockSession([_Result(None)])

    resp = await get_langfuse_config(user=SimpleNamespace(), db=db)

    assert resp.is_from_env is False


# ---------------------------------------------------------------------------
# 3. PUT guard
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_update_langfuse_config_blocked_when_env_managed(monkeypatch):
    from fastapi import HTTPException

    _set_langfuse_env(monkeypatch, "pk-lf-123", "sk-lf-456")

    body = LangfuseConfigRequest(enabled=True, host="https://cloud.langfuse.com")

    with pytest.raises(HTTPException) as exc:
        await update_langfuse_config(
            body=body,
            request=SimpleNamespace(),
            user=SimpleNamespace(email="admin@filigran.io"),
            db=SimpleNamespace(),  # never touched — guard fires first
        )

    assert exc.value.status_code == 403
    assert "LANGFUSE_*" in str(exc.value.detail)
