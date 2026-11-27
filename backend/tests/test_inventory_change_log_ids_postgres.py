"""An id read from the inventory change log resolves to its merged asset (#2702).

``list_inventory_changes`` names a record change by its source record id; the row
also carries the merged asset's id and name, and ``get_asset`` / ``lookup_assets``
accept that record id (bare or ``record:<uuid>``) and answer the merged asset.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import app.models  # noqa: F401 - registers every mapper on Base.metadata
from app.models.organization import Organization
from app.models.organization_context import OrganizationContextVersion
from app.services import organization_asset_records as records
from app.services import organization_asset_store as store
from app.services import organization_inventory_query as q
from tests.scratch_database import ALL_TABLES, scratch_database

pytestmark = pytest.mark.integration

HAND = {"name": "payments-api", "category": "host", "serial": "SN-2702"}
SYNCED = {"name": "payments-api", "category": "host", "source": "github", "external_id": "repo-1", "serial": "SN-2702"}


@asynccontextmanager
async def _database() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    async with scratch_database("changelogids", tables=ALL_TABLES, extensions=("vector",)) as db:
        yield db.sessions


async def _merged(session: AsyncSession) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    """An organisation whose one asset is merged from a manual record A and a github record B."""
    org = Organization(id=uuid.uuid4(), name="A", role="primary", relationship=None)
    session.add(org)
    await session.flush()
    session.add(
        OrganizationContextVersion(organization_id=org.id, version=1, profile={"assets_exposure": [HAND]}, source="api")
    )
    await session.flush()
    assert await store.ensure_built(session) == org.id
    await session.commit()
    synced = await records.create_record(session, org.id, SYNCED, origin="sync:s", actor="sync:s")
    await session.commit()
    rows = await records.asset_rows(session, org.id)
    assert len({r.asset_id for r in rows}) == 1, "A and B are one asset"
    asset_id = rows[0].asset_id
    assert asset_id is not None
    return org.id, synced.id, asset_id


async def test_the_change_log_names_the_merged_asset_and_its_record_id_resolves() -> None:
    async with _database() as sessions, sessions() as session:
        org, record_b, asset_id = await _merged(session)
        await records.update_record(session, org, record_b, {"criticality": "high"}, origin="sync:s", actor="sync:s")
        await session.commit()
        log = await q.list_inventory_changes(session, org)
        detail = await q.get_asset(session, org, str(record_b))
        looked = await q.lookup_assets(session, org, [str(record_b), f"record:{record_b}"])
        asset_detail = await q.get_asset(session, org, str(asset_id))
        stranger = await q.get_asset(session, org, str(uuid.uuid4()))
        stranger_lookup = await q.lookup_assets(session, org, [f"record:{uuid.uuid4()}"])
    update = next(c for c in log["items"] if c["op"] == "update" and c["entity_id"] == str(record_b))
    assert update["asset_id"] == str(asset_id)
    assert update["source"] == "github" and update["external_id"] == "repo-1"
    assert detail is not None and detail["id"] == str(asset_id)
    assert update["asset_name"] == detail["name"]
    assert {v["id"] for v in looked["found"].values()} == {str(asset_id)}
    assert looked["not_found"] == []
    assert asset_detail is not None and asset_detail["id"] == str(asset_id)
    assert stranger is None
    assert stranger_lookup["found"] == {}


async def test_a_deleted_record_change_keeps_its_name_and_no_asset_id() -> None:
    async with _database() as sessions, sessions() as session:
        org, record_b, _asset_id = await _merged(session)
        await records.delete_records(session, org, [record_b], origin="sync:s", actor="sync:s")
        await session.commit()
        log = await records.changes(session, org, entity_id=record_b)
    deleted = log[0]
    assert deleted["op"] == "delete"
    assert deleted["asset_id"] is None
    assert deleted["asset_name"] == "payments-api"
    assert deleted["source"] == "github" and deleted["external_id"] == "repo-1"


def test_a_record_reference_reads_a_bare_or_prefixed_uuid_only() -> None:
    record_id = uuid.uuid4()
    assert store.record_reference(str(record_id)) == record_id
    assert store.record_reference(f"record:{record_id}") == record_id
    assert store.record_reference("github:repo-1") is None
    assert store.record_reference("web-02") is None
