"""Tests for ``GET /auth/config`` cost-data availability surface."""

from dataclasses import replace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.v1 import auth as auth_api
from app.core.database import get_db
from app.core.sso_helpers import ResolvedSSOConfig


def _make_client(monkeypatch, cost_data_available: bool) -> TestClient:
    async def _get_db():
        yield object()

    async def _get_sso_config(_db):
        return replace(
            ResolvedSSOConfig(),
            local_auth_enabled=True,
            registration_enabled=True,
        )

    monkeypatch.setattr("app.core.sso_helpers.get_sso_config", _get_sso_config)
    monkeypatch.setattr(
        auth_api.settings,
        "observability_cost_data_enabled",
        cost_data_available,
    )

    app = FastAPI()
    app.include_router(auth_api.router, prefix="/api/v1/auth")
    app.dependency_overrides[get_db] = _get_db
    return TestClient(app)


@pytest.mark.parametrize("cost_data_available", [True, False])
def test_auth_config_cost_data_available_reflects_setting(
    monkeypatch, cost_data_available: bool
):
    client = _make_client(monkeypatch, cost_data_available)
    resp = client.get("/api/v1/auth/config")
    assert resp.status_code == 200
    data = resp.json()
    assert "cost_data_available" in data
    assert data["cost_data_available"] is cost_data_available
