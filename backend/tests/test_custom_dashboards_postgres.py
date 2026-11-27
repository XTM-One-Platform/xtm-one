"""Custom dashboards against PostgreSQL: the routes, who may do what, the race.

A dashboard is its owner's: the owner reads, rearranges and deletes it; an
admin reads it and changes nothing; anyone else gets the 404 of an unknown id,
on every route. Two edits at one revision meet one write and one 409. The agent
tools write through the same rules on the real tables.

``integration``-marked and skips when PostgreSQL is unreachable; the temporary
database is the shared ``tests/scratch_database.py``.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select

import app.models  # noqa: F401 - registers every table on Base.metadata
from app.api import deps
from app.api.v1 import custom_dashboards
from app.core.database import get_db
from app.models.audit_log import AuditLog
from app.models.custom_dashboard import CustomDashboard
from app.models.user import User
from app.services.custom_dashboard_spec import validate_spec
from tests.scratch_database import ALL_TABLES, scratch_database

# Every dashboard route is behind DASHBOARD_STUDIO: on for the module.
pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("dashboard_studio_enabled")]

_SPEC = {
    "title": "Today",
    "widgets": [
        {"kind": "score", "id": "score", "label": "Exposure score", "value": 62, "max": 100},
        {"kind": "text", "id": "note", "body": "Two intrusion sets match **4** exposures."},
        {"kind": "prompts", "id": "ask", "suggestions": ["What changed since Monday?"]},
    ],
}


@asynccontextmanager
async def _client(session_factory, user: User) -> AsyncIterator[httpx.AsyncClient]:
    app = FastAPI()
    app.include_router(custom_dashboards.router, prefix="/api/v1/custom-dashboards")

    async def _get_db():
        async with session_factory() as db:
            yield db
            await db.commit()

    app.dependency_overrides[deps.get_current_user] = lambda: user
    app.dependency_overrides[get_db] = _get_db
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


@asynccontextmanager
async def _world():
    async with scratch_database(
        "custom_dashboards", tables=ALL_TABLES, extensions=("vector",), skip_without_extensions=True
    ) as scratch:
        async with scratch.sessions() as db:
            owner = User(email="owner@example.com", hashed_password="x", full_name="Owner", role="user")
            other = User(email="other@example.com", hashed_password="x", full_name="Other", role="user")
            admin = User(email="admin@example.com", hashed_password="x", full_name="Admin", role="admin")
            db.add_all([owner, other, admin])
            await db.flush()
            dashboard = CustomDashboard(
                user_id=owner.id, title="Today", widget_count=3, revision=1, spec=validate_spec(_SPEC)
            )
            db.add(dashboard)
            await db.commit()
        yield scratch, owner, other, admin, dashboard.id


async def test_the_owner_reads_lists_and_manages_their_dashboard() -> None:
    async with (
        _world() as (scratch, owner, _other, _admin, dashboard_id),
        _client(scratch.sessions, owner) as client,
    ):
        listed = await client.get("/api/v1/custom-dashboards")
        assert listed.status_code == 200
        assert [(d["id"], d["widget_count"], "spec" in d) for d in listed.json()] == [(str(dashboard_id), 3, False)]

        read = await client.get(f"/api/v1/custom-dashboards/{dashboard_id}")
        assert read.status_code == 200
        body = read.json()
        assert (body["can_manage"], body["revision"]) == (True, 1)
        assert [w["id"] for w in body["widgets"]] == ["score", "note", "ask"]


async def test_anyone_else_gets_the_404_of_an_unknown_id_on_every_route() -> None:
    async with _world() as (scratch, _owner, other, _admin, dashboard_id):
        url = f"/api/v1/custom-dashboards/{dashboard_id}"
        async with _client(scratch.sessions, other) as client:
            assert (await client.get("/api/v1/custom-dashboards")).json() == []
            assert (await client.get(url)).status_code == 404
            patched = await client.patch(url, json={"revision": 1, "title": "Mine now"})
            assert patched.status_code == 404
            assert (await client.delete(url)).status_code == 404
            assert (await client.get("/api/v1/custom-dashboards/not-a-uuid")).status_code == 404
        async with scratch.sessions() as db:
            stored = await db.get(CustomDashboard, dashboard_id)
            assert (stored.title, stored.revision) == ("Today", 1)


async def test_an_admin_reads_it_and_changes_nothing() -> None:
    async with _world() as (scratch, _owner, _other, admin, dashboard_id):
        url = f"/api/v1/custom-dashboards/{dashboard_id}"
        async with _client(scratch.sessions, admin) as client:
            read = await client.get(url)
            assert (read.status_code, read.json()["can_manage"]) == (200, False)
            # Not in the admin's own library.
            assert (await client.get("/api/v1/custom-dashboards")).json() == []
            assert (await client.patch(url, json={"revision": 1, "title": "x"})).status_code == 403
            assert (await client.delete(url)).status_code == 403


async def test_the_owner_rearranges_at_the_revision_read() -> None:
    async with _world() as (scratch, owner, _other, _admin, dashboard_id):
        url = f"/api/v1/custom-dashboards/{dashboard_id}"
        async with _client(scratch.sessions, owner) as client:
            edited = await client.patch(
                url,
                json={
                    "revision": 1,
                    "title": "Morning brief",
                    "operations": [{"op": "move", "id": "ask", "after": "start"}, {"op": "remove", "id": "note"}],
                },
            )
            assert edited.status_code == 200, edited.text
            body = edited.json()
            assert (body["title"], body["revision"], body["widget_count"]) == ("Morning brief", 2, 2)
            assert [w["id"] for w in body["widgets"]] == ["ask", "score"]

            stale = await client.patch(url, json={"revision": 1, "title": "Older tab"})
            assert stale.status_code == 409
            assert "it is now at revision 2" in stale.json()["detail"]

        async with scratch.sessions() as db:
            audits = (await db.execute(select(AuditLog).where(AuditLog.entity_id == dashboard_id))).scalars().all()
            assert [(a.action, a.entity_type) for a in audits] == [("update", "custom_dashboard")]


async def test_a_drag_places_widgets_on_the_grid_and_a_drag_session_is_one_version() -> None:
    async with _world() as (scratch, owner, _other, _admin, dashboard_id):
        url = f"/api/v1/custom-dashboards/{dashboard_id}"
        async with _client(scratch.sessions, owner) as client:
            first = await client.patch(
                url,
                json={"revision": 1, "operations": [{"op": "layout", "items": [{"id": "note", "x": 0, "y": 0, "w": 6, "h": 5}]}]},
            )
            second = await client.patch(
                url,
                json={"revision": 2, "operations": [{"op": "layout", "items": [{"id": "ask", "x": 6, "y": 0, "w": 6, "h": 3}]}]},
            )
            outside = await client.patch(
                url,
                json={"revision": 3, "operations": [{"op": "layout", "items": [{"id": "ask", "x": 9, "y": 0, "w": 6, "h": 3}]}]},
            )
            mixed = await client.patch(
                url,
                json={"revision": 3, "operations": [{"op": "layout", "id": "ask", "items": [{"id": "ask", "x": 0, "y": 0, "w": 6, "h": 3}]}]},
            )
            history = await client.get(f"{url}/versions")
    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text
    widgets = {w["id"]: w["layout"] for w in second.json()["widgets"]}
    assert (widgets["note"], widgets["ask"]) == ({"x": 0, "y": 0, "w": 6, "h": 5}, {"x": 6, "y": 0, "w": 6, "h": 3})
    assert outside.status_code == 422 and "x + w must be at most 12" in outside.text
    assert mixed.status_code == 422 and "a layout carries 'items'" in mixed.text
    # The two drags are one version, at the last revision.
    assert [(v["revision"], v["summary"]) for v in history.json()] == [(3, "rearranged widgets")]


@pytest.mark.parametrize(
    ("body", "fragment"),
    [
        ({"revision": 1, "operations": [{"op": "remove", "id": "ghost"}]}, "no widget has the id 'ghost'"),
        (
            {
                "revision": 1,
                "operations": [
                    {"op": "remove", "id": "score"},
                    {"op": "remove", "id": "note"},
                    {"op": "remove", "id": "ask"},
                ],
            },
            "at least one widget",
        ),
        ({"revision": 1, "operations": [{"op": "upsert", "id": "score"}]}, "an upsert needs 'widget'"),
        ({"revision": 1, "operations": [{"op": "upsert", "widget": {"kind": "gauge"}}]}, "kind must be one of"),
        (
            {"revision": 1, "operations": [{"op": "remove", "id": "score", "widget": {"kind": "text"}}]},
            "a remove names the widget's 'id' and carries no 'widget'",
        ),
        ({"revision": 1, "spec": {"title": "x"}}, "spec"),
        ({"revision": 0}, "revision"),
        ({"revision": 1, "title": ""}, "title"),
    ],
)
async def test_an_invalid_edit_is_a_422_that_writes_nothing(body, fragment) -> None:
    async with _world() as (scratch, owner, _other, _admin, dashboard_id):
        async with _client(scratch.sessions, owner) as client:
            refused = await client.patch(f"/api/v1/custom-dashboards/{dashboard_id}", json=body)
            assert refused.status_code == 422, refused.text
            assert fragment in refused.text
        async with scratch.sessions() as db:
            stored = await db.get(CustomDashboard, dashboard_id)
            assert (stored.revision, stored.widget_count) == (1, 3)


async def test_two_edits_at_one_revision_meet_one_write_and_one_conflict() -> None:
    async with _world() as (scratch, owner, _other, _admin, dashboard_id):
        url = f"/api/v1/custom-dashboards/{dashboard_id}"
        async with _client(scratch.sessions, owner) as client:
            first, second = await asyncio.gather(
                client.patch(url, json={"revision": 1, "title": "Tab A"}),
                client.patch(url, json={"revision": 1, "title": "Tab B"}),
            )
            assert sorted([first.status_code, second.status_code]) == [200, 409], (first.text, second.text)
        async with scratch.sessions() as db:
            assert (await db.get(CustomDashboard, dashboard_id)).revision == 2


async def test_the_owner_deletes_it() -> None:
    async with (
        _world() as (scratch, owner, _other, _admin, dashboard_id),
        _client(scratch.sessions, owner) as client,
    ):
        assert (await client.delete(f"/api/v1/custom-dashboards/{dashboard_id}")).status_code == 204
        assert (await client.get(f"/api/v1/custom-dashboards/{dashboard_id}")).status_code == 404


async def test_the_agent_tools_build_and_edit_the_users_own_dashboard(monkeypatch) -> None:
    import app.core.database as database
    from app.services.builtin_tools import build_custom_dashboard_tools
    from app.tools.base import ToolCall

    async with _world() as (scratch, owner, other, _admin, dashboard_id):
        monkeypatch.setattr(database, "async_session", scratch.sessions)

        sink: list[str] = []

        async def _call(user: User, name: str, arguments: dict):
            _defs, executor = build_custom_dashboard_tools(None, str(user.id), dashboard_sink=sink)
            return await executor.execute_tool(ToolCall(id="c", name=name, arguments=arguments))

        built = await _call(owner, "build_dashboard", _SPEC)
        assert built.is_error is False, built.content
        async with scratch.sessions() as db:
            rows = (
                (await db.execute(select(CustomDashboard).where(CustomDashboard.user_id == owner.id))).scalars().all()
            )
            assert len(rows) == 2
        built_id = next(str(r.id) for r in rows if r.id != dashboard_id)
        # The chat appends this card even when the model leaves the marker out.
        assert sink == [built_id]

        edit = {"dashboard_id": str(dashboard_id), "revision": 1, "operations": [{"op": "remove", "id": "note"}]}
        refused = await _call(other, "edit_dashboard", edit)
        assert (refused.is_error, refused.content) == (True, f"No dashboard `{dashboard_id}` you can edit was found.")

        edited = await _call(owner, "edit_dashboard", edit)
        assert edited.is_error is False, edited.content
        # A refused edit records nothing; a saved one records its dashboard.
        assert sink == [built_id, str(dashboard_id)]
        listed = await _call(owner, "list_dashboards", {})
        assert listed.content.count("- `") == 2
        async with scratch.sessions() as db:
            stored = await db.get(CustomDashboard, dashboard_id)
            assert (stored.revision, stored.widget_count) == (2, 2)


async def test_a_dashboard_stored_with_rows_of_36_px_loads_in_opencti_rows_and_is_kept_until_saved() -> None:
    """A row written before D8 (a version 2 definition, rows of 36 px) reads in today's manifest, placed in
    OpenCTI's rows; loading writes nothing back; its next save stores the manifest."""
    from sqlalchemy import text

    from app.services.dashboard_native import EXTENSION

    legacy = {
        "version": 2, "title": "Old board",
        "dataSources": {}, "visualizations": {"note": {"type": "xtm.text", "options": {"body": "hi"}}}, "inputs": {},
        "layout": {"tabs": {"items": [{"layoutId": "main", "label": "Main"}]},
                   "layoutDefinitions": {"main": {"structure": [{"item": "note", "position": {"x": 0, "y": 8, "w": 6, "h": 8}}]}}},
    }  # fmt: skip
    async with _world() as (scratch, owner, _other, _admin, _dashboard_id):
        async with scratch.sessions() as db:
            row = CustomDashboard(user_id=owner.id, title="Old board", spec=legacy, widget_count=1, revision=1)
            db.add(row)
            await db.commit()
            board = row.id
        async with scratch.sessions() as db:
            loaded = await db.get(CustomDashboard, board)
            assert loaded.spec[EXTENSION]["grid"] == "opencti"
            # 8-16 rows of 36 px are 4-7 rows of 82 px (3.5 and 7.0 rounded).
            assert {k: loaded.spec["widgets"]["note"]["layout"][k] for k in "xywh"} == {"x": 0, "y": 4, "w": 6, "h": 3}
            assert not db.dirty
            await db.commit()
            stored = (await db.execute(text("SELECT spec FROM custom_dashboards WHERE id = :id"), {"id": board})).scalar_one()
            assert stored["version"] == 2  # loading wrote nothing back
        async with _client(scratch.sessions, owner) as client:
            patched = await client.patch(
                f"/api/v1/custom-dashboards/{board}", json={"revision": 1, "title": "Old board, saved"}
            )
            assert patched.status_code == 200, patched.text
        async with scratch.sessions() as db:
            stored = (await db.execute(text("SELECT spec FROM custom_dashboards WHERE id = :id"), {"id": board})).scalar_one()
            assert stored[EXTENSION]["grid"] == "opencti"
            assert {k: stored["widgets"]["note"]["layout"][k] for k in "xywh"} == {"x": 0, "y": 4, "w": 6, "h": 3}
