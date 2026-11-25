"""#2237: a relationship listing names the ids of both ends, and a MITRE ID search is exact first.

An agent walking a campaign to its techniques read ``[Campaign] X --uses-->
[Attack-Pattern] Adversary-in-the-Middle`` with the relationship id alone, then
searched ``T1557`` and got the Mobile technique sharing that name first, the
Enterprise one outside the first page. These tests stub ``_graphql`` (never
real HTTP).
"""

from typing import Any

import pytest

from app.integrations.providers.opencti import build_tools
from app.integrations.providers.opencti import knowledge as knowledge_mod
from app.integrations.providers.opencti._common import (
    _RELATIONSHIP_FIELDS,
    _format_relationship,
)
from app.tools.base import CallableToolExecutor, ToolCall

TOKEN = "https://demo.opencti.io||test-token"


def _technique(node_id: str, mitre_id: str, name: str) -> dict[str, Any]:
    return {
        "id": node_id,
        "standard_id": f"attack-pattern--{node_id}",
        "entity_type": "Attack-Pattern",
        "name": name,
        "x_mitre_id": mitre_id,
    }


_ENTERPRISE = _technique("ap-1557", "T1557", "Adversary-in-the-Middle")
_SUB = _technique("ap-1557-001", "T1557.001", "LLMNR/NBT-NS Poisoning and SMB Relay")
_MOBILE = _technique("ap-1638", "T1638", "Adversary-in-the-Middle")
_HARDWARE = _technique("ap-1200", "T1200", "Hardware Additions")
_THEFT = _technique("ap-1657", "T1657", "Financial Theft")
_BY_MITRE_ID: dict[str, dict[str, Any]] = {
    str(n["x_mitre_id"]): n for n in (_ENTERPRISE, _SUB, _MOBILE, _HARDWARE, _THEFT)
}


# ── list_opencti_relationships / get_opencti_relationship formatting ──────


def test_relationship_names_the_id_stix_id_and_mitre_id_of_each_end() -> None:
    rel = {
        "id": "rel-1",
        "relationship_type": "uses",
        "from": {
            "id": "camp-1",
            "standard_id": "campaign--camp-1",
            "entity_type": "Campaign",
            "name": "Operation X",
        },
        "to": dict(_ENTERPRISE),
    }
    out = _format_relationship(rel)
    assert "  From: camp-1 (campaign--camp-1)" in out
    assert "  To: ap-1557 (attack-pattern--ap-1557, MITRE T1557)" in out
    assert "  ID: rel-1" in out


def test_relationship_end_without_an_id_prints_no_end_line() -> None:
    rel = {"id": "rel-2", "relationship_type": "uses", "from": {}, "to": {"name": "B"}}
    out = _format_relationship(rel)
    assert "From:" not in out and "To:" not in out


def test_relationship_fragment_requests_the_mitre_id_on_both_ends() -> None:
    assert _RELATIONSHIP_FIELDS.count("... on AttackPattern { name x_mitre_id }") == 2


# ── search_opencti / list_opencti_entities exact MITRE ID first ──────────


def _patch(monkeypatch: pytest.MonkeyPatch, calls: list[tuple[str, Any]]) -> None:
    async def _fake_graphql(
        client: Any, base_url: str, headers: Any, query: str, variables: Any = None, **_kw: Any
    ) -> tuple[dict[str, Any], None]:
        calls.append((query, variables))
        root = "stixDomainObjects" if "stixDomainObjects" in query else "stixCoreObjects"
        filters = (variables or {}).get("filters") or {}
        mitre = [f["values"][0] for f in filters.get("filters", []) if f.get("key") == "x_mitre_id"]
        if mitre:
            hit = _BY_MITRE_ID.get(mitre[0])
            return {root: {"edges": [{"node": hit}] if hit else []}}, None
        # Full-text ranking by _score: the name match of another matrix first.
        ranked = [_MOBILE, _HARDWARE, _ENTERPRISE, _SUB, _THEFT]
        first = (variables or {}).get("first") or 10
        return {
            root: {
                "edges": [{"node": n} for n in ranked[:first]],
                "pageInfo": {"globalCount": 16},
            }
        }, None

    monkeypatch.setattr(knowledge_mod, "_graphql", _fake_graphql)


async def _run(name: str, **arguments: Any) -> str:
    executor = CallableToolExecutor()
    build_tools(TOKEN, executor, scope_headers={})
    result = await executor.execute_tool(ToolCall(id="t-2237", name=name, arguments=arguments))
    return str(result.content)


def _first_listed(out: str) -> str:
    positions: dict[str, int] = {
        mitre_id: out.find(f"MITRE ID: {mitre_id}\n")
        for mitre_id in _BY_MITRE_ID
        if out.find(f"MITRE ID: {mitre_id}\n") >= 0
    }
    return min(positions, key=lambda mitre_id: positions[mitre_id])


async def test_search_by_mitre_id_lists_the_exact_technique_first(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, Any]] = []
    _patch(monkeypatch, calls)
    out = await _run("search_opencti", search="T1557", types=["Attack-Pattern"], limit=2)
    assert _first_listed(out + "\n") == "T1557"
    assert "MITRE ID: T1638" in out
    assert "T1200" not in out, "the merged list keeps the limit"
    exact_filters = [v["filters"] for q, v in calls if "SearchByMitreId" in q]
    wanted = {"key": "x_mitre_id", "values": ["T1557"], "operator": "eq", "mode": "or"}
    assert exact_filters and wanted in exact_filters[0]["filters"]


async def test_search_by_mitre_id_is_case_insensitive_and_takes_sub_techniques(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch(monkeypatch, [])
    assert _first_listed(await _run("search_opencti", search="t1657", limit=2) + "\n") == "T1657"
    out = await _run("search_opencti", search="T1557.001", types=["Attack-Pattern"], limit=2)
    assert _first_listed(out + "\n") == "T1557.001"


async def test_exact_hit_is_not_listed_twice(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch(monkeypatch, [])
    out = await _run("search_opencti", search="T1557", limit=5)
    assert out.count("MITRE ID: T1557\n") == 1


async def test_plain_keyword_keeps_the_score_order_and_makes_no_exact_call(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, Any]] = []
    _patch(monkeypatch, calls)
    out = await _run("search_opencti", search="adversary", types=["Attack-Pattern"], limit=2)
    assert _first_listed(out + "\n") == "T1638"
    assert not [q for q, _ in calls if "MitreId" in q]


async def test_types_that_exclude_attack_patterns_skip_the_exact_call(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, Any]] = []
    _patch(monkeypatch, calls)
    await _run("search_opencti", search="T1557", types=["Malware"], limit=2)
    assert not [q for q, _ in calls if "MitreId" in q]


async def test_list_entities_with_a_mitre_id_search_lists_the_exact_technique_first(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, Any]] = []
    _patch(monkeypatch, calls)
    out = await _run("list_opencti_entities", search="T1557", types=["Attack-Pattern"], limit=2)
    assert _first_listed(out + "\n") == "T1557"
    assert [q for q, _ in calls if "ListSDOsByMitreId" in q]
