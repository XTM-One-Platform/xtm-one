"""The agent tool's ROI report stays bounded whatever the number of agents (issue #10627).

``get_roi_report`` on scope ``mine`` or ``platform`` answered ``report_dict``
whole: one full figures object per visible agent, idle ones included, each
repeating the assumptions and the computation, 281,176 characters on a live
platform, cut at the tool cap. :func:`roi_report.compact_report_dict` keeps the
totals and the shared figures once and pages compact rows by value.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime

from app.services import roi, roi_report

UNITS = ("threat_report_triaged", "cve_assessed", "briefing_written")


def _report(agents: int = 200, active: int = 15) -> roi_report.RoiReport:
    currency, rates, label = roi_report.rates_for("USD")
    ids = [uuid.uuid4() for _ in range(agents)]
    rows = [
        roi_report.RecordRow(
            agent_id=agent_id,
            fleet_id=None,
            measure=roi.RunMeasure(UNITS[i % len(UNITS)], units=i + 1, llm_cost=0.5),
            at=datetime(2026, 11, 5, tzinfo=UTC),
        )
        for i, agent_id in enumerate(ids[:active])
        for _ in range(3)
    ]
    names = {agent_id: f"agent {i:03d}" for i, agent_id in enumerate(ids)}
    report = roi_report.build_report(
        rows, window="7d", since=None, currency=currency, rates=rates, rate_label=label, agent_names=names
    )
    # load_report lists every visible agent, the idle ones with empty figures.
    empty = roi.compute_roi([], currency=currency, rates=rates)
    report.agents += [roi_report.ReportGroup(str(a), names[a], empty) for a in ids[active:]]
    return report


def test_the_default_answer_is_bounded_and_parses():
    text = json.dumps(roi_report.compact_report_dict(_report()), default=str)
    assert len(text) < 20_000
    assert json.loads(text)["agents"]["total"] == 15


def test_the_totals_equal_the_full_report():
    report = _report()
    full = roi_report.report_dict(report)
    compact = roi_report.compact_report_dict(report)
    for key, value in compact["totals"].items():
        assert full["organization"][key] == value
    assert compact["trend"] == full["trend"]
    # The same steps and figures; a saving step leaves out the name and currency the report gives (#10664).
    steps = full["organization"]["computation"]
    assert len(compact["computation"]) == len(steps)
    for short, step in zip(compact["computation"], steps, strict=True):
        dropped = roi_report.COMPACT_STEP_DROPPED if step["kind"] == "saving" else ()
        assert {k: v for k, v in step.items() if k not in dropped} == short


def test_idle_agents_are_counted_and_listed_on_request():
    report = _report()
    default = roi_report.compact_report_dict(report)
    assert default["agents"]["idle_not_listed"] == 185
    assert all(row["runs"] or row["units"] for row in default["agents"]["rows"])
    wide = roi_report.compact_report_dict(report, include_idle=True, limit=200)
    assert wide["agents"]["total"] == 200
    assert wide["agents"]["idle_not_listed"] == 0
    assert len(wide["agents"]["rows"]) == 200


def test_the_assumptions_appear_once():
    text = json.dumps(roi_report.compact_report_dict(_report()), default=str)
    for key in roi_report.COMPACT_SHARED_FIGURES:
        assert text.count(f'"{key}"') == 1
    # The prose twin of the facts is the agent or fleet report's (#10664).
    assert '"assumptions"' not in text


def _wide_report() -> roi_report.RoiReport:
    """15 units over 6 rate roles and 20 active agents: the live shape of scope mine, window 7d (#10664)."""
    by_role: dict[str, list[str]] = {}
    for baseline in roi.BASELINES:
        by_role.setdefault(baseline.rate_role, []).append(baseline.unit)
    roles = list(by_role.values())[:6]
    units = [r[0] for r in roles] + [u for r in roles for u in r[1:]]
    units = units[:15]
    currency, rates, label = roi_report.rates_for("USD")
    ids = [uuid.uuid4() for _ in range(20)]
    rows = [
        roi_report.RecordRow(
            agent_id=agent_id,
            fleet_id=None,
            measure=roi.RunMeasure(units[(i + k) % len(units)], units=i + 1, llm_cost=0.5),
            at=datetime(2026, 11, 5, tzinfo=UTC),
        )
        for i, agent_id in enumerate(ids)
        for k in range(3)
    ]
    names = {agent_id: f"agent {i:03d}" for i, agent_id in enumerate(ids)}
    return roi_report.build_report(
        rows, window="7d", since=None, currency=currency, rates=rates, rate_label=label, agent_names=names
    )


def test_a_wide_answer_stays_under_the_budget():
    from app.services.builtin_tools import roi as roi_tools

    report = _wide_report()
    facts = report.organization.assumption_facts
    assert sum(f["kind"] == "unit" for f in facts) == 15
    assert sum(f["kind"] == "rate" for f in facts) == 6
    compact = roi_report.compact_report_dict(report)
    assert compact["agents"]["total"] == 20
    assert len(compact["agents"]["rows"]) == 20
    text = roi_tools._dump(compact)
    assert len(text) < 20_000
    parsed = json.loads(text)
    # The totals reconcile with the rows.
    rows = parsed["agents"]["rows"]
    assert sum(row["runs"] for row in rows) == parsed["totals"]["runs"]
    assert sum(row["units"] for row in rows) == parsed["totals"]["units"]
    # Each row is rounded to the cent: the sum is within a cent per row of the total.
    assert abs(sum(row["value"] for row in rows) - parsed["totals"]["value"]) <= 0.01 * len(rows)


def test_each_source_is_written_once_and_resolves():
    report = _wide_report()
    full = report.organization.assumption_facts
    compact = roi_report.compact_report_dict(report)
    assert len(compact["sources"]) == len(set(compact["sources"]))
    assert len(compact["assumption_facts"]) == len(full)
    for short, fact in zip(compact["assumption_facts"], full, strict=True):
        if "source" in fact:
            assert compact["sources"][short.pop("source_ref")] == fact["source"]
        assert short == {k: v for k, v in fact.items() if k != "source"}


def test_rows_are_paged_by_value_with_the_next_offset():
    report = _report()
    first = roi_report.compact_report_dict(report, limit=10)["agents"]
    assert len(first["rows"]) == 10
    assert first["remaining"] == 5
    assert first["next_offset"] == 10
    values = [row["value"] for row in first["rows"]]
    assert values == sorted(values, reverse=True)
    rest = roi_report.compact_report_dict(report, limit=10, offset=10)["agents"]
    assert len(rest["rows"]) == 5
    assert rest["remaining"] == 0
    assert rest["next_offset"] is None
    assert {r["id"] for r in first["rows"]}.isdisjoint({r["id"] for r in rest["rows"]})


def test_a_row_is_compact():
    row = roi_report.compact_report_dict(_report())["agents"]["rows"][0]
    assert set(row) == {"id", "name", *roi_report.COMPACT_ROW_FIELDS}


def test_masked_costs_stay_masked():
    report = _report()
    report.cost_masked = True
    compact = roi_report.compact_report_dict(report)
    assert compact["totals"]["llm_cost"] is None
    assert all(row["llm_cost"] is None for row in compact["agents"]["rows"])


def test_the_tool_keeps_the_whole_report_for_one_agent_or_fleet():
    import inspect

    from app.services.builtin_tools import roi as roi_tools

    source = inspect.getsource(roi_tools.build_roi_tools)
    assert "if scope_id is not None:" in source
    assert 'return _dump({**roi_report.report_dict(report), "figures": ledger})' in source
    assert "compact_report_dict(" in source
