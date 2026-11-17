"""ROI core: catalog, unit declaration, unit count and figures (issue #8990)."""

import pytest

from app.services import roi


def test_catalog_has_the_ctem_units_with_ordered_minutes():
    # 20 units of #8990 plus the posture fleet and indicator decay units of #9836 R2.
    assert len(roi.CTEM_BASELINES) == 27
    for b in roi.CTEM_BASELINES:
        assert b.manual_min_low <= b.manual_min_likely <= b.manual_min_high
        assert 0 <= b.review_min < b.manual_min_likely
        assert b.rate_role in roi.CTEM_HOURLY_RATES
        assert "assumption" in b.minutes_label


def test_corporate_catalog_has_45_units_with_ordered_minutes_and_its_own_roles():
    """Copilot mode's built-in catalog (#9550, office units of #10006 S3, governance units of #10412,
    chat deliverable units of #10544)."""
    assert len(roi.CORPORATE_BASELINES) == 45
    assert len(roi.CORPORATE_HOURLY_RATES) == 11
    assert {b.fleet for b in roi.CORPORATE_BASELINES} == {
        "Executive office",
        "Sales",
        "Legal",
        "Finance",
        "HR",
        "IT / support",
        "Marketing",
        "Engineering",
        "Operations",
        "Security",
        "Product",
    }
    for b in roi.CORPORATE_BASELINES:
        assert b.manual_min_low <= b.manual_min_likely <= b.manual_min_high
        assert 0 <= b.review_min < b.manual_min_likely
        assert b.rate_role in roi.CORPORATE_HOURLY_RATES
        assert b.minutes_label == roi.LABEL_ASSUMPTION
    for role, by_currency in roi.CORPORATE_HOURLY_RATES.items():
        assert set(by_currency) == {"USD", "EUR"}
        for rate in by_currency.values():
            assert rate.role == role and rate.amount > 0 and rate.source
    assert roi.CORPORATE_HOURLY_RATES["legal_counsel"]["USD"].amount == 109.66
    assert roi.CORPORATE_HOURLY_RATES["manager"]["EUR"].amount == 50.76


def test_the_office_fleet_units_are_seeded_on_existing_roles():
    """#10006 S3: the units the office desks deliver, on seeded rate roles only."""
    expected = {
        "change_delivered_pr_merged": "software_engineer",
        "bug_fixed_pr_merged": "software_engineer",
        "ui_review": "software_engineer",
        "release_notes_written": "software_engineer",
        "meeting_scheduled_or_moved": "executive_assistant",
        "executive_digest_prepared": "executive_assistant",
        "governance_cycle_prepared": "manager",
        "press_quote_drafted": "marketing_specialist",
        "security_alert_triaged": "security_analyst",
        "vulnerability_assessed": "security_analyst",
        "compliance_evidence_collected": "security_analyst",
        "access_review_completed": "security_analyst",
        "policy_reviewed": "security_analyst",
        "vendor_risk_assessed": "security_analyst",
        "sharing_exposure_reviewed": "security_analyst",
        "legal_request_triaged": "legal_counsel",
        "product_spec_drafted": "knowledge_worker",
    }
    by_unit = {b.unit: b for b in roi.CORPORATE_BASELINES}
    for unit, role in expected.items():
        assert by_unit[unit].rate_role == role
        assert by_unit[unit].minutes_label == roi.LABEL_ASSUMPTION
    # The security analyst rate is the published occupation of XTM One mode's analyst rate.
    assert (
        roi.CORPORATE_HOURLY_RATES["security_analyst"]["USD"].amount == roi.CTEM_HOURLY_RATES["analyst"]["USD"].amount
    )
    # A merged change at the likely baseline: (240 - 30) min at the software engineer rate.
    figures = roi.compute_roi([roi.RunMeasure("change_delivered_pr_merged", 1, llm_cost=0.0)], currency="USD")
    assert figures.minutes_saved == 210
    assert figures.value == round(210 / 60 * 93.40, 2)


def test_a_copilot_fleet_may_declare_an_xtm_one_security_unit():
    """The lookups know both catalogs: a CTEM unit declared in Copilot mode keeps its figures."""
    assert "cve_assessed" in roi.BASELINES_BY_UNIT
    figures = roi.compute_roi([roi.RunMeasure("cve_assessed", 1, llm_cost=0.0)], currency="USD")
    assert figures.minutes_saved > 0


def test_the_two_catalogs_share_no_key_and_lookups_know_both():
    ctem = {b.unit for b in roi.CTEM_BASELINES}
    corporate = {b.unit for b in roi.CORPORATE_BASELINES}
    assert not ctem & corporate
    assert not set(roi.CTEM_HOURLY_RATES) & set(roi.CORPORATE_HOURLY_RATES)
    assert set(roi.BASELINES_BY_UNIT) == ctem | corporate
    assert set(roi.HOURLY_RATES) == set(roi.CTEM_HOURLY_RATES) | set(roi.CORPORATE_HOURLY_RATES)


def test_each_mode_lists_its_own_catalog_and_fallback_role():
    assert roi.builtin_baselines(xtm_one=True) is roi.CTEM_BASELINES
    assert roi.builtin_baselines(xtm_one=False) is roi.CORPORATE_BASELINES
    assert roi.builtin_rates(xtm_one=True) is roi.CTEM_HOURLY_RATES
    assert roi.builtin_rates(xtm_one=False) is roi.CORPORATE_HOURLY_RATES
    assert roi.fallback_rate_role(xtm_one=True) == "analyst"
    assert roi.fallback_rate_role(xtm_one=False) in roi.CORPORATE_HOURLY_RATES


def test_a_corporate_unit_is_valued_at_its_role_rate():
    figures = roi.compute_roi([roi.RunMeasure("contract_first_review", 2, llm_cost=1.0)], currency="USD")
    # 2 x (60 - 15) min at the legal counsel rate.
    assert figures.minutes_saved == 90
    assert figures.value == round(90 / 60 * 109.66, 2)


def test_every_built_in_unit_has_ordered_minutes():
    for b in roi.BASELINES:
        assert b.manual_min_low <= b.manual_min_likely <= b.manual_min_high
        assert 0 <= b.review_min < b.manual_min_likely
        assert b.rate_role in roi.HOURLY_RATES
        assert "assumption" in b.minutes_label


def test_only_the_cve_low_bound_is_published():
    published = [b.unit for b in roi.CTEM_BASELINES if "published" in b.minutes_label]
    assert published == ["cve_assessed"]


def test_rates_carry_their_source():
    assert roi.HOURLY_RATES["analyst"]["USD"].amount == 88.73
    assert roi.HOURLY_RATES["ciso"]["EUR"].amount == 52.67
    for by_currency in roi.HOURLY_RATES.values():
        for rate in by_currency.values():
            assert rate.source


@pytest.mark.parametrize(
    "schema",
    [
        {"type": "object", "x-xtm-roi": {"unit": "cve_assessed", "count": "cves"}},
        {"type": "json_schema", "definition": {"x-xtm-roi": {"unit": "cve_assessed", "count": "cves"}}},
    ],
    ids=["agent-shape", "dataset-shape"],
)
def test_unit_declaration_reads_both_shapes(schema):
    assert roi.unit_declaration(schema) == roi.UnitDeclaration("cve_assessed", "cves")
    assert roi.declaration_error(schema) is None


def test_declaration_absent_or_invalid():
    assert roi.unit_declaration(None) is None
    assert roi.unit_declaration({"type": "object"}) is None
    assert roi.declaration_error({"type": "object"}) is None
    assert "non-empty" in roi.declaration_error({"x-xtm-roi": {"unit": ""}})
    assert "not a known unit" in roi.declaration_error({"x-xtm-roi": {"unit": "nope", "count": "a"}})
    assert roi.declaration_error({"x-xtm-roi": {"unit": "mine", "count": "a"}}, known_units={"mine"}) is None


def test_count_units_reads_the_structured_output_not_the_claim():
    decl = roi.UnitDeclaration("cve_assessed", "result.cves")
    assert roi.count_units(decl, {"result": {"cves": [1, 2, 3]}, "summary": "I assessed 50 CVEs"}) == 3
    assert roi.count_units(decl, {"result": {"cves": 4}}) == 4
    assert roi.count_units(decl, {"result": {"cves": -2}}) == 0
    assert roi.count_units(decl, {"result": {"cves": True}}) == 0
    assert roi.count_units(decl, {"result": {}}) == 0
    assert roi.count_units(decl, "plain text") == 0


def test_count_units_counts_one_artefact():
    decl = roi.UnitDeclaration("stix_bundle_built", "id")
    assert roi.count_units(decl, {"id": "bundle--1"}) == 1
    assert roi.count_units(decl, {"id": "   "}) == 0
    assert roi.count_units(decl, {"id": {"a": 1}}) == 1
    assert roi.count_units(decl, {"id": {}}) == 0
    assert roi.count_units(decl, {"id": 1.5}) == 0


def test_compute_roi_with_baseline_review():
    figures = roi.compute_roi(
        [roi.RunMeasure("threat_report_triaged", units=6, llm_cost=1.0)], currency="USD", period_days=30
    )
    # 6 x (20 - 3) = 102 minutes, 1.7 h x 88.73
    assert figures.minutes_saved == 102.0
    assert figures.hours_saved == 1.7
    assert figures.value == round(102 / 60 * 88.73, 2)
    assert figures.net_value == round(figures.value - 1.0, 2)
    assert figures.roi == round((102 / 60 * 88.73 - 1.0) / 1.0, 2)
    assert figures.payback_months == 0.0
    assert figures.units_by_kind == {"threat_report_triaged": 6}
    assert any("assumption" in a for a in figures.assumptions)
    assert any("88.73 USD/h" in a for a in figures.assumptions)


def test_measured_review_rejected_run_and_unknown_unit():
    figures = roi.compute_roi(
        [
            roi.RunMeasure("briefing_written", units=1, llm_cost=0.5, review_min=60),
            roi.RunMeasure("cve_assessed", units=10, llm_cost=0.2, outcome=roi.OUTCOME_REJECTED),
            roi.RunMeasure("custom_unit", units=3, llm_cost=0.3),
        ],
        currency="EUR",
    )
    assert figures.runs == 3
    assert figures.units == 1
    assert figures.minutes_saved == 180.0
    assert figures.value == round(3 * 52.67, 2)
    assert figures.llm_cost == 1.0
    assert any("measured" in a for a in figures.assumptions)
    assert any("custom_unit" in a for a in figures.assumptions)


def test_payback_with_setup_cost_and_no_positive_net():
    figures = roi.compute_roi(
        [roi.RunMeasure("cve_assessed", units=60, llm_cost=0.0)], currency="USD", period_days=30, setup_cost=1000
    )
    # 60 x 17 min = 17 h x 88.73 = 1508.41 per 30 days
    assert figures.payback_months == round(1000 / 1508.41, 2)
    assert figures.roi is None
    empty = roi.compute_roi([], setup_cost=1000, period_days=30)
    assert empty.payback_months is None and empty.net_value == 0


def test_review_counts_and_unreviewed_runs_counted_like_accepted():
    accepted = roi.compute_roi([roi.RunMeasure("threat_report_triaged", units=6, llm_cost=1.0)])
    unreviewed = roi.compute_roi([roi.RunMeasure("threat_report_triaged", units=6, llm_cost=1.0, outcome=None)])
    # Same value: an unreviewed run is never silently dropped, only reported apart.
    assert unreviewed.value == accepted.value and unreviewed.net_value == accepted.net_value
    assert (accepted.reviewed, accepted.unreviewed, accepted.review_rate) == (1, 0, 1.0)
    assert (unreviewed.reviewed, unreviewed.unreviewed, unreviewed.review_rate) == (0, 1, 0.0)

    mixed = roi.compute_roi(
        [
            roi.RunMeasure("threat_report_triaged", units=6, outcome=None),
            roi.RunMeasure("threat_report_triaged", units=6, outcome=roi.OUTCOME_MODIFIED, review_min=30),
            roi.RunMeasure("threat_report_triaged", units=6, outcome=roi.OUTCOME_REJECTED),
        ]
    )
    # 6 x (20 - 3) = 102 for the unreviewed run, 6 x 20 - 30 = 90 for the modified one, 0 rejected.
    assert mixed.minutes_saved == 192.0
    assert (mixed.runs, mixed.reviewed, mixed.unreviewed, mixed.rejected) == (3, 2, 1, 1)
    assert mixed.review_rate == round(2 / 3, 4)
    assert roi.compute_roi([]).review_rate is None


def test_work_item_records_are_counted_apart_from_runs():
    # The agent Overview's review card opens the runs list, which never shows a work item (#9810).
    figures = roi.compute_roi(
        [
            roi.RunMeasure("threat_report_triaged", units=1, outcome=None),
            roi.RunMeasure("threat_report_triaged", units=1, outcome=None, work_item=True),
            roi.RunMeasure("threat_report_triaged", units=1, outcome=roi.OUTCOME_ACCEPTED, work_item=True),
        ]
    )
    assert (figures.runs, figures.unreviewed, figures.work_items, figures.unreviewed_work_items) == (3, 2, 2, 1)
    assert roi.compute_roi([]).work_items == 0


def test_windows_outcomes_and_gain_per_unit():
    assert roi.WINDOWS == {"7d": 7, "30d": 30, "90d": 90, "all": None}
    assert roi.OUTCOME_MODIFIED in roi.OUTCOMES
    assert roi.BASELINES_BY_UNIT["threat_report_triaged"].gained_min_likely == 17


def test_override_is_labelled():
    custom = dict(roi.BASELINES_BY_UNIT)
    figures = roi.compute_roi(
        [roi.RunMeasure("cve_assessed", units=1)], baselines=custom, overridden_units={"cve_assessed"}
    )
    assert any("organization override" in a for a in figures.assumptions)
