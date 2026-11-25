"""The hosts a CTEM case is validated on are recorded as case assets (#2395).

A ``run_order`` name that resolves to one canonical asset attaches it to the
case with provenance ``name_match_inferred``; one that resolves to nothing is
kept as unresolved. The case read serves the attached assets and counts them
(``asset_count_source`` ``attached``) when the issues and the inventory name
none; the remediation gate card names them as inferred. A case whose members
name assets is unchanged.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, cast

import pytest

from app.services import ctem_case_assets as case_assets
from app.services import ctem_case_loop as loop
from app.services import ctem_case_version_history as version_history

NOW = datetime(2026, 11, 25, 22, 0, tzinfo=UTC)
ORG = uuid.uuid4()
WEB = uuid.uuid4()
API = uuid.uuid4()


class _Result(list[Any]):
    def all(self) -> list[Any]:
        return list(self)


class _Nested:
    async def __aenter__(self) -> _Nested:
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False


class _AssetDb:
    """Answers the asset lookup by name key, then the sources of the assets found."""

    def __init__(self, assets: dict[str, list[uuid.UUID]], sources: dict[uuid.UUID, str], case: Any = None):
        self.assets = assets
        self.sources = sources
        self.case = case
        self.added: list[object] = []
        self.reads = 0

    def begin_nested(self) -> _Nested:
        return _Nested()

    def add(self, row: object) -> None:
        self.added.append(row)

    async def flush(self) -> None:
        return None

    async def get(self, _model: object, _id: object) -> Any:
        return self.case

    async def execute(self, statement: Any) -> _Result:
        self.reads += 1
        if self.reads % 2 == 1:
            keys = statement.whereclause.clauses[1].right.value
            rows = [(aid, name, key) for key in keys for name, ids in self.assets.items() for aid in ids if key == name]
            return _Result(rows)
        return _Result([(aid, src) for aid, src in self.sources.items()])


def _case(**extra: Any) -> Any:
    base = {
        "id": uuid.uuid4(),
        "organization_id": ORG,
        "remediation_kind": "upgrade",
        "state": "waiting_decision",
        "validations": [],
        "validation_plan": False,
        "product_label": "Example Server",
        "fixed_version": "2.0",
        "resolution_kind": None,
    }
    base.update(extra)
    return SimpleNamespace(**base)


@pytest.mark.asyncio
async def test_two_resolving_run_order_hosts_are_attached_as_inferred() -> None:
    db = _AssetDb({"web-1.example": [WEB], "api-1.example": [API]}, {WEB: "openaev", API: "openaev"})
    case = _case()
    await loop.record_validation(
        cast(Any, db),
        cast(Any, case),
        provider="openaev",
        kind="validation",
        ref="sim-1",
        result="pending",
        run_order=["web-1.example", "API-1.example "],
        now=NOW,
    )
    entry = case.validations[-1]
    assert [a["asset_id"] for a in entry["assets"]] == [str(WEB), str(API)]
    assert {a["provenance"] for a in entry["assets"]} == {case_assets.PROVENANCE_INFERRED}
    assert entry["assets"][0]["sources"] == ["openaev"]
    assert "unresolved" not in entry

    row: dict[str, Any] = {"asset_count": 0, "remediation_kind": "upgrade", "validations": case.validations}
    version_history.with_asset_count(row, [])
    assert row["asset_count"] == 2 and row["asset_count_source"] == case_assets.ATTACHED_SOURCE

    merged = case_assets.merged_assets([{"assets": [], "open": True}], case_assets.attached_assets(case.validations))
    assert [(a["name"], a["provenance"]) for a in merged] == [
        ("web-1.example", case_assets.PROVENANCE_INFERRED),
        ("api-1.example", case_assets.PROVENANCE_INFERRED),
    ]


@pytest.mark.asyncio
async def test_an_unresolved_run_order_name_attaches_nothing_and_is_flagged() -> None:
    db = _AssetDb({}, {})
    case = _case()
    await loop.record_validation(
        cast(Any, db),
        cast(Any, case),
        provider="openaev",
        kind="validation",
        ref="sim-2",
        result="pending",
        run_order=["ghost.example"],
        now=NOW,
    )
    entry = case.validations[-1]
    assert "assets" not in entry and entry["unresolved"] == ["ghost.example"]
    assert case_assets.attached_assets(case.validations) == []


def test_an_ambiguous_name_is_never_guessed() -> None:
    entry: dict[str, Any] = {}
    case_assets.attach_run_order(entry, ["shared"], {}, actor="a", at=NOW.isoformat())
    assert entry == {"unresolved": ["shared"]}


@pytest.mark.asyncio
async def test_the_remediation_gate_names_the_attached_hosts_as_inferred() -> None:
    validations = [
        {
            "provider": "openaev",
            "ref": "sim-1",
            "assets": [
                {"asset_id": str(WEB), "name": "web-1.example", "provenance": case_assets.PROVENANCE_INFERRED},
                {"asset_id": str(API), "name": "api-1.example", "provenance": case_assets.PROVENANCE_INFERRED},
            ],
        }
    ]
    case = _case(validations=validations)
    db = _AssetDb({}, {}, case=case)
    ask = {"question": "Upgrade?", "context": "Both backends are exposed."}
    out = await case_assets.complete_gate_ask(cast(Any, db), f"ctem:remediation:{case.id}", ask)
    assert out is not None
    first = str(out["context"]).splitlines()[0]
    assert first.startswith("Assets:")
    assert "web-1.example (inferred from a name match)" in first
    assert "api-1.example (inferred from a name match)" in first
    # Another item's ask is untouched; a card that has the line keeps its own words.
    assert await case_assets.complete_gate_ask(cast(Any, db), "dev:x", ask) == ask
    assert await case_assets.complete_gate_ask(cast(Any, db), f"ctem:remediation:{case.id}", out) == out


def test_a_case_whose_issues_name_assets_keeps_its_issue_count() -> None:
    validations = [{"assets": [{"asset_id": str(WEB), "name": "web-1.example", "provenance": "name_match_inferred"}]}]
    row: dict[str, Any] = {"asset_count": 3, "remediation_kind": "upgrade", "validations": validations}
    version_history.with_asset_count(row, [])
    assert row["asset_count"] == 3 and row["asset_count_source"] == "issues"


def test_the_case_page_adds_attached_hosts_the_issues_do_not_name() -> None:
    answer = {"assets": [{"name": "web-1.example", "issue_count": 1}], "asset_count": 1, "version_source": "none"}
    attached = [
        {"asset_id": str(WEB), "name": "WEB-1.example", "provenance": case_assets.PROVENANCE_INFERRED},
        {"asset_id": str(API), "name": "api-1.example", "provenance": case_assets.PROVENANCE_INFERRED},
    ]
    out = case_assets.with_attached(answer, attached)
    assert [r["name"] for r in out["assets"]] == ["web-1.example", "api-1.example"]
    assert out["assets"][0]["provenance"] == case_assets.PROVENANCE_ISSUE
    assert out["assets"][1]["provenance"] == case_assets.PROVENANCE_INFERRED
    assert out["asset_count"] == 2
