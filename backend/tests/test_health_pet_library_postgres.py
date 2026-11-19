"""The Concierge companion's pet library (#8261) against PostgreSQL.

Every signed-in user lists pets and reads a spritesheet; only administrators
add or remove one; a hostile upload is a 422 and stores nothing; a built-in
pet is never removed.

``integration``-marked; skips when PostgreSQL is unreachable.
"""

from __future__ import annotations

import io
import json
import zipfile
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
import pytest
from fastapi import FastAPI
from PIL import Image
from sqlalchemy import func, select

import app.models  # noqa: F401 - registers every table on Base.metadata
from app.api import deps
from app.api.v1 import platform_health_companion as routes
from app.core.config import settings
from app.core.database import get_db
from app.models.audit_log import AuditLog
from app.models.health_pet import HealthPet
from app.models.user import User
from tests.health_support import World, health_database

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def _flag_on(monkeypatch):
    monkeypatch.setattr(settings, "enabled_features", "CONCIERGE")


@asynccontextmanager
async def _client(sessions, user: User) -> AsyncIterator[httpx.AsyncClient]:
    app = FastAPI()
    app.include_router(routes.router, prefix="/api/v1/health")

    async def _get_db():
        async with sessions() as db:
            yield db
            await db.commit()

    async def _current():
        async with sessions() as db:
            return await db.get(User, user.id)

    app.dependency_overrides[deps.get_current_user] = _current
    app.dependency_overrides[get_db] = _get_db
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        yield client


def _package(slug: str = "owl", **manifest) -> bytes:
    sheet = io.BytesIO()
    Image.new("RGBA", (1536, 1872), (200, 100, 50, 255)).save(sheet, format="PNG")
    data = {"id": slug, "displayName": "Owl", "spritesheetPath": "spritesheet.png", **manifest}
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as archive:
        archive.writestr("pet.json", json.dumps(data))
        archive.writestr("spritesheet.png", sheet.getvalue())
    return out.getvalue()


async def _upload(client: httpx.AsyncClient, package: bytes) -> httpx.Response:
    return await client.post("/api/v1/health/pets", files=[("files", ("owl.zip", package, "application/zip"))])


# ── The library ────────────────────────────────────────────────────────────


async def test_every_user_reads_the_library_and_alfred_comes_first() -> None:
    async with health_database() as sessions:
        world = await World().build(sessions)
        async with _client(sessions, world.stranger) as client:
            listing = await client.get("/api/v1/health/pets")
            sheet = await client.get("/api/v1/health/pets/alfred/spritesheet")
            missing = await client.get("/api/v1/health/pets/nobody/spritesheet")
            too_long = await client.get(f"/api/v1/health/pets/{'x' * 300}/spritesheet")
    assert [(p["slug"], p["builtin"], p["sprite_version"]) for p in listing.json()] == [("alfred", True, 1)]
    assert sheet.status_code == 200
    assert sheet.headers["content-type"] == "image/webp"
    assert sheet.headers["x-content-type-options"] == "nosniff"
    assert sheet.content[:4] == b"RIFF" and sheet.content[8:12] == b"WEBP"
    assert sheet.headers["etag"] == f'"{listing.json()[0]["sha256"]}"'
    assert (missing.status_code, too_long.status_code) == (404, 404)


async def test_only_an_administrator_adds_a_pet_and_it_is_stored_re_encoded() -> None:
    async with health_database() as sessions:
        world = await World().build(sessions)
        package = _package()
        async with _client(sessions, world.configurer) as client:
            refused = await _upload(client, package)
        async with _client(sessions, world.admin) as client:
            created = await _upload(client, package)
            again = await _upload(client, package)
            builtin_id = await _upload(client, _package("alfred"))
        async with _client(sessions, world.reader) as client:
            listing = await client.get("/api/v1/health/pets")
            sheet = await client.get("/api/v1/health/pets/owl/spritesheet")
        async with sessions() as db:
            rows = (await db.execute(select(HealthPet))).scalars().all()
            audits = (
                (await db.execute(select(AuditLog.action).where(AuditLog.entity_type == "health_pet"))).scalars().all()
            )
    assert refused.status_code == 403
    assert created.status_code == 201 and created.json()["slug"] == "owl"
    assert again.status_code == 409 and again.json()["detail"]["code"] == "slug_taken"
    assert builtin_id.status_code == 409
    assert [p["slug"] for p in listing.json()] == ["alfred", "owl"]
    assert [(r.slug, r.created_by_user_id) for r in rows] == [("owl", world.admin.id)]
    assert sheet.content == rows[0].spritesheet and sheet.content[8:12] == b"WEBP"
    assert audits == ["create"]


@pytest.mark.parametrize(
    ("files", "code"),
    [
        ([("files", ("x.zip", b"not a zip", "application/zip"))], "not_a_package"),
        ([("files", ("x.zip", _package(displayName="Owl\nx"), "application/zip"))], "manifest"),
        ([("files", ("x.zip", _package(spritesheetPath="../spritesheet.png"), "application/zip"))], "manifest"),
        (
            [
                (
                    "files",
                    ("pet.json", b'{"id":"owl","displayName":"Owl","spritesheetPath":"s.svg"}', "application/json"),
                ),
                ("files", ("s.svg", b"<svg/>", "image/svg+xml")),
            ],
            "manifest",
        ),
        (
            [
                (
                    "files",
                    ("pet.json", b'{"id":"owl","displayName":"Owl","spritesheetPath":"s.png"}', "application/json"),
                ),
                ("files", ("s.png", b"<svg/>", "image/png")),
            ],
            "image_type",
        ),
        ([("files", ("a", b"1", "text/plain"))] * 3, "not_a_package"),
    ],
    ids=["not-zip", "control-char", "traversal", "svg-path", "svg-content", "three-files"],
)
async def test_a_hostile_upload_is_a_422_and_stores_nothing(files, code) -> None:
    async with health_database() as sessions:
        world = await World().build(sessions)
        async with _client(sessions, world.admin) as client:
            response = await client.post("/api/v1/health/pets", files=files)
        async with sessions() as db:
            count = await db.scalar(select(func.count()).select_from(HealthPet))
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == code
    assert count == 0


async def test_removing_a_pet_is_for_administrators_and_a_built_in_never() -> None:
    async with health_database() as sessions:
        world = await World().build(sessions)
        async with _client(sessions, world.admin) as client:
            await _upload(client, _package())
        async with _client(sessions, world.reader) as client:
            refused = await client.delete("/api/v1/health/pets/owl")
        async with _client(sessions, world.admin) as client:
            builtin = await client.delete("/api/v1/health/pets/alfred")
            removed = await client.delete("/api/v1/health/pets/owl")
            gone = await client.delete("/api/v1/health/pets/owl")
            listing = await client.get("/api/v1/health/pets")
        async with sessions() as db:
            audits = (
                (
                    await db.execute(
                        select(AuditLog.action)
                        .where(AuditLog.entity_type == "health_pet")
                        .order_by(AuditLog.created_at)
                    )
                )
                .scalars()
                .all()
            )
    assert (refused.status_code, builtin.status_code, removed.status_code, gone.status_code) == (403, 409, 204, 404)
    assert [p["slug"] for p in listing.json()] == ["alfred"]
    assert audits == ["create", "delete"]


async def test_every_library_route_is_served_without_any_flag(monkeypatch) -> None:
    """The capability is built in: no feature flag darkens a route."""
    async with health_database() as sessions:
        world = await World().build(sessions)
        monkeypatch.setattr(settings, "enabled_features", "")
        async with _client(sessions, world.admin) as client:
            for method, url in [
                ("GET", "/api/v1/health/pets"),
                ("GET", "/api/v1/health/pets/alfred/spritesheet"),
                ("POST", "/api/v1/health/pets"),
                ("DELETE", "/api/v1/health/pets/owl"),
            ]:
                assert (await client.request(method, url)).status_code != 403, url
