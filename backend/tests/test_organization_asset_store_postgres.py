"""The canonical asset store on PostgreSQL (#1178, S2).

Built from the profile on the first read, rebuilt when a newer profile is
saved, one asset per real thing with its source records and software,
paginated, "assets of a software", and organizations that never cross.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, cast

import pytest
from sqlalchemy import Table, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import app.models  # noqa: F401 - registers every mapper on Base.metadata
from app.models.organization import Organization
from app.models.organization_asset import OrganizationAsset, OrganizationAssetSoftware, OrganizationAssetSource
from app.models.organization_context import OrganizationContextVersion
from app.services import organization_asset_store as store
from tests.scratch_database import scratch_database, with_referenced_tables

pytestmark = pytest.mark.integration


@asynccontextmanager
async def _database() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    models: list[Any] = [
        OrganizationAsset,
        OrganizationAssetSource,
        OrganizationAssetSoftware,
        OrganizationContextVersion,
    ]
    tables = with_referenced_tables([cast(Table, m.__table__) for m in models])
    async with scratch_database("orgassets", tables=tables) as db:
        yield db.sessions


async def _org(session: AsyncSession, name: str, *, primary: bool) -> uuid.UUID:
    org = Organization(
        id=uuid.uuid4(),
        name=name,
        role="primary" if primary else "tracked",
        relationship=None if primary else "customer",
    )
    session.add(org)
    await session.flush()
    return org.id


async def _save(session: AsyncSession, org: uuid.UUID, version: int, assets: list[dict[str, Any]]) -> None:
    session.add(
        OrganizationContextVersion(
            organization_id=org, version=version, profile={"assets_exposure": assets}, source="api"
        )
    )
    await session.flush()


LAPTOP_INTUNE = {
    "name": "laptop-01",
    "category": "host",
    "source": "intune",
    "external_id": "d-1",
    "serial": "SN-778",
    "software": [{"vendor": "F5", "product": "nginx", "version": "1.27.1"}, {"product": "curl"}],
}
LAPTOP_OPENAEV = {
    "name": "LAPTOP-01",
    "category": "host",
    "source": "openaev",
    "external_id": "a-9",
    "serial": "sn-778",
    "criticality": "high",
}
SERVER = {
    "name": "web-02",
    "category": "host",
    "source": "openaev",
    "external_id": "a-10",
    "serial": "SN-900",
    "software": [{"vendor": "f5", "product": "NGINX", "version": "1.25.3"}],
}


async def test_the_first_read_builds_one_asset_per_real_thing_with_its_records_and_software() -> None:
    async with _database() as sessions, sessions() as session:
        org = await _org(session, "A", primary=True)
        await _save(session, org, 1, [LAPTOP_INTUNE, LAPTOP_OPENAEV, SERVER])
        assert await store.ensure_built(session) == org
        await session.commit()
        page = await store.list_assets(session, org)
        laptop = next(a for a in page.items if a["sources"] == ["intune", "openaev"])
        detail = await store.get_asset(session, org, uuid.UUID(laptop["id"]))
    assert page.total == 2
    assert laptop["criticality"] == "high"
    assert laptop["software_count"] == 2
    assert detail is not None
    assert detail["explanation"] == "merged on serial"
    assert [r["source"] for r in detail["records"]] == ["intune", "openaev"]
    versions = {s["product"].casefold(): s["version"] for s in detail["software"]["items"]}
    assert versions == {"curl": "unknown", "nginx": "1.27.1"}


async def test_assets_of_a_software_answer_each_asset_with_the_version_it_runs() -> None:
    async with _database() as sessions, sessions() as session:
        org = await _org(session, "A", primary=True)
        await _save(session, org, 1, [LAPTOP_INTUNE, LAPTOP_OPENAEV, SERVER])
        await store.ensure_built(session)
        await session.commit()
        every = await store.assets_of_software(session, org, product="nginx")
        one = await store.assets_of_software(session, org, product="NGINX", vendor="f5", version="1.25.3")
    assert every.total == 2
    assert sorted(i["version"] for i in every.items) == ["1.25.3", "1.27.1"]
    assert [i["asset_name"] for i in one.items] == ["web-02"]


async def test_a_newer_profile_rebuilds_the_store_and_an_unchanged_one_does_not() -> None:
    async with _database() as sessions, sessions() as session:
        org = await _org(session, "A", primary=True)
        await _save(session, org, 1, [LAPTOP_INTUNE, SERVER])
        await store.ensure_built(session)
        await session.commit()
        first_ids = set((await session.execute(select(OrganizationAsset.id))).scalars().all())
        await store.ensure_built(session)
        await session.commit()
        same_ids = set((await session.execute(select(OrganizationAsset.id))).scalars().all())
        await _save(session, org, 2, [LAPTOP_INTUNE])
        await store.ensure_built(session)
        await session.commit()
        rows = (await session.execute(select(OrganizationAsset))).scalars().all()
        software = (await session.execute(select(func.count()).select_from(OrganizationAssetSoftware))).scalar()
    assert same_ids == first_ids
    assert [(r.name, r.profile_version) for r in rows] == [("laptop-01", 2)]
    assert software == 2


async def test_an_unchanged_asset_keeps_its_id_when_a_newer_profile_rebuilds_the_store() -> None:
    async with _database() as sessions, sessions() as session:
        org = await _org(session, "A", primary=True)
        await _save(session, org, 1, [LAPTOP_INTUNE, SERVER])
        await store.ensure_built(session)
        await session.commit()
        before = {a["name"]: a["id"] for a in (await store.list_assets(session, org)).items}
        await _save(session, org, 2, [LAPTOP_INTUNE, {**SERVER, "criticality": "high"}])
        await store.ensure_built(session)
        await session.commit()
        after = {a["name"]: a["id"] for a in (await store.list_assets(session, org)).items}
        detail = await store.get_asset(session, org, uuid.UUID(before["web-02"]))
    assert after == before
    assert detail is not None
    assert detail["name"] == "web-02"


async def test_pagination_and_search_are_by_name_and_organizations_never_cross() -> None:
    async with _database() as sessions, sessions() as session:
        org = await _org(session, "A", primary=True)
        other = await _org(session, "B", primary=False)
        await _save(session, org, 1, [LAPTOP_INTUNE, SERVER])
        await _save(session, other, 1, [{**SERVER, "name": "web-99"}])
        await store.ensure_built(session)
        await store.ensure_built(session, other)
        await session.commit()
        first = await store.list_assets(session, org, limit=1)
        second = await store.list_assets(session, org, offset=1, limit=1)
        found = await store.list_assets(session, org, q="WEB")
        theirs = await store.list_assets(session, other)
        foreign = await store.get_asset(session, org, uuid.UUID(theirs.items[0]["id"]))
    assert (first.total, [a["name"] for a in first.items + second.items]) == (2, ["laptop-01", "web-02"])
    assert [a["name"] for a in found.items] == ["web-02"]
    assert [a["name"] for a in theirs.items] == ["web-99"]
    assert foreign is None


async def test_no_profile_builds_nothing() -> None:
    async with _database() as sessions, sessions() as session:
        await _org(session, "A", primary=True)
        assert await store.ensure_built(session) is None
