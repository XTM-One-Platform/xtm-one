"""The Dashboard Studio, against PostgreSQL: a conversation bound to a dashboard.

A studio conversation carries ``custom_dashboard_id`` (created bound, or bound
later with ``PUT /chat/conversations/{id}/dashboard``), to a dashboard the
caller reads as themselves; an unreadable one is the unknown id's 404. Per
turn, ``load_dashboard_studio_context`` resolves it with the caller's own tier
(read-only says so) and the widget selected (only one that exists) - nothing
while the flag is off. Integrations are not listed per turn: the
``list_platform_integrations`` tool gives them when the agent needs them.
A person edits a widget from the inspector with ``PATCH`` ``upsert``, validated
like an agent's write.

``integration``-marked; the temporary database is ``tests/scratch_database.py``.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
import pytest
from fastapi import Depends, FastAPI
from sqlalchemy.ext.asyncio import AsyncSession

import app.models  # noqa: F401 - registers every table on Base.metadata
from app.api import deps
from app.api.v1 import chat, custom_dashboards
from app.core.database import get_db
from app.models.chat import Conversation
from app.models.custom_dashboard import CustomDashboard
from app.models.integration import Integration
from app.models.user import User
from app.services.dashboard_definition import from_widgets
from app.services.dashboard_native import to_native
from app.services.dashboard_studio import load_dashboard_studio_context
from tests.scratch_database import ALL_TABLES, scratch_database

# Every dashboard route is behind DASHBOARD_STUDIO: on for the module.
pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("dashboard_studio_enabled")]

_SPEC = {
    "title": "Threat board",
    "widgets": [
        {"kind": "score", "id": "score", "label": "Exposure score", "value": 58},
        {"kind": "text", "id": "note", "body": "Notes"},
    ],
}


@asynccontextmanager
async def _client(session_factory, user_id: uuid.UUID) -> AsyncIterator[httpx.AsyncClient]:
    app = FastAPI()
    app.include_router(chat.router, prefix="/api/v1/chat")
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
    # The chat's licence gate is not what this test is about (as in test_workspace_sharing_postgres).
    app.dependency_overrides[deps.require_chat_enabled] = lambda: None
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        yield client


@asynccontextmanager
async def _world():
    async with scratch_database(
        "dashboard_studio", tables=ALL_TABLES, extensions=("vector",), skip_without_extensions=True
    ) as scratch:
        async with scratch.sessions() as db:
            owner = User(email="owner@example.com", hashed_password="x", full_name="Owner", role="user")
            viewer = User(email="viewer@example.com", hashed_password="x", full_name="Viewer", role="user")
            stranger = User(email="stranger@example.com", hashed_password="x", full_name="Stranger", role="user")
            db.add_all([owner, viewer, stranger])
            await db.flush()
            dashboard = CustomDashboard(
                user_id=owner.id, title="Threat board", widget_count=2, revision=1, spec=to_native(from_widgets(_SPEC)),
                shared_user_ids=[str(viewer.id)], shared_user_permissions={str(viewer.id): "read_only"},
            )  # fmt: skip
            cti = Integration(name="Lab <b>CTI</b>", provider="opencti", auth_type="api_key", user_id=owner.id)
            foreign = Integration(name="theirs", provider="opencti", auth_type="api_key", user_id=stranger.id)
            db.add_all([dashboard, cti, foreign])
            await db.commit()
            ids = {
                "owner": owner.id, "viewer": viewer.id, "stranger": stranger.id, "dashboard": dashboard.id,
                "cti": cti.id, "foreign": foreign.id,
            }  # fmt: skip
        yield scratch, ids


async def test_a_studio_conversation_is_created_bound_and_listed(dashboard_studio_enabled) -> None:
    async with _world() as (scratch, ids):
        async with _client(scratch.sessions, ids["viewer"]) as client:
            created = await client.post("/api/v1/chat/conversations", json={"custom_dashboard_id": str(ids["dashboard"])})
            listed = await client.get("/api/v1/chat/conversations", params={"custom_dashboard_id": str(ids["dashboard"])})
        async with _client(scratch.sessions, ids["stranger"]) as client:
            refused = await client.post("/api/v1/chat/conversations", json={"custom_dashboard_id": str(ids["dashboard"])})
    assert created.status_code == 201, created.text
    body = created.json()
    assert (body["custom_dashboard_id"], body["title"]) == (str(ids["dashboard"]), "Studio: Threat board")
    assert [c["id"] for c in listed.json()] == [body["id"]]
    assert (refused.status_code, refused.json()["detail"]) == (404, "Dashboard not found")


async def test_an_existing_conversation_is_bound_to_one_subject_at_a_time(dashboard_studio_enabled) -> None:
    async with _world() as (scratch, ids):
        async with scratch.sessions() as db:
            conv = Conversation(user_id=ids["owner"], channel="web", title="Chat", video_project_id=uuid.uuid4())
            db.add(conv)
            await db.commit()
            conv_id = conv.id
        url = f"/api/v1/chat/conversations/{conv_id}/dashboard"
        async with _client(scratch.sessions, ids["owner"]) as client:
            unknown = await client.put(url, json={"custom_dashboard_id": str(uuid.uuid4())})
            bound = await client.put(url, json={"custom_dashboard_id": str(ids["dashboard"])})
        async with _client(scratch.sessions, ids["viewer"]) as client:
            not_theirs = await client.put(url, json={"custom_dashboard_id": str(ids["dashboard"])})
        async with scratch.sessions() as db:
            stored = await db.get(Conversation, conv_id)
            binding = (stored.custom_dashboard_id, stored.video_project_id, stored.presentation_id)
    assert unknown.status_code == 404
    assert bound.status_code == 200, bound.text
    assert not_theirs.status_code == 404
    assert binding == (ids["dashboard"], None, None)


async def test_the_turn_context_follows_the_callers_own_tier(dashboard_studio_enabled) -> None:
    async with _world() as (scratch, ids), scratch.sessions() as db:
        owner = await load_dashboard_studio_context(
            db, user_id=ids["owner"], dashboard_id=ids["dashboard"], focused_widget="score"
        )
        viewer = await load_dashboard_studio_context(
            db, user_id=ids["viewer"], dashboard_id=ids["dashboard"], focused_widget="ghost"
        )
        stranger = await load_dashboard_studio_context(db, user_id=ids["stranger"], dashboard_id=ids["dashboard"])
    assert stranger is None
    assert (owner.can_edit, owner.focused_widget, owner.tool_ids) == (True, "score", ("custom_dashboards",))
    prompt = owner.prompt()
    assert f"dashboard_id: {ids['dashboard']}" in prompt
    assert "- The user has selected widget score" in prompt
    assert "  - score [xtm.score, 0,0 12x3] Exposure score" in prompt
    assert str(ids["cti"]) not in prompt and str(ids["foreign"]) not in prompt
    assert (viewer.can_edit, viewer.focused_widget) == (False, None)
    assert "READ-ONLY" in viewer.prompt()


async def test_no_studio_context_while_the_flag_is_off(monkeypatch) -> None:
    from app.core.config import settings

    monkeypatch.setattr(settings, "enabled_features", "")
    async with _world() as (scratch, ids), scratch.sessions() as db:
        assert await load_dashboard_studio_context(db, user_id=ids["owner"], dashboard_id=ids["dashboard"]) is None


async def test_the_inspector_upserts_a_widget_validated_like_an_agents(dashboard_studio_enabled) -> None:
    async with _world() as (scratch, ids):
        url = f"/api/v1/custom-dashboards/{ids['dashboard']}"
        async with _client(scratch.sessions, ids["owner"]) as client:
            added = await client.patch(
                url,
                json={
                    "revision": 1,
                    "operations": [{"op": "upsert", "widget": {"kind": "text", "id": "brief", "body": "New"}, "after": "start"}],
                },
            )
            foreign = await client.patch(
                url,
                json={
                    "revision": 2,
                    "operations": [
                        {
                            "op": "upsert",
                            "widget": {
                                "kind": "platform", "id": "live", "source": "opencti",
                                "integration_id": str(ids["foreign"]),
                                "native": {"type": "number", "perspective": "entities", "dataSelection": [{}]},
                            },
                        }
                    ],
                },
            )  # fmt: skip
            history = await client.get(f"{url}/versions")
        async with _client(scratch.sessions, ids["viewer"]) as client:
            viewer = await client.patch(
                url, json={"revision": 2, "operations": [{"op": "upsert", "widget": {"kind": "text", "body": "x"}}]}
            )
    assert added.status_code == 200, added.text
    assert [w["id"] for w in added.json()["widgets"]] == ["brief", "score", "note"]
    assert foreign.status_code == 422
    assert "integration_id is not an integration of opencti you can use" in foreign.json()["detail"]
    assert history.json()[0]["summary"] == "added brief"
    assert viewer.status_code == 403


async def test_the_source_editor_writes_a_whole_definition_at_the_revision_read(dashboard_studio_enabled) -> None:
    async with _world() as (scratch, ids):
        url = f"/api/v1/custom-dashboards/{ids['dashboard']}"
        async with _client(scratch.sessions, ids["owner"]) as client:
            current = (await client.get(url)).json()
            definition = current["spec"]
            definition["dataSources"]["answer"] = {"type": "static", "options": {"shape": "number", "data": {"value": 42}}}
            definition["visualizations"]["big"] = {"type": "xtm.singlevalue", "title": "Answer", "dataSources": {"primary": "answer"}}
            first = definition["layout"]["tabs"]["items"][0]["layoutId"]
            definition["layout"]["layoutDefinitions"][first]["structure"].append(
                {"item": "big", "position": {"x": 0, "y": 30, "w": 4, "h": 6}}
            )
            saved = await client.put(f"{url}/definition", json={"revision": current["revision"], "definition": definition})
            stale = await client.put(f"{url}/definition", json={"revision": current["revision"], "definition": definition})
            broken = await client.put(
                f"{url}/definition",
                json={"revision": current["revision"] + 1, "definition": {**definition, "visualizations": {"x": {"type": "xtm.line"}}}},
            )
            data = await client.post(f"{url}/data-sources/answer/data", json={})
            by_widget = await client.post(f"{url}/widgets/big/data", json={})
            missing = await client.post(f"{url}/data-sources/ghost/data", json={})
        async with _client(scratch.sessions, ids["viewer"]) as client:
            viewer = await client.put(f"{url}/definition", json={"revision": current["revision"] + 1, "definition": definition})
            viewer_data = await client.post(f"{url}/data-sources/answer/data", json={})
        async with _client(scratch.sessions, ids["stranger"]) as client:
            stranger = await client.post(f"{url}/data-sources/answer/data", json={})
    assert saved.status_code == 200, saved.text
    assert saved.json()["revision"] == current["revision"] + 1
    assert saved.json()["spec"]["visualizations"]["big"]["type"] == "xtm.singlevalue"
    assert stale.status_code == 409
    assert broken.status_code == 422 and "needs dataSources.primary" in broken.text
    assert (data.json()["status"], data.json()["shape"], data.json()["data"]) == ("ok", "number", {"value": 42.0, "change": None})
    assert by_widget.json()["data"] == data.json()["data"]
    assert missing.status_code == 404
    assert viewer.status_code == 403
    assert viewer_data.json()["data"] == {"value": 42.0, "change": None}
    assert stranger.status_code == 404


async def test_an_element_edit_sets_and_deletes_by_path(dashboard_studio_enabled) -> None:
    async with _world() as (scratch, ids):
        url = f"/api/v1/custom-dashboards/{ids['dashboard']}"
        async with _client(scratch.sessions, ids["owner"]) as client:
            added = await client.patch(
                url,
                json={"revision": 1, "operations": [{"op": "set", "path": "inputs.q", "value": {"type": "input.text", "token": "q"}}]},
            )
            mixed = await client.patch(
                url, json={"revision": 2, "operations": [{"op": "set", "path": "inputs.q", "id": "q", "value": {}}]}
            )
            removed = await client.patch(url, json={"revision": 2, "operations": [{"op": "delete", "path": "inputs.q"}]})
    assert added.status_code == 200, added.text
    assert added.json()["spec"]["inputs"]["q"]["token"] == "q"
    assert mixed.status_code == 422
    assert removed.status_code == 200 and removed.json()["spec"]["inputs"] == {}


async def test_maps_and_graphs_are_read_in_their_closed_shapes_and_an_svg_choropleth_saved_sanitized(
    dashboard_studio_enabled,
) -> None:
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10"><script>alert(1)</script>'
        '<rect id="hall" width="10" height="10" onclick="steal()"/></svg>'
    )
    async with _world() as (scratch, ids):
        url = f"/api/v1/custom-dashboards/{ids['dashboard']}"
        async with _client(scratch.sessions, ids["owner"]) as client:
            current = (await client.get(url)).json()
            definition = current["spec"]
            definition["dataSources"]["places"] = {
                "type": "static",
                "options": {"shape": "geo", "data": {"regions": [{"code": "FR", "value": 3}, {"code": "hall", "value": 1}],
                                                     "points": [{"label": "Lyon", "lat": 45.76, "lon": 4.84, "value": 2}]}},
            }  # fmt: skip
            definition["dataSources"]["links"] = {
                "type": "static",
                "options": {"shape": "graph", "data": {"edges": [{"source": "a", "target": "b", "value": 2}]}},
            }
            definition["visualizations"]["world"] = {"type": "xtm.map", "dataSources": {"primary": "places"}}
            definition["visualizations"]["floor"] = {"type": "xtm.choropleth.svg", "dataSources": {"primary": "places"}, "options": {"svg": svg}}
            definition["visualizations"]["flows"] = {"type": "xtm.sankey", "dataSources": {"primary": "links"}}
            first = definition["layout"]["tabs"]["items"][0]["layoutId"]
            for index, vid in enumerate(("world", "floor", "flows")):
                definition["layout"]["layoutDefinitions"][first]["structure"].append(
                    {"item": vid, "position": {"x": 0, "y": 40 + index * 8, "w": 12, "h": 8}}
                )
            saved = await client.put(f"{url}/definition", json={"revision": current["revision"], "definition": definition})
            broken = await client.put(
                f"{url}/definition",
                json={
                    "revision": current["revision"] + 1,
                    "definition": {**definition, "visualizations": {**definition["visualizations"], "floor": {
                        "type": "xtm.choropleth.svg", "dataSources": {"primary": "places"}, "options": {"svg": "<svg><g></svg>"}}}},
                },
            )  # fmt: skip
        async with _client(scratch.sessions, ids["viewer"]) as client:
            read = (await client.get(url)).json()
            places = await client.post(f"{url}/data-sources/places/data", json={})
            flows = await client.post(f"{url}/widgets/flows/data", json={})
    assert saved.status_code == 200, saved.text
    floor = read["spec"]["visualizations"]["floor"]["options"]["svg"]
    assert 'id="hall"' in floor and "<script" not in floor and "onclick" not in floor
    assert broken.status_code == 422 and "options.svg is not a usable SVG" in broken.text
    assert (places.json()["status"], places.json()["shape"]) == ("ok", "geo")
    assert places.json()["data"] == {
        "points": [{"label": "Lyon", "lat": 45.76, "lon": 4.84, "value": 2.0, "group": None}],
        "regions": [{"code": "FR", "label": "FR", "value": 3.0}, {"code": "hall", "label": "hall", "value": 1.0}],
    }
    assert (flows.json()["shape"], flows.json()["data"]["edges"]) == (
        "graph",
        [{"source": "a", "target": "b", "label": None, "value": 2.0}],
    )
