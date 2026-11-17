"""Browser profiles as a platform feature: their API, the binding writes, the keep-alive.

* ``/api/v1/browser-profiles`` is mounted outside the Video Studio feature
  flag, and the Video Studio router no longer serves profiles;
* an agent write or a person's General Assistant preference that ADDS a
  profile is checked against what the writer may use (no admin bypass; a
  company-managed agent binds company-managed profiles only);
* the keep-alive runs from an adapter of its own, whatever feature is on.
"""

from __future__ import annotations

import asyncio
import uuid
from types import SimpleNamespace

import httpx
import pytest
from fastapi import HTTPException

from app.models.agent import Agent
from app.models.agent_copy import build_agent_copy_kwargs
from app.models.browser_profile import BrowserProfile

A, B = str(uuid.uuid4()), str(uuid.uuid4())


async def test_the_browser_profiles_api_is_outside_the_video_studio_flag():
    from app.main import application

    transport = httpx.ASGITransport(app=application)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/api/v1/browser-profiles")
    assert response.status_code == 401, response.text

    paths = {getattr(route, "path", "") for route in application.routes}
    assert "/api/v1/browser-profiles" in paths
    assert "/api/v1/browser-profiles/{profile_id}/mfa-screenshot" in paths
    assert not [p for p in paths if p.startswith(("/api/v1/video-studio/browser-profiles", "/api/v1/video-studio/mfa"))]


def _refusing(calls: list, refused: list):
    async def refused_additions(db, **kwargs):
        calls.append(kwargs)
        return refused

    return refused_additions


async def test_an_agent_write_adding_a_profile_the_writer_may_not_use_is_refused(monkeypatch):
    from app.api.v1 import agents

    calls: list = []
    monkeypatch.setattr("app.api.v1.binding_access.refused_additions", _refusing(calls, [A]))
    with pytest.raises(HTTPException) as exc:
        await agents._checked_browser_profile_ids(None, SimpleNamespace(), [A], baseline=None, company_managed=False)
    assert exc.value.status_code == 400
    assert calls[0]["model"] is BrowserProfile
    assert calls[0]["is_admin"] is False
    assert calls[0]["company_only"] is False


async def test_a_company_managed_agent_binds_company_managed_profiles_only(monkeypatch):
    from app.api.v1 import agents

    calls: list = []
    monkeypatch.setattr("app.api.v1.binding_access.refused_additions", _refusing(calls, []))
    stored = await agents._checked_browser_profile_ids(
        None, SimpleNamespace(), [A, B], baseline=[A], company_managed=True
    )
    assert stored == [A, B]
    assert calls[0]["company_only"] is True
    assert calls[0]["baseline"] == [A]


async def test_an_agent_write_binding_nothing_checks_nothing(monkeypatch):
    from app.api.v1 import agents

    calls: list = []
    monkeypatch.setattr("app.api.v1.binding_access.refused_additions", _refusing(calls, []))
    assert await agents._checked_browser_profile_ids(None, SimpleNamespace(), [], baseline=[A], company_managed=False) is None
    assert calls == []


async def test_the_general_assistant_preference_is_checked_like_a_binding(monkeypatch):
    from app.api.v1 import settings as settings_api

    user = SimpleNamespace(ga_browser_profile_ids=[A])
    with pytest.raises(HTTPException) as bad:
        await settings_api._checked_ga_browser_profile_ids(None, user, ["not-an-id"])
    assert bad.value.status_code == 422

    calls: list = []
    monkeypatch.setattr("app.api.v1.binding_access.refused_additions", _refusing(calls, [B]))
    with pytest.raises(HTTPException) as refused:
        await settings_api._checked_ga_browser_profile_ids(None, user, [A, B])
    assert refused.value.status_code == 403
    assert calls[0]["baseline"] == [A] and calls[0]["is_admin"] is False

    monkeypatch.setattr("app.api.v1.binding_access.refused_additions", _refusing([], []))
    assert await settings_api._checked_ga_browser_profile_ids(None, user, [A.upper()]) == [A]
    assert await settings_api._checked_ga_browser_profile_ids(None, user, []) is None


def test_a_copy_keeps_the_agent_bindings_and_their_read_only_marks():
    source = Agent(name="Analyst", browser_profile_ids=[A, B], browser_profile_read_only=[B])
    kwargs = build_agent_copy_kwargs(source)
    assert kwargs["browser_profile_ids"] == [A, B]
    assert kwargs["browser_profile_read_only"] == [B]
    kwargs["browser_profile_ids"].append("x")
    assert source.browser_profile_ids == [A, B]


def test_the_keepalive_runs_from_an_adapter_of_its_own():
    from app.services.orchestration_adapters import list_adapters, register_builtin_adapters
    from app.services.orchestration_adapters.browser_profiles import ADAPTER_NAME

    register_builtin_adapters()
    assert ADAPTER_NAME in list_adapters()


async def test_the_keepalive_sweep_starts_at_most_once_per_interval(monkeypatch):
    from app.services.browser_profiles import keepalive
    from app.services.orchestration_adapters.browser_profiles import BrowserProfileSessionsAdapter

    runs: list = []

    async def sweep():
        runs.append(1)

    monkeypatch.setattr(keepalive, "keepalive_sweep", sweep)
    adapter = BrowserProfileSessionsAdapter()
    await adapter.periodic(SimpleNamespace())
    await asyncio.sleep(0)
    await adapter.periodic(SimpleNamespace())
    await asyncio.sleep(0)
    assert runs == [1]


async def test_the_first_keepalive_sweep_runs_on_a_host_booted_minutes_ago(monkeypatch):
    from app.services.browser_profiles import keepalive
    from app.services.orchestration_adapters import browser_profiles as adapter_module
    from app.services.orchestration_adapters.browser_profiles import BrowserProfileSessionsAdapter

    runs: list = []

    async def sweep():
        runs.append(1)

    monkeypatch.setattr(keepalive, "keepalive_sweep", sweep)
    monkeypatch.setattr(adapter_module, "time", SimpleNamespace(monotonic=lambda: 12.0))
    await BrowserProfileSessionsAdapter().periodic(SimpleNamespace())
    await asyncio.sleep(0)
    assert runs == [1]
