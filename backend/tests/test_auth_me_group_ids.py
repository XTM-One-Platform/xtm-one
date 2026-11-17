"""Tests for the ``group_ids`` field on ``GET /auth/me``.

The Agentic Flow "Map" tab computes group-shared manage rights on the client
with the SAME intersection the backend enforces (``helpers.get_group_permission``
only counts a shared group the *viewer* belongs to). For that the frontend needs
the viewer's own group memberships, so ``/auth/me`` now surfaces ``group_ids``.

These pin that the field is populated from ``get_user_group_ids`` (so a future
refactor can't silently drop it) and that it degrades to ``[]`` for a user with
no group memberships. Mirrors the repo's fake-session convention - the backend
suite has no real-DB harness.
"""

import uuid

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import deps
from app.api.v1 import auth as auth_api
from app.core.database import get_db


def _make_client(monkeypatch, group_ids):
    fake_user = _fake_user()

    async def _groups(_db, _uid):
        return list(group_ids)

    monkeypatch.setattr(auth_api, "get_user_group_ids", _groups)

    async def _managed(_db, _uid):
        return []

    monkeypatch.setattr(auth_api, "managed_groups", _managed)

    # ``/auth/me`` also resolves the Terms of Use state, which consults
    # the Agentic AI license. Stub it: this suite has no real DB, and the ToU
    # contract is covered by tests/test_terms_of_use_accept.py.
    import app.api.v1.platform as platform_api

    async def _agentic(_db):
        return False

    monkeypatch.setattr(platform_api, "is_agentic_ai_licensed", _agentic)

    app = FastAPI()
    app.include_router(auth_api.router, prefix="/api/v1/auth")

    async def _get_db():
        yield object()

    # ``/auth/me`` is deliberately wired to the UNGATED resolver so it stays
    # reachable while a user's ToU acceptance is pending — it carries the state
    # the consent gate renders from.
    app.dependency_overrides[deps.get_current_user_ungated] = lambda: fake_user
    app.dependency_overrides[get_db] = _get_db
    return TestClient(app)


def _fake_user():
    """A minimal user carrying every attribute ``me()`` serialises."""
    return type(
        "_User",
        (),
        {
            "id": uuid.uuid4(),
            "email": "u@example.com",
            "full_name": "Test User",
            "title": None,
            "timezone": None,
            "theme": None,
            "role": "user",
            "sso_provider": None,
            "default_agent_id": None,
            "ga_builtin_tool_ids": None,
            "ga_custom_tool_ids": None,
            "ga_integration_bindings": None,
            "ga_mcp_server_ids": None,
            "ga_knowledge_base_ids": None,
            "channel_agent_config": None,
            "mcp_exposed_agent_ids": None,
        },
    )()


def test_me_returns_group_ids_from_helper(monkeypatch):
    gids = [str(uuid.uuid4()), str(uuid.uuid4())]
    client = _make_client(monkeypatch, gids)
    resp = client.get("/api/v1/auth/me")
    assert resp.status_code == 200
    assert resp.json()["group_ids"] == gids


def test_me_group_ids_empty_for_no_membership(monkeypatch):
    client = _make_client(monkeypatch, [])
    resp = client.get("/api/v1/auth/me")
    assert resp.status_code == 200
    assert resp.json()["group_ids"] == []


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
