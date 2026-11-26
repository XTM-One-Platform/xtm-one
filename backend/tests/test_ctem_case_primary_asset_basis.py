"""A case's primary asset count separates proven from inferred links (#2396).

A primary asset a signal reaches only through an inferred path (an inventory link of
source ``rule``, a name match, a primary asset named in the evidence) is counted in
``inferred_primary_asset_count``, never in ``primary_asset_count``, which counts the
primary assets a ``manual`` or ``opencrq`` link proves. The signal row of the case
lists the primary assets it reached with their basis, so the rows explain the counts.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from app.models.ctem_case import CtemCase, CtemCaseIssue
from app.services import ctem_cases as rules
from app.services import ctem_cases_view as view_of
from app.services import ctem_scope_gate as gate
from app.services.ctem_primary_assets import decision_primary_assets, link_basis

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


def _hop(source: str) -> dict[str, Any]:
    return {"primary_asset_key": "payments platform", "asset": "srv-a", "through": "srv-a", "source": source}


def _decision(*entries: dict[str, Any]) -> dict[str, Any]:
    return {"scope": {"sources": [{"from": "asset", "name": "srv-a", "criticality": "high"}, *entries]}}


def _signal(external_id: str, decision: dict[str, Any]) -> rules.SourceIssue:
    reached = decision_primary_assets(decision)
    return rules.SourceIssue(
        source="signal",
        external_id=external_id,
        name="CVE-2026-1",
        primary_assets=tuple(j["name"] for j in reached),
        inferred_primary_assets=tuple(j["name"] for j in reached if j["basis"] == "inferred"),
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


def _row_links(rendered: dict[str, Any]) -> dict[str, str]:
    return {j["name"]: j["basis"] for row in rendered["issues"] for j in row.get("primary_asset_links") or []}


def test_link_basis_proven_only_on_a_recorded_link() -> None:
    assert link_basis([_hop("rule")]) == "inferred"
    assert link_basis(None) == "inferred"
    assert link_basis([_hop("rule"), _hop("manual")]) == "proven"
    assert link_basis([_hop("opencrq")]) == "proven"


def test_a_rule_link_only_case_counts_the_primary_asset_as_inferred() -> None:
    decision = _decision(
        {"from": "primary_asset", "name": "Payments Platform", "criticality": "critical", "chain": [_hop("rule")]}
    )
    rendered = _read(_signal("s-1", decision))
    assert rendered["primary_asset_count"] == 0
    assert rendered["inferred_primary_asset_count"] == 1
    assert rendered["primary_asset_basis"] == view_of.PRIMARY_ASSET_BASIS
    signal_row = next(r for r in rendered["issues"] if r["source"] == "signal")
    assert signal_row["primary_assets"] == ["Payments Platform"]
    assert signal_row["primary_asset_links"] == [{"name": "Payments Platform", "basis": "inferred"}]


def test_a_name_match_without_chain_is_inferred() -> None:
    decision = _decision({"from": "primary_asset", "name": "Payments Platform", "criticality": "critical"})
    rendered = _read(_signal("s-1", decision))
    assert rendered["primary_asset_count"] == 0 and rendered["inferred_primary_asset_count"] == 1


def test_a_manual_or_opencrq_link_counts_the_primary_asset_as_proven() -> None:
    for source in ("manual", "opencrq"):
        decision = _decision(
            {"from": "primary_asset", "name": "Payments Platform", "criticality": "critical", "chain": [_hop(source)]}
        )
        rendered = _read(_signal("s-1", decision))
        assert rendered["primary_asset_count"] == 1, source
        assert rendered["inferred_primary_asset_count"] == 0, source
        assert _row_links(rendered) == {"Payments Platform": "proven"}


def test_a_recorded_basis_wins_over_the_kept_chain() -> None:
    """The gate writes ``basis`` from every chain before the cut; the read trusts it."""
    decision = _decision(
        {
            "from": "primary_asset",
            "name": "Payments Platform",
            "criticality": "critical",
            "chain": [_hop("rule")],
            "basis": "proven",
        }
    )
    assert decision_primary_assets(decision) == [{"name": "Payments Platform", "basis": "proven"}]


def test_a_primary_asset_one_member_proves_is_proven_for_the_case() -> None:
    inferred = _signal(
        "s-1",
        _decision(
            {"from": "primary_asset", "name": "Payments Platform", "criticality": "high", "chain": [_hop("rule")]}
        ),
    )
    proven = _signal(
        "s-2",
        _decision(
            {"from": "primary_asset", "name": "Payments Platform", "criticality": "high", "chain": [_hop("manual")]}
        ),
    )
    rendered = _read(inferred, proven)
    assert rendered["primary_asset_count"] == 1 and rendered["inferred_primary_asset_count"] == 0


def test_the_rows_explain_the_case_counts() -> None:
    """For one viewer, the rows' primary assets are the case's proven plus inferred ones."""
    members = [
        _signal(
            "s-1",
            _decision(
                {
                    "from": "primary_asset",
                    "name": "Payments Platform",
                    "criticality": "critical",
                    "chain": [_hop("rule")],
                },
                {"from": "primary_asset", "name": "HR records", "criticality": "high", "chain": [_hop("opencrq")]},
            ),
        ),
        rules.SourceIssue(source="opencti", external_id="1", name="CVE-2026-2", primary_assets=("Billing",)),
    ]
    rendered = _read(*members)
    links = _row_links(rendered)
    assert set(links) == {"Payments Platform", "HR records", "Billing"}
    assert sum(1 for b in links.values() if b == "proven") == rendered["primary_asset_count"] == 2
    assert sum(1 for b in links.values() if b == "inferred") == rendered["inferred_primary_asset_count"] == 1


def test_a_view_cached_before_2396_reads_every_primary_asset_as_proven() -> None:
    """A stored issue without ``inferred_primary_assets`` keeps its former count."""
    stored = view_of._issue_json(
        rules.SourceIssue(source="opencti", external_id="1", name="CVE-2026-2", primary_assets=("Billing",))
    )
    stored.pop("inferred_primary_assets")
    issue = view_of._issue_from_json(stored)
    assert issue.inferred_primary_assets == ()
    assert rules.aggregates([issue])["primary_asset_count"] == 1


def test_the_by_primary_asset_filter_keeps_inferred_links() -> None:
    issue = _signal(
        "s-1",
        _decision(
            {"from": "primary_asset", "name": "Payments Platform", "criticality": "high", "chain": [_hop("rule")]}
        ),
    )
    case = _case()
    rows = [CtemCaseIssue(case_id=case.id, source="signal", external_id="s-1", open=True)]
    view = view_of.ViewerView(issues={issue.member_id: issue}, open_ids={issue.member_id})
    assert view_of.reaches_primary_asset(rows, view, "payments platform") is True


def test_the_decision_record_says_proven_or_inferred_when_a_primary_asset_sets_criticality() -> None:
    sources = [
        {"from": "asset", "name": "srv-a", "criticality": "high"},
        {"from": "primary_asset", "name": "Payments Platform", "criticality": "critical", "basis": "inferred"},
    ]
    assert gate._primary_asset_basis(sources, "critical") == "inferred"
    assert gate._primary_asset_basis(sources, "high") is None
    sources.append({"from": "primary_asset", "name": "HR records", "criticality": "critical", "basis": "proven"})
    assert gate._primary_asset_basis(sources, "critical") == "proven"
