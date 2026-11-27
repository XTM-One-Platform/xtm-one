"""The inventory query surface agents read, on PostgreSQL, at scale (#2443, S1).

A fixture of 10,000 assets, 2,000 software (with versions and 20,000
installations) and SBOM dependency edges is written straight into the store
tables; every function answers one page with a bounded number of SQL
statements, whatever the inventory size, and never loads the whole inventory.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from typing import Any

import pytest
from sqlalchemy import event, insert, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import app.models  # noqa: F401 - registers every mapper on Base.metadata
from app.models.organization import Organization
from app.models.organization_asset import (
    OrganizationAsset,
    OrganizationAssetSoftware,
    OrganizationSoftware,
    OrganizationSoftwareDependency,
    OrganizationSoftwareVersion,
)
from app.models.organization_asset_record import (
    OrganizationAssetGroup,
    OrganizationAssetGroupMember,
    OrganizationAssetRecord,
    OrganizationPrimaryAssetLink,
)
from app.services import organization_inventory_query as q
from app.services.organization_inventory_query import group_source_id  # the real one: conftest stubs q's
from tests.scratch_database import ALL_TABLES, scratch_database

pytestmark = pytest.mark.integration

ASSETS = 10_000
SOFTWARE = 2_000
#: The most SQL statements one query function may run, whatever the inventory size.
MAX_STATEMENTS = 8


@asynccontextmanager
async def _database() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    async with scratch_database("orginventoryq", tables=ALL_TABLES, extensions=("vector",)) as db:
        yield db.sessions


@contextmanager
def _statements(session: AsyncSession) -> Iterator[list[str]]:
    seen: list[str] = []

    def _record(_conn: Any, _cursor: Any, statement: str, *_args: Any) -> None:
        seen.append(statement)

    engine = session.bind.sync_engine
    event.listen(engine, "before_cursor_execute", _record)
    try:
        yield seen
    finally:
        event.remove(engine, "before_cursor_execute", _record)


def _field(value: str) -> dict[str, Any]:
    return {"value": value, "source": "fixture", "external_id": "", "candidates": []}


async def _inventory(session: AsyncSession) -> dict[str, Any]:
    """10k assets, 2k software with 3 versions each, 20k installations, an SBOM chain, a group, a primary asset."""
    org = Organization(id=uuid.uuid4(), name="A", role="primary", relationship=None)
    session.add(org)
    await session.flush()
    categories = ("host", "application", "network_device", "cloud_resource")
    criticalities = ("critical", "high", "medium", "low")
    asset_ids = [uuid.uuid4() for _ in range(ASSETS)]
    await session.execute(
        insert(OrganizationAsset),
        [
            {
                "id": asset_id,
                "organization_id": org.id,
                "canonical_key": f"k{i:05d}",
                "name": f"host-{i:05d}",
                "name_key": f"host-{i:05d}",
                "fields": {
                    "category": _field(categories[i % 4]),
                    "criticality": _field(criticalities[i % 4]),
                    "os_family": _field("windows" if i % 2 else "linux"),
                    "internet_facing": _field("yes" if i % 10 == 0 else "no"),
                },
            }
            for i, asset_id in enumerate(asset_ids)
        ],
    )
    software_ids = [uuid.uuid4() for _ in range(SOFTWARE)]
    await session.execute(
        insert(OrganizationSoftware),
        [
            {
                "id": sid,
                "organization_id": org.id,
                "vendor": f"vendor{j % 50}",
                "vendor_key": f"vendor{j % 50}",
                "product": f"product{j:04d}",
                "product_key": f"product{j:04d}",
                "name": f"vendor{j % 50} product{j:04d}",
                "name_key": f"vendor{j % 50} product{j:04d}",
                "software_type": "library" if j % 3 == 0 else "application",
            }
            for j, sid in enumerate(software_ids)
        ],
    )
    versions: dict[tuple[int, str], uuid.UUID] = {}
    rows = []
    for j, sid in enumerate(software_ids):
        for value in ("1.9.0", "1.10.2", "2.0.0"):
            vid = uuid.uuid4()
            versions[(j, value)] = vid
            rows.append({"id": vid, "organization_id": org.id, "software_id": sid, "version": value})
    await session.execute(insert(OrganizationSoftwareVersion), rows)
    installs = []
    for i, asset_id in enumerate(asset_ids):
        for k in (0, 1):
            j = (i + k * 7) % SOFTWARE
            value = ("1.9.0", "1.10.2", "2.0.0")[(i + k) % 3]
            installs.append(
                {
                    "organization_id": org.id,
                    "asset_id": asset_id,
                    "vendor": f"vendor{j % 50}",
                    "vendor_key": f"vendor{j % 50}",
                    "product": f"product{j:04d}",
                    "product_key": f"product{j:04d}",
                    "version": value,
                    "software_version_id": versions[(j, value)],
                }
            )
    await session.execute(insert(OrganizationAssetSoftware), installs)
    # SBOM: product0001 (every version) depends on product0000 2.0.0.
    await session.execute(
        insert(OrganizationSoftwareDependency),
        [
            {
                "organization_id": org.id,
                "version_id": versions[(1, value)],
                "depends_on_version_id": versions[(0, "2.0.0")],
                "kind": "depends_on",
            }
            for value in ("1.9.0", "1.10.2", "2.0.0")
        ],
    )
    record = OrganizationAssetRecord(
        organization_id=org.id,
        source="manual",
        external_id="r-1",
        asset_id=asset_ids[0],
        name="host-00000",
        name_key="host-00000",
    )
    group = OrganizationAssetGroup(
        organization_id=org.id, source="manual", external_id="g-1", name="DMZ", name_key="dmz"
    )
    session.add_all([record, group])
    await session.flush()
    session.add(OrganizationAssetGroupMember(group_id=group.id, record_id=record.id))
    session.add(OrganizationPrimaryAssetLink(organization_id=org.id, primary_asset_key="payments", record_id=record.id))
    await session.commit()
    return {"org": org.id, "assets": asset_ids, "group": group.id}


async def test_the_inventory_is_queried_by_pages_with_bounded_statements() -> None:
    async with _database() as sessions, sessions() as session:
        world = await _inventory(session)
        org = world["org"]

        with _statements(session) as seen:
            found = await q.search_assets(session, org, category=["host"], criticality=["critical"], limit=20)
        assert found["total"] == ASSETS // 4 and len(found["items"]) == 20 and found["next_offset"] == 20
        assert len(seen) <= MAX_STATEMENTS, f"{len(seen)} statements for one asset page"

        with _statements(session) as seen:
            counts = await q.aggregate_inventory(session, org, entity="assets", group_by="os_family")
        assert {b["value"]: b["count"] for b in counts["buckets"]} == {"linux": 5000, "windows": 5000}
        assert len(seen) == 1

        with _statements(session) as seen:
            software = await q.search_software(session, org, query="product019", limit=5)
        assert software["total"] == 10 and len(software["items"]) == 5
        assert len(seen) <= MAX_STATEMENTS

        with _statements(session) as seen:
            groups = await q.list_asset_groups(session, org)
            primary = await q.list_primary_assets(session, org)
        assert groups["items"] == [
            {"id": str(world["group"]), "name": "DMZ", "source": "manual", "source_id": "g-1", "members": 1}
        ]
        assert primary["items"] == [{"primary_asset": "payments", "records": 1, "groups": 0}]
        assert len(seen) == 4

        sizes = await q.inventory_sizes(session, org)
        assert sizes == {"assets": ASSETS, "software": SOFTWARE, "asset_groups": 1, "primary_assets": 1}


async def test_find_assets_running_compares_versions_semantically_and_walks_dependencies() -> None:
    async with _database() as sessions, sessions() as session:
        world = await _inventory(session)
        org = world["org"]

        with _statements(session) as seen:
            ranged = await q.find_assets_running(
                session, org, product="product0000", min_version="1.10", max_version="2.0", limit=200
            )
        # 1.10.2 is in [1.10, 2.0); 1.9.0 is not (a text compare would say "1.9.0" > "1.10").
        assert ranged["versions_matched"] == 1
        assert all(run["version"] == "1.10.2" for item in ranged["items"] for run in item["runs"])
        assert len(seen) <= MAX_STATEMENTS

        direct = await q.find_assets_running(
            session, org, product="product0000", version="2.0.0", through_dependencies=False, limit=200
        )
        through = await q.find_assets_running(session, org, product="product0000", version="2.0.0", limit=200)
        assert through["total"] > direct["total"]
        vias = {run["via"] for item in through["items"] for run in item["runs"] if run["via"]}
        assert vias and all(v.startswith("product0001 ") for v in vias)

        with pytest.raises(q.QueryError):
            await q.find_assets_running(session, org)


async def test_get_and_lookup_answer_one_asset_by_id_prefix_or_name() -> None:
    async with _database() as sessions, sessions() as session:
        world = await _inventory(session)
        org, first = world["org"], world["assets"][0]

        by_prefix = await q.get_asset(session, org, str(first)[:8], software_limit=1)
        assert by_prefix is not None and by_prefix["id"] == str(first)
        assert by_prefix["groups"][0]["name"] == "DMZ"
        assert [p["primary_asset"] for p in by_prefix["primary_assets"]] == ["payments"]
        assert await q.get_asset(session, org, "nothing-like-this") is None

        looked = await q.lookup_assets(session, org, ["host-00042", "unknown-host"])
        assert looked["found"]["host-00042"]["name"] == "host-00042"
        assert looked["not_found"] == ["unknown-host"]

        detail = await q.get_software(session, org, "vendor0 product0000", installations_limit=3)
        assert detail is not None and len(detail["installations"]["items"]) == 3


async def test_group_by_source_totals_the_distinct_assets_not_the_overlapping_buckets() -> None:
    async with _database() as sessions, sessions() as session:
        org = Organization(id=uuid.uuid4(), name="B", role="primary", relationship=None)
        session.add(org)
        await session.flush()
        sources = {"a": ("intune", "openaev"), "b": ("openaev",), "c": ("intune",)}
        for name, origins in sources.items():
            asset = OrganizationAsset(
                id=uuid.uuid4(),
                organization_id=org.id,
                canonical_key=name,
                name=name,
                name_key=name,
                fields={"os_family": _field("linux")},
            )
            session.add(asset)
            await session.flush()
            for origin in origins:
                session.add(
                    OrganizationAssetRecord(
                        organization_id=org.id,
                        source=origin,
                        external_id=f"{origin}-{name}",
                        asset_id=asset.id,
                        name=name,
                        name_key=name,
                    )
                )
        await session.commit()

        by_source = await q.aggregate_inventory(session, org.id, group_by="source", os_family=["linux"])
        assert {b["value"]: b["count"] for b in by_source["buckets"]} == {"intune": 2, "openaev": 2}
        assert by_source["total"] == 3 and by_source["bucket_sum"] == 4 and by_source["multi_valued"] is True

        by_os = await q.aggregate_inventory(session, org.id, group_by="os_family", os_family=["linux"])
        found = await q.search_assets(session, org.id, os_family=["linux"])
        assert by_os["total"] == found["total"] == 3
        assert "bucket_sum" not in by_os and "multi_valued" not in by_os


async def test_a_source_synced_group_answers_the_id_its_source_module_takes() -> None:
    """#2714: an OpenAEV group's row names OpenAEV's id; the inventory id maps to it."""
    async with _database() as sessions, sessions() as session:
        world = await _inventory(session)
        org, first = world["org"], world["assets"][0]
        aev_id = "de799619-00cb-409c-8f29-8013e50fdb08"
        group = OrganizationAssetGroup(
            organization_id=org,
            source="openaev",
            external_id=aev_id,
            name="All Linux x86_64",
            name_key="all linux x86_64",
        )
        session.add(group)
        await session.flush()
        record = (
            await session.execute(select(OrganizationAssetRecord.id).where(OrganizationAssetRecord.asset_id == first))
        ).scalar_one()
        session.add(OrganizationAssetGroupMember(group_id=group.id, record_id=record))
        await session.commit()

        listed = await q.list_asset_groups(session, org, query="linux")
        assert listed["items"] == [
            {"id": str(group.id), "name": "All Linux x86_64", "source": "openaev", "source_id": aev_id, "members": 1}
        ]
        detail = await q.get_asset(session, org, str(first))
        assert detail is not None
        assert {g["source_id"] for g in detail["groups"]} == {"g-1", aev_id}

        assert await group_source_id(session, str(group.id), source="openaev", organization_id=org) == aev_id
        # Another source's group, an unknown id or a non-id never map.
        assert await group_source_id(session, str(world["group"]), source="openaev", organization_id=org) is None
        assert await group_source_id(session, str(uuid.uuid4()), source="openaev", organization_id=org) is None
        assert await group_source_id(session, "not-an-id", source="openaev", organization_id=org) is None


def test_version_in_range_is_semantic_and_says_when_it_cannot_compare() -> None:
    assert q.version_in_range("1.10.2", min_version="1.9", max_version="2.0") is True
    assert q.version_in_range("2.0", max_version="2.0") is False
    assert q.version_in_range("2.0", max_version="2.0", max_inclusive=True) is True
    assert q.version_in_range("unknown", min_version="1.0") is None
