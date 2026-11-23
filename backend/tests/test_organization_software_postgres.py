"""Software as an entity of the asset store, on PostgreSQL (#1495).

A rebuild creates one software per vendor and product with its versions, links
every installation to its version, keeps the software id across rebuilds,
writes the dependency edges an SBOM gives, answers the assets where a version
is present directly or through a dependency, and removes a synced software
nothing installs any more while a hand-typed one stays.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, cast

import pytest
from sqlalchemy import Table, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import app.models  # noqa: F401 - registers every mapper on Base.metadata
from app.models.organization import Organization
from app.models.organization_asset import (
    OrganizationAsset,
    OrganizationAssetDecision,
    OrganizationAssetSbom,
    OrganizationAssetSoftware,
    OrganizationAssetSource,
    OrganizationSoftware,
    OrganizationSoftwareDependency,
    OrganizationSoftwareVersion,
)
from app.models.organization_context import OrganizationContextVersion
from app.services import organization_asset_store as store
from app.services import organization_software_store as sw
from tests.scratch_database import scratch_database, with_referenced_tables

pytestmark = pytest.mark.integration

HOST = {
    "name": "web-02",
    "category": "host",
    "source": "openaev",
    "external_id": "a-10",
    "software": [
        {"vendor": "Google", "product": "Chrome", "version": "131.0"},
        {"vendor": "google", "product": "chrome", "version": ""},
    ],
}
HOST_2 = {
    "name": "web-03",
    "category": "host",
    "source": "openaev",
    "external_id": "a-11",
    "software": [{"vendor": "Google", "product": "Chrome", "version": "131.0"}],
}
APP = {
    "bomFormat": "CycloneDX",
    "metadata": {"component": {"name": "web-app", "version": "2.0"}},
    "components": [
        {
            "bom-ref": "app",
            "name": "web-app",
            "version": "2.0",
            "components": [{"bom-ref": "ossl", "name": "openssl", "version": "3.0.15"}],
        },
        {"bom-ref": "z", "name": "zlib", "version": "1.3.1"},
    ],
    "dependencies": [{"ref": "app", "dependsOn": ["ossl"]}, {"ref": "ossl", "dependsOn": ["z"]}],
}


@asynccontextmanager
async def _database() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    models: list[Any] = [
        OrganizationAsset,
        OrganizationAssetSource,
        OrganizationAssetSoftware,
        OrganizationAssetDecision,
        OrganizationAssetSbom,
        OrganizationSoftware,
        OrganizationSoftwareVersion,
        OrganizationSoftwareDependency,
        OrganizationContextVersion,
    ]
    tables = with_referenced_tables([cast(Table, m.__table__) for m in models])
    async with scratch_database("orgsoftware", tables=tables) as db:
        yield db.sessions


async def _setup(session: AsyncSession, rows: list[dict[str, Any]]) -> uuid.UUID:
    org = Organization(id=uuid.uuid4(), name="A", role="primary", relationship=None)
    session.add(org)
    await session.flush()
    session.add(
        OrganizationContextVersion(organization_id=org.id, version=1, profile={"assets_exposure": rows}, source="api")
    )
    await session.flush()
    await store.ensure_built(session)
    await session.commit()
    return org.id


async def _profile(session: AsyncSession, org: uuid.UUID, version: int, rows: list[dict[str, Any]]) -> None:
    session.add(
        OrganizationContextVersion(
            organization_id=org, version=version, profile={"assets_exposure": rows}, source="api"
        )
    )
    await session.flush()
    await store.ensure_built(session, org)
    await session.commit()


async def test_a_rebuild_creates_software_with_versions_and_links_every_installation() -> None:
    async with _database() as sessions, sessions() as session:
        org = await _setup(session, [HOST, HOST_2])
        page = await sw.list_software(session, org)
        unlinked = (
            await session.execute(
                select(OrganizationAssetSoftware.id).where(OrganizationAssetSoftware.software_version_id.is_(None))
            )
        ).all()
        chrome = page.items[0]
        detail = await sw.get_software(session, org, uuid.UUID(chrome["id"]))
    assert page.total == 1
    assert chrome["name"] == "Google Chrome" and chrome["origin"] == "synced" and not chrome["editable"]
    assert chrome["version_count"] == 2 and chrome["asset_count"] == 2 and chrome["has_unknown_version"]
    assert unlinked == []
    assert detail is not None
    assert {v["version"]: v["asset_count"] for v in detail["versions"]} == {"131.0": 2, "unknown": 1}
    assert detail["installations"]["total"] == 3


async def test_the_software_id_survives_a_rebuild_and_unused_synced_software_goes() -> None:
    async with _database() as sessions, sessions() as session:
        org = await _setup(session, [HOST])
        first = (await sw.list_software(session, org)).items[0]["id"]
        session.add(
            OrganizationSoftware(
                organization_id=org,
                vendor="Acme",
                product="Portal",
                vendor_key="acme",
                product_key="portal",
                name="Acme Portal",
                name_key="acme portal",
                origin="manual",
                software_type="saas",
            )
        )
        await session.commit()
        await _profile(session, org, 2, [{**HOST, "name": "web-02b"}])
        again = {s["name"]: s["id"] for s in (await sw.list_software(session, org)).items}
        await _profile(session, org, 3, [{**HOST, "software": []}])
        left = {s["name"]: s["origin"] for s in (await sw.list_software(session, org)).items}
    assert again["Google Chrome"] == first
    assert left == {"Acme Portal": "manual"}


async def test_an_sbom_gives_dependency_edges_and_the_assets_of_a_version_through_them() -> None:
    async with _database() as sessions, sessions() as session:
        org = await _setup(session, [{"name": "repo-1", "category": "host", "source": "openaev", "external_id": "r1"}])
        asset_id = (await session.execute(select(OrganizationAsset.id))).scalars().one()
        await store.import_sbom(session, org, asset_id, data=json.dumps(APP).encode())
        await session.commit()
        items = {s["name"]: s for s in (await sw.list_software(session, org)).items}
        ossl = await sw.get_software(session, org, uuid.UUID(items["openssl"]["id"]))
        zlib = await sw.get_software(session, org, uuid.UUID(items["zlib"]["id"]))
        assert ossl is not None and zlib is not None
        zlib_version = uuid.UUID(zlib["versions"][0]["id"])
        direct = await sw.assets_of_version(session, org, zlib_version, through_dependencies=False)
        through = await sw.assets_of_version(session, org, zlib_version)
    assert {(d["software_name"], d["kind"]) for d in ossl["depends_on"]} == {
        ("zlib", "depends_on"),
        ("web-app", "bundled_in"),
    }
    assert {(d["software_name"], d["kind"]) for d in ossl["used_by"]} == {("web-app", "depends_on")}
    assert [u["software_name"] for u in zlib["used_by"]] == ["openssl"]
    assert direct is not None and direct.total == 1 and direct.items[0]["via"] is None
    assert through is not None and through.total == 3
    assert {i["via"] for i in through.items} == {None, "openssl 3.0.15", "web-app 2.0"}
