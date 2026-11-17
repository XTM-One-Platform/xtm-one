"""Files uploaded to a custom dashboard's canvases, through the real routes, against PostgreSQL.

Uploading is editing: the owner, a ``config`` grantee (by user or group) and,
on a company dashboard, an admin; a ``read_only`` grantee gets the 403 and a
stranger the 404. A file is served to whoever reads the dashboard, only while
its definition draws it and only through the dashboard it was uploaded to; a
revoked share stops at once. Deleting the dashboard deletes its files.

``integration``-marked; the temporary database is ``tests/scratch_database.py``,
the object store an in-memory stand-in.
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
from app.api.v1 import custom_dashboards
from app.core.database import get_db
from app.models.custom_dashboard import CustomDashboard
from app.models.file_attachment import FileAttachment
from app.models.group import Group, GroupMember
from app.models.user import User
from app.services.dashboard_assets import DASHBOARD_ASSET_SOURCE
from app.services.dashboard_definition import validate_definition
from tests.scratch_database import ALL_TABLES, scratch_database

# Every dashboard route is behind DASHBOARD_STUDIO: on for the module.
pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("dashboard_studio_enabled")]

_PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
_SVG = (
    b'<svg xmlns="http://www.w3.org/2000/svg" onload="alert(1)"><script>alert(2)</script>'
    b'<rect width="10" height="10"/></svg>'
)


def _definition(title: str, file_id: str | None = None) -> dict:
    visualizations: dict = {"note": {"type": "xtm.markdown", "options": {"markdown": "Hi"}}}
    structure: list = [{"item": "note", "position": {"x": 0, "y": 0, "w": 100, "h": 50}}]
    if file_id:
        visualizations["map"] = {"type": "xtm.image", "options": {"fileId": file_id, "alt": "Map"}}
        structure.append({"item": "map", "position": {"x": 0, "y": 60, "w": 100, "h": 50}})
    return validate_definition(
        {
            "version": 2,
            "title": title,
            "visualizations": visualizations,
            "layout": {
                "tabs": {"items": [{"layoutId": "canvas", "label": "Canvas"}]},
                "layoutDefinitions": {"canvas": {"type": "absolute", "structure": structure}},
            },
        }
    )


@pytest.fixture
def object_store(monkeypatch) -> dict[str, bytes]:
    from app.services.file_storage import file_storage

    objects: dict[str, bytes] = {}

    async def upload(data, filename, content_type=None, prefix=""):
        key = f"{prefix}/{uuid.uuid4().hex}"
        objects[key] = data
        return key

    async def download(key):
        return objects[key]

    async def delete(key):
        objects.pop(key, None)

    monkeypatch.setattr(file_storage, "upload", upload)
    monkeypatch.setattr(file_storage, "download", download)
    monkeypatch.setattr(file_storage, "delete", delete)
    return objects


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
        "dashboard_assets", tables=ALL_TABLES, extensions=("vector",), skip_without_extensions=True
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
                user_id=people["owner"].id, title="Ops room", widget_count=1, revision=1,
                spec=_definition("Ops room"),
                shared_user_ids=[str(people["viewer"].id), str(people["editor"].id)],
                shared_user_permissions={str(people["viewer"].id): "read_only", str(people["editor"].id): "config"},
                shared_group_ids=[str(group.id)],
                shared_group_permissions={str(group.id): "config"},
            )  # fmt: skip
            company = CustomDashboard(
                user_id=people["admin"].id, title="Company wall", widget_count=1, revision=1,
                spec=_definition("Company wall"), is_company_managed=True,
            )  # fmt: skip
            other = CustomDashboard(
                user_id=people["stranger"].id, title="Elsewhere", widget_count=1, revision=1,
                spec=_definition("Elsewhere"),
            )  # fmt: skip
            db.add_all([dashboard, company, other])
            await db.commit()
            ids = {name: user.id for name, user in people.items()}
            ids |= {"dashboard": dashboard.id, "company": company.id, "other": other.id}
        yield scratch, ids


def _assets(ids: dict, key: str = "dashboard", suffix: str = "") -> str:
    return f"/api/v1/custom-dashboards/{ids[key]}/assets{suffix}"


async def _upload(client: httpx.AsyncClient, url: str, data: bytes = _PNG, name: str = "map.png", ctype: str = "image/png"):
    return await client.post(url, files={"file": (name, data, ctype)})


async def _draw(scratch, dashboard_id: uuid.UUID, file_id: str) -> None:
    async with scratch.sessions() as db:
        row = await db.get(CustomDashboard, dashboard_id)
        row.spec = _definition(row.title, file_id)
        await db.commit()


@pytest.mark.parametrize(
    ("who", "key", "status"),
    [
        ("owner", "dashboard", 201),
        ("editor", "dashboard", 201),
        ("member", "dashboard", 201),
        ("viewer", "dashboard", 403),
        ("admin", "dashboard", 403),
        ("stranger", "dashboard", 404),
        ("admin", "company", 201),
        ("owner", "company", 403),
    ],
)
async def test_who_may_upload_a_file_to_a_dashboard(who, key, status, object_store, dashboard_studio_enabled) -> None:
    async with _world() as (scratch, ids), _client(scratch.sessions, ids[who]) as client:
        answer = await _upload(client, _assets(ids, key))
    assert answer.status_code == status, answer.text
    if status == 201:
        assert (answer.json()["content_type"], answer.json()["size"]) == ("image/png", len(_PNG))
        assert len(object_store) == 1
        assert next(iter(object_store)).startswith(f"dashboard-assets/{ids[key]}/")
    else:
        assert object_store == {}


async def test_uploading_is_a_studio_route(object_store, monkeypatch) -> None:
    from app.core.config import settings

    monkeypatch.setattr(settings, "enabled_features", "")
    async with _world() as (scratch, ids), _client(scratch.sessions, ids["owner"]) as client:
        assert (await _upload(client, _assets(ids))).status_code == 404


@pytest.mark.parametrize(
    ("data", "name", "ctype", "detail"),
    [
        (b"<html><script>alert(1)</script></html>", "page.html", "text/html", "Only PNG, JPEG, GIF, WebP and SVG"),
        (b"<html></html>", "trick.png", "image/png", "Only PNG, JPEG, GIF, WebP and SVG"),
        (b"<svg", "broken.svg", "image/svg+xml", "could not be read as an SVG"),
        (b"", "empty.png", "image/png", "empty"),
        (b"\x89PNG\r\n\x1a\n" + b"\x00" * (5 * 1024 * 1024), "huge.png", "image/png", "larger than 5 MB"),
    ],
    ids=["html", "html-as-png", "broken-svg", "empty", "too-large"],
)
async def test_an_upload_that_is_no_image_is_refused(data, name, ctype, detail, object_store, dashboard_studio_enabled) -> None:
    async with _world() as (scratch, ids), _client(scratch.sessions, ids["owner"]) as client:
        answer = await _upload(client, _assets(ids), data, name, ctype)
    assert (answer.status_code, detail in answer.json()["detail"]) == (422, True)
    assert object_store == {}


async def test_an_svg_is_cleaned_on_the_way_in(object_store, dashboard_studio_enabled) -> None:
    async with _world() as (scratch, ids), _client(scratch.sessions, ids["owner"]) as client:
        answer = await _upload(client, _assets(ids), _SVG, "logo.svg", "image/svg+xml")
    assert answer.status_code == 201
    stored = next(iter(object_store.values()))
    assert (b"script" in stored, b"onload" in stored, b"<rect" in stored) == (False, False, True)


async def test_a_file_is_served_only_while_its_dashboard_draws_it(object_store, dashboard_studio_enabled) -> None:
    async with _world() as (scratch, ids):
        async with _client(scratch.sessions, ids["owner"]) as client:
            file_id = (await _upload(client, _assets(ids))).json()["id"]
            # Uploaded, not drawn yet: unknown.
            assert (await client.get(_assets(ids, suffix=f"/{file_id}"))).status_code == 404
        await _draw(scratch, ids["dashboard"], file_id)
        for who, status in (("owner", 200), ("viewer", 200), ("member", 200), ("admin", 200), ("stranger", 404)):
            async with _client(scratch.sessions, ids[who]) as client:
                read = await client.get(_assets(ids, suffix=f"/{file_id}"))
            assert read.status_code == status, who
            if status == 200:
                assert read.content == _PNG
                assert (read.headers["x-content-type-options"], "sandbox" in read.headers["content-security-policy"]) == (
                    "nosniff",
                    True,
                )
        async with _client(scratch.sessions, ids["owner"]) as client:
            assert (await client.get(_assets(ids, suffix="/not-a-uuid"))).status_code == 404


async def test_a_file_of_another_dashboard_is_never_served_through_this_one(object_store, dashboard_studio_enabled) -> None:
    async with _world() as (scratch, ids):
        async with _client(scratch.sessions, ids["stranger"]) as client:
            foreign = (await _upload(client, _assets(ids, "other"))).json()["id"]
        # The owner names the stranger's file in their own dashboard: nothing is served.
        await _draw(scratch, ids["dashboard"], foreign)
        async with _client(scratch.sessions, ids["owner"]) as client:
            assert (await client.get(_assets(ids, suffix=f"/{foreign}"))).status_code == 404


async def test_a_revoked_share_stops_at_once(object_store, dashboard_studio_enabled) -> None:
    async with _world() as (scratch, ids):
        async with _client(scratch.sessions, ids["owner"]) as client:
            file_id = (await _upload(client, _assets(ids))).json()["id"]
        await _draw(scratch, ids["dashboard"], file_id)
        async with scratch.sessions() as db:
            row = await db.get(CustomDashboard, ids["dashboard"])
            row.shared_user_ids = [str(ids["editor"])]
            row.shared_user_permissions = {str(ids["editor"]): "config"}
            await db.commit()
        async with _client(scratch.sessions, ids["viewer"]) as client:
            assert (await client.get(_assets(ids, suffix=f"/{file_id}"))).status_code == 404


async def test_deleting_the_dashboard_deletes_its_files(object_store, dashboard_studio_enabled) -> None:
    async with _world() as (scratch, ids):
        async with _client(scratch.sessions, ids["owner"]) as client:
            await _upload(client, _assets(ids))
            await _upload(client, _assets(ids), _SVG, "logo.svg", "image/svg+xml")
        async with _client(scratch.sessions, ids["stranger"]) as client:
            await _upload(client, _assets(ids, "other"))
        async with _client(scratch.sessions, ids["owner"]) as client:
            assert (await client.delete(f"/api/v1/custom-dashboards/{ids['dashboard']}")).status_code == 204
        async with scratch.sessions() as db:
            rows = (await db.execute(select(FileAttachment).where(FileAttachment.source == DASHBOARD_ASSET_SOURCE))).scalars().all()
        assert [row.s3_object_name.split("/")[1] for row in rows] == [str(ids["other"])]
        assert [key.split("/")[1] for key in object_store] == [str(ids["other"])]


async def test_a_copy_draws_copies_of_its_images_never_the_original_s_files(object_store, dashboard_studio_enabled) -> None:
    async with _world() as (scratch, ids):
        async with _client(scratch.sessions, ids["owner"]) as client:
            file_id = (await _upload(client, _assets(ids))).json()["id"]
        await _draw(scratch, ids["dashboard"], file_id)
        async with _client(scratch.sessions, ids["viewer"]) as client:
            copied = await client.post(f"/api/v1/custom-dashboards/{ids['dashboard']}/duplicate")
            assert copied.status_code == 201, copied.text
            copy_id = copied.json()["id"]
            [twin] = [v["options"]["fileId"] for v in copied.json()["spec"]["visualizations"].values() if v["type"] == "xtm.image"]
            served = await client.get(f"/api/v1/custom-dashboards/{copy_id}/assets/{twin}")
            # The original's file is never served through the copy.
            original = await client.get(f"/api/v1/custom-dashboards/{copy_id}/assets/{file_id}")
        async with _client(scratch.sessions, ids["owner"]) as client:
            assert (await client.delete(f"/api/v1/custom-dashboards/{ids['dashboard']}")).status_code == 204
        async with _client(scratch.sessions, ids["viewer"]) as client:
            after = await client.get(f"/api/v1/custom-dashboards/{copy_id}/assets/{twin}")
    assert twin != file_id
    assert (served.status_code, served.content, original.status_code) == (200, _PNG, 404)
    # The copy keeps its image when the original is deleted.
    assert (after.status_code, after.content) == (200, _PNG)
