"""The OpenCTI stack lookups stay under Elasticsearch's clause limit (#1811).

A synced software inventory holds tens of thousands of rows: one OR filter
carrying every name and CPE made OpenCTI answer ``too_many_clauses`` (500) on
the CTEM Cases page. The lookup now goes in batches, and the ``fromId`` list
of the stack vulnerabilities read too.

No DB / network: ``_graphql`` is a fake that refuses any filter over the limit.
"""

from __future__ import annotations

from typing import Any, cast

import pytest

from app.services import ctem_cases_source as source
from app.services import ctem_metrics_collector as collector

#: The clause limit the fake OpenCTI enforces (a production tenant answered 7182).
CLAUSE_LIMIT = 1024
#: No HTTP client: the fake ``_graphql`` never uses it.
NO_CLIENT = cast(Any, None)

Answer = tuple[dict[str, Any] | None, str | None]


def _big_stack(n: int) -> list[dict[str, str]]:
    return [
        {
            "product": f"product-{i}",
            "vendor": "acme",
            "cpe": f"cpe:2.3:a:acme:product-{i}:1.0",
            "label": f"acme product-{i}",
        }
        for i in range(n)
    ]


class _ClauseLimitedOpenCti:
    """Answers one Software per value, a ``has`` relation per Software, and refuses an oversized filter."""

    def __init__(self) -> None:
        self.vuln_calls: list[int] = []

    async def __call__(
        self, client: Any, base_url: str, headers: dict[str, str], query: str, variables: dict[str, Any]
    ) -> Answer:
        if "stixCyberObservables" in query:
            values = [v for f in variables["filters"]["filters"] for v in f["values"]]
            if len(values) > CLAUSE_LIMIT:
                return None, "too_many_clauses: maxClauseCount is set to 7182"
            key = variables["filters"]["filters"][0]["key"]
            names = [v if key == ["name"] else v.split(":")[4] for v in values]
            offset = int(variables.get("after") or 0)
            chunk = names[offset : offset + variables["first"]]
            nodes = [{"id": f"sw-{n}", "name": n, "vendor": "acme", "cpe": ""} for n in chunk]
            more = offset + len(chunk) < len(names)
            return {
                "stixCyberObservables": {
                    "edges": [{"node": node} for node in nodes],
                    "pageInfo": {"hasNextPage": more, "endCursor": str(offset + len(chunk)) if more else None},
                }
            }, None
        if "CtemCaseStackVulns" in query:
            ids = variables["from"]
            self.vuln_calls.append(len(ids))
            if len(ids) > CLAUSE_LIMIT:
                return None, "too_many_clauses: maxClauseCount is set to 7182"
            rels = [
                {
                    "from": {"id": sw_id},
                    "to": {"id": f"v-{sw_id}", "standard_id": f"vulnerability--{sw_id}", "name": f"CVE-{sw_id}"},
                }
                for sw_id in ids
            ]
            return {"stixCoreRelationships": {"edges": [{"node": r} for r in rels], "pageInfo": {}}}, None
        return {"stixCoreRelationships": {"edges": []}}, None


def test_the_filter_batches_carry_one_key_under_the_chunk() -> None:
    batches = collector._stack_filter_batches(_big_stack(450))
    sizes = [(b["filters"][0]["key"], len(b["filters"][0]["values"])) for b in batches]
    assert sizes == [(["name"], 200), (["name"], 200), (["name"], 50), (["cpe"], 200), (["cpe"], 200), (["cpe"], 50)]
    assert all(b["mode"] == "or" for b in batches)


@pytest.mark.asyncio
async def test_a_fifty_thousand_row_stack_never_sends_an_oversized_filter(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(collector, "_graphql", _ClauseLimitedOpenCti())
    nodes, complete, err = await collector._read_stack_software(
        NO_CLIENT, "http://octi", {}, collector._OCTI_STACK_SOFTWARE, _big_stack(50_000)
    )
    assert err is None
    # The cap holds and says so: the read is incomplete, never failed.
    assert len(nodes) == collector._STACK_SOFTWARE_CAP and complete is False
    assert len({n["id"] for n in nodes}) == len(nodes)


@pytest.mark.asyncio
async def test_a_stack_larger_than_one_chunk_returns_issues_from_every_chunk(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _ClauseLimitedOpenCti()
    monkeypatch.setattr(source, "_graphql", fake)
    read = await source._read_opencti(NO_CLIENT, "http://octi", {}, _big_stack(450))
    assert read is not None and read.complete is True
    products = {i.product for i in read.issues}
    # The first, a middle and the last product: each in another name chunk.
    assert {"product-0", "product-250", "product-449"} <= products
    assert len(products) == 450
    # The fromId list went in chunks too.
    assert fake.vuln_calls and max(fake.vuln_calls) <= source._STACK_IDS_CHUNK


@pytest.mark.asyncio
async def test_the_cases_read_of_a_huge_stack_is_incomplete_not_failed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(source, "_graphql", _ClauseLimitedOpenCti())
    read = await source._read_opencti(NO_CLIENT, "http://octi", {}, _big_stack(2_000))
    assert read is not None
    assert read.complete is False and read.issues


@pytest.mark.asyncio
async def test_an_error_on_one_batch_fails_the_read(monkeypatch: pytest.MonkeyPatch) -> None:
    async def refuse(
        client: Any, base_url: str, headers: dict[str, str], query: str, variables: dict[str, Any]
    ) -> Answer:
        return None, "OpenCTI: 401"

    monkeypatch.setattr(source, "_graphql", refuse)
    assert await source._read_opencti(NO_CLIENT, "http://octi", {}, _big_stack(3)) is None
