"""ROI report: grouping per agent and fleet, rate resolution, CSV and Markdown exports (issue #8990, slice 3)."""

import csv
import io
import uuid
from datetime import UTC, datetime

import pytest

from app.services import roi, roi_report

A1, A2 = uuid.uuid4(), uuid.uuid4()
F1 = uuid.uuid4()
NOW = datetime(2026, 11, 5, 12, 0, tzinfo=UTC)


def _rows():
    return [
        roi_report.RecordRow(A1, F1, roi.RunMeasure("threat_report_triaged", units=6, llm_cost=1.0)),
        roi_report.RecordRow(A2, F1, roi.RunMeasure("cve_assessed", units=10, llm_cost=0.5)),
        roi_report.RecordRow(A2, None, roi.RunMeasure("cve_assessed", units=3, llm_cost=0.2, outcome="rejected")),
    ]


def _report(**kw):
    currency, rates, label = roi_report.rates_for("USD")
    return roi_report.build_report(
        _rows(),
        window="30d",
        since=roi_report.window_since("30d", now=NOW),
        currency=currency,
        rates=rates,
        rate_label=label,
        agent_names={A1: "Threat relevance", A2: "Vuln triage"},
        fleet_names={F1: "CTEM"},
        **kw,
    )


def test_window_since_maps_the_windows_and_refuses_others():
    assert roi_report.window_since("all", now=NOW) is None
    assert (NOW - roi_report.window_since("7d", now=NOW)).days == 7
    with pytest.raises(ValueError, match="window"):
        roi_report.window_since("1y", now=NOW)


def test_rates_use_the_organization_rate_only_in_its_currency():
    currency, rates, label = roi_report.rates_for(None, loaded_hourly_rate=100.0, org_currency="eur")
    assert currency == "EUR" and label == roi.LABEL_OVERRIDE
    assert rates["analyst"]["EUR"].amount == 100.0 and rates["ciso"]["EUR"].amount == 100.0
    currency, rates, label = roi_report.rates_for("USD", loaded_hourly_rate=100.0, org_currency="EUR")
    assert currency == "USD" and label == "catalog default"
    assert rates["analyst"]["USD"].amount == roi.HOURLY_RATES["analyst"]["USD"].amount


def test_unknown_currency_falls_back_to_usd():
    currency, _rates, _label = roi_report.rates_for("JPY")
    assert currency == "USD"


def test_report_sums_the_organization_and_groups_by_agent_and_fleet():
    report = _report()
    org = report.organization
    assert org.runs == 3
    assert org.units == 16  # the rejected run counts no unit
    assert org.llm_cost == pytest.approx(1.7)
    by_agent = {g.name: g.figures for g in report.agents}
    assert by_agent["Threat relevance"].units == 6
    assert by_agent["Vuln triage"].units == 10 and by_agent["Vuln triage"].runs == 2
    assert [g.name for g in report.fleets] == ["CTEM"]
    assert report.fleets[0].figures.units == 16 and report.fleets[0].figures.runs == 2
    assert report.agents[0].figures.net_value >= report.agents[1].figures.net_value
    assert report.estimated is True


def test_report_is_not_estimated_once_every_used_unit_is_overridden():
    report = _report(overridden_units={"threat_report_triaged", "cve_assessed"})
    assert report.estimated is False


def test_report_dict_carries_every_figure_and_assumption():
    data = roi_report.report_dict(_report())
    assert data["window"] == "30d" and data["currency"] == "USD"
    assert data["organization"]["hours_saved"] == _report().organization.hours_saved
    assert data["organization"]["assumptions"]
    assert {a["name"] for a in data["agents"]} == {"Threat relevance", "Vuln triage"}
    # The review counts travel with every group (#9310); the fixture has one rejected run.
    org = data["organization"]
    assert org["reviewed"] + org["unreviewed"] == org["runs"]
    assert org["rejected"] == 1 and org["review_rate"] is not None


def test_report_dict_carries_the_assumptions_as_structured_facts():
    """A client renders the assumptions in its own language from the facts, never the English lines (#9718)."""
    org = roi_report.report_dict(_report())["organization"]
    facts = org["assumption_facts"]
    kinds = [fact["kind"] for fact in facts]
    assert kinds.count("unit") == 2 and "rate" in kinds
    unit = next(fact for fact in facts if fact["kind"] == "unit")
    assert {"unit", "name", "manual_min", "review_min", "label"} <= set(unit)
    rate = next(fact for fact in facts if fact["kind"] == "rate")
    assert rate["currency"] == "USD" and rate["amount"] > 0 and rate["role"] and rate["source"]
    assert len(facts) == len(org["assumptions"])


def test_report_dict_carries_the_computation_with_its_numbers():
    """Every figure says how it is reached: per unit, the total, the review count, the rejected runs (#9810)."""
    org = roi_report.report_dict(_report())["organization"]
    steps = org["computation"]
    kinds = [step["kind"] for step in steps]
    assert kinds == ["saving", "saving", "total", "review", "rejected"]
    savings = [step for step in steps if step["kind"] == "saving"]
    # The rejected run of the fixture counts no unit: cve_assessed counts 10, not 13.
    assert {s["unit"]: s["units"] for s in savings} == {"cve_assessed": 10, "threat_report_triaged": 6}
    for s in savings:
        assert s["minutes_saved"] == pytest.approx(s["manual_min"] - s["review_min"])
        assert s["value"] == pytest.approx(s["minutes_saved"] / 60.0 * s["rate"], abs=0.01)
    total = steps[kinds.index("total")]
    assert total["minutes_saved"] == pytest.approx(sum(s["minutes_saved"] for s in savings), abs=0.1)
    assert total["net_value"] == pytest.approx(total["value"] - total["llm_cost"], abs=0.01)
    assert steps[kinds.index("review")] == {"kind": "review", "reviewed": org["reviewed"], "total": org["runs"]}
    assert steps[-1] == {"kind": "rejected", "count": 1}


def test_computation_is_empty_without_a_run():
    assert roi.compute_roi([]).computation == []


def test_csv_has_one_line_per_scope_and_neutralises_formulas():
    rows = _rows()
    currency, rates, label = roi_report.rates_for("USD")
    report = roi_report.build_report(
        rows,
        window="all",
        since=None,
        currency=currency,
        rates=rates,
        rate_label=label,
        agent_names={A1: "=HYPERLINK(1)", A2: "Vuln triage"},
        fleet_names={F1: "CTEM"},
    )
    lines = list(csv.reader(io.StringIO(roi_report.report_csv(report))))
    figures = lines[: lines.index([])]  # the methodology and assumptions follow a blank line (#9836 R7a)
    assert figures[0][0] == "scope"
    assert [line[0] for line in figures[1:]] == ["organization", "fleet", "agent", "agent"]
    names = [line[2] for line in figures[1:]]
    assert "'=HYPERLINK(1)" in names


def test_markdown_is_the_one_page_value_report():
    md = roi_report.report_markdown(_report(), generated_at=NOW)
    assert md.startswith("# Value delivered by XTM One")
    assert "the last 30 days" in md.lower()
    assert "## By fleet" in md and "## By agent" in md and "## Assumptions" in md
    assert "estimated" in md


def test_ctem_figures_do_not_move_without_an_organization_rate():
    """Zero regression (#9321): with no role rate, the table is the catalog's, figure for figure."""
    _currency, rates, label = roi_report.rates_for("USD")
    assert label == "catalog default"
    for role, by_currency in roi.HOURLY_RATES.items():
        assert rates[role]["USD"] == by_currency["USD"]
    _currency, rates, label = roi_report.rates_for("EUR", loaded_hourly_rate=90.0, org_currency="EUR")
    assert label == roi.LABEL_OVERRIDE and {r["EUR"].amount for r in rates.values()} == {90.0}


def test_a_role_rate_wins_in_its_currency_only():
    legal = roi_report.RoleRate("legal", "USD", 150.0, "payroll 2026")
    analyst = roi_report.RoleRate("analyst", "EUR", 60.0)
    currency, rates, label = roi_report.rates_for(
        "USD", loaded_hourly_rate=90.0, org_currency="USD", role_rates=[legal, analyst]
    )
    assert currency == "USD" and label.endswith(roi_report.LABEL_ROLE_RATES)
    assert rates["legal"]["USD"].amount == 150.0 and rates["legal"]["USD"].source == "payroll 2026"
    # The EUR analyst rate does not apply in USD: the profile's loaded rate does.
    assert rates["analyst"]["USD"].amount == 90.0


def test_a_role_without_a_rate_falls_back_to_the_analyst_rate_and_says_so():
    _currency, rates, _label = roi_report.rates_for("USD", roles={"paralegal"})
    assert rates["paralegal"]["USD"].amount == roi.HOURLY_RATES["analyst"]["USD"].amount
    assert "no rate for role paralegal" in rates["paralegal"]["USD"].source


def test_copilot_mode_falls_back_to_the_knowledge_worker_rate():
    """Copilot mode's corporate catalog (#9550): an unknown role is valued at a corporate role, never a security one."""
    _currency, rates, _label = roi_report.rates_for(
        "USD", roles={"paralegal"}, fallback_role=roi.fallback_rate_role(xtm_one=False)
    )
    assert rates["paralegal"]["USD"].amount == roi.HOURLY_RATES["knowledge_worker"]["USD"].amount
    assert "knowledge_worker rate" in rates["paralegal"]["USD"].source


def test_a_custom_unit_values_at_its_role_rate():
    unit = roi.Baseline("contract_reviewed", "Contract reviewed", "", "Legal", 30, 60, 120, 10, rate_role="legal")
    catalog = {**roi.BASELINES_BY_UNIT, unit.unit: unit}
    currency, rates, label = roi_report.rates_for(
        "USD", role_rates=[roi_report.RoleRate("legal", "USD", 120.0)], roles={"legal"}
    )
    report = roi_report.build_report(
        [roi_report.RecordRow(A1, F1, roi.RunMeasure("contract_reviewed", units=2))],
        window="all",
        since=None,
        currency=currency,
        rates=rates,
        rate_label=label,
        baselines=catalog,
    )
    # 2 x (60 - 10) = 100 min at 120/h.
    assert report.organization.value == pytest.approx(200.0)


def test_the_title_names_xtm_one_in_xtm_one_mode_only():
    assert roi_report.report_title(xtm_one=True) == "Value delivered by XTM One"
    assert "XTM" not in roi_report.report_title(xtm_one=False)
    md = roi_report.report_markdown(_report(), generated_at=NOW, title=roi_report.report_title(xtm_one=False))
    assert md.startswith("# Value delivered by your agents")


def test_a_fleet_report_names_its_fleet_in_the_exports():
    report = _report()
    report.scope, report.scope_id, report.scope_name = roi_report.SCOPE_FLEET, str(F1), "Legal ops"
    md = roi_report.report_markdown(report, generated_at=NOW)
    assert "**Fleet: Legal ops**" in md
    lines = list(csv.reader(io.StringIO(roi_report.report_csv(report))))
    assert lines[1][:3] == ["fleet_total", str(F1), "Legal ops"]
    data = roi_report.report_dict(report)
    assert data["scope"] == "fleet" and data["scope_name"] == "Legal ops"


def test_markdown_escapes_table_cells():
    rows = [roi_report.RecordRow(A1, None, roi.RunMeasure("threat_report_triaged", units=1))]
    currency, rates, label = roi_report.rates_for("USD")
    report = roi_report.build_report(
        rows, window="7d", since=None, currency=currency, rates=rates, rate_label=label, agent_names={A1: "a|<b>"}
    )
    md = roi_report.report_markdown(report, generated_at=NOW)
    assert "a\\|&lt;b&gt;" in md
