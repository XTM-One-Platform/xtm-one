# SPDX-License-Identifier: Apache-2.0

"""CTEM reconcile and scope gate (#847): the pure rules, on fixtures."""

from __future__ import annotations

from dataclasses import replace
from datetime import date
from types import SimpleNamespace

import pytest

from app.services import ctem_scope_gate as gate
from app.services import ctem_scope_gate_store as store
from app.services import organization_context as oc

TODAY = date(2026, 11, 21)


def _profile(**policy) -> oc.OrganizationProfile:
    return oc.parse_profile(
        {
            "primary_assets": [
                {
                    "name": "Customer SaaS",
                    "criticality": "critical",
                    "asset_names": ["DMZ"],
                    "opencrq_asset_id": "crq-saas",
                },
                {"name": "Wiki", "criticality": "medium", "asset_names": ["wiki.example.com"]},
            ],
            "software": [{"product": "nginx", "vendor": "f5"}, {"product": "Confluence"}],
            "assets_exposure": [
                {"name": "shop.example.com", "group": "DMZ", "internet_facing": "yes", "products": ["nginx"]},
                {"name": "wiki.example.com", "internet_facing": "no", "products": ["Confluence"], "tags": ["lab"]},
                {"name": "db-1", "criticality": "very_high", "source": "opencrq", "external_id": "sa-42"},
                {"name": "printer-3", "criticality": "low"},
            ],
            "asset_groups": [{"name": "DMZ", "internet_facing": "yes"}],
            "prioritization_policy": policy,
        }
    )


def _facts(path="new_exposure", lens="outside", kind="asset", ref="https://SHOP.example.com:443/login", **evidence):
    return gate.SignalFacts(
        trigger_path=path, lens=lens, subject_kind=kind, subject_ref=ref, source_provider="manual", evidence=evidence
    )


def test_stable_key_joins_a_url_a_hostname_and_a_trailing_dot():
    assert gate.stable_key("https://Shop.Example.com:443/login?x=1") == "shop.example.com"
    assert gate.stable_key("shop.example.com.") == "shop.example.com"
    assert gate.stable_key("10.0.0.1") == "10.0.0.1"
    assert gate.stable_key("arn:aws:ec2:eu-west-1:1:instance/i-1") == "arn:aws:ec2:eu-west-1:1:instance/i-1"
    assert gate.stable_key("") == ""


def test_in_scope_at_or_above_the_threshold_opens_a_case_with_its_severity_and_sla():
    decision = gate.decide(_profile(), _facts(), today=TODAY)
    assert decision.outcome == "case" and decision.state == "confirmed"
    assert decision.record["scope"]["criticality"] == "critical"
    assert decision.record["scope"]["threshold"] == "high"
    # critical (4) + one lens, low confidence (0) = 4 points: P2, 72 h.
    assert decision.severity == "P2" and decision.record["sla_hours"] == 72
    assert decision.record["reconcile"]["assets"] == ["shop.example.com"]
    assert ("primary_asset", "Customer SaaS") in {(s["from"], s["name"]) for s in decision.record["scope"]["sources"]}


def test_two_lenses_raise_the_confidence_and_a_proof_makes_it_high():
    related = [gate.RelatedSignal(id="s2", lens="inside", source_provider="openaev")]
    two = gate.decide(_profile(), _facts(), related=related, today=TODAY)
    assert two.record["reconcile"]["confidence"] == "medium" and two.severity == "P1"
    proven = gate.decide(_profile(), _facts(path="proven_exploitable", lens="inside"), today=TODAY)
    assert proven.record["reconcile"]["confidence"] == "high" and proven.record["reconcile"]["proven"] is True


def test_below_the_threshold_goes_to_the_watchlist_with_the_threshold_named():
    decision = gate.decide(_profile(), _facts(ref="printer-3"), today=TODAY)
    assert decision.outcome == "watchlist" and decision.state == "watchlist"
    assert "below the scope threshold high" in decision.reason
    assert decision.severity is None
    lowered = gate.decide(_profile(scope_min_criticality="low"), _facts(ref="printer-3"), today=TODAY)
    assert lowered.outcome == "case" and lowered.severity == "P3"


def _aikido_profile() -> oc.OrganizationProfile:
    return oc.parse_profile(
        {
            "primary_assets": [
                {"name": "Product source code", "criticality": "critical", "asset_names": ["filigran/portal"]}
            ],
            "assets_exposure": [{"name": "filigran/portal", "source": "aikido", "external_id": "991"}],
        }
    )


def test_an_aikido_issue_on_a_repository_mapped_to_a_primary_asset_opens_a_case():
    """CTEM-25: a repository is an asset row (source aikido, external_id), linked to its primary asset by name."""
    facts = gate.SignalFacts(
        trigger_path="code_flaw",
        lens="inside",
        subject_kind="code",
        subject_ref="lodash",
        source_provider="aikido",
        source_external_id="7",
        evidence={"assets": ["991"], "affected_package": "lodash", "severity": "critical"},
    )
    decision = gate.decide(_aikido_profile(), facts, today=TODAY)
    assert decision.outcome == "case"
    assert ("primary_asset", "Product source code") in {
        (s["from"], s["name"]) for s in decision.record["scope"]["sources"]
    }
    unmapped = gate.SignalFacts(
        trigger_path="code_flaw",
        lens="inside",
        subject_kind="code",
        subject_ref="lodash",
        source_provider="aikido",
        source_external_id="8",
        evidence={"assets": ["filigran/sandbox"], "affected_package": "lodash"},
    )
    assert gate.decide(_aikido_profile(), unmapped, today=TODAY).outcome == "to_validate"


def test_an_unresolved_subject_is_to_validate():
    decision = gate.decide(_profile(), _facts(ref="unknown.example.net"), today=TODAY)
    assert decision.outcome == "to_validate"
    assert decision.record["reconcile"]["keys"] == ["unknown.example.net"]


def test_a_contradicted_signal_is_discarded():
    related = [gate.RelatedSignal(id="s9", lens="inside", source_provider="openaev", verdict="not_exposed")]
    decision = gate.decide(_profile(), _facts(), related=related, today=TODAY)
    assert decision.outcome == "discarded" and "s9" in decision.reason
    own = gate.decide(_profile(), _facts(verdict="false_positive"), today=TODAY)
    assert own.outcome == "discarded"


def test_a_policy_exclusion_discards_and_an_accepted_risk_goes_to_the_watchlist():
    excluded = gate.decide(
        _profile(exclusions=[{"kind": "tag", "value": "lab", "reason": "test"}], scope_min_criticality="low"),
        _facts(ref="wiki.example.com"),
        today=TODAY,
    )
    assert excluded.outcome == "discarded" and "tag lab" in excluded.reason
    accepted = gate.decide(
        _profile(accepted_risks=[{"subject": "CVE-2026-12345", "owner": "CISO", "expires_on": "2027-01-31"}]),
        _facts(path="new_vulnerability", lens="world", kind="software", ref="nginx", cve="CVE-2026-12345"),
        today=TODAY,
    )
    assert accepted.outcome == "watchlist" and "CISO" in accepted.reason
    expired = gate.decide(
        _profile(accepted_risks=[{"subject": "CVE-2026-12345", "owner": "CISO", "expires_on": "2026-01-01"}]),
        _facts(path="new_vulnerability", lens="world", kind="software", ref="nginx", cve="CVE-2026-12345"),
        today=TODAY,
    )
    assert expired.outcome == "case"


def test_software_resolves_through_the_tech_stack_and_a_cpe():
    decision = gate.decide(
        _profile(),
        _facts(path="new_vulnerability", lens="world", kind="software", ref="cpe:2.3:a:f5:nginx:1.27.1"),
        today=TODAY,
    )
    assert decision.record["reconcile"]["products"] == ["nginx"]
    assert "shop.example.com" in decision.record["reconcile"]["assets"]
    assert decision.outcome == "case"


def test_a_threat_resolves_to_the_tech_stack_through_its_targets_and_matched_fields():
    """#1720: a campaign naming a tech stack product is no longer subject_unresolved."""
    by_field = gate.decide(
        _profile(),
        _facts(
            path="relevant_threat",
            lens="world",
            kind="threat",
            ref="campaign--74c27bbf",
            matched_fields=["sector (Technology)", "products (nginx, AWS)"],
        ),
        today=TODAY,
    )
    assert by_field.record["reconcile"]["products"] == ["nginx"]
    assert by_field.record["reconcile"]["software_refs"] == ["nginx", "AWS"]
    assert "shop.example.com" in by_field.record["reconcile"]["assets"]
    assert by_field.record["reason_code"] != "subject_unresolved"
    by_relationship = gate.decide(
        _profile(),
        _facts(
            path="relevant_threat",
            lens="world",
            kind="threat",
            ref="intrusion-set--1",
            relationships=[
                {"relationship_type": "targets", "target": "Confluence"},
                {"type": "located-at", "name": "nginx"},
            ],
        ),
        today=TODAY,
    )
    assert by_relationship.record["reconcile"]["products"] == ["Confluence"]
    assert gate.threat_software({"uses": "nginx", "targets": ["Confluence"]}) == ["Confluence", "nginx"]


def test_an_unresolved_threat_names_how_many_software_references_were_tried():
    decision = gate.decide(
        _profile(),
        _facts(path="relevant_threat", lens="world", kind="threat", ref="campaign--x", targets=["Oracle"]),
        today=TODAY,
    )
    assert decision.record["reason_code"] == "subject_unresolved"
    assert decision.record["reason_params"] == {"keys": 1, "software": 1}
    assert decision.record["reconcile"]["software_refs"] == ["Oracle"]


def test_cve_thresholds_route_to_the_watchlist_and_kev_passes_them():
    low = gate.decide(
        _profile(cvss_min=7.0, epss_min=0.5),
        _facts(
            path="new_vulnerability",
            lens="world",
            kind="vulnerability",
            ref="CVE-2026-2",
            software="nginx",
            cvss=9.2,
            epss=0.41,
        ),
        today=TODAY,
    )
    assert low.outcome == "watchlist" and "EPSS 0.41 below the policy minimum 0.5" in low.reason
    kev = gate.decide(
        _profile(cvss_min=7.0, epss_min=0.5),
        _facts(
            path="new_vulnerability",
            lens="world",
            kind="vulnerability",
            ref="CVE-2026-2",
            software="nginx",
            cvss=9.2,
            epss=0.41,
            kev=True,
        ),
        today=TODAY,
    )
    assert kev.outcome == "case" and kev.record["policy_checks"][0]["rule"] == "kev_always_in"


def _cve(**evidence):
    return _facts(
        path="new_vulnerability", lens="world", kind="vulnerability", ref="CVE-2026-21762", software="nginx", **evidence
    )


def test_2631_a_cvss_in_the_listener_evidence_key_passes_and_is_recorded():
    # The OpenCTI listener writes cvss_base_score (x_opencti_ prefix removed): the gate read null before #2631.
    decision = gate.decide(_profile(cvss_min=7.0), _cve(cvss_base_score=8.8), today=TODAY)
    assert decision.outcome == "case"
    check = next(c for c in decision.record["policy_checks"] if c["rule"] == "cvss_min")
    assert check == {"rule": "cvss_min", "value": 8.8, "threshold": 7.0, "passed": True, "source": "evidence"}
    advisory = gate.decide(_profile(cvss_min=7.0), _cve(cvss={"score": 8.8}), today=TODAY)
    assert advisory.outcome == "case"


def test_2631_a_score_below_the_minimum_opens_no_case_and_names_it():
    decision = gate.decide(_profile(cvss_min=7.0), _cve(cvss_base_score=5.3), today=TODAY)
    assert decision.outcome == "watchlist" and decision.state == "watchlist"
    assert decision.reason == "CVSS 5.3 below the policy minimum 7.0"
    assert decision.record["reason_code"] == "score_below_policy"
    epss = gate.decide(_profile(epss_min=0.1), _cve(epss_score=0.02), today=TODAY)
    assert epss.outcome == "watchlist" and epss.record["reason_params"]["score"] == "EPSS"


def test_2631_the_opencti_score_is_read_when_the_evidence_has_none():
    facts = replace(_cve(), cve_cvss=5.3, score_lookup=gate.LOOKUP_READ)
    decision = gate.decide(_profile(cvss_min=7.0), facts, today=TODAY)
    assert decision.outcome == "watchlist"
    check = decision.record["policy_checks"][0]
    assert check["value"] == 5.3 and check["source"] == "opencti" and check["passed"] is False
    assert decision.record["reconcile"]["score_lookup"] == gate.LOOKUP_READ
    # The evidence wins over OpenCTI.
    first = replace(_cve(cvss=8.8), cve_cvss=5.3)
    assert gate.decide(_profile(cvss_min=7.0), first, today=TODAY).outcome == "case"
    # A KEV flag OpenCTI holds passes the thresholds like one in the evidence.
    kev = replace(_cve(), cve_cvss=5.3, cve_kev=True)
    assert gate.decide(_profile(cvss_min=7.0), kev, today=TODAY).outcome == "case"


def test_2631_no_score_anywhere_is_held_for_validation_never_a_silent_pass():
    held = gate.decide(_profile(cvss_min=7.0), _cve(), today=TODAY)
    assert held.outcome == "watchlist" and held.record["reason_code"] == "score_missing"
    assert held.reason.startswith("no score read")
    check = held.record["policy_checks"][0]
    assert check["note"] == "no score read" and check["passed"] is False and check["missing_score"] == "hold"
    passed = gate.decide(_profile(cvss_min=7.0, missing_score="pass"), _cve(), today=TODAY)
    assert passed.outcome == "case"
    check = passed.record["policy_checks"][0]
    assert check["note"] == "no score read" and check["passed"] is True


def test_2631_no_minimum_set_reads_nothing_and_holds_nothing():
    assert oc.PrioritizationPolicy().missing_score == "hold"
    assert gate.decide(_profile(), _cve(), today=TODAY).outcome == "case"
    assert store._needs_scores(_profile(), _cve()) == []
    assert store._needs_scores(_profile(cvss_min=7.0), _cve(cvss=8.8, epss=0.3)) == []
    assert store._needs_scores(_profile(cvss_min=7.0), _cve()) == ["CVE-2026-21762"]


def test_2631_the_highest_opencti_score_of_the_named_cves_is_kept():
    from app.services import ctem_cve_software

    nodes: list[dict[str, object]] = [
        {"x_opencti_cvss_base_score": 5.3, "x_opencti_epss_score": None, "x_opencti_cisa_kev": False},
        {"x_opencti_cvss_base_score": "8.8", "x_opencti_epss_score": 0.4, "x_opencti_cisa_kev": True},
    ]
    assert ctem_cve_software.highest_scores(nodes) == (8.8, 0.4, True)
    assert ctem_cve_software.highest_scores([]) == (None, None, False)


def test_internet_exposure_and_primary_asset_minimum_are_honoured():
    inside = gate.decide(_profile(internet_exposure_required=True), _facts(lens="inside", ref="db-1"), today=TODAY)
    assert inside.outcome == "watchlist" and "internet" in inside.reason
    pa_row = gate.decide(
        _profile(min_primary_asset_criticality="critical", scope_min_criticality="medium"),
        _facts(ref="wiki.example.com"),
        today=TODAY,
    )
    assert pa_row.outcome == "watchlist" and "primary asset at or above critical" in pa_row.reason


def test_risk_scenarios_add_criticality_and_can_be_required():
    index = gate.ScenarioIndex(
        connected=True,
        note="",
        by_asset_id={"crq-saas": [gate.RiskScenarioRef(id="rs1", name="Data theft", criticality="critical")]},
    )
    with_scenario = gate.decide(_profile(require_risk_scenario=True), _facts(), scenarios=index, today=TODAY)
    assert with_scenario.outcome == "case"
    assert with_scenario.record["scope"]["risk_scenarios"] == [{"id": "rs1", "name": "Data theft"}]
    missing = gate.decide(_profile(require_risk_scenario=True), _facts(), today=TODAY)
    assert missing.outcome == "watchlist" and "OpenCRQ not read" in missing.reason
    by_external_id = gate.ScenarioIndex(
        connected=True, note="", by_asset_id={"sa-42": [gate.RiskScenarioRef(id="rs2", name="Outage")]}
    )
    db = gate.decide(
        _profile(require_risk_scenario=True), _facts(lens="inside", ref="db-1"), scenarios=by_external_id, today=TODAY
    )
    assert db.outcome == "case"


def test_every_outcome_carries_a_reason_code_the_frontend_translates():
    # #851 UX round 2 B4: the loop UI reads reason_code and reason_params, never the English line.
    related = [gate.RelatedSignal(id="s9", lens="inside", source_provider="openaev", verdict="not_exposed")]
    cases = {
        "in_scope": gate.decide(_profile(), _facts(), today=TODAY),
        "below_scope_threshold": gate.decide(_profile(), _facts(ref="printer-3"), today=TODAY),
        "subject_unresolved": gate.decide(_profile(), _facts(ref="unknown.example.net"), today=TODAY),
        "contradicted_by_signals": gate.decide(_profile(), _facts(), related=related, today=TODAY),
        "contradicted_by_evidence": gate.decide(_profile(), _facts(verdict="false_positive"), today=TODAY),
        "internet_exposure_required": gate.decide(
            _profile(internet_exposure_required=True), _facts(lens="inside", ref="db-1"), today=TODAY
        ),
        "risk_scenario_unreadable": gate.decide(_profile(require_risk_scenario=True), _facts(), today=TODAY),
        # CTEM-29: a third party nothing depends on, a leaked credential of a disabled account.
        "no_dependency": gate.decide(
            _profile(),
            gate.SignalFacts(
                trigger_path="third_party_event", lens="world", subject_kind="third_party", subject_ref="Unrelated Ltd"
            ),
            today=TODAY,
        ),
        "account_disabled": gate.decide(
            _profile(),
            gate.SignalFacts(
                trigger_path="credential_leak",
                lens="world",
                subject_kind="identity",
                subject_ref="jdoe@corp.example",
                evidence={"account_disabled": True},
            ),
            today=TODAY,
        ),
    }
    for code, decision in cases.items():
        assert decision.record["reason_code"] == code, decision.reason
        assert isinstance(decision.record["reason_params"], dict)
    assert cases["below_scope_threshold"].record["reason_params"] == {"criticality": "low", "threshold": "high"}
    assert cases["contradicted_by_signals"].record["reason_params"] == {"count": 1}


def test_the_decision_record_lists_every_input():
    decision = gate.decide(_profile(cvss_min=7.0), _facts(cvss=9.0), today=TODAY)
    record = decision.record
    assert record["version"] == gate.DECISION_VERSION
    assert set(record) >= {
        "inputs",
        "reconcile",
        "scope",
        "policy_checks",
        "severity",
        "sla_hours",
        "outcome",
        "state",
        "reason",
    }
    assert set(record["inputs"]) >= {
        "trigger_path",
        "lens",
        "subject",
        "source_provider",
        "verdict",
        "cves",
        "related_signals",
    }
    assert set(record["reconcile"]) >= {
        "keys",
        "assets",
        "asset_groups",
        "primary_assets_named",
        "products",
        "lenses",
        "confidence",
        "proven",
        "contradicted_by",
    }
    assert record["scope"]["opencrq"] == {"connected": False, "note": "OpenCRQ not read"}


@pytest.mark.parametrize(
    ("criticality", "confidence", "expected"),
    [
        ("critical", "high", "P1"),
        ("critical", "medium", "P1"),
        ("critical", "low", "P2"),
        ("high", "high", "P1"),
        ("high", "medium", "P2"),
        ("high", "low", "P3"),
        ("medium", "high", "P2"),
        ("low", "high", "P3"),
    ],
)
def test_the_severity_formula(criticality, confidence, expected):
    assert gate.severity_of(criticality, confidence) == expected


def test_new_policy_fields_have_their_defaults_and_count_only_when_set():
    policy = oc.PrioritizationPolicy()
    assert policy.scope_min_criticality == "high" and policy.require_risk_scenario is False
    assert (policy.sla_hours.p1, policy.sla_hours.p2, policy.sla_hours.p3) == (24, 72, 720)
    assert oc.exposure_counts(oc.parse_profile({}))["prioritization_policy"] == 0
    changed = oc.parse_profile(
        {"prioritization_policy": {"scope_min_criticality": "critical", "sla_hours": {"p1": 12}}}
    )
    assert oc.exposure_counts(changed)["prioritization_policy"] == 2
    with pytest.raises(ValueError):
        oc.parse_profile({"prioritization_policy": {"scope_min_criticality": "extreme"}})


def test_a_scenario_weighs_by_loss_against_tolerance():
    assert store._scenario_criticality({"annualLossExposure": 3, "riskTolerance": 2}) == "critical"
    assert store._scenario_criticality({"annualLossExposure": 1, "riskTolerance": 2}) == "high"
    assert store._scenario_criticality({"annualLossExposure": 0.01, "riskTolerance": 2}) == "low"
    assert store._scenario_criticality({}) == gate.SCENARIO_DEFAULT_CRITICALITY


async def test_evaluate_writes_state_and_decision_and_leaves_a_case_alone(monkeypatch):
    async def _no_related(_db, _row):
        return []

    monkeypatch.setattr(store, "related_signals", _no_related)
    row = SimpleNamespace(
        id="x",
        organization_id="o",
        state="new",
        trigger_path="new_exposure",
        lens="outside",
        subject_kind="asset",
        subject_ref="shop.example.com",
        source_provider="manual",
        source_external_id=None,
        subject_label=None,
        evidence={},
        decision=None,
        updated_at=None,
    )
    decision = await store.evaluate(None, row, profile=_profile())
    assert decision is not None and row.state == "confirmed" and row.decision["outcome"] == "case"
    assert "decided_at" in row.decision
    row.state = "case_opened"
    assert await store.evaluate(None, row, profile=_profile()) is None
