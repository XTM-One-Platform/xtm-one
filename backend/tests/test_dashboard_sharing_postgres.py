"""Shared custom dashboards and their history, through the real routes, against PostgreSQL.

One rule (``custom_dashboards.load_dashboard_for``) on every route and tool:
the owner; a ``config`` grantee (by user or group) edits and shares; a
``read_only`` grantee reads; a company dashboard is read by everyone and
edited by admins; an admin reads anyone else's personal dashboard and changes
none. A dashboard one may not read is the unknown id's 404; one read but not
editable is a 403. A revoked share stops at once. Every write is a version; a
restore writes the old spec as a new revision.

``integration``-marked; the temporary database is ``tests/scratch_database.py``.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
import pytest
from fastapi import Depends, FastAPI
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

import app.models  # noqa: F401 - registers every table on Base.metadata
from app.api import deps
from app.api.v1 import custom_dashboards, settings
from app.core.database import get_db
from app.models.custom_dashboard import CustomDashboard, CustomDashboardVersion
from app.models.group import Group, GroupMember
from app.models.user import User
from app.services.custom_dashboard_spec import validate_spec
from tests.scratch_database import ALL_TABLES, scratch_database

# Every dashboard route is behind DASHBOARD_STUDIO: on for the module.
pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("dashboard_studio_enabled")]

_SPEC = {
    "title": "Board brief",
    "widgets": [
        {"kind": "score", "id": "score", "label": "Exposure score", "value": 58},
        {"kind": "text", "id": "note", "body": "Notes"},
    ],
}


@asynccontextmanager
async def _client(session_factory, user_id: uuid.UUID) -> AsyncIterator[httpx.AsyncClient]:
    app = FastAPI()
    app.include_router(custom_dashboards.router, prefix="/api/v1/custom-dashboards")
    app.include_router(settings.router, prefix="/api/v1/settings")

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
async def _world():
    async with scratch_database(
        "dashboard_sharing", tables=ALL_TABLES, extensions=("vector",), skip_without_extensions=True
    ) as scratch:
        async with scratch.sessions() as db:
            people = {
                name: User(email=f"{name}@example.com", hashed_password="x", full_name=name.title(), role=role)
                for name, role in (
                    ("owner", "user"), ("viewer", "user"), ("editor", "user"), ("member", "user"),
                    ("manager", "user"), ("lead", "user"), ("stranger", "user"), ("admin", "admin"),
                )
            }  # fmt: skip
            db.add_all(people.values())
            await db.flush()
            group = Group(name="Analysts")
            leads = Group(name="Leads")
            db.add_all([group, leads])
            await db.flush()
            db.add(GroupMember(group_id=group.id, user_id=people["member"].id))
            db.add(GroupMember(group_id=leads.id, user_id=people["lead"].id))
            dashboard = CustomDashboard(
                user_id=people["owner"].id,
                title="Board brief",
                widget_count=2,
                revision=1,
                spec=validate_spec(_SPEC),
                shared_user_ids=[str(people["viewer"].id), str(people["editor"].id), str(people["manager"].id)],
                shared_user_permissions={
                    str(people["viewer"].id): "read_only", str(people["editor"].id): "config",
                    # The strongest grant: on a dashboard it also deletes (OpenCTI's "admin" access).
                    str(people["manager"].id): "config_with_chats",
                },
                shared_group_ids=[str(group.id), str(leads.id)],
                shared_group_permissions={str(group.id): "config", str(leads.id): "config_with_chats"},
            )
            company = CustomDashboard(
                user_id=people["admin"].id, title="Company KPIs", widget_count=2, revision=1,
                spec=validate_spec({**_SPEC, "title": "Company KPIs"}), is_company_managed=True,
            )  # fmt: skip
            db.add_all([dashboard, company])
            await db.commit()
            ids = {name: user.id for name, user in people.items()}
            ids |= {"dashboard": dashboard.id, "company": company.id, "group": group.id}
        yield scratch, ids


def _url(ids: dict, key: str = "dashboard", suffix: str = "") -> str:
    return f"/api/v1/custom-dashboards/{ids[key]}{suffix}"


def _remove(revision: int, wid: str = "note") -> dict:
    return {"revision": revision, "operations": [{"op": "remove", "id": wid}]}


@pytest.mark.parametrize(
    ("who", "status", "permission", "can_manage"),
    [
        ("owner", 200, "owner", True),
        ("editor", 200, "config", True),
        ("member", 200, "config", True),
        ("manager", 200, "manage", True),
        ("lead", 200, "manage", True),
        ("viewer", 200, "read_only", False),
        ("admin", 200, "read_only", False),
        ("stranger", 404, None, None),
    ],
)
async def test_who_reads_a_shared_dashboard_and_at_which_tier(who, status, permission, can_manage) -> None:
    async with _world() as (scratch, ids), _client(scratch.sessions, ids[who]) as client:
        read = await client.get(_url(ids))
    assert read.status_code == status
    if status == 200:
        body = read.json()
        assert (body["permission"], body["can_manage"]) == (permission, can_manage)
        # Who it is shared with goes to those who may change it only.
        assert len(body["shared_user_ids"]) == (3 if can_manage else 0)
        # An admin reads it by their role, not as themselves: never their /dashboard.
        assert body["can_use_as_home"] is (who != "admin")


@pytest.mark.parametrize(
    ("who", "status"),
    [("owner", 200), ("editor", 200), ("member", 200), ("manager", 200), ("viewer", 403), ("admin", 403), ("stranger", 404)],
)
async def test_who_edits_a_shared_dashboard(who, status) -> None:
    async with _world() as (scratch, ids), _client(scratch.sessions, ids[who]) as client:
        edited = await client.patch(_url(ids), json=_remove(1))
    assert edited.status_code == status, edited.text
    if status == 403:
        assert edited.json()["detail"] == "You can view this dashboard but not change it"


async def test_a_company_dashboard_is_read_by_everyone_and_changed_by_admins() -> None:
    async with _world() as (scratch, ids):
        async with _client(scratch.sessions, ids["stranger"]) as client:
            read = await client.get(_url(ids, "company"))
            refused = await client.patch(_url(ids, "company"), json=_remove(1))
            listed = await client.get("/api/v1/custom-dashboards", params={"scope": "company"})
        async with _client(scratch.sessions, ids["admin"]) as client:
            changed = await client.patch(_url(ids, "company"), json=_remove(1))
    assert read.json()["permission"] == "read_only"
    assert (refused.status_code, refused.json()["detail"]) == (403, "Only admins can change a company dashboard")
    assert [row["title"] for row in listed.json()] == ["Company KPIs"]
    assert changed.status_code == 200


async def test_lists_are_three_tier_for_everyone() -> None:
    async with _world() as (scratch, ids):
        async with _client(scratch.sessions, ids["viewer"]) as client:
            shared = await client.get("/api/v1/custom-dashboards", params={"scope": "shared"})
            mine = await client.get("/api/v1/custom-dashboards", params={"scope": "mine"})
        async with _client(scratch.sessions, ids["admin"]) as client:
            everything = await client.get("/api/v1/custom-dashboards")
    assert [(r["title"], r["permission"]) for r in shared.json()] == [("Board brief", "read_only")]
    assert mine.json() == []
    # An admin's list holds what they may open as themselves, not the owner's private board.
    assert [r["title"] for r in everything.json()] == ["Company KPIs"]


async def test_sharing_is_changed_by_editors_and_a_revoked_share_stops_at_once() -> None:
    async with _world() as (scratch, ids):
        async with _client(scratch.sessions, ids["viewer"]) as client:
            refused = await client.put(_url(ids, suffix="/sharing"), json={"shared_user_ids": []})
        async with _client(scratch.sessions, ids["editor"]) as client:
            changed = await client.put(
                _url(ids, suffix="/sharing"),
                json={
                    "shared_user_ids": [str(ids["editor"])],
                    "shared_user_permissions": {str(ids["editor"]): "config"},
                },
            )
            published = await client.put(_url(ids, suffix="/sharing"), json={"is_company_managed": True})
        async with _client(scratch.sessions, ids["viewer"]) as client:
            after = await client.get(_url(ids))
        async with scratch.sessions() as db:
            stored = await db.get(CustomDashboard, ids["dashboard"])
            kept = (stored.revision, stored.is_company_managed)
    assert refused.status_code == 403
    assert changed.status_code == 200, changed.text
    assert changed.json()["shared_user_ids"] == [str(ids["editor"])]
    # Making it a company dashboard is an admin's act.
    assert published.status_code == 403
    assert after.status_code == 404
    # Sharing writes no revision.
    assert kept == (1, False)


async def test_every_write_is_a_version_and_a_restore_is_a_new_revision() -> None:
    async with _world() as (scratch, ids):
        async with _client(scratch.sessions, ids["editor"]) as client:
            first = await client.patch(_url(ids), json=_remove(1))
            assert first.status_code == 200
            renamed = await client.patch(_url(ids), json={"revision": 2, "title": "Board brief v2"})
            assert renamed.status_code == 200
            history = await client.get(_url(ids, suffix="/versions"))
            stale = await client.post(_url(ids, suffix="/versions/2/restore"), json={"revision": 2})
            unknown = await client.post(_url(ids, suffix="/versions/99/restore"), json={"revision": 3})
            restored = await client.post(_url(ids, suffix="/versions/2/restore"), json={"revision": 3})
            version = await client.get(_url(ids, suffix="/versions/2"))
        async with _client(scratch.sessions, ids["viewer"]) as client:
            viewer_history = await client.get(_url(ids, suffix="/versions"))
            viewer_restore = await client.post(_url(ids, suffix="/versions/2/restore"), json={"revision": 4})
        async with scratch.sessions() as db:
            latest = (
                await db.execute(
                    select(CustomDashboardVersion).where(
                        CustomDashboardVersion.dashboard_id == ids["dashboard"], CustomDashboardVersion.revision == 4
                    )
                )
            ).scalar_one()
    assert [(v["revision"], v["source"], v["author_name"], v["summary"]) for v in history.json()] == [
        (3, "manual", "Editor", "renamed"),
        (2, "manual", "Editor", "removed note"),
    ]
    assert (stale.status_code, unknown.status_code) == (409, 404)
    assert restored.status_code == 200, restored.text
    body = restored.json()
    assert (body["revision"], body["title"], [w["id"] for w in body["widgets"]]) == (4, "Board brief", ["score"])
    assert version.json()["spec"]["title"] == "Board brief"
    assert [v["revision"] for v in viewer_history.json()] == [4, 3, 2]
    assert viewer_restore.status_code == 403
    assert (latest.source, latest.restored_from_revision, latest.summary) == ("restore", 2, "Restored revision 2")


@pytest.mark.parametrize(
    ("who", "status"),
    [("owner", 204), ("manager", 204), ("lead", 204), ("editor", 403), ("member", 403), ("viewer", 403), ("admin", 403), ("stranger", 404)],
)
async def test_the_owner_and_those_who_manage_it_delete(who, status) -> None:
    async with _world() as (scratch, ids), _client(scratch.sessions, ids[who]) as client:
        deleted = await client.delete(_url(ids))
    assert deleted.status_code == status


async def test_a_shared_dashboard_may_be_ones_home_until_the_share_is_revoked() -> None:
    async with _world() as (scratch, ids):
        async with _client(scratch.sessions, ids["viewer"]) as client:
            chosen = await client.put("/api/v1/settings/preferences", json={"home_dashboard_id": str(ids["dashboard"])})
            private = await client.put("/api/v1/settings/preferences", json={"home_dashboard_id": str(uuid.uuid4())})
        async with _client(scratch.sessions, ids["admin"]) as client:
            admin_choice = await client.put(
                "/api/v1/settings/preferences", json={"home_dashboard_id": str(ids["dashboard"])}
            )
    assert chosen.status_code == 200, chosen.text
    assert private.status_code == 404
    # An admin reads the owner's board but does not make it theirs.
    assert admin_choice.status_code == 404


async def test_the_agent_acts_with_its_users_tier(monkeypatch) -> None:
    import app.core.database as database
    from app.services.builtin_tools import build_custom_dashboard_tools
    from app.tools.base import ToolCall

    async with _world() as (scratch, ids):
        monkeypatch.setattr(database, "async_session", scratch.sessions)

        async def _call(user_id: uuid.UUID, name: str, arguments: dict):
            _defs, executor = build_custom_dashboard_tools(None, str(user_id))
            return await executor.execute_tool(ToolCall(id="c", name=name, arguments=arguments))

        edit = {"dashboard_id": str(ids["dashboard"]), "operations": [{"op": "remove", "id": "note"}]}
        viewer_read = await _call(ids["viewer"], "get_dashboard", {"dashboard_id": str(ids["dashboard"])})
        viewer_edit = await _call(ids["viewer"], "edit_dashboard", edit)
        admin_edit = await _call(ids["admin"], "edit_dashboard", edit)
        member_edit = await _call(ids["member"], "edit_dashboard", edit)
        listed = await _call(ids["viewer"], "list_dashboards", {})
    assert "shared with you to read: you cannot change it" in viewer_read.content
    assert viewer_edit.content == f"No dashboard `{ids['dashboard']}` you can edit was found."
    assert admin_edit.content == f"No dashboard `{ids['dashboard']}` you can edit was found."
    assert member_edit.is_error is False, member_edit.content
    assert ", read only)" in listed.content


async def test_two_versions_are_compared_by_whoever_reads_the_dashboard() -> None:
    async with _world() as (scratch, ids):
        async with _client(scratch.sessions, ids["editor"]) as client:
            assert (await client.patch(_url(ids), json=_remove(1))).status_code == 200
            assert (await client.patch(_url(ids), json={"revision": 2, "title": "Board brief v2"})).status_code == 200
        async with _client(scratch.sessions, ids["viewer"]) as client:
            diff = await client.get(_url(ids, suffix="/versions/diff?from=2&to=3"))
            unknown = await client.get(_url(ids, suffix="/versions/diff?from=2&to=99"))
            malformed = await client.get(_url(ids, suffix="/versions/diff?from=0&to=3"))
        async with _client(scratch.sessions, ids["stranger"]) as client:
            hidden = await client.get(_url(ids, suffix="/versions/diff?from=2&to=3"))
    assert diff.status_code == 200, diff.text
    body = diff.json()
    assert (body["from_version"], body["to_version"], body["changes"]) == (2, 3, "renamed")
    assert [(line["type"], line["content"].strip()) for line in body["lines"] if line["type"] != "context"] == [
        ("remove", '"title": "Board brief",'),
        ("add", '"title": "Board brief v2",'),
    ]
    assert (body["added"], body["removed"]) == (1, 1)
    # The compare shows the stored manifest: the title is XTM One's, under its extension key.
    assert '-    "title": "Board brief",' in body["unified_diff"]
    assert (unknown.status_code, malformed.status_code, hidden.status_code) == (404, 422, 404)


async def test_the_agent_reads_one_part_and_places_a_new_visualization_in_one_step(monkeypatch, dashboard_studio_enabled) -> None:
    import json

    import app.core.database as database
    from app.services.builtin_tools import build_custom_dashboard_tools
    from app.tools.base import ToolCall

    async with _world() as (scratch, ids):
        monkeypatch.setattr(database, "async_session", scratch.sessions)

        async def _call(name: str, arguments: dict):
            _defs, executor = build_custom_dashboard_tools(None, str(ids["editor"]))
            return await executor.execute_tool(ToolCall(id="c", name=name, arguments=arguments))

        dashboard = str(ids["dashboard"])
        layout = await _call("get_dashboard", {"dashboard_id": dashboard, "part": "layout"})
        unknown = await _call("get_dashboard", {"dashboard_id": dashboard, "part": "secrets"})
        source = {"type": "static", "options": {"shape": "number", "data": {"value": 58}}}
        gauge = {"type": "xtm.fillergauge", "title": "Exposure", "dataSources": {"primary": "exposure"}, "options": {"maxValue": 100}}
        lid = json.loads(layout.content.split(":\n", 1)[1])["tabs"]["items"][0]["layoutId"]
        edited = await _call(
            "edit_dashboard",
            {
                "dashboard_id": dashboard,
                "operations": [
                    {"op": "set", "path": "dataSources.exposure", "value": source},
                    {"op": "set", "path": "visualizations.gauge", "value": gauge},
                    {"op": "layout", "layoutId": lid, "items": [{"id": "gauge", "x": 6, "y": 20, "w": 6, "h": 5}]},
                ],
            },
        )
        async with scratch.sessions() as db:
            stored = await db.get(CustomDashboard, ids["dashboard"])
            from app.services.dashboard_definition import upgrade

            structure = upgrade(stored.spec)["layout"]["layoutDefinitions"][lid]["structure"]
    assert layout.is_error is False and layout.content.startswith("`layout` of dashboard **Board brief**")
    assert unknown.is_error is True
    assert edited.is_error is False, edited.content
    assert structure[-1]["item"] == "gauge" and structure[-1]["position"] == {"x": 6, "y": 20, "w": 6, "h": 5}
