"""ROI exports carry the methodology and the assumptions (#9836 R7a, design 10.6).

The CSV gains a ``methodology`` line and an ``assumptions`` section; the PDF (its Markdown
source) gains a last page with the formula, the report's own numbers and every input with
its label and source. A masked cost reads "Hidden by your administrator", never 0.
"""

import csv
import io
import uuid
from datetime import UTC, datetime

from app.services import roi, roi_report

A1, A2 = uuid.uuid4(), uuid.uuid4()
F1 = uuid.uuid4()
NOW = datetime(2026, 11, 8, 12, 0, tzinfo=UTC)


def _report(*, masked: bool = False) -> roi_report.RoiReport:
    rows = [
        roi_report.RecordRow(A1, F1, roi.RunMeasure("threat_report_triaged", units=6, llm_cost=1.0)),
        roi_report.RecordRow(A2, F1, roi.RunMeasure("cve_assessed", units=10, llm_cost=0.5)),
        roi_report.RecordRow(A2, None, roi.RunMeasure("cve_assessed", units=3, llm_cost=0.2, outcome="rejected")),
    ]
    currency, rates, label = roi_report.rates_for("USD")
    report = roi_report.build_report(
        rows,
        window="30d",
        since=roi_report.window_since("30d", now=NOW),
        currency=currency,
        rates=rates,
        rate_label=label,
        agent_names={A1: "Threat relevance", A2: "Vuln triage"},
        fleet_names={F1: "CTEM"},
    )
    report.cost_masked = masked
    return report


def _sections(text: str) -> tuple[list[list[str]], list[list[str]], list[list[str]]]:
    lines = list(csv.reader(io.StringIO(text)))
    first = lines.index([])
    second = lines.index([], first + 1)
    return lines[:first], lines[first + 1 : second], lines[second + 1 :]


def test_csv_figures_are_unchanged_and_followed_by_the_methodology_and_assumptions():
    report = _report()
    figures, methodology, assumptions = _sections(roi_report.report_csv(report))
    # Zero regression: the figure lines are those of the export before R7a.
    o = report.organization
    assert figures[1][:3] == ["organization", "", "organization"]
    assert float(figures[1][6]) == o.llm_cost and float(figures[1][8]) == o.net_value
    assert methodology == [["methodology", roi_report.METHODOLOGY]]
    assert assumptions[0] == ["assumption", "kind", "name", "value", "label", "source"]
    kinds = [row[1] for row in assumptions[1:]]
    assert kinds.count("unit") == 2 and "rate" in kinds
    # One row per assumption fact, every row labelled.
    assert len(assumptions) - 1 == len(o.assumption_facts)
    assert all(row[4] for row in assumptions[1:] if row[1] in ("unit", "rate"))


def test_csv_masked_costs_read_hidden_never_zero():
    figures, _m, _a = _sections(roi_report.report_csv(_report(masked=True)))
    header = figures[0]
    for line in figures[1:]:
        for column in ("llm_cost", "net_value", "roi", "payback_months"):
            assert line[header.index(column)] == roi_report.HIDDEN_COST
        assert float(line[header.index("value")]) >= 0


def test_assumption_rows_carry_the_range_and_the_source():
    rows = roi_report.assumption_rows(_report().organization)
    unit = next(r for r in rows if r[0] == "unit")
    assert "manual min" in unit[2] and "review min per unit" in unit[2]
    rate = next(r for r in rows if r[0] == "rate")
    assert rate[2].endswith("USD/h") and rate[4]


def test_pdf_last_page_reproduces_the_figure_from_the_shown_numbers():
    report = _report()
    md = roi_report.report_markdown(report, generated_at=NOW)
    page = md[md.index("## How these figures are computed") :]
    assert "page-break-before: always" in md[: md.index("## How these figures are computed")]
    o = report.organization
    assert f"= {o.net_value:,.2f} USD" in page
    savings = [s for s in o.computation if s["kind"] == "saving"]
    assert len(savings) == 2
    for s in savings:
        assert f"= {s['minutes_saved']:g} min" in page
    # The shown per-unit values sum to the value line.
    assert round(sum(s["value"] for s in savings), 2) == round(o.value, 2)
    assert "## Assumptions" in page and "| Input | Value | Label | Source |" in page
    assert "1 rejected (no unit counted)" in page
    assert roi_report.METHODOLOGY in page


def test_pdf_masked_costs_read_hidden_never_zero():
    md = roi_report.report_markdown(_report(masked=True), generated_at=NOW)
    hero = next(line for line in md.splitlines() if line.endswith("|") and " h | " in line)
    assert hero.count(roi_report.HIDDEN_COST) == 3  # AI cost, net value, ROI
    page = md[md.index("## How these figures are computed") :]
    assert f"Net value = value - AI cost: {roi_report.HIDDEN_COST}" in page
    assert "ROI = net value / AI cost =" not in page


def test_no_run_has_a_methodology_and_no_assumption_table():
    currency, rates, label = roi_report.rates_for("USD")
    empty = roi_report.build_report([], window="7d", since=None, currency=currency, rates=rates, rate_label=label)
    md = roi_report.report_markdown(empty, generated_at=NOW)
    assert "## How these figures are computed" in md and "Counted: 0 units from 0 runs." in md
    assert "| Input | Value | Label | Source |" not in md
    _f, methodology, assumptions = _sections(roi_report.report_csv(empty))
    assert methodology[0][0] == "methodology" and assumptions == [list(roi_report._CSV_ASSUMPTION_COLUMNS)]
