"""Making a dashboard by hand, against PostgreSQL, through the real routes.

``POST /custom-dashboards`` writes an empty dashboard of the caller's (a grid
tab, its description when given), kept as its first version and audited;
``POST /custom-dashboards/import`` makes one from a file the widget packs read
(an XTM One pack, an OpenCTI or OpenAEV export), each platform widget bound to
an integration of its platform the caller can use: the only one, else the one
named, else a 422 that lists them - and nothing is kept. Both are the Dashboard
Studio's (a 404 while it is off), and an invalid body is a 422.

``PUT .../tags`` files a dashboard under tags (cleaned; those who may edit it),
leaving its definition and revision as they were; ``POST .../duplicate`` gives
whoever reads a dashboard a private copy of it, its tags included, a platform
widget copied only through an integration the copier can use.

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
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

import app.models  # noqa: F401 - registers every table on Base.metadata
from app.api import deps
from app.api.v1 import custom_dashboards
from app.core.database import get_db
from app.models.audit_log import AuditLog
from app.models.custom_dashboard import CustomDashboard, CustomDashboardVersion
from app.models.integration import Integration
from app.models.user import User
from app.services.custom_dashboard_spec import validate_spec
from app.services.dashboard_definition import summary_widgets
from tests.scratch_database import ALL_TABLES, scratch_database

# Every dashboard route is behind DASHBOARD_STUDIO: on for the module.
pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("dashboard_studio_enabled")]

_PACK = {
    "format": "xtm-one-widget-pack",
    "version": 1,
    "name": "Threat figures",
    "widgets": [
        {"kind": "text", "id": "note", "body": "Read me"},
        {
            "kind": "platform", "id": "reports", "source": "opencti", "title": "Reports",
            "native": {"type": "number", "perspective": "entities", "dataSelection": [{}]},
        },
    ],
}  # fmt: skip


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


@asynccontextmanager
async def _world(*, second_cti: bool = False):
    async with scratch_database("dashboards_create", tables=ALL_TABLES, extensions=("vector",), skip_without_extensions=True) as scratch:
        async with scratch.sessions() as db:
            owner = User(email="owner@example.com", hashed_password="x", full_name="Owner", role="user")
            db.add(owner)
            await db.flush()
            cti = Integration(name="Lab", provider="opencti", auth_type="api_key", user_id=owner.id)
            db.add(cti)
            if second_cti:
                db.add(Integration(name="Prod", provider="opencti", auth_type="api_key", user_id=owner.id))
            await db.commit()
            ids = {"owner": owner.id, "cti": cti.id}
        yield scratch, ids


def _upload(document: dict, **form: str) -> dict:
    return {"files": {"file": ("export.json", json.dumps(document).encode(), "application/json")}, "data": form}


async def _count(db: AsyncSession, model) -> int:
    return (await db.execute(select(func.count()).select_from(model))).scalar_one()


async def test_an_empty_dashboard_is_made_by_hand_kept_as_a_version_and_audited(dashboard_studio_enabled) -> None:
    async with _world() as (scratch, ids):
        async with _client(scratch.sessions, ids["owner"]) as client:
            made = await client.post("/api/v1/custom-dashboards", json={"title": "  Weekly board  ", "description": "For the **SOC**"})
            listed = await client.get("/api/v1/custom-dashboards")
        async with scratch.sessions() as db:
            versions = await _count(db, CustomDashboardVersion)
            audit = (await db.execute(select(AuditLog.action, AuditLog.entity_type, AuditLog.entity_name))).all()
    assert made.status_code == 201, made.text
    body = made.json()
    assert (body["title"], body["spec"]["description"], body["widget_count"], body["revision"]) == ("Weekly board", "For the **SOC**", 0, 1)
    assert (body["permission"], body["can_manage"], body["user_id"]) == ("owner", True, str(ids["owner"]))
    assert body["spec"]["layout"]["tabs"]["options"]["showTabBar"] is False
    assert [d["id"] for d in listed.json()] == [body["id"]]
    assert versions == 1
    assert audit == [("create", "custom_dashboard", "Weekly board")]


@pytest.mark.parametrize(
    "body",
    [{}, {"title": ""}, {"title": "   "}, {"title": "x" * 500}, {"title": "Board", "description": "x" * 5000}, {"title": "Board", "owner": "me"}, {"title": None}],
    ids=["missing", "empty", "blank", "long-title", "long-description", "extra", "null"],
)
async def test_an_invalid_new_dashboard_is_a_422(dashboard_studio_enabled, body) -> None:
    async with _world() as (scratch, ids):
        async with _client(scratch.sessions, ids["owner"]) as client:
            answer = await client.post("/api/v1/custom-dashboards", json=body)
        async with scratch.sessions() as db:
            kept = await _count(db, CustomDashboard)
    assert (answer.status_code, kept) == (422, 0)


async def test_a_file_becomes_a_dashboard_its_platform_widgets_on_the_only_integration(dashboard_studio_enabled) -> None:
    async with _world() as (scratch, ids), _client(scratch.sessions, ids["owner"]) as client:
        imported = await client.post("/api/v1/custom-dashboards/import", **_upload(_PACK))
        titled = await client.post("/api/v1/custom-dashboards/import", **_upload(_PACK, title="Mine"))
    assert imported.status_code == 201, imported.text
    body = imported.json()
    assert (body["title"], body["widget_count"], body["import_notes"]) == ("Threat figures", 2, [])
    widgets = {w["id"]: w for w in summary_widgets(body["spec"])}
    assert sorted(widgets) == ["note", "reports"]
    assert widgets["reports"]["integration_id"] == str(ids["cti"])
    assert titled.json()["title"] == "Mine"


async def test_an_import_with_several_integrations_names_one_or_keeps_nothing(dashboard_studio_enabled) -> None:
    async with _world(second_cti=True) as (scratch, ids):
        async with _client(scratch.sessions, ids["owner"]) as client:
            ambiguous = await client.post("/api/v1/custom-dashboards/import", **_upload(_PACK))
            foreign = await client.post("/api/v1/custom-dashboards/import", **_upload(_PACK, bindings=json.dumps({"opencti": str(uuid.uuid4())})))
            malformed = await client.post("/api/v1/custom-dashboards/import", **_upload(_PACK, bindings="[1]"))
            named = await client.post("/api/v1/custom-dashboards/import", **_upload(_PACK, bindings=json.dumps({"opencti": str(ids["cti"])})))
        async with scratch.sessions() as db:
            kept = await _count(db, CustomDashboard)
    assert ambiguous.status_code == 422
    assert ambiguous.json()["detail"].startswith("Choose the opencti integration these widgets read: ")
    assert (foreign.status_code, foreign.json()["detail"]) == (422, "The opencti integration named is not one you can use.")
    assert (malformed.status_code, malformed.json()["detail"]) == (422, "bindings must map each platform to an integration id.")
    assert named.status_code == 201, named.text
    # Only the named import was kept: a refused one leaves no empty dashboard behind.
    assert kept == 1


@pytest.mark.parametrize(
    ("content", "status"),
    [(b"not json", 422), (json.dumps({"hello": "world"}).encode(), 422), (b"{" + b" " * (5 * 1024 * 1024) + b"}", 413)],
    ids=["not-json", "not-an-export", "too-large"],
)
async def test_a_file_that_is_no_dashboard_is_refused(dashboard_studio_enabled, content, status) -> None:
    async with _world() as (scratch, ids):
        async with _client(scratch.sessions, ids["owner"]) as client:
            answer = await client.post("/api/v1/custom-dashboards/import", files={"file": ("x.json", content, "application/json")})
        async with scratch.sessions() as db:
            kept = await _count(db, CustomDashboard)
    assert (answer.status_code, kept) == (status, 0)


@asynccontextmanager
async def _shared_world(*, platform: bool = False):
    """A dashboard of the owner's, read-only for a reader; with a platform widget through the owner's own integration."""
    async with _world() as (scratch, ids), scratch.sessions() as db:
        reader = User(email="reader@example.com", hashed_password="x", full_name="Reader", role="user")
        db.add(reader)
        await db.flush()
        widgets = [{"kind": "text", "id": "note", "body": "Notes"}]
        if platform:
            widgets.append({**_PACK["widgets"][1], "integration_id": str(ids["cti"])})
        dashboard = CustomDashboard(
            user_id=ids["owner"], title="Board", widget_count=len(widgets), revision=3,
            spec=validate_spec({"title": "Board", "widgets": widgets}), tags=["soc"],
            shared_user_ids=[str(reader.id)], shared_user_permissions={str(reader.id): "read_only"},
        )  # fmt: skip
        db.add(dashboard)
        await db.commit()
        yield scratch, {**ids, "reader": reader.id, "dashboard": dashboard.id}


async def test_tags_are_cleaned_and_kept_by_those_who_may_edit_and_leave_the_revision(dashboard_studio_enabled) -> None:
    async with _shared_world() as (scratch, ids):
        url = f"/api/v1/custom-dashboards/{ids['dashboard']}/tags"
        async with _client(scratch.sessions, ids["owner"]) as client:
            tagged = await client.put(url, json={"tags": ["  Weekly  Review ", "SOC", "soc", ""]})
            too_many = await client.put(url, json={"tags": [f"t{n}" for n in range(21)]})
            too_long = await client.put(url, json={"tags": ["x" * 41]})
            listed = await client.get("/api/v1/custom-dashboards")
        async with _client(scratch.sessions, ids["reader"]) as client:
            refused = await client.put(url, json={"tags": ["mine"]})
    assert tagged.status_code == 200, tagged.text
    assert (tagged.json()["tags"], tagged.json()["revision"]) == (["weekly review", "soc"], 3)
    assert (too_many.status_code, too_long.status_code, refused.status_code) == (422, 422, 403)
    assert [d["tags"] for d in listed.json()] == [["weekly review", "soc"]]


async def test_a_reader_duplicates_a_dashboard_into_a_private_copy() -> None:
    async with _shared_world() as (scratch, ids), _client(scratch.sessions, ids["reader"]) as client:
        copied = await client.post(f"/api/v1/custom-dashboards/{ids['dashboard']}/duplicate")
        mine = await client.get("/api/v1/custom-dashboards")
    assert copied.status_code == 201, copied.text
    body = copied.json()
    assert (body["title"], body["tags"], body["user_id"], body["permission"], body["is_shared"], body["revision"]) == (
        "Board (copy)", ["soc"], str(ids["reader"]), "owner", False, 1,
    )  # fmt: skip
    assert [w["id"] for w in summary_widgets(body["spec"])] == ["note"]
    assert sorted(d["title"] for d in mine.json()) == ["Board", "Board (copy)"]


async def test_a_copy_never_reads_through_someone_else_s_integration(dashboard_studio_enabled) -> None:
    async with _shared_world(platform=True) as (scratch, ids):
        async with _client(scratch.sessions, ids["reader"]) as client:
            refused = await client.post(f"/api/v1/custom-dashboards/{ids['dashboard']}/duplicate")
        async with _client(scratch.sessions, ids["owner"]) as client:
            own = await client.post(f"/api/v1/custom-dashboards/{ids['dashboard']}/duplicate")
            unknown = await client.post(f"/api/v1/custom-dashboards/{uuid.uuid4()}/duplicate")
        async with scratch.sessions() as db:
            kept = await _count(db, CustomDashboard)
    assert refused.status_code == 422
    assert own.status_code == 201, own.text
    assert unknown.status_code == 404
    # The board and the owner's copy: the refused copy left nothing behind.
    assert kept == 2


async def test_making_a_dashboard_is_the_studio_s(monkeypatch) -> None:
    from app.core.config import settings

    monkeypatch.setattr(settings, "enabled_features", "")
    async with _world() as (scratch, ids), _client(scratch.sessions, ids["owner"]) as client:
        made = await client.post("/api/v1/custom-dashboards", json={"title": "Board"})
        imported = await client.post("/api/v1/custom-dashboards/import", **_upload(_PACK))
    assert (made.status_code, imported.status_code) == (404, 404)
