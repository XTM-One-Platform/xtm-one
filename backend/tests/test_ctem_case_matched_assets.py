"""The assets the gate matched reach the case, and an unmatched case says why it opened (#2635).

A signal member carries the inventory assets its gate decision matched (the
reconcile's assets and the assets of the chains that reached a primary asset),
so a case opened from it counts them (``asset_count``) and lists them on its
signal row; its primary assets keep their proven / inferred basis (#2396).
A case that matched no asset and no primary asset names what put it in scope
(``opened_on``, the kinds of the decision's scope sources). Read from the
decision at each view build: the cases opened before this change are fixed
without a backfill.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from app.models.ctem_case import CtemCase, CtemCaseIssue
from app.services import ctem_cases as rules
from app.services import ctem_cases_view as view_of
from app.services.ctem_primary_assets import decision_assets, decision_primary_assets, decision_scope_kinds

NOW = datetime.now(UTC)


def _case() -> CtemCase:
    return CtemCase(
        id=uuid.uuid4(),
        organization_id=uuid.uuid4(),
        vertical="cyber",
        title="Upgrade Acme Browser",
        remediation_kind="upgrade",
        keys=["upgrade:acme-browser"],
        pinned_issues=[],
        state="new",
        state_since=NOW,
        severity="P1",
    )


def _hop(asset: str, source: str = "rule") -> dict[str, Any]:
    return {"primary_asset_key": "payments platform", "asset": asset, "through": asset, "source": source}


def _signal(external_id: str, decision: dict[str, Any]) -> rules.SourceIssue:
    """The member ``ctem_cases_source._signal_members`` builds from a stored decision."""
    reached = decision_primary_assets(decision)
    return rules.SourceIssue(
        source="signal",
        external_id=external_id,
        name="CVE-2026-1",
        assets=tuple(decision_assets(decision)),
        primary_assets=tuple(j["name"] for j in reached),
        inferred_primary_assets=tuple(j["name"] for j in reached if j["basis"] == "inferred"),
        scope_kinds=tuple(decision_scope_kinds(decision)),
    )


def _read(*issues: rules.SourceIssue) -> dict[str, Any]:
    case = _case()
    rows = [CtemCaseIssue(case_id=case.id, source=i.source, external_id=i.external_id, open=True) for i in issues]
    view = view_of.ViewerView(
        issues={i.member_id: i for i in issues},
        open_ids={i.member_id for i in issues},
        signals={i.member_id for i in issues if i.source == "signal"},
    )
    rendered = view_of.hydrate(case, rows, [], view)
    assert rendered is not None
    return rendered


IN_SCOPE = {
    "reconcile": {"assets": ["srv-a", "srv-a"], "primary_assets": [_hop("laptop-042")]},
    "scope": {
        "sources": [
            {"from": "asset", "name": "srv-a", "criticality": "high"},
            {
                "from": "primary_asset",
                "name": "Payments Platform",
                "criticality": "critical",
                "basis": "inferred",
                "chain": [_hop("laptop-042"), _hop("laptop-077")],
            },
            {
                "from": "primary_asset",
                "name": "Payroll",
                "criticality": "high",
                "basis": "proven",
                "chain": [_hop("srv-a", "manual")],
            },
        ]
    },
}


def test_decision_assets_reads_the_reconcile_and_the_chains_once_each() -> None:
    assert decision_assets(IN_SCOPE) == ["srv-a", "laptop-042", "laptop-077"]
    assert decision_assets({}) == [] and decision_assets(None) == [] and decision_assets({"reconcile": "x"}) == []


def test_a_case_opened_from_matched_assets_counts_them_and_its_primary_assets_by_basis() -> None:
    rendered = _read(_signal("s-1", IN_SCOPE))
    assert rendered["asset_count"] == 3
    assert rendered["primary_asset_count"] == 1
    assert rendered["inferred_primary_asset_count"] == 1
    assert rendered["opened_on"] == []
    row = next(r for r in rendered["issues"] if r["source"] == "signal")
    assert row["primary_asset_links"] == [
        {"name": "Payments Platform", "basis": "inferred"},
        {"name": "Payroll", "basis": "proven"},
    ]


def test_inferred_primary_assets_alone_are_never_a_silent_zero() -> None:
    decision = {
        "scope": {
            "sources": [
                {"from": "primary_asset", "name": "Payments Platform", "criticality": "critical", "basis": "inferred"}
            ]
        }
    }
    rendered = _read(_signal("s-1", decision))
    assert rendered["primary_asset_count"] == 0
    assert rendered["inferred_primary_asset_count"] == 1
    assert rendered["opened_on"] == []


def test_a_case_where_nothing_matched_names_what_opened_it() -> None:
    decision = {
        "scope": {
            "sources": [
                {"from": "owned_domain", "name": "example.com", "criticality": "high"},
                {"from": "risk_scenario", "name": "Ransomware", "criticality": "critical"},
                {"from": "owned_domain", "name": "example.org", "criticality": "high"},
            ]
        }
    }
    rendered = _read(_signal("s-1", decision))
    assert rendered["asset_count"] == 0
    assert rendered["primary_asset_count"] == 0 and rendered["inferred_primary_asset_count"] == 0
    assert rendered["opened_on"] == ["owned_domain", "risk_scenario"]


def test_a_view_cached_before_the_change_reads_without_scope_kinds() -> None:
    issue = _signal("s-1", IN_SCOPE)
    stored = view_of._issue_json(issue)
    assert stored["scope_kinds"] == ["asset", "primary_asset"]
    stored.pop("scope_kinds")
    assert view_of._issue_from_json(stored).scope_kinds == ()
