# SPDX-License-Identifier: Apache-2.0

"""Asset inventory search: case-insensitive field filters and free text over the OS and owner (#2085).

1. ``os_family:windows``, ``os_family:WINDOWS`` and ``os_family:Windows`` keep the same assets.
2. The free text ``windows`` or ``26200`` finds the hosts by their OS family or version; a name still works.
3. A field filter that keeps no asset names the values the field holds, in both agent reads.
"""

from __future__ import annotations

import json

import pytest

from app.services import organization_asset_store as store
from app.services import organization_context_categories as cats
from tests.test_organization_asset_store_postgres import _database, _org, _save

WIN = {
    "name": "laptop-07",
    "category": "host",
    "source": "intune",
    "external_id": "d-7",
    "serial": "SN-007",
    "os_family": "Windows",
    "os_version": "10.0.26200.9911",
    "owner": "Alice Martin",
}
LINUX = {
    "name": "web-03",
    "category": "host",
    "source": "openaev",
    "external_id": "a-3",
    "serial": "SN-303",
    "os_family": "Linux",
    "os_version": "6.8.0",
}


# ── renderers (no database) ──────────────────────────────────────────────


def test_an_empty_page_names_the_values_the_filtered_field_holds() -> None:
    held = {"os_family": ["Linux", "Windows"]}
    page = cats.render_asset_page([], total=0, query="os_family:macos", offset=0, limit=50, held=held)
    assert "no row matching 'os_family:macos'" in page
    assert "os_family holds: Linux, Windows." in page
    assert page.endswith("Do not guess it.")
    assert cats.held_values_line({"os_family": []}) == "os_family holds: no value."


# ── the store on PostgreSQL ──────────────────────────────────────────────


@pytest.mark.integration
async def test_field_filters_are_case_insensitive_and_free_text_reads_the_os_and_owner() -> None:
    async with _database() as sessions, sessions() as session:
        org = await _org(session, "A", primary=True)
        await _save(session, org, 1, [WIN, LINUX])
        await store.ensure_built(session)
        await session.commit()

        async def names(**kwargs: object) -> list[str]:
            page = await store.list_assets(session, org, **kwargs)  # type: ignore[arg-type]
            return [a["name"] for a in page.items]

        cased = [await names(filters={"os_family": [v]}) for v in ("windows", "WINDOWS", "Windows")]
        by_family = await names(q="windows")
        by_build = await names(q="26200")
        by_owner = await names(q="alice")
        by_name = await names(q="WEB")
        macos = await store.list_assets(session, org, filters={"os_family": ["macos"]})
        held = await store.held_values(session, org, {"os_family": ["macos"], "source": ["intune"]})
        facets = await store.asset_facets(session, org, filters={"category": ["HOST"]})
        _, text = cats.split_asset_query("26200")
        via_context = await names(q=text)
    assert cased == [["laptop-07"]] * 3
    assert by_family == ["laptop-07"]
    assert by_build == ["laptop-07"]
    assert by_owner == ["laptop-07"]
    assert by_name == ["web-03"]
    assert via_context == ["laptop-07"]
    assert macos.total == 0
    assert held == {"os_family": ["Linux", "Windows"]}
    # A facet value picked in another case still narrows the other facets; the facet counts keep their values.
    assert facets["category"] == [{"value": "host", "count": 2}]
    assert facets["source"] == [{"value": "intune", "count": 1}, {"value": "openaev", "count": 1}]
    assert json.dumps(held)
