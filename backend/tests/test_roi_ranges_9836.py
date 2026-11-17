"""ROI everywhere, slice R4a (#9836, ``dev-docs/roi-outcomes.md`` 10.5 and 10.6).

Every estimated figure of a report carries its low / high range, computed from
the catalog's low and high manual minutes with the same review minutes, rate
and model cost; the likely figures are unchanged; a masked cost masks its range
too; the unit facts carry the low / high minutes and their source, so "How this
is computed" reproduces a figure from the report alone.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest

from app.services import roi, roi_report

A1 = uuid.uuid4()
NOW = datetime(2026, 11, 8, 12, 0, tzinfo=UTC)
TRIAGE = roi.BASELINES_BY_UNIT["threat_report_triaged"]  # 10 / 20 / 45 manual min, 3 review min


def _rate() -> float:
    return roi.HOURLY_RATES[TRIAGE.rate_role]["USD"].amount


def test_the_range_brackets_the_likely_figures_and_follows_the_catalog():
    f = roi.compute_roi([roi.RunMeasure("threat_report_triaged", units=6, llm_cost=1.0)], currency="USD")
    assert f.minutes_saved == pytest.approx(6 * (TRIAGE.manual_min_likely - TRIAGE.review_min))
    assert f.minutes_saved_low == pytest.approx(6 * (TRIAGE.manual_min_low - TRIAGE.review_min))
    assert f.minutes_saved_high == pytest.approx(6 * (TRIAGE.manual_min_high - TRIAGE.review_min))
    assert f.value_low == pytest.approx(round(f.minutes_saved_low / 60 * _rate(), 2))
    assert f.value_high == pytest.approx(round(f.minutes_saved_high / 60 * _rate(), 2))
    assert f.net_value_low == pytest.approx(round(f.value_low - 1.0, 2))
    assert f.net_value_high == pytest.approx(round(f.value_high - 1.0, 2))
    assert f.value_low <= f.value <= f.value_high
    assert f.roi_low is not None and f.roi_high is not None and f.roi_low <= f.roi <= f.roi_high


def test_the_likely_figures_are_unchanged_by_the_range():
    measures = [
        roi.RunMeasure("threat_report_triaged", units=6, llm_cost=1.0),
        roi.RunMeasure("cve_assessed", units=10, llm_cost=0.5, review_min=12),
        roi.RunMeasure("cve_assessed", units=3, llm_cost=0.2, outcome=roi.OUTCOME_REJECTED),
    ]
    f = roi.compute_roi(measures, currency="USD", period_days=30)
    likely = sum(
        max(
            roi.BASELINES_BY_UNIT[m.unit].manual_min_likely * m.units
            - (m.review_min if m.review_min is not None else roi.BASELINES_BY_UNIT[m.unit].review_min * m.units),
            0,
        )
        for m in measures
        if m.outcome != roi.OUTCOME_REJECTED
    )
    assert f.minutes_saved == pytest.approx(round(likely, 1))
    assert f.net_value == pytest.approx(round(f.value - f.llm_cost, 2))


def test_payback_is_shortest_at_the_high_end():
    f = roi.compute_roi(
        [roi.RunMeasure("threat_report_triaged", units=60, llm_cost=0.0)],
        currency="USD",
        period_days=30,
        setup_cost=1000,
    )
    assert f.payback_months_low is not None and f.payback_months_high is not None
    assert f.payback_months_low <= f.payback_months <= f.payback_months_high


def test_no_cost_no_roi_at_either_end():
    f = roi.compute_roi([roi.RunMeasure("threat_report_triaged", units=1, llm_cost=0.0)], currency="USD")
    assert f.roi is None and f.roi_low is None and f.roi_high is None


def _report():
    currency, rates, label = roi_report.rates_for("USD")
    return roi_report.build_report(
        [roi_report.RecordRow(A1, None, roi.RunMeasure("threat_report_triaged", units=6, llm_cost=1.0))],
        window="30d",
        since=roi_report.window_since("30d", now=NOW),
        currency=currency,
        rates=rates,
        rate_label=label,
        agent_names={A1: "Threat relevance"},
    )


def test_the_report_answers_the_range_of_every_estimated_figure():
    data = roi_report.report_dict(_report())
    org = data["organization"]
    assert set(org["range"]) == {"minutes_saved", "hours_saved", "value", "net_value", "roi", "payback_months"}
    low, high = org["range"]["value"]
    assert low <= org["value"] <= high
    low_h, high_h = org["range"]["hours_saved"]
    assert low_h <= org["hours_saved"] <= high_h
    assert data["agents"][0]["range"]["value"] == org["range"]["value"]


def test_a_masked_cost_masks_its_range_never_zero():
    report = _report()
    report.cost_masked = True
    org = roi_report.report_dict(report)["organization"]
    for key in roi_report.COST_FIELDS:
        assert org[key] is None
        if key in org["range"]:
            assert org["range"][key] == [None, None], key
    assert None not in org["range"]["value"] and None not in org["range"]["hours_saved"]


def test_the_unit_fact_carries_its_range_and_source():
    f = roi.compute_roi([roi.RunMeasure("threat_report_triaged", units=1, llm_cost=0.1)], currency="USD")
    fact = next(x for x in f.assumption_facts if x["kind"] == "unit")
    assert fact["manual_min_low"] == TRIAGE.manual_min_low
    assert fact["manual_min_high"] == TRIAGE.manual_min_high
    assert fact["source"] == TRIAGE.minutes_source
