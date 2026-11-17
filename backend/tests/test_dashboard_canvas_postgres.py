"""Canvas edits through the real PATCH route, against PostgreSQL.

The studio saves a canvas change as ``set`` / ``delete`` operations by path (an
element, the layout, the theme): the owner and a ``config`` grantee (by user or
group) may, a ``read_only`` grantee gets the 403, a stranger the 404, an admin
edits a company dashboard and no one else's. An edit that only moves items is
one "rearranged" version per minute, like a drag on the grid; adding an element
is a version of its own.

``integration``-marked; the temporary database is ``tests/scratch_database.py``.
"""

from __future__ import annotations

import copy
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
from app.models.group import Group, GroupMember
from app.models.user import User
from app.services.dashboard_definition import validate_definition
from tests.scratch_database import ALL_TABLES, scratch_database

# Every dashboard route is behind DASHBOARD_STUDIO: on for the module.
pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("dashboard_studio_enabled")]


def _definition(title: str) -> dict:
    return validate_definition(
        {
            "version": 2,
            "title": title,
            "visualizations": {
                "box": {"type": "xtm.shape.rectangle", "options": {"rx": 8}},
                "note": {"type": "xtm.markdown", "options": {"markdown": "Hi"}},
            },
            "layout": {
                "tabs": {"items": [{"layoutId": "wall", "label": "Wall"}]},
                "layoutDefinitions": {
                    "wall": {
                        "type": "absolute",
                        "structure": [
                            {"item": "box", "position": {"x": 0, "y": 0, "w": 100, "h": 50}},
                            {"item": "note", "position": {"x": 200, "y": 0, "w": 100, "h": 50}},
                        ],
                    }
                },
            },
        }
    )


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

    app.dependency_overrides[deps.get_current_user] = _signed_in
    app.dependency_overrides[get_db] = _get_db
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        yield client


@asynccontextmanager
async def _world():
    async with scratch_database(
        "dashboard_canvas", tables=ALL_TABLES, extensions=("vector",), skip_without_extensions=True
    ) as scratch:
        async with scratch.sessions() as db:
            people = {
                name: User(email=f"{name}@example.com", hashed_password="x", full_name=name.title(), role=role)
                for name, role in (
                    ("owner", "user"), ("viewer", "user"), ("editor", "user"), ("member", "user"),
                    ("stranger", "user"), ("admin", "admin"),
                )
            }  # fmt: skip
            db.add_all(people.values())
            await db.flush()
            group = Group(name="Analysts")
            db.add(group)
            await db.flush()
            db.add(GroupMember(group_id=group.id, user_id=people["member"].id))
            dashboard = CustomDashboard(
                user_id=people["owner"].id, title="Wall", widget_count=0, revision=1, spec=_definition("Wall"),
                shared_user_ids=[str(people["viewer"].id), str(people["editor"].id)],
                shared_user_permissions={str(people["viewer"].id): "read_only", str(people["editor"].id): "config"},
                shared_group_ids=[str(group.id)],
                shared_group_permissions={str(group.id): "config"},
            )  # fmt: skip
            company = CustomDashboard(
                user_id=people["admin"].id, title="Company wall", widget_count=0, revision=1,
                spec=_definition("Company wall"), is_company_managed=True,
            )  # fmt: skip
            db.add_all([dashboard, company])
            await db.commit()
            ids = {name: user.id for name, user in people.items()}
            ids |= {"dashboard": dashboard.id, "company": company.id}
        yield scratch, ids


def _add_ellipse() -> list[dict]:
    layout = copy.deepcopy(_definition("Wall")["layout"])
    layout["layoutDefinitions"]["wall"]["structure"].append(
        {"item": "dot", "type": "block", "position": {"x": 400, "y": 0, "w": 80, "h": 80}}
    )
    return [
        {"op": "set", "path": "visualizations.dot", "value": {"type": "xtm.shape.ellipse", "options": {"fillOpacity": 0.5}}},
        {"op": "set", "path": "layout", "value": layout},
        {"op": "set", "path": "theme", "value": "light"},
    ]


@pytest.mark.parametrize(
    ("who", "key", "status"),
    [
        ("owner", "dashboard", 200),
        ("editor", "dashboard", 200),
        ("member", "dashboard", 200),
        ("viewer", "dashboard", 403),
        ("admin", "dashboard", 403),
        ("stranger", "dashboard", 404),
        ("admin", "company", 200),
        ("owner", "company", 403),
    ],
)
async def test_who_may_edit_a_canvas(who, key, status) -> None:
    async with _world() as (scratch, ids), _client(scratch.sessions, ids[who]) as client:
        answer = await client.patch(f"/api/v1/custom-dashboards/{ids[key]}", json={"revision": 1, "operations": _add_ellipse()})
        stored = (await client.get(f"/api/v1/custom-dashboards/{ids[key]}")).json() if status != 404 else None
    assert answer.status_code == status, answer.text
    if status == 200:
        spec = answer.json()["spec"]
        assert spec["visualizations"]["dot"] == {
            "type": "xtm.shape.ellipse", "dataSources": {}, "options": {"fillOpacity": 0.5},
            "context": {}, "eventHandlers": [],
            "containerOptions": {"visibility": {"showConditions": [], "hideConditions": [], "hideWhenNoData": False}},
        }  # fmt: skip
        assert spec["theme"] == "light"
        # Shapes are no widgets.
        assert answer.json()["widget_count"] == 0
    elif stored is not None:
        assert "dot" not in stored["spec"]["visualizations"]


async def test_an_invalid_canvas_edit_is_a_422_naming_the_field() -> None:
    async with _world() as (scratch, ids), _client(scratch.sessions, ids["owner"]) as client:
        answer = await client.patch(
            f"/api/v1/custom-dashboards/{ids['dashboard']}",
            json={"revision": 1, "operations": [{"op": "set", "path": "visualizations.box", "value": {"type": "xtm.shape.rectangle", "options": {"fillColor": "red"}}}]},
        )
    assert answer.status_code == 422
    assert "visualizations.box: options: fillColor: a colour is #rgb, #rrggbb or #rrggbbaa" in answer.json()["detail"]


async def test_moves_on_a_canvas_are_one_version_and_an_added_element_is_its_own() -> None:
    async with _world() as (scratch, ids), _client(scratch.sessions, ids["owner"]) as client:
        url = f"/api/v1/custom-dashboards/{ids['dashboard']}"
        revision = 1
        for x in (10, 20):
            layout = copy.deepcopy(_definition("Wall")["layout"])
            layout["layoutDefinitions"]["wall"]["structure"][0]["position"]["x"] = x
            # A move on a canvas, and a change of the stacking order: placements both.
            layout["layoutDefinitions"]["wall"]["structure"].reverse()
            moved = await client.patch(url, json={"revision": revision, "operations": [{"op": "set", "path": "layout", "value": layout}]})
            assert moved.status_code == 200, moved.text
            revision = moved.json()["revision"]
        added = await client.patch(url, json={"revision": revision, "operations": _add_ellipse()})
        assert added.status_code == 200, added.text
        history = (await client.get(f"{url}/versions")).json()
    assert [(v["revision"], v["summary"] == "rearranged widgets") for v in history] == [(4, False), (3, True)]
