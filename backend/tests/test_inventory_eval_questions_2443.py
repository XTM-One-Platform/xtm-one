"""The eval set of ten inventory questions (#2443): each answered by ONE query call, exactly.

The questions an agent asks of an inventory of thousands of assets, on the S1
fixture (10,000 assets, 2,000 software, 20,000 installations, an SBOM chain, a
group, a primary asset), plus one IP-identified source record and one change.
Before #2443 each of them was a page of ``get_organization_context(category=...)``
read in Python (or a preloaded, truncated slice of the prompt); each is now one
call with a bounded number of SQL statements and an exact answer.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from app.models.organization_asset import OrganizationAssetSource
from app.models.organization_asset_record import OrganizationAssetChange
from app.services import organization_inventory_query as q
from tests.test_organization_inventory_query_postgres import (
    ASSETS,
    MAX_STATEMENTS,
    _database,
    _inventory,
    _statements,
    pytestmark,  # noqa: F401 - the integration mark
)


async def _world(session: Any) -> dict[str, Any]:
    world = await _inventory(session)
    first = world["assets"][0]
    session.add(
        OrganizationAssetSource(
            organization_id=world["org"],
            asset_id=first,
            source="edr",
            external_id="edr-1",
            identifiers={"ip": ["10.20.30.40"], "hostname": ["host-00000"]},
        )
    )
    session.add(
        OrganizationAssetChange(
            organization_id=world["org"],
            entity="record",
            entity_id=first,
            op="update",
            origin="api",
            actor="lab",
            before={"criticality": "low"},
            after={"criticality": "critical"},
        )
    )
    await session.commit()
    return world


async def test_ten_inventory_questions_are_each_one_call() -> None:
    async with _database() as sessions, sessions() as session:
        world = await _world(session)
        org, first = world["org"], world["assets"][0]
        answers: dict[str, Any] = {}

        async def ask(question: str, call: Any, *, bound: int = MAX_STATEMENTS) -> Any:
            with _statements(session) as seen:
                answer = await call
            assert len(seen) <= bound, f"{question}: {len(seen)} statements"
            answers[question] = answer
            return answer

        # 1. Lookup by IP.
        found = await ask("Which asset is 10.20.30.40?", q.lookup_assets(session, org, ["10.20.30.40"]))
        assert found["found"]["10.20.30.40"]["id"] == str(first)

        # 2. One asset with its groups and primary assets.
        # The whole asset detail (resolution, sources, software page, groups, primary assets): a
        # constant number of statements, whatever the inventory size.
        asset = await ask(
            "What is host-00000 and what does it serve?", q.get_asset(session, org, "host-00000"), bound=16
        )
        assert asset is not None and asset["groups"][0]["name"] == "DMZ"
        assert [p["primary_asset"] for p in asset["primary_assets"]] == ["payments"]

        # 3. Version range, semantic (1.10.2 is inside [1.10, 2.0), 1.9.0 is not).
        ranged = await ask(
            "Which assets run product0000 from 1.10 up to 2.0?",
            q.find_assets_running(session, org, product="product0000", min_version="1.10", max_version="2.0"),
        )
        assert ranged["versions_matched"] == 1
        assert {run["version"] for item in ranged["items"] for run in item["runs"]} == {"1.10.2"}

        # 4. Through a dependency: product0001 bundles product0000 2.0.0.
        through = await ask(
            "Which assets carry product0000 2.0.0, directly or bundled?",
            q.find_assets_running(session, org, product="product0000", version="2.0.0", limit=200),
        )
        assert any(run["via"] for item in through["items"] for run in item["runs"])

        # 5. The exposure question of the issue: internet-facing Windows hosts below a version, with primary assets.
        exposed = await ask(
            "Internet-facing Windows hosts running product0000 below 2.0, with the primary assets they serve?",
            q.find_assets_running(
                session,
                org,
                product="product0000",
                max_version="2.0",
                internet_facing=["yes"],
                os_family=["windows"],
                limit=200,
            ),
        )
        assert exposed["filters"] == {"internet_facing": ["yes"], "os_family": ["windows"]}
        assert all(i["internet_facing"] == "yes" and i["os_family"] == "windows" for i in exposed["items"])
        assert all("primary_assets" in i and "criticality" in i for i in exposed["items"])

        # 6. Aggregation.
        counts = await ask(
            "How many assets per criticality?", q.aggregate_inventory(session, org, group_by="criticality")
        )
        assert sum(b["count"] for b in counts["buckets"]) == ASSETS

        # 7. Filtered count without paging.
        facing = await ask(
            "How many internet-facing assets per OS?",
            q.aggregate_inventory(session, org, group_by="os_family", internet_facing=["yes"]),
        )
        assert sum(b["count"] for b in facing["buckets"]) == ASSETS // 10

        # 8. Primary asset reach.
        primary = await ask("Which primary assets does the inventory support?", q.list_primary_assets(session, org))
        assert primary["items"] == [{"primary_asset": "payments", "records": 1, "groups": 0}]

        # 9. Software search with counts.
        software = await ask("Which vendor0 software do we run?", q.search_software(session, org, vendor="vendor0"))
        assert software["total"] == 40

        # 10. Changes since a date.
        since = (datetime.now(UTC) - timedelta(days=1)).date().isoformat()
        changes = await ask(
            "What changed in the inventory since yesterday?", q.list_inventory_changes(session, org, since=since)
        )
        assert [c["op"] for c in changes["items"]] == ["update"]

        assert len(answers) == 10
