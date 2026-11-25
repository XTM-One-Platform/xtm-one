"""Asset group memberships kept in sync with their source on PostgreSQL (#1556).

A group source answers each group's members; two sync runs add then remove
the source's own memberships, a hand membership and a record's own ``group``
stay, a member the inventory does not hold is counted on the group, and the
member page and the groups of a record read the rows back.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import app.models  # noqa: F401 - registers every mapper on Base.metadata
from app.models.organization import Organization
from app.models.organization_asset_record import (
    OrganizationAssetGroup,
    OrganizationAssetGroupMember,
    OrganizationAssetRecord,
)
from app.models.organization_context import OrganizationContextVersion
from app.services import organization_asset_records as records
from app.services import organization_asset_store as store
from tests.scratch_database import ALL_TABLES, scratch_database

pytestmark = pytest.mark.integration


@asynccontextmanager
async def _database() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    async with scratch_database("orggroupmembers", tables=ALL_TABLES, extensions=("vector",)) as db:
        yield db.sessions


def _asset(name: str, external_id: str, **extra: Any) -> dict[str, Any]:
    return {"name": name, "category": "host", "source": "openaev", "external_id": external_id, **extra}


async def _primary(session: AsyncSession, assets: list[dict[str, Any]]) -> uuid.UUID:
    org = Organization(id=uuid.uuid4(), name="A", role="primary", relationship=None)
    session.add(org)
    await session.flush()
    session.add(
        OrganizationContextVersion(organization_id=org.id, version=1, profile={"assets_exposure": assets}, source="api")
    )
    await session.flush()
    assert await store.ensure_built(session) == org.id
    await session.commit()
    return org.id


async def _group(session: AsyncSession, org: uuid.UUID, name: str, external_id: str) -> uuid.UUID:
    await records.write_rows(
        session,
        org,
        "asset_groups",
        [{"name": name, "source": "openaev", "external_id": external_id}],
        origin="sync:s-1",
        actor="sync:s-1",
    )
    await session.flush()
    group = next(g for g in await records.groups_out(session, org) if g["external_id"] == external_id)
    return uuid.UUID(group["id"])


async def _members(session: AsyncSession, group_id: uuid.UUID) -> dict[str, str]:
    rows = await records.group_members(session, (await _org_of(session, group_id)), group_id, limit=1000)
    return {r.name: via for r, via in rows}


async def _org_of(session: AsyncSession, group_id: uuid.UUID) -> uuid.UUID:
    return (
        await session.execute(
            select(OrganizationAssetGroup.organization_id).where(OrganizationAssetGroup.id == group_id)
        )
    ).scalar_one()


async def test_two_sync_runs_add_then_remove_the_sources_memberships_and_keep_a_hand_one() -> None:
    async with _database() as sessions, sessions() as session:
        org = await _primary(session, [_asset("srv-1", "a1"), _asset("srv-2", "a2"), _asset("srv-3", "a3")])
        group = await _group(session, org, "Servers", "g1")
        record_id = (
            await session.execute(
                select(OrganizationAssetRecord.id).where(
                    OrganizationAssetRecord.organization_id == org, OrganizationAssetRecord.external_id == "a3"
                )
            )
        ).scalar_one()
        session.add(OrganizationAssetGroupMember(group_id=group, record_id=record_id, source="manual"))
        await session.flush()

        first = await records.sync_group_members(
            session, org, "openaev", {"g1": ["a1", "a2", "missing"]}, member_source="openaev", sync_id="s-1"
        )
        await session.commit()
        assert (first.added, first.removed, first.unresolved) == (2, 0, 1)
        assert await _members(session, group) == {"srv-1": "openaev", "srv-2": "openaev", "srv-3": "manual"}

        second = await records.sync_group_members(
            session, org, "openaev", {"g1": ["a2"]}, member_source="openaev", sync_id="s-1"
        )
        await session.commit()
        assert (second.added, second.removed, second.unresolved) == (0, 1, 0)
        assert await _members(session, group) == {"srv-2": "openaev", "srv-3": "manual"}
        counts = await records.group_member_counts(session, org)
        out = next(g for g in await records.groups_out(session, org) if g["id"] == str(group))
    assert counts[group] == 2
    assert out["member_count"] == 2 and out["unresolved_members"] == 0 and out["members_synced_at"]


async def test_members_the_inventory_lacks_are_counted_on_the_group_never_dropped() -> None:
    async with _database() as sessions, sessions() as session:
        org = await _primary(session, [_asset("srv-1", "a1")])
        group = await _group(session, org, "DMZ", "g2")
        result = await records.sync_group_members(
            session, org, "openaev", {"g2": ["a1", "x1", "x2"]}, member_source="openaev"
        )
        await session.commit()
        out = next(g for g in await records.groups_out(session, org) if g["id"] == str(group))
    assert result.unresolved == 2
    assert out["member_count"] == 1 and out["unresolved_members"] == 2


async def test_a_group_absent_from_the_answer_is_left_untouched() -> None:
    async with _database() as sessions, sessions() as session:
        org = await _primary(session, [_asset("srv-1", "a1")])
        group = await _group(session, org, "Kept", "g3")
        await records.sync_group_members(session, org, "openaev", {"g3": ["a1"]}, member_source="openaev")
        await session.commit()
        result = await records.sync_group_members(session, org, "openaev", {"other": ["a1"]}, member_source="openaev")
        await session.commit()
        members = await _members(session, group)
    assert result.groups == 0
    assert members == {"srv-1": "openaev"}


async def test_the_member_page_is_a_keyset_by_name_and_a_record_lists_its_groups() -> None:
    async with _database() as sessions, sessions() as session:
        org = await _primary(session, [_asset(f"srv-{i:02d}", f"a{i}") for i in range(5)])
        group = await _group(session, org, "All", "g4")
        await records.sync_group_members(
            session, org, "openaev", {"g4": [f"a{i}" for i in range(5)]}, member_source="openaev"
        )
        await session.commit()
        first = await records.group_members(session, org, group, limit=2)
        last_record = first[-1][0]
        second = await records.group_members(session, org, group, limit=2, after=(last_record.name_key, last_record.id))
        groups = await records.record_groups(session, org, [last_record.id])
    assert [r.name for r, _v in first] == ["srv-00", "srv-01"]
    assert [r.name for r, _v in second] == ["srv-02", "srv-03"]
    assert groups == [{"id": str(group), "name": "All", "source": "openaev", "via": "openaev"}]


async def test_the_asset_list_filters_and_counts_by_group_and_an_asset_names_its_groups() -> None:
    async with _database() as sessions, sessions() as session:
        org = await _primary(session, [_asset(f"srv-{i}", f"a{i}") for i in range(4)])
        web = await _group(session, org, "Web", "g6")
        db_group = await _group(session, org, "Databases", "g7")
        await records.sync_group_members(
            session, org, "openaev", {"g6": ["a0", "a1"], "g7": ["a1", "a2", "gone"]}, member_source="openaev"
        )
        await session.commit()
        in_web = await store.list_assets(session, org, filters={"group": [str(web)]})
        in_either = await store.list_assets(session, org, filters={"group": [str(web), str(db_group)]})
        not_an_id = await store.list_assets(session, org, filters={"group": ["not-a-uuid"]})
        facets = await store.asset_facets(session, org)
        narrowed = await store.asset_facets(session, org, filters={"group": [str(web)]}, q="srv-0")
        shared = next(a for a in in_either.items if a["name"] == "srv-1")
        detail = await store.get_asset(session, org, uuid.UUID(shared["id"]))
        one = await records.group_detail(session, org, await records.get_group(session, org, db_group))
    assert [a["name"] for a in in_web.items] == ["srv-0", "srv-1"]
    assert in_either.total == 3
    assert not_an_id.total == 0
    assert facets["group"] == [
        {"value": str(db_group), "count": 2, "label": "Databases"},
        {"value": str(web), "count": 2, "label": "Web"},
    ]
    # The group facet skips its own pick and applies the search.
    assert narrowed["group"] == [{"value": str(web), "count": 1, "label": "Web"}]
    assert detail is not None
    assert [g["name"] for g in detail["groups"]] == ["Databases", "Web"]
    assert one["member_count"] == 2 and one["unresolved_members"] == 1 and one["name"] == "Databases"


async def test_the_largest_page_still_reads_the_extra_row_that_says_more_exist(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(records, "GROUP_MEMBERS_PAGE_MAX", 2)
    async with _database() as sessions, sessions() as session:
        org = await _primary(session, [_asset(f"srv-{i:02d}", f"a{i}") for i in range(5)])
        group = await _group(session, org, "Many", "g5")
        await records.sync_group_members(
            session, org, "openaev", {"g5": [f"a{i}" for i in range(5)]}, member_source="openaev"
        )
        await session.commit()
        probe = await records.group_members(session, org, group, limit=records.GROUP_MEMBERS_PAGE_MAX + 1)
        capped = await records.group_members(session, org, group, limit=100)
    assert len(probe) == 3
    assert len(capped) == 3
