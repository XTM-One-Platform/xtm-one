"""ROI everywhere, slice R5a (#9836, ``dev-docs/roi-outcomes.md`` 10.7).

The Value page reads one report: its "By unit of work" rows and its weekly
trend come with it, the unit rows sum to the totals like the agent and fleet
rows do, "This quarter" is a window the API accepts, and a masked cost masks
the unit rows and the trend's net value too (never 0).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from app.services import roi, roi_report

A1 = uuid.uuid4()
A2 = uuid.uuid4()
F1 = uuid.uuid4()
NOW = datetime(2026, 11, 8, 12, 0, tzinfo=UTC)  # a Sunday


def _rows() -> list[roi_report.RecordRow]:
    return [
        roi_report.RecordRow(
            A1, F1, roi.RunMeasure("threat_report_triaged", units=6, llm_cost=1.0), NOW - timedelta(days=1)
        ),
        roi_report.RecordRow(
            A1, F1, roi.RunMeasure("threat_report_triaged", units=2, llm_cost=0.5), NOW - timedelta(days=9)
        ),
        roi_report.RecordRow(A2, None, roi.RunMeasure("cve_assessed", units=10, llm_cost=0.5), NOW - timedelta(days=2)),
        roi_report.RecordRow(
            A2,
            None,
            roi.RunMeasure("cve_assessed", units=3, llm_cost=0.2, outcome=roi.OUTCOME_REJECTED),
            NOW - timedelta(days=16),
        ),
    ]


def _report(window: str = "30d") -> roi_report.RoiReport:
    currency, rates, label = roi_report.rates_for("USD")
    return roi_report.build_report(
        _rows(),
        window=window,
        since=roi_report.window_since(window, now=NOW),
        currency=currency,
        rates=rates,
        rate_label=label,
        agent_names={A1: "Threat relevance", A2: "Vuln triage"},
        fleet_names={F1: "Threat intel"},
        period_days=roi_report.window_days(window, now=NOW),
    )


def test_the_unit_rows_sum_to_the_totals_like_the_agent_rows():
    data = roi_report.report_dict(_report())
    org = data["organization"]
    assert {u["id"] for u in data["units"]} == {"threat_report_triaged", "cve_assessed"}
    assert data["units"][0]["name"] == roi.BASELINES_BY_UNIT[data["units"][0]["id"]].name
    for key in ("runs", "units", "hours_saved", "value", "llm_cost", "net_value"):
        assert sum(u[key] for u in data["units"]) == pytest.approx(org[key], abs=0.02)
        assert sum(a[key] for a in data["agents"]) == pytest.approx(org[key], abs=0.02)
    nets = [u["net_value"] for u in data["units"]]
    assert nets == sorted(nets, reverse=True)


def test_the_trend_is_one_point_per_monday_oldest_first_and_sums_to_the_value():
    data = roi_report.report_dict(_report())
    weeks = [p["week"] for p in data["trend"]]
    assert weeks == sorted(weeks)
    assert all(datetime.fromisoformat(w).weekday() == 0 for w in weeks)
    assert weeks == ["2026-10-19", "2026-10-26", "2026-11-02"]
    assert sum(p["value"] for p in data["trend"]) == pytest.approx(data["organization"]["value"], abs=0.02)
    assert sum(p["runs"] for p in data["trend"]) == data["organization"]["runs"]
    for p in data["trend"]:
        low, high = p["value_range"]
        assert low <= p["value"] <= high


def test_a_record_without_a_time_counts_in_the_totals_not_the_trend():
    currency, rates, label = roi_report.rates_for("USD")
    report = roi_report.build_report(
        [roi_report.RecordRow(A1, None, roi.RunMeasure("threat_report_triaged", units=1))],
        window="all",
        since=None,
        currency=currency,
        rates=rates,
        rate_label=label,
    )
    assert report.organization.runs == 1 and report.trend == []


def test_this_quarter_is_a_window_from_the_first_day_of_the_quarter():
    assert "quarter" in roi_report.REPORT_WINDOWS
    assert roi_report.window_since("quarter", now=NOW) == datetime(2026, 10, 1, tzinfo=UTC)
    assert roi_report.window_since("quarter", now=datetime(2026, 3, 31, tzinfo=UTC)) == datetime(2026, 1, 1, tzinfo=UTC)
    assert roi_report.window_days("quarter", now=NOW) == 39
    assert roi_report.window_days("30d", now=NOW) == 30
    assert roi_report.window_days("all", now=NOW) is None
    with pytest.raises(ValueError):
        roi_report.window_since("year", now=NOW)


def test_the_quarter_window_is_named_in_the_one_page_report():
    report = _report("quarter")
    assert "This quarter (since 2026-10-01)" in roi_report.report_markdown(report, generated_at=NOW)


def test_a_masked_cost_masks_the_unit_rows_and_the_trend_net_value():
    report = _report()
    report.cost_masked = True
    data = roi_report.report_dict(report)
    for row in data["units"]:
        assert row["llm_cost"] is None and row["net_value"] is None and row["roi"] is None
        assert row["value"] is not None
    for p in data["trend"]:
        assert p["net_value"] is None and p["net_value_range"] == [None, None]
        assert p["value"] is not None


def test_the_api_window_pattern_accepts_the_quarter():
    import re

    from app.api.v1 import roi as roi_api

    assert re.match(roi_api._WINDOW_PATTERN, "quarter")
    assert re.match(roi_api._WINDOW_PATTERN, "30d")
    assert not re.match(roi_api._WINDOW_PATTERN, "year")
