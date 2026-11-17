"""Dashboard widget packs, against PostgreSQL, through the real routes and the agent tools.

The built-ins are written on startup (company-managed, read-only, refreshed,
dropped once no longer shipped); an import parses a file into checked widgets
with a report; a pack saved from a dashboard drops its integrations; inserting
binds each platform widget to an integration of its platform the person can
use (the one named, else the only one) and writes through ``edit_dashboard``
(revision-checked, ids kept unique); the entity inputs a pack's widgets read
(an OpenCTI custom view's) come along, bound the same way, once per token. A built-in is duplicated, never changed;
a shared pack is listed by its grantee; everything is a 404 while the
Dashboard Studio is off.

``integration``-marked; the temporary database is ``tests/scratch_database.py``.
"""

from __future__ import annotations

import json
import shutil
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import pytest
from fastapi import Depends, FastAPI
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

import app.models  # noqa: F401 - registers every table on Base.metadata
from app.api import deps
from app.api.v1 import dashboard_widget_packs
from app.core.database import get_db
from app.models.custom_dashboard import CustomDashboard
from app.models.dashboard_widget_pack import DashboardWidgetPack
from app.models.integration import Integration
from app.models.user import User
from app.services import dashboard_widget_packs as packs
from app.services.custom_dashboard_spec import validate_spec
from app.services.dashboard_definition import summary_widgets
from tests.scratch_database import ALL_TABLES, scratch_database

# Every dashboard route is behind DASHBOARD_STUDIO: on for the module.
pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("dashboard_studio_enabled")]

_SPEC = {"title": "Board", "widgets": [{"kind": "text", "id": "note", "body": "Notes"}]}
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
    app.include_router(dashboard_widget_packs.router, prefix="/api/v1/dashboard-widget-packs")

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
    async with scratch_database(
        "widget_packs", tables=ALL_TABLES, extensions=("vector",), skip_without_extensions=True
    ) as scratch:
        async with scratch.sessions() as db:
            owner = User(email="owner@example.com", hashed_password="x", full_name="Owner", role="user")
            other = User(email="other@example.com", hashed_password="x", full_name="Other", role="user")
            admin = User(email="admin@example.com", hashed_password="x", full_name="Admin", role="admin")
            db.add_all([owner, other, admin])
            await db.flush()
            cti = Integration(name="Lab", provider="opencti", auth_type="api_key", user_id=owner.id)
            db.add(cti)
            if second_cti:
                db.add(Integration(name="Prod", provider="opencti", auth_type="api_key", user_id=owner.id))
            dashboard = CustomDashboard(
                user_id=owner.id, title="Board", widget_count=1, revision=1, spec=validate_spec(_SPEC),
                shared_user_ids=[str(other.id)], shared_user_permissions={str(other.id): "read_only"},
            )  # fmt: skip
            db.add(dashboard)
            await db.commit()
            ids = {"owner": owner.id, "other": other.id, "admin": admin.id, "cti": cti.id, "dashboard": dashboard.id}
            await packs.seed_builtin_packs(db)
        yield scratch, ids


def _upload(document: dict, name: str = "pack.json") -> dict:
    return {"files": {"file": (name, json.dumps(document).encode(), "application/json")}}


async def test_the_builtins_are_seeded_once_and_follow_what_is_shipped(dashboard_studio_enabled, tmp_path, monkeypatch) -> None:
    async with _world() as (scratch, _ids), scratch.sessions() as db:
        rows = (await db.execute(select(DashboardWidgetPack))).scalars().all()
        assert sorted(r.name for r in rows) == [
            "OpenAEV - Simulation results",
            "OpenCRQ - Risk overview",
            "OpenCTI - Global overview",
            "OpenCTI - Vulnerability management",
        ]
        assert all(r.is_company_managed and r.user_id is None and "builtin" in r.tags for r in rows)
        assert await packs.seed_builtin_packs(db) == 0
        shipped = tmp_path / "packs"
        shipped.mkdir()
        shutil.copy(Path(packs.BUILTIN_DIR) / "opencrq-risk-overview.json", shipped / "opencrq-risk-overview.json")
        monkeypatch.setattr(packs, "BUILTIN_DIR", shipped)
        await packs.seed_builtin_packs(db)
        left = (await db.execute(select(DashboardWidgetPack.name))).scalars().all()
        assert left == ["OpenCRQ - Risk overview"]


async def test_a_pack_is_imported_listed_and_inserted_bound_to_the_only_integration(dashboard_studio_enabled) -> None:
    async with _world() as (scratch, ids), _client(scratch.sessions, ids["owner"]) as client:
        imported = await client.post("/api/v1/dashboard-widget-packs/import", **_upload(_PACK))
        assert imported.status_code == 201, imported.text
        pack = imported.json()
        assert (pack["origin"], pack["sources"], pack["widget_count"]) == ("upload", ["opencti"], 2)
        assert "integration_id" not in pack["widgets"][1]
        listed = await client.get("/api/v1/dashboard-widget-packs")
        names = [p["name"] for p in listed.json()]
        # Built-ins first.
        assert names[-1] == "Threat figures" and all(p["builtin"] for p in listed.json()[:4])
        stale = await client.post(
            f"/api/v1/dashboard-widget-packs/{pack['id']}/insert",
            json={"dashboard_id": str(ids["dashboard"]), "revision": 7},
        )
        inserted = await client.post(
            f"/api/v1/dashboard-widget-packs/{pack['id']}/insert",
            json={"dashboard_id": str(ids["dashboard"]), "revision": 1, "after": "start"},
        )
    assert stale.status_code == 409
    assert inserted.status_code == 200, inserted.text
    body = inserted.json()
    # The pack's "note" met the dashboard's: renamed, never overwritten.
    assert body["inserted"] == ["note-2", "reports"]
    widgets = body["dashboard"]["widgets"]
    # The pack keeps its arrangement and goes below the dashboard's widgets.
    assert [w["id"] for w in widgets] == ["note", "note-2", "reports"]
    note_bottom = widgets[0]["layout"]["y"] + widgets[0]["layout"]["h"]
    assert widgets[1]["layout"]["y"] == note_bottom
    assert widgets[2]["layout"]["y"] >= note_bottom
    assert widgets[2]["integration_id"] == str(ids["cti"])
    assert body["dashboard"]["revision"] == 2


async def test_several_integrations_are_a_choice_and_a_named_one_must_be_usable(dashboard_studio_enabled) -> None:
    async with _world(second_cti=True) as (scratch, ids), _client(scratch.sessions, ids["owner"]) as client:
        pack = (await client.post("/api/v1/dashboard-widget-packs/import", **_upload(_PACK))).json()
        url = f"/api/v1/dashboard-widget-packs/{pack['id']}/insert"
        ambiguous = await client.post(url, json={"dashboard_id": str(ids["dashboard"]), "revision": 1})
        usable = await client.get("/api/v1/dashboard-widget-packs/integrations")
        wrong = await client.post(
            url, json={"dashboard_id": str(ids["dashboard"]), "revision": 1, "bindings": {"opencti": str(uuid.uuid4())}}
        )
        named = await client.post(
            url, json={"dashboard_id": str(ids["dashboard"]), "revision": 1, "bindings": {"opencti": str(ids["cti"])}}
        )
    assert ambiguous.status_code == 422
    assert [i["name"] for i in usable.json()] == ["Lab", "Prod"]
    assert ambiguous.json()["detail"].startswith("Choose the opencti integration these widgets read: ")
    assert (wrong.status_code, wrong.json()["detail"]) == (422, "The opencti integration named is not one you can use.")
    assert named.status_code == 200


async def test_a_reader_saves_a_dashboard_as_a_pack_but_does_not_insert_into_it(dashboard_studio_enabled) -> None:
    async with _world() as (scratch, ids), _client(scratch.sessions, ids["other"]) as client:
        saved = await client.post(
            "/api/v1/dashboard-widget-packs/from-dashboard",
            json={"dashboard_id": str(ids["dashboard"]), "name": "My copy"},
        )
        refused = await client.post(
            f"/api/v1/dashboard-widget-packs/{saved.json()['id']}/insert",
            json={"dashboard_id": str(ids["dashboard"]), "revision": 1},
        )
    assert saved.status_code == 201
    assert (saved.json()["origin"], saved.json()["widget_count"]) == ("dashboard", 1)
    assert refused.status_code == 404


async def test_a_builtin_is_duplicated_never_changed(dashboard_studio_enabled) -> None:
    async with _world() as (scratch, ids), _client(scratch.sessions, ids["admin"]) as client:
        builtin = (await client.get("/api/v1/dashboard-widget-packs", params={"scope": "builtin"})).json()[0]
        renamed = await client.patch(f"/api/v1/dashboard-widget-packs/{builtin['id']}", json={"name": "Mine"})
        deleted = await client.delete(f"/api/v1/dashboard-widget-packs/{builtin['id']}")
        copy = await client.post(f"/api/v1/dashboard-widget-packs/{builtin['id']}/duplicate")
    assert (renamed.status_code, renamed.json()["detail"]) == (403, "A built-in pack is not changed: duplicate it")
    assert deleted.status_code == 403
    assert copy.status_code == 201
    assert (copy.json()["builtin"], copy.json()["can_manage"], copy.json()["name"]) == (
        False, True, f"{builtin['name']} (copy)",
    )  # fmt: skip


async def test_a_shared_pack_reaches_its_grantee_only(dashboard_studio_enabled) -> None:
    async with _world() as (scratch, ids):
        async with _client(scratch.sessions, ids["owner"]) as client:
            pack = (await client.post("/api/v1/dashboard-widget-packs/import", **_upload(_PACK))).json()
        async with _client(scratch.sessions, ids["other"]) as client:
            before = await client.get(f"/api/v1/dashboard-widget-packs/{pack['id']}")
        async with _client(scratch.sessions, ids["owner"]) as client:
            shared = await client.put(
                f"/api/v1/dashboard-widget-packs/{pack['id']}/sharing",
                json={"shared_user_ids": [str(ids["other"])], "shared_user_permissions": {str(ids["other"]): "read_only"}},
            )
        async with _client(scratch.sessions, ids["other"]) as client:
            after = await client.get(f"/api/v1/dashboard-widget-packs/{pack['id']}")
            change = await client.patch(f"/api/v1/dashboard-widget-packs/{pack['id']}", json={"name": "Theirs"})
    assert before.status_code == 404
    assert shared.status_code == 200, shared.text
    assert (after.status_code, after.json()["can_manage"]) == (200, False)
    assert change.status_code == 403


@pytest.mark.parametrize(
    ("content", "status", "fragment"),
    [
        (b"not json", 422, "is not valid JSON"),
        (json.dumps({"format": "xtm-one-widget-pack", "version": 9, "name": "x", "widgets": []}).encode(), 422, "version 9"),
        (b"x" * (5 * 1024 * 1024 + 1), 413, "larger than 5 MB"),
    ],
    ids=["garbage", "version", "too-large"],
)
async def test_an_unreadable_file_is_refused_with_a_reason(dashboard_studio_enabled, content, status, fragment) -> None:
    async with _world() as (scratch, ids), _client(scratch.sessions, ids["owner"]) as client:
        refused = await client.post(
            "/api/v1/dashboard-widget-packs/import", files={"file": ("x.json", content, "application/json")}
        )
    assert refused.status_code == status
    assert fragment in refused.json()["detail"]


async def test_nothing_while_the_studio_is_off(monkeypatch) -> None:
    from app.core.config import settings

    monkeypatch.setattr(settings, "enabled_features", "")
    async with _world() as (scratch, ids), _client(scratch.sessions, ids["owner"]) as client:
        listed = await client.get("/api/v1/dashboard-widget-packs")
        imported = await client.post("/api/v1/dashboard-widget-packs/import", **_upload(_PACK))
    assert (listed.status_code, imported.status_code) == (404, 404)


async def test_the_agent_lists_and_inserts_a_builtin(dashboard_studio_enabled, monkeypatch) -> None:
    import app.core.database as database
    from app.services.builtin_tools import build_custom_dashboard_tools
    from app.tools.base import ToolCall

    async with _world() as (scratch, ids):
        monkeypatch.setattr(database, "async_session", scratch.sessions)
        _defs, executor = build_custom_dashboard_tools(None, str(ids["owner"]))

        async def _call(name: str, arguments: dict):
            return await executor.execute_tool(ToolCall(id="c", name=name, arguments=arguments))

        listed = await _call("list_widget_packs", {"source": "opencti"})
        async with scratch.sessions() as db:
            builtin = (
                await db.execute(select(DashboardWidgetPack).where(DashboardWidgetPack.name == "OpenCTI - Global overview"))
            ).scalar_one()
        inserted = await _call(
            "insert_widgets_from_pack",
            {"pack_id": str(builtin.id), "dashboard_id": str(ids["dashboard"]), "revision": 1},
        )
        async with scratch.sessions() as db:
            stored = await db.get(CustomDashboard, ids["dashboard"])
            platform = [w for w in summary_widgets(stored.spec) if w["kind"] == "platform"]
    assert "OpenCTI - Global overview" in listed.content
    assert "OpenCRQ" not in listed.content
    assert inserted.is_error is False, inserted.content
    assert len(platform) == builtin.widget_count
    assert {w["integration_id"] for w in platform} == {str(ids["cti"])}


async def test_an_inserted_opencti_widget_keeps_its_portable_standard_ids(dashboard_studio_enabled, monkeypatch) -> None:
    """Resolved when the widget is read (``dashboard_live.opencti.fetch``), never stored: the dashboard stays
    exportable to another OpenCTI, and inserting asks the platform nothing."""
    from app.services.dashboard_live import _http

    author = "identity--11111111-1111-4111-8111-111111111111"
    monkeypatch.setattr(
        _http, "_client", lambda: httpx.AsyncClient(transport=httpx.MockTransport(lambda _r: pytest.fail("no request")))
    )
    document = {
        **_PACK,
        "widgets": [
            {
                "kind": "platform", "id": "by-author", "source": "opencti",
                "native": {
                    "type": "number", "perspective": "entities",
                    "dataSelection": [{"filters": {"mode": "and", "filters": [{"key": ["createdBy"], "values": [author]}], "filterGroups": []}}],
                },
            }
        ],
    }  # fmt: skip
    async with _world() as (scratch, ids), _client(scratch.sessions, ids["owner"]) as client:
        pack = (await client.post("/api/v1/dashboard-widget-packs/import", **_upload(document))).json()
        inserted = await client.post(
            f"/api/v1/dashboard-widget-packs/{pack['id']}/insert", json={"dashboard_id": str(ids["dashboard"]), "revision": 1}
        )
    assert inserted.status_code == 200, inserted.text
    widget = next(w for w in inserted.json()["dashboard"]["widgets"] if w["id"] == "by-author")
    assert widget["native"]["dataSelection"][0]["filters"]["filters"][0]["values"] == [author]


async def test_a_long_duplicated_name_can_be_sent_back_on_update(dashboard_studio_enabled) -> None:
    long_name = "N" * 118
    async with _world() as (scratch, ids), _client(scratch.sessions, ids["owner"]) as client:
        pack = (await client.post("/api/v1/dashboard-widget-packs/import", **_upload({**_PACK, "name": long_name}))).json()
        copy = (await client.post(f"/api/v1/dashboard-widget-packs/{pack['id']}/duplicate")).json()
        renamed = await client.patch(
            f"/api/v1/dashboard-widget-packs/{copy['id']}", json={"name": copy["name"], "description": "Mine"}
        )
    assert copy["name"] == f"{long_name} (copy)"
    assert renamed.status_code == 200, renamed.text


def _custom_view() -> dict:
    from app.services.dashboard_interop.opencti import encode_base64_json

    selected = {"filters": {"mode": "and", "filters": [{"key": ["objects"], "values": ["SELF_ID"], "operator": "eq", "mode": "or"}], "filterGroups": []}}
    widgets = {"w1": {"type": "number", "perspective": "entities", "layout": {"x": 0, "y": 0, "w": 4, "h": 2}, "parameters": {"title": "Reports about it"}, "dataSelection": [selected]}}
    manifest = encode_base64_json({"config": {}, "widgets": widgets})
    return {"openCTI_version": "7.260706.0", "type": "custom-view", "configuration": {"name": "Country view", "manifest": manifest, "target_entity_type": "Country"}}


async def test_a_custom_view_is_inserted_with_its_entity_input_bound_and_shown_once(dashboard_studio_enabled) -> None:
    from app.services.dashboard_definition import upgrade

    async with _world() as (scratch, ids), _client(scratch.sessions, ids["owner"]) as client:
        imported = await client.post("/api/v1/dashboard-widget-packs/import", **_upload(_custom_view(), "view.json"))
        assert imported.status_code == 201, imported.text
        pack = imported.json()
        assert pack["origin"] == "opencti"
        assert pack["inputs"] == [{"type": "input.entity", "title": "Country", "token": "entity", "options": {"source": "opencti", "entityTypes": ["Country"]}, "dataSources": {}}]
        first = await client.post(f"/api/v1/dashboard-widget-packs/{pack['id']}/insert", json={"dashboard_id": str(ids["dashboard"]), "revision": 1})
        assert first.status_code == 200, first.text
        second = await client.post(f"/api/v1/dashboard-widget-packs/{pack['id']}/insert", json={"dashboard_id": str(ids["dashboard"]), "revision": 2})
        assert second.status_code == 200, second.text
        exported = await client.get(f"/api/v1/dashboard-widget-packs/{pack['id']}/export")
        async with scratch.sessions() as db:
            stored = await db.get(CustomDashboard, ids["dashboard"])
            definition = upgrade(stored.spec)
            # Saved back as a pack, the dashboard's widgets carry the input they read.
            saved = await packs.pack_from_dashboard(db, user_id=ids["owner"], dashboard=stored, name="Again")
            saved_inputs = saved.inputs
    inputs = definition["inputs"]
    # Inserted twice, one input: the second insertion reads the token the first one's input sets.
    assert list(inputs) == ["input_entity"]
    assert inputs["input_entity"]["options"] == {"source": "opencti", "integration_id": str(ids["cti"]), "entityTypes": ["Country"]}
    assert definition["layout"]["globalInputs"] == ["input_entity"]
    sources = [s for s in definition["dataSources"].values() if s.get("type") == "platform"]
    assert len(sources) == 2
    assert all(s["options"]["native"]["dataSelection"][0]["filters"]["filters"][0]["values"] == ["$entity$"] for s in sources)
    document = json.loads(exported.content)
    assert document["inputs"] == pack["inputs"]
    assert [i["token"] for i in saved_inputs] == ["entity"]
    assert "integration_id" not in saved_inputs[0]["options"]


async def test_an_entity_input_reads_through_an_integration_its_writer_can_use_only(dashboard_studio_enabled) -> None:
    from app.services.custom_dashboard_spec import DashboardSpecError
    from app.services.custom_dashboards import save_dashboard
    from app.services.dashboard_definition import upgrade

    async with _world() as (scratch, ids), scratch.sessions() as db:
        stored = await db.get(CustomDashboard, ids["dashboard"])
        definition = upgrade(stored.spec)

        def with_input(integration_id: object) -> dict:
            options = {"source": "opencti", "integration_id": str(integration_id), "entityTypes": ["Country"]}
            return {**definition, "inputs": {"country": {"type": "input.entity", "token": "entity", "title": "Country", "options": options}}}

        # The Lab integration is the owner's: another person cannot point an input at it.
        with pytest.raises(DashboardSpecError, match="integration"):
            await save_dashboard(db, user_id=ids["other"], spec_payload=with_input(ids["cti"]), dashboard=stored, expected_revision=1)
        await db.rollback()
        stored = await db.get(CustomDashboard, ids["dashboard"])
        saved = await save_dashboard(db, user_id=ids["owner"], spec_payload=with_input(ids["cti"]), dashboard=stored, expected_revision=1)
        assert upgrade(saved.spec)["inputs"]["country"]["options"]["integration_id"] == str(ids["cti"])
