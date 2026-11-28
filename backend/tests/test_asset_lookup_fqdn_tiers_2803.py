"""An exact FQDN wins over the hostname it derives (#2803).

``kibana.b.example.com`` used to answer ``kibana.a.example.com``: the lookup unioned
every reading of the value (the FQDN and its first label ``kibana``) and answered the
first asset by name. The readings are now ranked and an FQDN never falls back to its
bare first label.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import app.models  # noqa: F401 - registers every mapper on Base.metadata
from app.models.organization import Organization
from app.models.organization_context import OrganizationContextVersion
from app.services import organization_asset_store as store
from app.services import organization_inventory_query as q
from tests.scratch_database import ALL_TABLES, scratch_database


def _keys(value: str) -> set[tuple[str, str]]:
    return store._lookup_keys(value)


A, B = uuid.uuid4(), uuid.uuid4()
HELD = {("fqdn", "kibana.a.example.com"): {A}, ("fqdn", "kibana.b.example.com"): {B}, ("hostname", "kibana"): {A, B}}


def test_an_exact_fqdn_answers_its_own_holder_only() -> None:
    assert store._tiers("kibana.b.example.com", _keys("kibana.b.example.com"), HELD, {}) == ("fqdn", {B}, set())


def test_an_fqdn_no_asset_holds_never_falls_back_to_its_first_label() -> None:
    kind, ids, possible = store._tiers("kibana.c.example.com", _keys("kibana.c.example.com"), HELD, {})
    assert (kind, ids) == ("", set())
    assert possible == {A, B}


def test_a_bare_hostname_still_answers_through_the_hostname_tier() -> None:
    assert store._tiers("kibana", _keys("kibana"), HELD, {}) == ("hostname", {A, B}, set())


def test_an_exact_name_wins_over_the_hostname() -> None:
    by_name = {"kibana": {B}}
    assert store._tiers("kibana", _keys("kibana"), HELD, by_name) == ("name", {B}, set())


def test_the_tiers_run_strongest_first() -> None:
    assert store.MATCH_TIERS == ("fqdn", "name", "hostname", "ip", "mac")


@asynccontextmanager
async def _database() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    async with scratch_database("assetfqdn", tables=ALL_TABLES, extensions=("vector",)) as db:
        yield db.sessions


def _vhost(name: str, external_id: str) -> dict[str, Any]:
    return {"name": name, "category": "web_application", "source": "openaev", "external_id": external_id, "fqdn": name}


async def _world(session: AsyncSession) -> uuid.UUID:
    org = Organization(id=uuid.uuid4(), name="A", role="primary", relationship=None)
    session.add(org)
    await session.flush()
    assets = [_vhost("kibana.a.example.com", "v-a"), _vhost("kibana.b.example.com", "v-b")]
    session.add(
        OrganizationContextVersion(organization_id=org.id, version=1, profile={"assets_exposure": assets}, source="api")
    )
    await session.flush()
    await store.ensure_built(session, org.id)
    await session.commit()
    return org.id


@pytest.mark.integration
async def test_lookup_and_get_asset_answer_the_exact_fqdn_not_the_first_by_name() -> None:
    async with _database() as sessions, sessions() as session:
        org = await _world(session)
        looked = await q.lookup_assets(session, org, ["kibana.b.example.com", "kibana.c.example.com", "kibana"])
        detail = await q.get_asset(session, org, "kibana.b.example.com")
        missing = await q.get_asset(session, org, "kibana.c.example.com")
    hit = looked["found"]["kibana.b.example.com"]
    assert hit["name"] == "kibana.b.example.com"
    assert (hit["match_kind"], hit["matches"], hit["ambiguous"]) == ("fqdn", 1, False)
    assert detail is not None and detail["name"] == "kibana.b.example.com"
    assert detail["match_kind"] == "fqdn" and "ambiguous" not in detail

    assert "kibana.c.example.com" in looked["not_found"]
    assert missing is None
    possible = looked["possible_matches"]["kibana.c.example.com"]
    assert possible["count"] == 2
    assert {p["name"] for p in possible["items"]} == {"kibana.a.example.com", "kibana.b.example.com"}

    bare = looked["found"]["kibana"]
    assert (bare["match_kind"], bare["matches"], bare["ambiguous"]) == ("hostname", 2, True)
