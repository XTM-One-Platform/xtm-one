# SPDX-License-Identifier: Apache-2.0
"""#2748 on PostgreSQL: software synced with an architecture, locale or version suffix is found.

Device inventories store ``Mozilla Thunderbird (x64 fr)`` or ``Oracle VirtualBox 7.2.6``, one
product per variant. ``find_assets_running`` and a case's running versions both answer them under
the product, and a name that matches nothing names its near matches instead of a bare zero.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import pytest
from sqlalchemy import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import app.models  # noqa: F401 - registers every mapper on Base.metadata
from app.models.organization import Organization
from app.models.organization_asset import (
    OrganizationAsset,
    OrganizationAssetSoftware,
    OrganizationSoftware,
    OrganizationSoftwareVersion,
)
from app.services import ctem_case_versions as versions
from app.services import organization_inventory_query as q
from tests.scratch_database import ALL_TABLES, scratch_database

pytestmark = pytest.mark.integration

#: (asset, vendor, product, version) installs of the fixture.
_INSTALLS = [
    ("tb-fr", "Mozilla", "Mozilla Thunderbird (x64 fr)", "115.10.1"),
    ("tb-us", "Mozilla", "Mozilla Thunderbird (x64 en-US)", "115.16.0"),
    ("tb-esr", "Mozilla", "Mozilla Thunderbird ESR (x64 fr)", "115.9.0"),
    ("ff", "Mozilla", "Mozilla Firefox (x64 en-GB)", "115.0"),
    ("vb-old", "Oracle Corporation", "Oracle VirtualBox 7.0.20", "7.0.20"),
    ("vb-new", "Oracle Corporation", "Oracle VirtualBox 7.2.22", "7.2.22"),
    ("vb-mac", "Oracle", "VirtualBox", "7.2.6"),
]


@asynccontextmanager
async def _database() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    async with scratch_database("findsuffix2748", tables=ALL_TABLES, extensions=("vector",)) as db:
        yield db.sessions


def _key(value: str) -> str:
    return " ".join(value.split()).casefold()


async def _inventory(session: AsyncSession) -> dict[str, Any]:
    org = Organization(id=uuid.uuid4(), name="A", role="primary", relationship=None)
    session.add(org)
    await session.flush()
    assets: dict[str, uuid.UUID] = {}
    software: dict[str, uuid.UUID] = {}
    for asset, vendor, product, version in _INSTALLS:
        assets[asset] = uuid.uuid4()
        software[product] = uuid.uuid4()
        version_id = uuid.uuid4()
        session.add(
            OrganizationAsset(
                id=assets[asset], organization_id=org.id, canonical_key=asset, name=asset, name_key=asset, fields={}
            )
        )
        await session.flush()
        await session.execute(
            insert(OrganizationSoftware),
            [
                {
                    "id": software[product],
                    "organization_id": org.id,
                    "vendor": vendor,
                    "vendor_key": _key(vendor),
                    "product": product,
                    "product_key": _key(product),
                    "name": product,
                    "name_key": _key(product),
                    "software_type": "application",
                }
            ],
        )
        await session.execute(
            insert(OrganizationSoftwareVersion),
            [{"id": version_id, "organization_id": org.id, "software_id": software[product], "version": version}],
        )
        await session.execute(
            insert(OrganizationAssetSoftware),
            [
                {
                    "organization_id": org.id,
                    "asset_id": assets[asset],
                    "vendor": vendor,
                    "vendor_key": _key(vendor),
                    "product": product,
                    "product_key": _key(product),
                    "version": version,
                    "software_version_id": version_id,
                }
            ],
        )
    await session.commit()
    return {"org": org.id, "assets": assets, "software": software}


def _names(answer: dict[str, Any]) -> set[str]:
    return {item["asset_name"] for item in answer["items"]}


async def test_a_suffixed_product_is_found_under_its_product_name() -> None:
    async with _database() as sessions, sessions() as session:
        world = await _inventory(session)
        org = world["org"]
        for product in ("thunderbird", "Thunderbird", "Mozilla Thunderbird"):
            found = await q.find_assets_running(session, org, product=product, max_version="115.16.0")
            assert _names(found) == {"tb-fr"}, product
            assert found["versions_matched"] >= 1
            assert "Mozilla Thunderbird ESR (x64 fr)" not in found["products_matched"]
        every = await q.find_assets_running(session, org, product="Mozilla Thunderbird")
        assert _names(every) == {"tb-fr", "tb-us"}
        assert sorted(every["products_matched"]) == ["Mozilla Thunderbird (x64 en-US)", "Mozilla Thunderbird (x64 fr)"]

        boxes = await q.find_assets_running(session, org, product="VirtualBox", max_version="7.2.24")
        assert _names(boxes) == {"vb-old", "vb-new", "vb-mac"}
        # A label naming a version stays that product only.
        assert _names(await q.find_assets_running(session, org, product="Oracle VirtualBox 7.0.20")) == {"vb-old"}


async def test_a_software_id_names_the_software() -> None:
    async with _database() as sessions, sessions() as session:
        world = await _inventory(session)
        sid = world["software"]["Mozilla Thunderbird (x64 fr)"]
        found = await q.find_assets_running(session, world["org"], product=str(sid), max_version="115.16.0")
        assert _names(found) == {"tb-fr"}


async def test_a_naming_miss_names_its_near_matches() -> None:
    async with _database() as sessions, sessions() as session:
        world = await _inventory(session)
        found = await q.find_assets_running(session, world["org"], product="Thunder")
        assert found["total"] == 0 and found["items"] == []
        assert {p["name"] for p in found["similar_products"]} == {
            "Mozilla Thunderbird (x64 fr)",
            "Mozilla Thunderbird (x64 en-US)",
            "Mozilla Thunderbird ESR (x64 fr)",
        }
        nothing = await q.find_assets_running(session, world["org"], product="nginx")
        assert nothing["total"] == 0 and nothing["similar_products"] == []


async def test_a_case_reads_the_suffixed_rows() -> None:
    async with _database() as sessions, sessions() as session:
        world = await _inventory(session)
        org = world["org"]
        thunderbird = await versions.read_running_versions(
            session, org, product_label="Mozilla Thunderbird", fixed_version="115.16"
        )
        assert thunderbird is not None
        rows = {v["version"]: v["status"] for v in thunderbird["versions"]}
        assert rows == {"115.10.1": "below_fixed", "115.16.0": "fixed"}

        box = await versions.read_running_versions(
            session, org, product_label="Oracle VirtualBox", fixed_version="7.2.24"
        )
        assert box is not None
        assert {v["version"] for v in box["versions"]} == {"7.0.20", "7.2.6", "7.2.22"}
        assert box["assets_below_fixed"] == 3
