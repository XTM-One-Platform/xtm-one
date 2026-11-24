"""Live platform widgets through the real routes, against PostgreSQL, as a non-admin.

``POST /custom-dashboards/{id}/widgets/{wid}/data`` reads the widget from the
stored dashboard and the platform through the viewer's own access to the
integration; ``POST /custom-dashboards/{id}/widgets/preview`` draws a widget
not saved yet, for whoever may edit the dashboard. The integration rule is the
one of ``platform_access``: visible to the viewer as a non-admin (an
administrator does not borrow a colleague's private integration), of the
widget's provider, with a credential the viewer may use. A share revoked stops
the data at once, cache or not; a write may only point a widget at such an
integration. Both routes are 404 while the Dashboard Studio is off.

The platform is an ``httpx.MockTransport``; ``_get_token`` is stubbed to a
per-integration key so the test sees which credential each request carried.
``integration``-marked; the temporary database is ``tests/scratch_database.py``.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
import pytest
from fastapi import Depends, FastAPI
from sqlalchemy.ext.asyncio import AsyncSession

import app.models  # noqa: F401 - registers every table on Base.metadata
from app.api import deps
from app.api.v1 import custom_dashboards
from app.core.database import get_db
from app.models.custom_dashboard import CustomDashboard
from app.models.integration import Integration
from app.models.user import User
from app.services import dashboard_live
from app.services.custom_dashboard_spec import DashboardSpecError, validate_spec
from app.services.custom_dashboards import save_dashboard
from app.services.dashboard_definition import summary_widgets
from app.services.dashboard_live import _http
from tests.scratch_database import ALL_TABLES, scratch_database

# Every dashboard route is behind DASHBOARD_STUDIO: on for the module.
pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("dashboard_studio_enabled")]

_NUMBER = {
    "type": "number",
    "perspective": "entities",
    "dataSelection": [{"filters": {"mode": "and", "filters": [{"key": ["entity_type"], "values": ["Report"]}]}}],
}
_ANSWER = {"data": {"stixCoreObjectsNumber": {"total": 42, "count": 39}}}


def _widget(wid: str, integration_id: uuid.UUID, source: str = "opencti", native: dict | None = None) -> dict:
    return {"kind": "platform", "id": wid, "source": source, "integration_id": str(integration_id), "native": native or _NUMBER}


@asynccontextmanager
async def _client(session_factory, user_id: uuid.UUID) -> AsyncIterator[httpx.AsyncClient]:
    app = FastAPI()
    app.include_router(custom_dashboards.router, prefix="/api/v1/custom-dashboards")

    async def _get_db():
        async with session_factory() as db:
            yield db
            await db.commit()

    async def _signed_in(db: AsyncSession = Depends(get_db)) -> User:
        return await db.get(User, user_id)

    for dependency in (deps.get_current_user, deps.get_current_user_ungated):
        app.dependency_overrides[dependency] = _signed_in
    app.dependency_overrides[get_db] = _get_db
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        yield client


@pytest.fixture
def platform(monkeypatch):
    """The platform (one recorded request per call), the token stub and an in-memory cache."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=_ANSWER)

    async def token(db, integration_id, provider="", *, user_id=None, integ=None, **_kw):
        return f"https://{integ.name}.example||key-{integ.name}"

    store: dict[str, dict] = {}

    async def cache_get(key):
        return store.get(key)

    async def cache_set(key, value):
        store[key] = value

    async def no_limit(_user):
        return None

    monkeypatch.setattr(_http, "_client", lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    monkeypatch.setattr("app.services.integration_tools._get_token", token)
    monkeypatch.setattr(dashboard_live, "_cache_get", cache_get)
    monkeypatch.setattr(dashboard_live, "_cache_set", cache_set)
    monkeypatch.setattr(dashboard_live, "_enforce_rate", no_limit)
    return seen


@asynccontextmanager
async def _world():
    async with scratch_database(
        "dashboard_live", tables=ALL_TABLES, extensions=("vector",), skip_without_extensions=True
    ) as scratch:
        async with scratch.sessions() as db:
            owner = User(email="owner@example.com", hashed_password="x", full_name="Owner", role="user")
            other = User(email="other@example.com", hashed_password="x", full_name="Other", role="user")
            admin = User(email="admin@example.com", hashed_password="x", full_name="Admin", role="admin")
            db.add_all([owner, other, admin])
            await db.flush()
            own = Integration(name="own", provider="opencti", auth_type="api_key", user_id=owner.id)
            shared = Integration(
                name="shared", provider="opencti", auth_type="api_key", user_id=other.id, shared_user_ids=[str(owner.id)]
            )
            private = Integration(name="private", provider="opencti", auth_type="api_key", user_id=other.id)
            per_user = Integration(
                name="peruser", provider="opencti", auth_type="api_key", user_id=other.id,
                shared_user_ids=[str(owner.id)], credential_mode="per_user",
            )  # fmt: skip
            crq = Integration(name="crq", provider="opencrq", auth_type="api_key", user_id=None, is_company_managed=True)
            db.add_all([own, shared, private, per_user, crq])
            await db.flush()
            spec = validate_spec(
                {
                    "title": "Threat board",
                    "widgets": [
                        _widget("own", own.id),
                        _widget("shared", shared.id),
                        {"kind": "text", "id": "note", "body": "Notes"},
                    ],
                }
            )
            dashboard = CustomDashboard(user_id=owner.id, title="Threat board", widget_count=3, revision=1, spec=spec)
            db.add(dashboard)
            await db.commit()
            ids = {
                "owner": owner.id, "other": other.id, "admin": admin.id, "dashboard": dashboard.id,
                "own": own.id, "shared": shared.id, "private": private.id, "per_user": per_user.id, "crq": crq.id,
            }  # fmt: skip
        yield scratch, ids


def _data_url(ids: dict, wid: str) -> str:
    return f"/api/v1/custom-dashboards/{ids['dashboard']}/widgets/{wid}/data"


async def test_both_routes_are_not_found_while_the_studio_is_off(platform, monkeypatch) -> None:
    from app.core.config import settings

    monkeypatch.setattr(settings, "enabled_features", "")
    async with _world() as (scratch, ids), _client(scratch.sessions, ids["owner"]) as client:
        data = await client.post(_data_url(ids, "own"), json={})
        preview = await client.post(
            f"/api/v1/custom-dashboards/{ids['dashboard']}/widgets/preview", json={"widget": _widget("x", ids["own"])}
        )
    assert (data.status_code, preview.status_code) == (404, 404)
    assert platform == []


async def test_the_owner_reads_through_their_own_and_a_shared_integration(platform, dashboard_studio_enabled) -> None:
    async with _world() as (scratch, ids), _client(scratch.sessions, ids["owner"]) as client:
        own = await client.post(_data_url(ids, "own"), json={"range": "30d"})
        shared = await client.post(_data_url(ids, "shared"), json={})
    assert own.status_code == 200, own.text
    body = own.json()
    assert (body["status"], body["shape"], body["data"]) == ("ok", "number", {"value": 42.0, "change": 3.0, "previous": 39.0, "period": "day"})
    assert body["open_url"] == "https://own.example/dashboard/workspaces/dashboards"
    assert shared.json()["status"] == "ok"
    # An empty draft header: the live graph, never the account's own UI draft.
    assert [(r.url.host, r.url.path, r.headers["authorization"], r.headers.get("opencti-draft-id")) for r in platform] == [
        ("own.example", "/graphql", "Bearer key-own", ""),
        ("shared.example", "/graphql", "Bearer key-shared", ""),
    ]
    variables = json.loads(platform[0].content)["variables"]
    # As OpenCTI reads a dashboard's period: its dates first, the widget's own filters in a group.
    filters = variables["filters"]
    assert [(f["key"], f["operator"]) for f in filters["filters"]] == [(["created_at"], "gt"), (["created_at"], "lt")]
    assert filters["filterGroups"][0]["filters"][0]["key"] == ["entity_type"]
    assert variables["startDate"] is not None


async def test_an_admin_reading_the_dashboard_does_not_borrow_the_owners_private_integration(
    platform, dashboard_studio_enabled
) -> None:
    async with _world() as (scratch, ids), _client(scratch.sessions, ids["admin"]) as client:
        read = await client.post(_data_url(ids, "own"), json={})
    assert read.status_code == 200
    assert read.json() == {
        "status": "error", "shape": None, "data": None, "code": "integration_unavailable", "fetched_at": None, "open_url": None,
        "presentation": None, "snapshot": None, "changes": None,
    }  # fmt: skip
    assert platform == []


async def test_a_revoked_share_stops_the_data_at_once_despite_a_warm_cache(platform, dashboard_studio_enabled) -> None:
    async with _world() as (scratch, ids), _client(scratch.sessions, ids["owner"]) as client:
        first = await client.post(_data_url(ids, "shared"), json={})
        assert first.json()["status"] == "ok"
        async with scratch.sessions() as db:
            integration = await db.get(Integration, ids["shared"])
            integration.shared_user_ids = []
            await db.commit()
        after = await client.post(_data_url(ids, "shared"), json={})
    assert after.json()["code"] == "integration_unavailable"
    assert len(platform) == 1


async def test_the_page_s_refresh_reads_the_platform_again_through_the_route(platform, dashboard_studio_enabled) -> None:
    async with _world() as (scratch, ids), _client(scratch.sessions, ids["owner"]) as client:
        await client.post(_data_url(ids, "own"), json={})
        cached = await client.post(_data_url(ids, "own"), json={})
        refreshed = await client.post(_data_url(ids, "own"), json={"fresh": True})
        wrong = await client.post(_data_url(ids, "own"), json={"fresh": "please"})
    assert (cached.json()["status"], refreshed.json()["status"]) == ("ok", "ok")
    # The first read and the refresh asked the platform; the second read was the kept answer.
    assert len(platform) == 2
    assert wrong.status_code == 422


async def test_another_person_does_not_read_the_dashboard(platform, dashboard_studio_enabled) -> None:
    async with _world() as (scratch, ids), _client(scratch.sessions, ids["other"]) as client:
        read = await client.post(_data_url(ids, "shared"), json={})
        preview = await client.post(
            f"/api/v1/custom-dashboards/{ids['dashboard']}/widgets/preview", json={"widget": _widget("x", ids["shared"])}
        )
    assert (read.status_code, preview.status_code) == (404, 404)
    assert platform == []


@pytest.mark.parametrize(
    ("wid", "body", "status"),
    [("missing", {}, 404), ("note", {}, 404), ("own", {"range": "2y"}, 422), ("own", {"widget": {}}, 422)],
    ids=["unknown-widget", "not-a-platform-widget", "unknown-range", "unknown-key"],
)
async def test_the_data_route_reads_only_stored_platform_widgets(platform, dashboard_studio_enabled, wid, body, status) -> None:
    async with _world() as (scratch, ids), _client(scratch.sessions, ids["owner"]) as client:
        response = await client.post(_data_url(ids, wid), json=body)
    assert response.status_code == status
    assert platform == []


async def test_the_owner_previews_a_widget_checked_like_a_write(platform, dashboard_studio_enabled) -> None:
    url = None
    async with _world() as (scratch, ids), _client(scratch.sessions, ids["owner"]) as client:
        url = f"/api/v1/custom-dashboards/{ids['dashboard']}/widgets/preview"
        ok = await client.post(url, json={"widget": _widget("p", ids["own"])})
        bad_native = await client.post(url, json={"widget": _widget("p", ids["own"], native={"type": "nope"})})
        not_platform = await client.post(url, json={"widget": {"kind": "text", "body": "x"}})
        private = await client.post(url, json={"widget": _widget("p", ids["private"])})
    assert ok.status_code == 200 and ok.json()["status"] == "ok"
    assert (bad_native.status_code, not_platform.status_code) == (422, 422)
    assert "OpenCTI widget type must be one of" in bad_native.json()["detail"]
    assert private.json()["code"] == "integration_unavailable"
    assert [r.url.host for r in platform] == ["own.example"]


@pytest.mark.parametrize(
    ("integration", "allowed"),
    [("own", True), ("shared", True), ("crq-as-opencti", False), ("private", False), ("per_user", False)],
)
async def test_a_write_points_a_widget_only_at_an_integration_the_writer_can_use(
    dashboard_studio_enabled, integration, allowed
) -> None:
    async with _world() as (scratch, ids), scratch.sessions() as db:
        target = ids["crq"] if integration == "crq-as-opencti" else ids[integration]
        payload = {"title": "New", "widgets": [_widget("w", target)]}
        if allowed:
            saved = await save_dashboard(db, user_id=ids["owner"], spec_payload=payload)
            assert summary_widgets(saved.spec)[0]["integration_id"] == str(target)
        else:
            with pytest.raises(DashboardSpecError) as refused:
                await save_dashboard(db, user_id=ids["owner"], spec_payload=payload)
            assert "is not an integration of opencti you can use" in str(refused.value)


async def test_an_opencrq_company_integration_is_usable_by_everyone(dashboard_studio_enabled) -> None:
    async with _world() as (scratch, ids), scratch.sessions() as db:
        payload = {"title": "Risk", "widgets": [_widget("k", ids["crq"], "opencrq", {"metric": "kev_findings"})]}
        saved = await save_dashboard(db, user_id=ids["other"], spec_payload=payload)
    assert summary_widgets(saved.spec)[0]["source"] == "opencrq"
