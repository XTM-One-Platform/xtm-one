"""SBOM import on the canonical asset store, on PostgreSQL (#1178).

An SBOM imported on an asset joins its software as source ``sbom`` with its
bundling kept; the same document again is idempotent; another document adds;
the SBOM follows its asset through a rebuild (a newer profile); an unknown
asset is refused.
"""

from __future__ import annotations

import json
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
    OrganizationAssetSbom,
)
from app.models.organization_context import OrganizationContextVersion
from app.services import organization_asset_store as store
from tests.scratch_database import ALL_TABLES, scratch_database

pytestmark = pytest.mark.integration

SERVER = {"name": "web-02", "category": "host", "source": "openaev", "external_id": "a-10", "serial": "SN-900"}
SERVER_MDM = {"name": "WEB-02", "category": "host", "source": "intune", "external_id": "d-7", "serial": "sn-900"}

IMAGE = {
    "bomFormat": "CycloneDX",
    "metadata": {"component": {"name": "ingress-nginx", "version": "1.15.1"}},
    "components": [
        {
            "bom-ref": "img",
            "name": "ingress-nginx-controller",
            "version": "1.15.1",
            "components": [
                {"name": "nginx", "version": "1.27.1", "publisher": "F5", "purl": "pkg:generic/nginx@1.27.1"}
            ],
        },
        {"name": "zlib", "version": "NOASSERTION"},
    ],
}
APP = {
    "spdxVersion": "SPDX-2.3",
    "name": "web-app",
    "packages": [{"SPDXID": "SPDXRef-ossl", "name": "openssl", "versionInfo": "3.0.15"}],
}


@asynccontextmanager
async def _database() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    async with scratch_database("orgsbom", tables=ALL_TABLES, extensions=("vector",)) as db:
        yield db.sessions


async def _setup(session: AsyncSession, rows: list[dict[str, Any]]) -> tuple[uuid.UUID, uuid.UUID]:
    org = Organization(id=uuid.uuid4(), name="A", role="primary", relationship=None)
    session.add(org)
    await session.flush()
    session.add(
        OrganizationContextVersion(organization_id=org.id, version=1, profile={"assets_exposure": rows}, source="api")
    )
    await session.flush()
    await store.ensure_built(session)
    await session.commit()
    asset_id = (await session.execute(select(OrganizationAsset.id))).scalars().one()
    return org.id, asset_id


async def _software(session: AsyncSession, org: uuid.UUID, asset_id: uuid.UUID) -> dict[str, dict[str, Any]]:
    detail = await store.get_asset(session, org, asset_id, software_limit=200)
    assert detail is not None
    return {s["product"]: s for s in detail["software"]["items"]}


async def test_cyclonedx_import_adds_the_components_with_their_bundling_and_unknown_versions() -> None:
    async with _database() as sessions, sessions() as session:
        org, asset_id = await _setup(session, [SERVER])
        result = await store.import_sbom(session, org, asset_id, data=json.dumps(IMAGE).encode(), file_name="i.json")
        await session.commit()
        soft = await _software(session, org, asset_id)
    assert result["format"] == "cyclonedx" and result["components"] == 3 and not result["replaced"]
    assert result["unknown_versions"] == 1 and result["bundled"] == 1
    assert soft["nginx"]["version"] == "1.27.1"
    assert soft["nginx"]["bundled_in"] == "ingress-nginx-controller 1.15.1"
    assert soft["nginx"]["purl"] == "pkg:generic/nginx@1.27.1"
    assert soft["nginx"]["sources"] == ["sbom"]
    assert soft["zlib"]["version"] == "unknown"


async def test_the_same_document_again_is_idempotent_and_another_document_adds() -> None:
    async with _database() as sessions, sessions() as session:
        org, asset_id = await _setup(session, [SERVER])
        await store.import_sbom(session, org, asset_id, data=json.dumps(IMAGE).encode())
        again = await store.import_sbom(session, org, asset_id, data=json.dumps(IMAGE).encode())
        await store.import_sbom(session, org, asset_id, data=json.dumps(APP).encode())
        await session.commit()
        docs = (await session.execute(select(func.count()).select_from(OrganizationAssetSbom))).scalar()
        soft = await _software(session, org, asset_id)
    assert again["replaced"] is True
    assert docs == 2
    assert set(soft) == {"ingress-nginx-controller", "nginx", "zlib", "openssl"}


async def test_the_sbom_follows_its_asset_through_a_rebuild_and_a_merge() -> None:
    async with _database() as sessions, sessions() as session:
        org, asset_id = await _setup(session, [SERVER])
        await store.import_sbom(session, org, asset_id, data=json.dumps(APP).encode())
        session.add(
            OrganizationContextVersion(
                organization_id=org, version=2, profile={"assets_exposure": [SERVER, SERVER_MDM]}, source="api"
            )
        )
        await session.flush()
        await store.ensure_built(session)
        await session.commit()
        asset_ids = (await session.execute(select(OrganizationAsset.id))).scalars().all()
        assert len(asset_ids) == 1  # merged on serial
        soft = await _software(session, org, asset_ids[0])
    assert soft["openssl"]["version"] == "3.0.15"


async def test_an_unknown_asset_is_refused() -> None:
    async with _database() as sessions, sessions() as session:
        org, _ = await _setup(session, [SERVER])
        with pytest.raises(store.SbomImportError):
            await store.import_sbom(session, org, uuid.uuid4(), data=json.dumps(APP).encode())
