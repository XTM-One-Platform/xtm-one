"""CTEM Cases: grouping by shared remediation, recompute and the state machine (#11545).

The rules of dev-docs/ctem-cases.md sections 4 to 6, on fixtures: one fix is
one case, nothing is invented, the user's work survives a recompute.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.models.ctem_case import CASE_STATES, CtemCase, CtemCaseEvent, CtemCaseIssue
from app.services import ctem_cases as cc

NOW = datetime(2026, 11, 13, 12, 0, tzinfo=UTC)
OWNER = "6f1c2c3e-0000-4000-8000-000000000001"


def _cti(ext: str, cve: str, *, product="Tomcat", vendor="Apache", fixed="9.0.98", **kw) -> cc.SourceIssue:
    return cc.SourceIssue(
        source="opencti", external_id=ext, name=cve, cve=cve, product=product, vendor=vendor, fixed_version=fixed, **kw
    )


def _aev(ext: str, cve: str, **kw) -> cc.SourceIssue:
    return cc.SourceIssue(source="openaev", external_id=ext, name=cve, cve=cve, **kw)


# ---------------------------------------------------------------------------
# Grouping (section 4.2)
# ---------------------------------------------------------------------------


def test_same_product_and_fixed_version_make_one_case():
    grouping = cc.group_issues([_cti("v1", "CVE-2026-1"), _cti("v2", "CVE-2026-2"), _cti("v3", "CVE-2026-3")])
    assert list(grouping.groups) == ["upgrade:apache/tomcat@9.0.98"]
    assert len(grouping.groups["upgrade:apache/tomcat@9.0.98"]) == 3
    assert grouping.remediations["upgrade:apache/tomcat@9.0.98"].title == "Upgrade Apache Tomcat to 9.0.98"


def test_two_fixed_versions_make_two_cases():
    grouping = cc.group_issues([_cti("v1", "CVE-2026-1"), _cti("v2", "CVE-2026-2", fixed="10.1.35")])
    assert sorted(grouping.groups) == ["upgrade:apache/tomcat@10.1.35", "upgrade:apache/tomcat@9.0.98"]


def test_keys_are_normalized():
    grouping = cc.group_issues(
        [
            _cti("v1", "CVE-1", product=" Tomcat ", vendor="APACHE"),
            _cti("v2", "CVE-2", product="tomcat", vendor="apache"),
        ]
    )
    assert list(grouping.groups) == ["upgrade:apache/tomcat@9.0.98"]


def test_a_course_of_action_wins_over_a_version():
    issue = _cti("v1", "CVE-1", course_of_action_id="course-of-action--abc", course_of_action_name="Disable AJP")
    grouping = cc.group_issues([issue])
    assert list(grouping.groups) == ["coa:course-of-action--abc"]
    assert grouping.remediations["coa:course-of-action--abc"].kind == "course_of_action"
    assert grouping.remediations["coa:course-of-action--abc"].title == "Disable AJP"


def test_a_product_without_a_fixed_version_is_an_upgrade_to_a_fixed_version():
    grouping = cc.group_issues([_cti("v1", "CVE-1", fixed=None)])
    remediation = grouping.remediations["upgrade:apache/tomcat@latest"]
    assert remediation.title == "Upgrade Apache Tomcat to a fixed version"
    assert remediation.fixed_version is None


def test_no_known_fix_is_counted_never_forced_into_a_case():
    lonely = cc.SourceIssue(source="opencti", external_id="v9", name="CVE-9", cve="CVE-9")
    grouping = cc.group_issues([lonely, _cti("v1", "CVE-1")])
    assert grouping.no_known_fix == [lonely]
    assert all(lonely not in members for members in grouping.groups.values())


def test_an_openaev_finding_joins_the_case_of_its_cve():
    grouping = cc.group_issues([_cti("v1", "CVE-2026-1"), _aev("f1", "cve-2026-1"), _aev("f2", "CVE-2026-77")])
    members = grouping.groups["upgrade:apache/tomcat@9.0.98"]
    assert [m.member_id for m in members] == ["opencti:v1", "openaev:f1"]
    assert [m.member_id for m in grouping.no_known_fix] == ["openaev:f2"]


def test_a_duplicate_issue_is_kept_once():
    grouping = cc.group_issues([_cti("v1", "CVE-1"), _cti("v1", "CVE-1")])
    assert len(grouping.groups["upgrade:apache/tomcat@9.0.98"]) == 1


# ---------------------------------------------------------------------------
# Recompute (section 4.3)
# ---------------------------------------------------------------------------


def test_a_new_key_becomes_a_new_case():
    plans = cc.plan_recompute([], [_cti("v1", "CVE-1")])
    assert len(plans) == 1
    assert plans[0].case_id is None and plans[0].new_state is None
    assert plans[0].remediation.key == "upgrade:apache/tomcat@9.0.98"
    assert plans[0].add == ["opencti:v1"]


def _case(state="remediating", members=None, **kw) -> cc.CaseSnapshot:
    return cc.CaseSnapshot(
        id="c1", keys=["upgrade:apache/tomcat@9.0.98"], state=state, members=dict(members or {}), **kw
    )


def test_recompute_adds_members_and_never_sets_owner_or_state():
    (plan,) = cc.plan_recompute([_case(members={"opencti:v1": True})], [_cti("v1", "CVE-1"), _cti("v2", "CVE-2")])
    assert plan.case_id == "c1" and plan.new_state is None
    assert plan.add == ["opencti:v2"] and plan.reopen == [] and plan.resolve == []
    # A plan carries membership and system transitions only: nothing for owner, due date or notes.
    assert set(vars(plan)) == {
        "case_id",
        "remediation",
        "add",
        "reopen",
        "resolve",
        "new_state",
        "reason",
        "evidence",
        "add_keys",
    }


def test_a_narrower_view_never_closes_or_shrinks_a_case():
    """#11613: Bob cannot see v1 and v2; his empty view leaves Alice's case as it is."""
    (plan,) = cc.plan_recompute([_case(members={"opencti:v1": True, "opencti:v2": True})], [])
    assert plan.add == plan.reopen == plan.resolve == [] and plan.new_state is None


def test_a_member_closes_only_when_the_readers_source_confirms_it():
    case = _case(members={"opencti:v1": True, "opencti:v2": True})
    (plan,) = cc.plan_recompute([case], [_cti("v2", "CVE-2")], closed=["opencti:v1"])
    assert plan.resolve == ["opencti:v1"] and plan.new_state is None


def test_a_case_whose_members_all_closed_is_resolved_with_evidence():
    case = _case(members={"opencti:v1": True})
    (plan,) = cc.plan_recompute([case], [], closed=["opencti:v1"])
    assert plan.new_state == "resolved"
    assert plan.evidence == cc.CLOSED_AT_SOURCE


def test_an_accepted_risk_keeps_its_state_when_its_members_close():
    (plan,) = cc.plan_recompute([_case("accepted_risk", {"opencti:v1": True})], [], closed=["opencti:v1"])
    assert plan.new_state is None


def test_a_resolved_case_reopens_when_its_issues_return():
    case = _case("resolved", {"opencti:v1": False})
    (plan,) = cc.plan_recompute([case], [_cti("v1", "CVE-1"), _cti("v2", "CVE-2")])
    assert plan.new_state == "new"
    assert plan.reopen == ["opencti:v1"] and plan.add == ["opencti:v2"]


def test_a_decided_closure_holds_while_its_issues_stay_open_unchanged() -> None:
    """#1646: a gate answered not exploitable; the same open issues never reopen the case."""
    case = _case("resolved", {"opencti:v1": True}, decided_closure="gate close_not_exploitable")
    (plan,) = cc.plan_recompute([case], [_cti("v1", "CVE-1")])
    assert plan.new_state is None
    assert plan.add == [] and plan.reopen == []


def test_a_decided_closure_reopens_on_a_new_issue_and_names_the_decision() -> None:
    case = _case("resolved", {"opencti:v1": True}, decided_closure="gate close_not_exploitable")
    (plan,) = cc.plan_recompute([case], [_cti("v1", "CVE-1"), _cti("v2", "CVE-2")])
    assert plan.new_state == "new"
    assert plan.add == ["opencti:v2"]
    assert "gate close_not_exploitable" in (plan.reason or "")


def test_a_decided_closure_reopens_when_a_closed_issue_returns() -> None:
    case = _case("resolved", {"opencti:v1": True, "opencti:v2": False}, decided_closure="ticket ITS-1 done")
    (plan,) = cc.plan_recompute([case], [_cti("v1", "CVE-1"), _cti("v2", "CVE-2")])
    assert plan.new_state == "new"
    assert plan.reopen == ["opencti:v2"]


def test_only_a_resolution_recompute_did_not_write_is_a_decided_closure() -> None:
    from types import SimpleNamespace
    from uuid import uuid4

    from app.services.ctem_cases_store import _decided_closures

    def state(to: str, evidence: str | None) -> SimpleNamespace:
        return SimpleNamespace(kind="state", to_value=to, evidence=evidence, reason=None)

    by_gate, by_source, reopened_then_source = uuid4(), uuid4(), uuid4()
    out = _decided_closures(
        {
            by_gate: [state("resolved", "gate close_not_exploitable")],
            by_source: [state("resolved", cc.CLOSED_AT_SOURCE)],
            reopened_then_source: [
                state("resolved", "gate close_not_exploitable"),
                state("new", None),
                state("resolved", cc.FIXED_IN_INVENTORY),
            ],
        }
    )
    assert out == {by_gate: "gate close_not_exploitable"}


def test_a_case_fixed_on_every_asset_resolves_while_its_issues_stay_open_at_source() -> None:
    """#1252: the inventory shows no asset below the fix, the case leaves the open cases."""
    case = _case(members={"opencti:v1": True}, fixed_in_inventory=True)
    (plan,) = cc.plan_recompute([case], [_cti("v1", "CVE-1")])
    assert plan.new_state == "resolved"
    assert plan.evidence == cc.FIXED_IN_INVENTORY
    # The member stays as the source says: the case leaves the scope, the issue is not closed.
    assert plan.resolve == []


def test_a_case_fixed_in_the_inventory_is_not_reopened_by_its_open_issues() -> None:
    case = _case("resolved", {"opencti:v1": True}, fixed_in_inventory=True)
    (plan,) = cc.plan_recompute([case], [_cti("v1", "CVE-1")])
    assert plan.new_state is None


def test_a_case_fixed_in_the_inventory_with_a_validation_plan_waits_for_its_proof() -> None:
    case = _case(members={"opencti:v1": True}, fixed_in_inventory=True, validation_plan=True)
    (plan,) = cc.plan_recompute([case], [_cti("v1", "CVE-1")])
    assert plan.new_state is None


def test_a_merge_maps_every_merged_key_to_the_surviving_case():
    case = cc.CaseSnapshot(id="c1", keys=["upgrade:apache/tomcat@9.0.98", "upgrade:apache/tomcat@10.1.35"], state="new")
    plans = cc.plan_recompute([case], [_cti("v1", "CVE-1"), _cti("v2", "CVE-2", fixed="10.1.35")])
    assert len(plans) == 1
    assert plans[0].add == ["opencti:v1", "opencti:v2"]


def test_a_split_keeps_its_pinned_issues_out_of_the_automatic_case():
    auto = cc.CaseSnapshot(id="c1", keys=["upgrade:apache/tomcat@9.0.98"], state="new")
    split = cc.CaseSnapshot(
        id="c2", keys=["split:c2"], state="remediating", pinned_issues=["opencti:v2"], members={"opencti:v2": True}
    )
    plans = {p.case_id: p for p in cc.plan_recompute([auto, split], [_cti("v1", "CVE-1"), _cti("v2", "CVE-2")])}
    assert plans["c1"].add == ["opencti:v1"]
    assert plans["c2"].add == [] and plans["c2"].new_state is None


# ---------------------------------------------------------------------------
# State machine (section 6)
# ---------------------------------------------------------------------------


def _check(**kw):
    base = {"current": "new", "actor_kind": "user", "owner_user_id": OWNER, "now": NOW}
    base.update(kw)
    cc.check_transition(**base)


def test_accepted_risk_needs_owner_reason_and_expiry():
    until = NOW + timedelta(days=30)
    _check(target="accepted_risk", reason="compensating control", accepted_until=until)
    with pytest.raises(cc.CaseRuleError, match="owner"):
        _check(target="accepted_risk", owner_user_id=None, reason="r", accepted_until=until)
    with pytest.raises(cc.CaseRuleError, match="reason"):
        _check(target="accepted_risk", reason="  ", accepted_until=until)
    with pytest.raises(cc.CaseRuleError, match="expiry"):
        _check(target="accepted_risk", reason="r", accepted_until=None)


def test_accepted_risk_expiry_is_in_the_future_and_bounded():
    with pytest.raises(cc.CaseRuleError, match="future"):
        _check(target="accepted_risk", reason="r", accepted_until=NOW)
    with pytest.raises(cc.CaseRuleError, match="90 days"):
        _check(target="accepted_risk", reason="r", accepted_until=NOW + timedelta(days=91))


def test_an_agent_cannot_accept_risk():
    with pytest.raises(cc.CaseRuleError, match="only a person"):
        _check(target="accepted_risk", actor_kind="agent", reason="r", accepted_until=NOW + timedelta(days=7))


def test_resolve_needs_evidence():
    with pytest.raises(cc.CaseRuleError, match="evidence"):
        _check(target="resolved", current="remediating")
    _check(target="resolved", current="remediating", evidence="CHG-1234")
    _check(target="resolved", current="remediating", actor_kind="agent", evidence="validation run 42")


def test_a_person_moving_to_remediating_names_an_owner():
    with pytest.raises(cc.CaseRuleError, match="owner"):
        _check(target="remediating", owner_user_id=None)
    _check(target="remediating")
    _check(target="remediating", owner_user_id=None, actor_kind="agent")


def test_unknown_or_same_state_is_refused():
    with pytest.raises(cc.CaseRuleError, match="unknown"):
        _check(target="closed")
    with pytest.raises(cc.CaseRuleError, match="already"):
        _check(target="new")


def test_an_agent_reopens_only_with_a_reason():
    with pytest.raises(cc.CaseRuleError, match="reason"):
        _check(target="new", current="resolved", actor_kind="agent")
    _check(target="new", current="resolved", actor_kind="agent", reason="finding seen again")


def test_expiry_returns_the_case_to_new():
    assert cc.expired_acceptance("accepted_risk", NOW - timedelta(seconds=1), NOW)
    assert cc.expired_acceptance("accepted_risk", NOW, NOW)
    assert not cc.expired_acceptance("accepted_risk", NOW + timedelta(days=1), NOW)
    assert not cc.expired_acceptance("resolved", NOW - timedelta(days=1), NOW)


# ---------------------------------------------------------------------------
# Priority and aggregates
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("issue", "priority"),
    [
        (_cti("v1", "CVE-1", kev=True, cvss=5.0), "critical"),
        (_cti("v1", "CVE-1", cvss=9.8), "critical"),
        (_cti("v1", "CVE-1", cvss=7.5), "high"),
        (_cti("v1", "CVE-1", cvss=3.0, epss=0.6), "high"),
        (_cti("v1", "CVE-1", cvss=5.0), "medium"),
        (_cti("v1", "CVE-1", cvss=2.0), "low"),
    ],
    ids=["kev", "cvss-9", "cvss-7", "epss-0.5", "cvss-4", "low"],
)
def test_default_priority(issue, priority):
    assert cc.default_priority([issue]) == priority


def test_aggregates_count_distinct_assets_and_primary_assets():
    members = [
        _cti("v1", "CVE-1", cvss=9.8, epss=0.9, kev=True, assets=("web-1", "web-2"), primary_assets=("billing",)),
        _cti("v2", "CVE-2", cvss=6.5, assets=("web-2",), first_seen_at=NOW - timedelta(days=3)),
    ]
    agg = cc.aggregates(members)
    assert agg == {
        "issue_count": 2,
        "asset_count": 2,
        "primary_asset_count": 1,
        "kev_count": 1,
        "max_cvss": 9.8,
        "max_epss": 0.9,
        "first_seen_at": NOW - timedelta(days=3),
    }


# ---------------------------------------------------------------------------
# Model
# ---------------------------------------------------------------------------


def test_the_model_carries_the_lifecycle_and_the_accepted_risk_constraint():
    assert CASE_STATES == (
        "new",
        "analyzing",
        "waiting_decision",
        "remediating",
        "validating",
        "mitigated_watching",
        "resolved",
        "accepted_risk",
    )
    names = {c.name for c in CtemCase.__table__.constraints}
    assert {"ck_ctem_cases_state_valid", "ck_ctem_cases_accepted_risk_complete"} <= names
    assert CtemCaseIssue.__table__.c.case_id.foreign_keys
    assert CtemCaseEvent.__table__.c.case_id.foreign_keys
