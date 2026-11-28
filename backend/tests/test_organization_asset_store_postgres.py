"""The canonical asset store on PostgreSQL (#1178, S2).

Built from the profile on the first read, rebuilt when a newer profile is
saved, one asset per real thing with its source records and software,
paginated, "assets of a software", and organizations that never cross.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import app.models  # noqa: F401 - registers every mapper on Base.metadata
from app.models.organization import Organization
from app.models.organization_asset import (
    OrganizationAsset,
    OrganizationAssetSoftware,
)
from app.models.organization_context import OrganizationContextVersion
from app.services import organization_asset_store as store
from tests.scratch_database import ALL_TABLES, scratch_database

pytestmark = pytest.mark.integration


@asynccontextmanager
async def _database() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    async with scratch_database("orgassets", tables=ALL_TABLES, extensions=("vector",)) as db:
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
    # Since #1543 a newer profile is applied as row writes: the removed record's asset goes, the
    # unchanged one is not rewritten (it keeps the version it was built at).
    assert [(r.name, r.profile_version) for r in rows] == [("laptop-01", 1)]
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


async def test_the_list_filters_on_fields_and_sources_and_the_facets_name_their_values() -> None:
    async with _database() as sessions, sessions() as session:
        org = await _org(session, "A", primary=True)
        await _save(session, org, 1, [LAPTOP_INTUNE, LAPTOP_OPENAEV, {**SERVER, "criticality": "low"}])
        await store.ensure_built(session)
        await session.commit()
        high = await store.list_assets(session, org, filters={"criticality": ["high"]})
        intune = await store.list_assets(session, org, filters={"source": ["intune"]})
        both = await store.list_assets(session, org, filters={"criticality": ["high", "low"], "source": []})
        none = await store.list_assets(session, org, filters={"category": ["database"]})
        facets = await store.asset_facets(session, org)
        narrowed = await store.asset_facets(session, org, filters={"criticality": ["high"], "source": ["intune"]})
    assert [a["name"].casefold() for a in high.items] == ["laptop-01"]
    assert all(r["id"] for r in high.items[0]["records"])
    assert [{k: r[k] for k in ("source", "external_id")} for r in high.items[0]["records"]] == [
        {"source": "intune", "external_id": "d-1"},
        {"source": "openaev", "external_id": "a-9"},
    ]
    assert [a["name"].casefold() for a in intune.items] == ["laptop-01"]
    assert both.total == 2
    assert none.total == 0
    assert facets["criticality"] == [{"value": "high", "count": 1}, {"value": "low", "count": 1}]
    assert facets["category"] == [{"value": "host", "count": 2}]
    # laptop-01 holds an intune and an openaev record, web-02 an openaev one: a source counts assets.
    assert facets["source"] == [{"value": "intune", "count": 1}, {"value": "openaev", "count": 2}]
    # A facet's counts apply the other facets picked, never its own (OR within, AND across, #1501).
    assert narrowed["criticality"] == [{"value": "high", "count": 1}]
    assert narrowed["source"] == [{"value": "intune", "count": 1}, {"value": "openaev", "count": 1}]
    assert narrowed["category"] == [{"value": "host", "count": 1}]


async def test_a_lookup_finds_an_asset_by_any_identifier_a_platform_holds_and_never_another_organization() -> None:
    host = {
        "name": "db-01",
        "category": "host",
        "source": "intune",
        "external_id": "d-7",
        "fqdn": "db-01.corp.example",
        "ip": ["10.0.0.5"],
        "mac": "AA-BB-CC-DD-EE-01",
    }
    async with _database() as sessions, sessions() as session:
        org = await _org(session, "A", primary=True)
        other = await _org(session, "B", primary=False)
        await _save(session, org, 1, [host, SERVER])
        await _save(session, other, 1, [{**SERVER, "name": "web-99", "external_id": "z-1"}])
        await store.ensure_built(session)
        await store.ensure_built(session, other)
        await session.commit()
        found = await store.find_assets_by_identifier(
            session,
            org,
            ["DB-01", "db-01.corp.example.", "10.0.0.5", "aa:bb:cc:dd:ee:01", "web-02", "web-99", "nothing", ""],
        )
    assert sorted(found) == ["10.0.0.5", "DB-01", "aa:bb:cc:dd:ee:01", "db-01.corp.example.", "web-02"]
    assert {m["name"] for k, m in found.items() if k != "web-02"} == {"db-01"}
    assert found["web-02"]["name"] == "web-02"
    assert found["DB-01"]["matches"] == 1
    assert set(found["DB-01"]) == {
        "id",
        "name",
        "category",
        "criticality",
        "internet_facing",
        "owner",
        "matches",
        "match_kind",
        "ambiguous",
    }
    assert found["db-01.corp.example."]["match_kind"] == "fqdn"
    assert found["DB-01"]["match_kind"] == "name"
    assert found["10.0.0.5"]["match_kind"] == "ip"


async def test_a_lookup_never_reads_a_free_value_as_a_serial() -> None:
    async with _database() as sessions, sessions() as session:
        org = await _org(session, "A", primary=True)
        await _save(session, org, 1, [{**SERVER, "name": "srv", "serial": "WEB02"}])
        await store.ensure_built(session)
        await session.commit()
        found = await store.find_assets_by_identifier(session, org, ["web-02"])
    assert found == {}


async def test_a_lookup_finds_a_web_application_by_name_and_by_its_source_reference() -> None:
    web_app = {
        "name": "00e80886.octi.example.io",
        "category": "web_application",
        "subcategory": "website",
        "source": "openaev",
        "external_id": "oaev-web-1",
    }
    async with _database() as sessions, sessions() as session:
        org = await _org(session, "A", primary=True)
        await _save(session, org, 1, [web_app, SERVER])
        await store.ensure_built(session)
        await session.commit()
        found = await store.find_assets_by_identifier(
            session,
            org,
            ["00e80886.octi.example.io", "openaev:oaev-web-1", "openaev:a-10", "openaev:missing", "intune:oaev-web-1"],
        )
    assert sorted(found) == ["00e80886.octi.example.io", "openaev:a-10", "openaev:oaev-web-1"]
    assert found["openaev:oaev-web-1"]["name"] == "00e80886.octi.example.io"
    assert found["openaev:oaev-web-1"]["matches"] == 1
    assert found["00e80886.octi.example.io"]["id"] == found["openaev:oaev-web-1"]["id"]
    assert found["openaev:a-10"]["name"] == "web-02"


async def test_a_split_keeps_two_records_apart_across_rebuilds_and_its_removal_merges_them_again() -> None:
    async with _database() as sessions, sessions() as session:
        org = await _org(session, "A", primary=True)
        await _save(session, org, 1, [LAPTOP_INTUNE, LAPTOP_OPENAEV])
        await store.ensure_built(session)
        records = [{"source": "intune", "external_id": "d-1"}, {"source": "openaev", "external_id": "a-9"}]
        row = await store.record_decision(session, org, kind="split", records=records, reason="two laptops")
        await session.commit()
        split = (await store.list_assets(session, org)).total
        await _save(session, org, 2, [LAPTOP_INTUNE, {**LAPTOP_OPENAEV, "criticality": "low"}])
        await store.ensure_built(session)
        after_rebuild = (await store.list_assets(session, org)).total
        with pytest.raises(store.DecisionError, match="already recorded"):
            await store.record_decision(session, org, kind="split", records=list(reversed(records)))
        listed = await store.list_decisions(session, org)
        assert await store.remove_decision(session, org, row.id) is not None
        await session.commit()
        merged = (await store.list_assets(session, org)).total
    assert (split, after_rebuild, merged) == (2, 2, 1)
    assert [d["kind"] for d in listed] == ["split"] and listed[0]["reason"] == "two laptops"


async def test_a_merge_joins_records_no_key_links_and_an_unknown_record_is_refused() -> None:
    async with _database() as sessions, sessions() as session:
        org = await _org(session, "A", primary=True)
        await _save(session, org, 1, [LAPTOP_INTUNE, SERVER])
        await store.ensure_built(session)
        with pytest.raises(store.DecisionError, match="unknown record"):
            await store.record_decision(
                session,
                org,
                kind="merge",
                records=[{"source": "intune", "external_id": "d-1"}, {"source": "x", "external_id": "nope"}],
            )
        await store.record_decision(
            session,
            org,
            kind="merge",
            records=[{"source": "intune", "external_id": "d-1"}, {"source": "openaev", "external_id": "a-10"}],
        )
        await session.commit()
        page = await store.list_assets(session, org)
        detail = await store.get_asset(session, org, uuid.UUID(page.items[0]["id"]))
    assert page.total == 1
    assert detail is not None and "administrator" in detail["explanation"]


async def test_saving_a_profile_version_refreshes_the_store_in_the_same_transaction() -> None:
    from app.services import organization_context as org_context

    async with _database() as sessions, sessions() as session:
        org = await _org(session, "A", primary=True)
        profile = org_context.parse_profile(
            {"assets_exposure": [{k: v for k, v in LAPTOP_INTUNE.items() if k not in ("serial", "software")}]}
        )
        saved = await org_context.save_version(session, profile, source="api", author_user_id=None, organization_id=org)
        built = (
            await session.execute(
                select(func.max(OrganizationAsset.profile_version)).where(OrganizationAsset.organization_id == org)
            )
        ).scalar()
    assert built == saved.version


async def test_no_profile_builds_nothing() -> None:
    async with _database() as sessions, sessions() as session:
        await _org(session, "A", primary=True)
        assert await store.ensure_built(session) is None
