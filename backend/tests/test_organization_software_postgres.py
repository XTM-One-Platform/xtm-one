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


async def test_the_asset_page_names_the_software_of_every_row() -> None:
    async with _database() as sessions, sessions() as session:
        org = await _setup(session, [HOST])
        chrome = (await sw.list_software(session, org)).items[0]
        asset_id = (await session.execute(select(OrganizationAsset.id))).scalar_one()
        detail = await store.get_asset(session, org, asset_id)
    assert detail is not None
    assert {s["software_id"] for s in detail["software"]["items"]} == {chrome["id"]}


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


# ── Slice 1b: an administrator's writes ───────────────────────────────────────────

PORTAL_CPE = "cpe:2.3:a:acme:portal:*:*:*:*:*:*:*:*"


async def test_a_hand_typed_software_takes_its_cpe_override_and_survives_a_rebuild() -> None:
    async with _database() as sessions, sessions() as session:
        org = await _setup(session, [HOST])
        made = await sw.create_software(
            session, org, vendor="Acme", product="Portal", software_type="saas", cpe=PORTAL_CPE, name="Portal"
        )
        await session.commit()
        await _profile(session, org, 2, [{**HOST, "name": "web-02b"}])
        kept = await sw.get_software(session, org, uuid.UUID(made["id"]))
        with pytest.raises(sw.SoftwareError) as twice:
            await sw.create_software(session, org, vendor="acme", product="PORTAL")
        with pytest.raises(sw.SoftwareError) as bad_cpe:
            await sw.create_software(session, org, vendor="Acme", product="Other", cpe="acme portal")
    assert made["origin"] == "manual" and made["editable"] and made["type"] == "saas"
    assert made["cpe"] == PORTAL_CPE and made["cpe_state"] == "override"
    assert kept is not None and kept["name"] == "Portal" and kept["cpe"] == PORTAL_CPE
    assert twice.value.status == 409 and bad_cpe.value.status == 422


async def test_a_filter_with_several_values_answers_any_of_them() -> None:
    async with _database() as sessions, sessions() as session:
        org = await _setup(session, [HOST])
        await sw.create_software(session, org, vendor="Acme", product="Portal", software_type="saas", cpe=PORTAL_CPE)
        await session.commit()
        both = await sw.list_software(session, org, origin=["manual", "synced"])
        hand = await sw.list_software(session, org, origin=["manual"], cpe_state=["override", "reported"])
        none = await sw.list_software(session, org, software_type=["firmware", "library"])
    assert both.total == 2
    assert [s["product"] for s in hand.items] == ["Portal"]
    assert none.total == 0


async def test_a_synced_software_is_read_only_but_for_its_cpe_override() -> None:
    async with _database() as sessions, sessions() as session:
        org = await _setup(session, [HOST])
        chrome = uuid.UUID((await sw.list_software(session, org)).items[0]["id"])
        with pytest.raises(sw.SoftwareError) as rename:
            await sw.update_software(session, org, chrome, {"name": "Browser"})
        with pytest.raises(sw.SoftwareError) as delete:
            await sw.delete_software(session, org, chrome)
        with pytest.raises(sw.SoftwareError) as attach:
            await sw.attach_software(session, org, chrome, uuid.uuid4())
        cpe = "cpe:2.3:a:google:chrome:*:*:*:*:*:*:*:*"
        after, changed = await sw.update_software(session, org, chrome, {"cpe_override": cpe})
        await session.commit()
        await _profile(session, org, 2, [{**HOST, "name": "web-02b"}])
        rebuilt = await sw.get_software(session, org, chrome)
    assert rename.value.status == 422 and delete.value.status == 422 and attach.value.status == 422
    assert changed == {"cpe_override": ["", cpe]} and after["cpe_state"] == "override"
    assert rebuilt is not None and rebuilt["cpe"] == cpe and rebuilt["cpe_state"] == "override"


async def test_a_hand_attachment_follows_the_asset_through_rebuilds_and_detaches() -> None:
    async with _database() as sessions, sessions() as session:
        org = await _setup(session, [HOST])
        asset_id = (await session.execute(select(OrganizationAsset.id))).scalars().one()
        made = await sw.create_software(session, org, vendor="Acme", product="Agent", cpe="")
        soft_id = uuid.UUID(made["id"])
        first = await sw.attach_software(session, org, soft_id, asset_id, version="4.2")
        again = await sw.attach_software(session, org, soft_id, asset_id, version="4.2")
        await store.rebuild_from_profile(session, org)
        await session.commit()
        await _profile(session, org, 2, [{**HOST, "name": "web-02b"}])
        attached = await sw.get_software(session, org, soft_id)
        with pytest.raises(sw.SoftwareError) as unknown_asset:
            await sw.attach_software(session, org, soft_id, uuid.uuid4())
        removed = await sw.detach_software(session, org, soft_id, asset_id)
        await store.rebuild_from_profile(session, org)
        await session.commit()
        detached = await sw.get_software(session, org, soft_id)
        docs = (await session.execute(select(OrganizationAssetSbom))).scalars().all()
    assert first["added"] and not again["added"]
    assert attached is not None and attached["asset_count"] == 1
    assert [i["version"] for i in attached["installations"]["items"]] == ["4.2"]
    assert attached["installations"]["items"][0]["sources"] == ["manual"]
    assert unknown_asset.value.status == 404 and removed == 1
    assert detached is not None and detached["asset_count"] == 0 and detached["origin"] == "manual"
    assert docs == []


async def test_deleting_a_hand_typed_software_removes_its_attachments() -> None:
    async with _database() as sessions, sessions() as session:
        org = await _setup(session, [HOST])
        asset_id = (await session.execute(select(OrganizationAsset.id))).scalars().one()
        made = await sw.create_software(session, org, vendor="Acme", product="Agent")
        await sw.attach_software(session, org, uuid.UUID(made["id"]), asset_id)
        await store.rebuild_from_profile(session, org)
        gone = await sw.delete_software(session, org, uuid.UUID(made["id"]))
        await store.rebuild_from_profile(session, org)
        await session.commit()
        names = [s["name"] for s in (await sw.list_software(session, org)).items]
    assert gone["detached"] == 1
    assert names == ["Google Chrome"]


async def test_the_csv_export_lists_the_software_and_neutralises_formulas() -> None:
    async with _database() as sessions, sessions() as session:
        org = await _setup(session, [HOST])
        await sw.create_software(session, org, vendor="", product="=HYPERLINK(1)")
        await session.commit()
        body = await sw.software_csv(session, org)
    lines = body.splitlines()
    assert lines[0] == ",".join(sw.CSV_COLUMNS)
    assert len(lines) == 3
    assert any(line.startswith("'=HYPERLINK(1)") for line in lines[1:])
    assert any(line.startswith("Google Chrome,Google,Chrome,application") for line in lines[1:])
