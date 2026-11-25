"""A profile save never fails while a sync writes the asset rows (#1752), on a real PostgreSQL.

Since #1752 the profile API carries profile fields only: ``PUT`` keeps the
stored row lists, so ``save_version`` applies no row write and takes no lock
on the records. A sync (Intune, Defender...) that holds the organization's row
lock in an open transaction therefore never blocks, conflicts with or refuses
an administrator's save of the owned domains, and the sync's row write is kept.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, cast

import pytest
from sqlalchemy import Table
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import app.models  # noqa: F401 - registers every mapper on Base.metadata
from app.api.v1 import organization_context as api
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
from app.models.user import User
from app.services import organization_asset_records as records
from app.services import organization_context as org_context
from tests.scratch_database import scratch_database, with_referenced_tables

pytestmark = pytest.mark.integration

#: A save that waits on the sync's row lock would hang until the sync commits: well past this bound.
_SAVE_BOUND_S = 10.0
_SYNCED = {
    "name": "laptop-01",
    "category": "host",
    "source": "intune",
    "external_id": "d-1",
    "serial": "SN-778",
    "last_seen": "2026-11-20T00:00:00Z",
}


@asynccontextmanager
async def _database() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    models: list[Any] = [
        User,
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
    async with scratch_database("orgsavesync", tables=tables) as db:
        yield db.sessions


async def _imported_primary(sessions: async_sessionmaker[AsyncSession]) -> tuple[uuid.UUID, User]:
    """The primary organization, version 1 holding one Intune row, its records imported; an administrator."""
    async with sessions() as session:
        admin = User(
            id=uuid.uuid4(), email=f"admin-{uuid.uuid4().hex[:8]}@test.local", full_name="A", hashed_password="x"
        )
        org = Organization(id=uuid.uuid4(), name="A", role="primary", relationship=None)
        session.add_all([admin, org])
        await session.flush()
        session.add(
            OrganizationContextVersion(
                organization_id=org.id,
                version=1,
                profile={"assets_exposure": [_SYNCED], "owned_domains": []},
                source="api",
            )
        )
        await session.flush()
        assert await records.ensure_records(session, org.id) == org.id
        await session.commit()
        return org.id, admin


async def _save(
    sessions: async_sessionmaker[AsyncSession], admin: User, body: dict[str, Any]
) -> api.OrganizationContextResponse:
    async with sessions() as session:
        return await api.save_organization_context(
            api.SaveOrganizationContextRequest(profile=body, based_on_version=1), db=session, admin=admin
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("stale_rows", [False, True], ids=["new-client", "old-client-sends-get-rows"])
async def test_a_profile_save_passes_while_a_sync_holds_the_row_lock(stale_rows: bool) -> None:
    async with _database() as sessions:
        org, admin = await _imported_primary(sessions)
        body: dict[str, Any] = {"owned_domains": ["example.com"]}
        if stale_rows:
            # What an older client sends back: the rows GET answered before the sync moved them.
            body["assets_exposure"] = [_SYNCED]
        async with sessions() as sync:
            # The sync writes its fresh row and keeps its transaction (and the organization's row lock) open.
            fresh = {**_SYNCED, "name": "laptop-01 renamed by intune", "last_seen": "2026-11-24T06:00:00Z"}
            written = await records.upsert_records(sync, org, [fresh], origin="sync:intune", actor="sync:intune")
            assert len(written.updated) == 1
            answer = await asyncio.wait_for(_save(sessions, admin, body), timeout=_SAVE_BOUND_S)
            await sync.commit()
        async with sessions() as session:
            row, profile = await org_context.current_profile(session, org)
    assert answer.version == 2
    assert answer.ignored_row_fields == (["assets_exposure"] if stale_rows else [])
    assert "assets_exposure" not in answer.profile
    assert row is not None and row.version == 2
    assert profile.owned_domains == ["example.com"]
    # The sync's write is kept: the save neither reverted nor refused it.
    assert [a.name for a in profile.assets_exposure] == ["laptop-01 renamed by intune"]
