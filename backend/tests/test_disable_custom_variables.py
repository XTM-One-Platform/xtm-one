"""Tests for the ``disable_custom_variables`` security flag.

The previous revision of this file only exercised a *local helper*
that mimicked the API guard.  That left the real surface uncovered:
the guard could be removed from the router, applied to the wrong
endpoints, return a different status/detail, or accidentally be
bypassed by GET endpoints — and none of those regressions would have
failed CI.  This rev wires up the full FastAPI router under
``TestClient`` and asserts that **every** endpoint on
``/api/v1/variables`` returns 403 when the flag is set.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from app.api import deps
from app.api.v1 import variables as variables_api
from app.core import config
from app.core.database import get_db

# ── Config-level defaults / overrides ─────────────────────────────────────


def test_config_disable_custom_variables_default_dev(monkeypatch):
    """In dev/copilot mode, ``disable_custom_variables`` defaults to False.

    Never ``importlib.reload(app.core.config)`` here (#1239): a reload leaves
    ``config.Settings`` a new class while ``config.settings`` stays an instance
    of the old one, so a later test patching the class misses the singleton.
    """
    import app.core.config as config_module

    monkeypatch.setenv("PLATFORM_MODE", "dev")
    cfg = config_module.Settings(platform_mode="dev")
    assert cfg.disable_custom_variables is False


def test_config_disable_custom_variables_default_xtm_one():
    """In ``xtm_one`` mode, ``disable_custom_variables`` defaults to True."""
    import app.core.config as config_module

    cfg = config_module.Settings(platform_mode="xtm_one")
    assert cfg.disable_custom_variables is True


def test_config_disable_custom_variables_explicit_override():
    """An explicit env value overrides the mode default."""
    import app.core.config as config_module

    cfg = config_module.Settings(
        platform_mode="xtm_one", disable_custom_variables=False
    )
    assert cfg.disable_custom_variables is False

    cfg2 = config_module.Settings(
        platform_mode="copilot", disable_custom_variables=True
    )
    assert cfg2.disable_custom_variables is True


# ── Endpoint-level guard via real FastAPI router ──────────────────────────
#
# These tests mount the real ``variables.router`` under TestClient and
# patch the dependency injectors so we exercise the actual decorator
# stack — the router-level dependency, the per-endpoint dependencies,
# the path/body validation, and the HTTPException handling.  A
# regression that removes the router-level guard, switches the status
# code, or forgets to gate one of the GET endpoints fails CI loudly.


@pytest.fixture
def fake_user():
    return SimpleNamespace(
        id=uuid.uuid4(), email="user@test", full_name="Test User", is_admin=True
    )


class _FakeDb:
    """The endpoint-level guard fires *before* any DB access, so the
    test database can be a no-op stub — we never want to reach an
    ``execute()`` / ``commit()`` here, and a passing test asserts that.
    """

    async def execute(self, *_args, **_kwargs):
        raise AssertionError(
            "DB execute() must NOT be called when "
            "disable_custom_variables is True — the router-level "
            "guard should have raised 403 before any DB access"
        )

    async def commit(self):
        raise AssertionError(
            "DB commit() must NOT be called when "
            "disable_custom_variables is True"
        )


@pytest.fixture
def variables_client(monkeypatch, fake_user) -> Iterator[TestClient]:
    """A TestClient mounted on the real ``variables.router`` with
    ``settings.disable_custom_variables`` forced to ``True`` for the
    whole test.
    """
    monkeypatch.setattr(config.settings, "disable_custom_variables", True)

    app = FastAPI()
    app.include_router(variables_api.router, prefix="/api/v1/variables")

    async def _get_db():
        yield _FakeDb()

    app.dependency_overrides[deps.get_current_user] = lambda: fake_user
    app.dependency_overrides[get_db] = _get_db
    with TestClient(app) as client:
        yield client


def _assert_disabled(response) -> None:
    assert response.status_code == 403, (
        f"expected 403 (custom variables disabled), got "
        f"{response.status_code}: {response.text!r}"
    )
    detail = response.json().get("detail", "")
    assert "disabled" in detail.lower(), (
        f"403 detail should mention 'disabled'; got {detail!r}"
    )


# Every endpoint on the router — including the GET endpoints the
# previous helper-only test didn't cover — must return 403 with a
# consistent detail string.  Parametrised so each endpoint shows up
# as its own test ID in CI output.


@pytest.mark.parametrize(
    "method,path,kwargs",
    [
        ("get",    "/api/v1/variables",                  {}),
        ("get",    "/api/v1/variables/00000000-0000-0000-0000-000000000001", {}),
        ("post",   "/api/v1/variables",                  {"json": {"name": "x"}}),
        ("put",    "/api/v1/variables/00000000-0000-0000-0000-000000000001", {"json": {"name": "y"}}),
        ("delete", "/api/v1/variables/00000000-0000-0000-0000-000000000001", {}),
    ],
)
def test_disable_custom_variables_blocks_every_endpoint(
    variables_client, method, path, kwargs
):
    response = getattr(variables_client, method)(path, **kwargs)
    _assert_disabled(response)


def test_auth_takes_precedence_over_feature_flag(monkeypatch):
    """When ``get_current_user`` raises 401 (broken auth), the response
    must be 401 — not 403 — even when ``disable_custom_variables`` is
    True.

    The guard depends on ``get_current_user`` so authentication runs as
    a prerequisite.  FastAPI short-circuits on the 401 from auth before
    the flag check runs, preventing the flag state from being leaked to
    unauthenticated callers.
    """
    monkeypatch.setattr(config.settings, "disable_custom_variables", True)

    app = FastAPI()
    app.include_router(variables_api.router, prefix="/api/v1/variables")

    async def _broken_user():
        raise HTTPException(status_code=401, detail="auth subsystem error")

    async def _get_db():
        yield _FakeDb()

    app.dependency_overrides[deps.get_current_user] = _broken_user
    app.dependency_overrides[get_db] = _get_db
    with TestClient(app) as client:
        response = client.get("/api/v1/variables")

    assert response.status_code == 401, (
        f"expected 401 (auth failure must take precedence over 403 flag "
        f"gate, so flag state is not leaked to anonymous callers), "
        f"got {response.status_code}: {response.text!r}"
    )


def test_disable_custom_variables_passthrough_when_flag_is_false(monkeypatch):
    """The router-level guard MUST be a no-op when the flag is false —
    the request continues to the per-handler logic. We assert that by
    arranging for ``GET /api/v1/variables`` to complete normally with
    a 200 response and an empty list, confirming the guard did not
    intercept the request.
    """
    monkeypatch.setattr(config.settings, "disable_custom_variables", False)

    fake_user = SimpleNamespace(
        id=uuid.uuid4(), email="user@test", full_name="Test User", is_admin=True
    )

    class _NotFoundDb:
        # ``GET /variables`` ultimately calls ``execute(...)`` and
        # iterates rows — return an empty list so the handler completes
        # with a 200 + empty payload (no rows in DB).
        async def execute(self, *_args, **_kwargs):
            return _EmptyResult()

        async def commit(self):
            return None

    class _EmptyResult:
        def scalars(self):
            return self

        def all(self):
            return []

    async def _get_db():
        yield _NotFoundDb()

    async def _no_groups(_db, _user_id):
        return []

    monkeypatch.setattr(variables_api, "get_user_group_ids", _no_groups)

    app = FastAPI()
    app.include_router(variables_api.router, prefix="/api/v1/variables")
    app.dependency_overrides[deps.get_current_user] = lambda: fake_user
    app.dependency_overrides[get_db] = _get_db

    with TestClient(app) as client:
        response = client.get("/api/v1/variables")

    assert response.status_code == 200, response.text
    assert response.json() == [], (
        "guard must be a pure no-op when the flag is false; observed "
        f"{response.json()!r}"
    )
