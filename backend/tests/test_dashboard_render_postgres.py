"""A dashboard's PDF / PNG export and the render routes its page reads through, against PostgreSQL.

The export (``POST /{id}/render-export``) is read access, through the real
route. The page the export's browser loads holds a render token, nothing
else: ``/render``, ``/render/widgets/.../data``, ``/render/data-sources/.../data``
accept only a token bound to THEIR dashboard and apply the ordinary read rule
to the token's user, so a token of someone who cannot read the dashboard (a
stranger, a share revoked since) reads nothing.

``integration``-marked; the temporary database is ``tests/scratch_database.py``.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from types import SimpleNamespace

import httpx
import pytest
from fastapi import Depends, FastAPI
from sqlalchemy.ext.asyncio import AsyncSession

import app.models  # noqa: F401 - registers every table on Base.metadata
from app.api import deps
from app.api.v1 import custom_dashboards
from app.core.database import get_db
from app.models.custom_dashboard import CustomDashboard
from app.models.user import User
from app.services import dashboard_export
from app.services.dashboard_definition import validate_definition
from app.services.dashboard_export import mint_dashboard_render_token
from app.services.presentation_export import ExportedFile
from tests.scratch_database import ALL_TABLES, scratch_database

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("dashboard_studio_enabled")]


def _definition(title: str) -> dict:
    return {
        "version": 2,
        "title": title,
        "dataSources": {"open": {"type": "static", "options": {"shape": "number", "data": {"value": 37}}}},
        "visualizations": {"kpi": {"type": "xtm.singlevalue", "title": "Open", "dataSources": {"primary": "open"}}},
        "layout": {
            "tabs": {"items": [{"layoutId": "main", "label": "Main"}]},
            "layoutDefinitions": {"main": {"structure": [{"item": "kpi", "position": {"x": 0, "y": 0, "w": 6, "h": 6}}]}},
        },
    }


@asynccontextmanager
async def _client(session_factory, user_id: uuid.UUID | None) -> AsyncIterator[httpx.AsyncClient]:
    """A client signed in as *user_id*, or with no session at all (the export's page)."""
    app = FastAPI()
    app.include_router(custom_dashboards.router, prefix="/api/v1/custom-dashboards")

    async def _get_db():
        async with session_factory() as db:
            yield db
            await db.commit()

    async def _signed_in(db: AsyncSession = Depends(get_db)) -> User:
        if user_id is None:
            from fastapi import HTTPException

            raise HTTPException(status_code=401, detail="Not authenticated")
        return await db.get(User, user_id)

    for dependency in (deps.get_current_user, deps.get_current_user_ungated):
        app.dependency_overrides[dependency] = _signed_in
    app.dependency_overrides[get_db] = _get_db
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        yield client


@asynccontextmanager
async def _world():
    async with scratch_database(
        "dashboard_render", tables=ALL_TABLES, extensions=("vector",), skip_without_extensions=True
    ) as scratch:
        async with scratch.sessions() as db:
            owner = User(email="owner@example.com", hashed_password="x", full_name="Owner", role="user")
            viewer = User(email="viewer@example.com", hashed_password="x", full_name="Viewer", role="user")
            stranger = User(email="stranger@example.com", hashed_password="x", full_name="Stranger", role="user")
            db.add_all([owner, viewer, stranger])
            await db.flush()
            share = {"shared_user_ids": [str(viewer.id)], "shared_user_permissions": {str(viewer.id): "read_only"}}
            shared = CustomDashboard(user_id=owner.id, title="Ops", widget_count=1, revision=1, spec=validate_definition(_definition("Ops")), **share)  # fmt: skip
            other = CustomDashboard(user_id=owner.id, title="Other", widget_count=1, revision=1, spec=validate_definition(_definition("Other")), **share)  # fmt: skip
            db.add_all([shared, other])
            await db.commit()
            ids = SimpleNamespace(owner=owner.id, viewer=viewer.id, stranger=stranger.id, dashboard=shared.id, other=other.id)
        yield scratch, ids


def _render(ids, suffix: str = "", token: str | None = None) -> str:
    query = f"?render_token={token}" if token is not None else ""
    return f"/api/v1/custom-dashboards/{ids.dashboard}/render{suffix}{query}"


async def test_the_render_routes_read_as_the_tokens_user_and_only_for_its_dashboard() -> None:
    async with _world() as (scratch, ids), _client(scratch.sessions, None) as client:
        token = mint_dashboard_render_token(ids.dashboard, ids.viewer)
        page = await client.get(_render(ids, token=token))
        widget = await client.post(_render(ids, "/widgets/kpi/data", token), json={})
        source = await client.post(_render(ids, "/data-sources/open/data"), json={}, headers={"Authorization": f"Bearer {token}"})
        unknown_widget = await client.post(_render(ids, "/widgets/nope/data", token), json={})
        # Bound to another dashboard, absent, forged.
        elsewhere = await client.get(_render(ids, token=mint_dashboard_render_token(ids.other, ids.viewer)))
        missing = await client.get(_render(ids))
        forged = await client.get(_render(ids, token="not-a-token"))
        # A stranger's token reads nothing: the read rule applies to the token's user.
        stranger = await client.get(_render(ids, token=mint_dashboard_render_token(ids.dashboard, ids.stranger)))
    assert page.status_code == 200, page.text
    assert (page.json()["title"], page.json()["can_manage"]) == ("Ops", False)
    assert widget.status_code == 200, widget.text
    assert (widget.json()["status"], widget.json()["data"]) == ("ok", {"value": 37.0, "change": None})
    assert source.json()["data"] == {"value": 37.0, "change": None}
    assert unknown_widget.status_code == 404
    assert (elsewhere.status_code, missing.status_code, forged.status_code, stranger.status_code) == (403, 401, 401, 404)


async def test_a_share_revoked_after_the_token_was_minted_stops_the_render() -> None:
    async with _world() as (scratch, ids):
        token = mint_dashboard_render_token(ids.dashboard, ids.viewer)
        async with _client(scratch.sessions, ids.owner) as owner:
            revoked = await owner.put(f"/api/v1/custom-dashboards/{ids.dashboard}/sharing", json={"shared_user_ids": []})
        async with _client(scratch.sessions, None) as client:
            page = await client.get(_render(ids, token=token))
    assert revoked.status_code == 200, revoked.text
    assert page.status_code == 404


@pytest.fixture
def exports(monkeypatch) -> list[dict]:
    """The renders asked for, each answered with a small fake file (no browser in this test)."""
    seen: list[dict] = []

    async def fake_export(**kwargs) -> ExportedFile:
        seen.append(kwargs)
        extension = kwargs["export_format"]
        return ExportedFile(f"ops.{extension}", "application/pdf" if extension == "pdf" else "image/png", b"bytes")

    monkeypatch.setattr(dashboard_export, "export_dashboard", fake_export)
    return seen


@pytest.mark.parametrize(
    ("who", "body", "status"),
    [
        ("viewer", {"format": "pdf"}, 200),
        ("owner", {"format": "png", "layout_ids": ["main"]}, 200),
        ("viewer", {"format": "png", "visualization_id": "kpi"}, 200),
        ("stranger", {"format": "pdf"}, 404),
        ("viewer", {"format": "pdf", "layout_ids": ["elsewhere"]}, 422),
        ("viewer", {"format": "png", "visualization_id": "nope"}, 404),
        # One visualization is a picture, with no tab named.
        ("viewer", {"format": "pdf", "visualization_id": "kpi"}, 422),
        ("viewer", {"format": "png", "visualization_id": "kpi", "layout_ids": ["main"]}, 422),
        ("viewer", {"format": "svg"}, 422),
    ],
)
async def test_who_may_export_and_what(who, body, status, exports) -> None:
    async with _world() as (scratch, ids), _client(scratch.sessions, getattr(ids, who)) as client:
        answer = await client.post(f"/api/v1/custom-dashboards/{ids.dashboard}/render-export", json=body)
    assert answer.status_code == status, answer.text
    if status != 200:
        assert exports == []
        return
    assert answer.content == b"bytes"
    assert answer.headers["cache-control"] == "private, no-store"
    assert answer.headers["content-disposition"].startswith("attachment;")
    assert exports == [
        {
            "dashboard_id": ids.dashboard,
            "user_id": getattr(ids, who),
            "title": "Ops",
            "export_format": body["format"],
            "layout_ids": body.get("layout_ids"),
            "visualization_id": body.get("visualization_id"),
        }
    ]


async def test_a_failed_render_says_nothing_of_why(monkeypatch) -> None:
    async def failing(**kwargs):
        raise dashboard_export.DashboardRenderError("The dashboard did not finish rendering in time: secret detail")

    monkeypatch.setattr(dashboard_export, "export_dashboard", failing)
    async with _world() as (scratch, ids), _client(scratch.sessions, ids.viewer) as client:
        answer = await client.post(f"/api/v1/custom-dashboards/{ids.dashboard}/render-export", json={"format": "pdf"})
    assert (answer.status_code, answer.json()) == (502, {"detail": "The dashboard could not be rendered."})
