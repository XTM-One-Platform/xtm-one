"""The asset record row writes on PostgreSQL (#1543).

One row in, one row out: a create, an update, a delete and a bulk delete write
only their rows, log each change, keep the #1367 lock and the group check, and
leave the canonical store equal to a full rebuild from the same records.
"""

from __future__ import annotations

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
from app.models.organization_asset_record import RECORD_MODELS
from app.models.organization_context import OrganizationContextVersion
from app.services import organization_asset_records as records
from app.services import organization_asset_store as store
from tests.scratch_database import scratch_database, with_referenced_tables

pytestmark = pytest.mark.integration


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
        *RECORD_MODELS,
    ]
    tables = with_referenced_tables([cast(Table, m.__table__) for m in models])
    async with scratch_database("orgrecords", tables=tables) as db:
        yield db.sessions


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


SYNCED = {"name": "laptop-01", "category": "host", "source": "intune", "external_id": "d-1", "serial": "SN-778"}
HAND = {"name": "web-02", "category": "host", "serial": "SN-900"}


async def _assets(session: AsyncSession) -> list[tuple[str, int]]:
    rows = (await session.execute(select(OrganizationAsset).order_by(OrganizationAsset.name))).scalars().all()
    return [(a.name, len(a.fields or {})) for a in rows]


async def test_a_hand_create_writes_one_record_logs_it_and_builds_its_asset() -> None:
    async with _database() as sessions, sessions() as session:
        org = await _primary(session, [SYNCED])
        record = await records.create_record(
            session, org, {"name": "db-01", "category": "host"}, origin="form", actor="form:u"
        )
        await session.commit()
        log = await records.changes(session, org, entity_id=record.id)
        names = [a for a, _n in await _assets(session)]
    assert record.external_id and record.row_version == 1
    assert [c["op"] for c in log] == ["create"]
    assert names == ["db-01", "laptop-01"]


async def test_an_update_with_a_stale_row_version_is_refused_and_a_fresh_one_moves_it() -> None:
    async with _database() as sessions, sessions() as session:
        org = await _primary(session, [SYNCED])
        record_id = (await records.create_record(session, org, HAND, origin="form", actor="form:u")).id
        await session.commit()
        updated = await records.update_record(
            session, org, record_id, {"criticality": "high"}, origin="form", actor="form:u", if_match=1
        )
        updated_version = updated.row_version
        await session.commit()
        with pytest.raises(records.StaleRowError):
            await records.update_record(
                session, org, record_id, {"criticality": "low"}, origin="form", actor="form:u", if_match=1
            )
        await session.rollback()
        log = await records.changes(session, org, entity_id=record_id)
    assert updated_version == 2
    assert log[0]["op"] == "update"
    assert log[0]["after"] == {"criticality": "high"}


async def test_a_synced_record_keeps_its_synced_fields_from_a_hand_write() -> None:
    async with _database() as sessions, sessions() as session:
        org = await _primary(session, [SYNCED])
        synced_id = (await records.asset_rows(session, org, source="intune"))[0].id
        with pytest.raises(records.RecordError, match="kept in sync"):
            await records.update_record(session, org, synced_id, {"serial": "OTHER"}, origin="form", actor="form:u")
        await session.rollback()
        kept = await records.update_record(
            session, org, synced_id, {"criticality": "very_high"}, origin="form", actor="form:u"
        )
        criticality = kept.criticality
        await session.commit()
    assert criticality == "very_high"


async def test_a_record_naming_a_missing_group_is_refused() -> None:
    async with _database() as sessions, sessions() as session:
        org = await _primary(session, [SYNCED])
        with pytest.raises(records.RecordError, match="no asset group"):
            await records.create_record(session, org, {**HAND, "group": "Nope"}, origin="form", actor="form:u")


async def test_a_bulk_delete_removes_the_rows_and_the_store_equals_a_full_rebuild() -> None:
    async with _database() as sessions, sessions() as session:
        org = await _primary(session, [SYNCED, {**HAND, "external_id": "h-1"}, {"name": "db-01", "category": "host"}])
        ids = [r.id for r in await records.asset_rows(session, org) if r.name != "laptop-01"]
        gone = await records.delete_records(session, org, ids, origin="form", actor="form:u")
        await session.commit()
        incremental = await _assets(session)
        await records.rebuild_from_records(session, org)
        await session.commit()
        rebuilt = await _assets(session)
        log = await records.changes(session, org)
    assert sorted(gone) == sorted(ids)
    assert incremental == rebuilt == [("laptop-01", incremental[0][1])]
    assert sum(1 for c in log if c["op"] == "delete") == 2


async def test_a_deleted_record_comes_back_on_restore() -> None:
    async with _database() as sessions, sessions() as session:
        org = await _primary(session, [SYNCED, {**HAND, "external_id": "h-1"}])
        hand = next(r for r in await records.asset_rows(session, org) if r.name == "web-02")
        await records.delete_records(session, org, [hand.id], origin="form", actor="form:u")
        await session.commit()
        change = (await records.changes(session, org, entity_id=hand.id))[0]
        result = await records.restore(session, org, [change["id"]], actor="restore:u")
        await session.commit()
        names = [a for a, _n in await _assets(session)]
    assert result.as_dict()["created"] == 1
    assert names == ["laptop-01", "web-02"]


async def test_deleting_a_group_keeps_its_assets_without_the_group() -> None:
    async with _database() as sessions, sessions() as session:
        org = await _primary(session, [SYNCED])
        group = await records.upsert_group(session, org, {"name": "DMZ"}, origin="form", actor="form:u")
        record = await records.create_record(session, org, {**HAND, "group": "DMZ"}, origin="form", actor="form:u")
        await session.commit()
        await records.delete_group(session, org, group.id, origin="form", actor="form:u")
        await session.commit()
        after = await records.get_record(session, org, record.id)
    assert records.record_row(after).get("group") in ("", None)


async def test_the_agent_row_path_plans_writes_and_removes_records_only_where_they_differ() -> None:
    from app.services import organization_context_rows as org_rows

    async with _database() as sessions, sessions() as session:
        org = await _primary(session, [SYNCED, HAND])
        sent = [
            {"name": "laptop-01", "category": "host", "external_id": "d-1", "serial": "SN-778"},
            {"name": "laptop-02", "category": "host", "external_id": "d-2"},
            {"name": "web-02", "category": "host", "external_id": "d-3"},
        ]
        refs = [("intune", r["external_id"]) for r in sent]
        stored = await records.rows_by_ref(session, org, "assets", refs)
        names = await records.name_owners(session, org, "assets", [r["name"] for r in sent], source="intune")
        for row in stored.values():
            names.pop(org_rows.name_key(row.get("name")), None)
        plan = org_rows.plan_record_upsert(
            "assets", "intune", sent, validate=records.validate_asset, stored=stored, names=names
        )
        # web-02 is a hand record of another source: a second record of the same thing, added beside it.
        assert (plan.unchanged, plan.added) == (["laptop-01"], ["laptop-02", "web-02"])
        result = await records.write_rows(session, org, "assets", plan.writes, origin="agent:intune", actor="agent:A")
        await session.commit()
        stored = await records.rows_by_ref(session, org, "assets", [("intune", "d-2"), ("intune", "x")])
        gone = org_rows.plan_record_remove("assets", "intune", ["d-2", "x"], stored=stored)
        removed = await records.remove_rows(
            session, org, "assets", gone.remove_refs, origin="agent:intune", actor="agent:A"
        )
        await session.commit()
        log = await records.changes(session, org, entity_id=removed[0])
        left = await records.rows_by_ref(session, org, "assets", refs)
    assert result.as_dict()["created"] == 2
    assert [name for name, _ in gone.refused] == ["x"]
    assert [c["op"] for c in log][:1] == ["delete"] and log[0]["actor"] == "agent:A"
    assert sorted(left) == [("intune", "d-1"), ("intune", "d-3")]


async def test_the_agent_row_path_writes_and_removes_groups() -> None:
    from app.services import organization_context_rows as org_rows

    async with _database() as sessions, sessions() as session:
        org = await _primary(session, [SYNCED])
        sent = [{"name": "Paris office", "external_id": "g-1"}]
        plan = org_rows.plan_record_upsert(
            "asset_groups", "intune", sent, validate=records.validate_group, stored={}, names={}
        )
        await records.write_rows(session, org, "asset_groups", plan.writes, origin="agent:intune", actor="agent:A")
        await session.commit()
        stored = await records.rows_by_ref(session, org, "asset_groups", [("intune", "g-1")])
        removed = await records.remove_rows(
            session, org, "asset_groups", [("intune", "g-1")], origin="agent:intune", actor="agent:A"
        )
        await session.commit()
        after = await records.rows_by_ref(session, org, "asset_groups", [("intune", "g-1")])
    assert stored[("intune", "g-1")]["name"] == "Paris office"
    assert len(removed) == 1 and after == {}


async def test_a_sync_writes_rows_and_removes_its_gone_rows_without_a_profile_version(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services import organization_context_import as imp

    def read(*rows: tuple[str, str]) -> imp.ReadResult:
        found = [imp.openaev_asset({"asset_id": i, "asset_name": n}) for i, n in rows]
        return imp.ReadResult(rows=found, total=len(found))

    async def plan(session: AsyncSession, result: imp.ReadResult) -> imp.ImportPlan:
        async def fake_read(*_a: object, **_k: object) -> imp.ReadResult:
            return result

        monkeypatch.setattr(imp, "read_source", fake_read)
        found, _version = await imp.preview_import(
            session,
            user_id=uuid.uuid4(),
            section="assets",
            source="openaev",
            filters={},
            remove_gone=True,
            sync_id="s1",
        )
        return found

    async with _database() as sessions, sessions() as session:
        org = await _primary(session, [SYNCED])
        first = await plan(session, read(("a1", "web-01"), ("a2", "db-01")))
        assert first.on_records and first.new == ["web-01", "db-01"]
        assert await imp.apply_record_import(session, first, actor="sync:s1") == 2
        await session.commit()
        second = await plan(session, read(("a1", "web-01")))
        assert (second.unchanged, second.gone, second.remove_refs) == (["web-01"], ["db-01"], [("openaev", "a2")])
        assert await imp.apply_record_import(session, second, actor="sync:s1") == 1
        await session.commit()
        left = sorted(r.name for r in await records.asset_rows(session, org, source="openaev"))
        versions = (await session.execute(select(OrganizationContextVersion.version))).scalars().all()
        names = [a for a, _n in await _assets(session)]
        log = await records.changes(session, org)
    assert left == ["web-01"]
    assert versions == [1]  # no profile version: rows only
    assert names == ["laptop-01", "web-01"]
    assert {c["origin"] for c in log if c["op"] in ("create", "delete")} >= {"sync:s1"}
