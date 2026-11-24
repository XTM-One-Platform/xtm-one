"""The CTEM posture inputs stamp read from PostgreSQL: the records and the profile (acceptance 1 to 3)."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, cast

import pytest
from sqlalchemy import Table
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
from app.services import ctem_posture_inputs as inputs
from app.services import organization_asset_records as records
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
    async with scratch_database("postureinputs", tables=tables) as db:
        yield db.sessions


ASSET = {"name": "web-01", "category": "host", "internet_facing": "no", "source": "intune", "external_id": "d-1"}


async def _version(session: AsyncSession, org: uuid.UUID, version: int, **profile: Any) -> None:
    session.add(OrganizationContextVersion(organization_id=org, version=version, profile=profile, source="form"))
    await session.commit()


async def test_a_save_outside_the_inputs_keeps_the_stamp_and_an_asset_change_moves_it() -> None:
    async with _database() as sessions, sessions() as session:
        org = Organization(id=uuid.uuid4(), name="A", role="primary", relationship=None)
        session.add(org)
        await session.flush()
        await _version(session, org.id, 1, assets_exposure=[ASSET], owned_domains=["a.example"])
        assert await records.ensure_records(session) == org.id
        await session.commit()
        first = await inputs.posture_inputs_stamp(session)
        assert first is not None and first.startswith(f"{org.id}:")

        # Two saves of Owned domains and a description: new versions, same stamp.
        await _version(session, org.id, 2, owned_domains=["a.example", "b.example"])
        await _version(session, org.id, 3, owned_domains=["b.example"], description="changed")
        assert await inputs.posture_inputs_stamp(session) == first

        # A record the build reads: the stamp moves.
        await records.create_record(
            session, org.id, {"name": "db-01", "category": "host", "internet_facing": "yes"}, origin="form", actor="u"
        )
        await session.commit()
        assert await inputs.posture_inputs_stamp(session) != first
