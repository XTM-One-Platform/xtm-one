"""The CSV and PDF exports of the ROI ledger's figures (issue #12077, slice L4c, design 12.8).

The export prints ``roi_ledger.figures`` (``Figures.as_dict``), the shape ``GET /roi``
answers under ``figures``: invariant (d) on the export, every scope including ``mine``,
every breakdown summing to the hero, a masked cost never read as 0.
"""

from __future__ import annotations

import csv
import inspect
import io
import uuid
from datetime import UTC, datetime
from typing import Any

import pytest

from app.api.v1 import roi as roi_api
from app.services import roi, roi_export, roi_report
from app.services.roi_figures import (
    ROW_NO_AGENT,
    ROW_OTHER,
    SCOPE_AGENT,
    SCOPE_FLEET,
    SCOPE_MINE,
    SCOPE_ORGANIZATION,
    Viewer,
    compute,
)
from tests.test_roi_ledger_invariants import ADMIN, BIG_FLEET, FIRST, LAST, PEOPLE, SEEDS, SHARED, _fixture, _viewer

NOW = datetime(2026, 11, 9, 12, 0, tzinfo=UTC)
NAMES = {str(SHARED): "Shared triage", str(BIG_FLEET): "=CTEM"}
ASSUMPTIONS = [("unit", "Threat report triaged", "20 manual min, 5 review min per unit", "assumption", "")]


def _as_dict(scope: str, viewer: Viewer, scope_id: uuid.UUID | None = None, *, masked: bool = False) -> dict[str, Any]:
    result = compute(
        _fixture(0),
        scope=scope,
        viewer=viewer,
        scope_id=scope_id,
        window_first=FIRST,
        window_last=LAST,
        cost_masked=masked,
    )
    result.currency = "EUR"
    return result.as_dict()


def _lines(text: str) -> list[list[str]]:
    lines = list(csv.reader(io.StringIO(text)))
    return lines[: lines.index([])]


SCOPES = [
    (SCOPE_ORGANIZATION, ADMIN, None),
    (SCOPE_ORGANIZATION, PEOPLE[3], None),
    (SCOPE_MINE, PEOPLE[2], None),
    (SCOPE_AGENT, PEOPLE[2], SHARED),
    (SCOPE_FLEET, ADMIN, BIG_FLEET),
]


@pytest.mark.parametrize(("scope", "person", "scope_id"), SCOPES)
def test_d_the_csv_total_is_the_figures_total_and_every_section_sums_to_it(
    scope: str, person: uuid.UUID, scope_id: uuid.UUID | None
) -> None:
    figures = _as_dict(scope, _viewer(person), scope_id)
    lines = _lines(roi_export.figures_csv(figures, names=NAMES, assumptions=ASSUMPTIONS))
    header = lines[0]
    total = lines[1]
    assert total[0] == "total"
    for column in ("runs", "units", "value", "ai_cost", "net_value"):
        assert float(total[header.index(column)]) == pytest.approx(figures["totals"][column])
    for section, _heading in roi_export.BREAKDOWNS:
        rows = [line for line in lines[2:] if line[0] == section]
        if figures.get(section) is None:
            assert not rows
            continue
        assert len(rows) == len(figures[section])
        for column in ("runs", "value", "ai_cost"):
            summed = sum(float(r[header.index(column)]) for r in rows)
            assert summed == pytest.approx(float(total[header.index(column)]), abs=0.05)


@pytest.mark.parametrize("seed", SEEDS[:20])
def test_d_the_export_of_an_agent_is_its_by_agent_row_for_the_administrator(seed: int) -> None:
    facts = _fixture(seed)
    org = compute(facts, scope=SCOPE_ORGANIZATION, viewer=_viewer(ADMIN), window_first=FIRST, window_last=LAST)
    agent = compute(
        facts, scope=SCOPE_AGENT, viewer=_viewer(ADMIN), scope_id=SHARED, window_first=FIRST, window_last=LAST
    )
    lines = _lines(roi_export.figures_csv(agent.as_dict(), names=NAMES, assumptions=[]))
    header = lines[0]
    row = next(r for r in org.as_dict()["by_agent"] if r["key"] == str(SHARED))
    assert float(lines[1][header.index("value")]) == pytest.approx(row["value"])
    assert float(lines[1][header.index("runs")]) == row["runs"]


def test_residual_and_folded_rows_carry_their_design_names() -> None:
    figures = _as_dict(SCOPE_ORGANIZATION, _viewer(PEOPLE[3]))
    lines = _lines(roi_export.figures_csv(figures, names=NAMES, assumptions=[]))
    by_agent = {line[1]: line[2] for line in lines if line[0] == "by_agent"}
    assert by_agent.get(ROW_OTHER) == "Other agents and fleets"
    assert by_agent.get(ROW_NO_AGENT) == "No agent (general chat)"
    sources = {line[2] for line in lines if line[0] == "by_source"}
    assert sources <= {"Automations", "Chat (estimated)", "Other AI usage"}
    # A name that starts like a formula is quoted.
    assert "'=CTEM" in {line[2] for line in lines if line[0] == "by_fleet"}


def test_a_non_administrator_export_names_no_one_else() -> None:
    figures = _as_dict(SCOPE_ORGANIZATION, _viewer(PEOPLE[3]))
    text = roi_export.figures_csv(figures, names=NAMES, assumptions=[])
    assert figures["by_person"] is None and "by_person" not in text
    for person in PEOPLE:
        assert str(person) not in text


def test_masked_costs_read_hidden_never_zero_in_both_formats() -> None:
    figures = _as_dict(SCOPE_MINE, _viewer(PEOPLE[2]), masked=True)
    lines = _lines(roi_export.figures_csv(figures, names=NAMES, assumptions=[]))
    header = lines[0]
    for line in lines[1:]:
        for column in ("ai_cost", "net_value", "roi", "payback_months"):
            assert line[header.index(column)] == roi_export.HIDDEN_COST
    md = roi_export.figures_markdown(figures, names=NAMES, assumptions=[], window="30d", title="T", generated_at=NOW)
    assert f"Net value = value - AI cost: {roi_export.HIDDEN_COST}" in md
    assert " 0.00 EUR | " not in md.split("## By source")[0]


def test_the_markdown_has_the_hero_the_breakdowns_and_the_method() -> None:
    figures = _as_dict(SCOPE_MINE, _viewer(PEOPLE[2]))
    md = roi_export.figures_markdown(
        figures,
        names=NAMES,
        assumptions=ASSUMPTIONS,
        window="30d",
        title="Value delivered by XTM One",
        generated_at=NOW,
    )
    assert md.startswith("# Value delivered by XTM One")
    assert "**Your own runs and chats**" in md
    assert "## By source" in md and "## How these figures are computed" in md
    assert "## Assumptions" in md and roi_export.METHODOLOGY in md
    assert f"{figures['totals']['value']:,.2f} EUR" in md


def test_the_filename_names_the_scope() -> None:
    assert roi_export.export_filename("30d", "pdf", xtm_one=True) == "xtm-one-value-30d.pdf"
    assert roi_export.export_filename("7d", "csv", xtm_one=False, scope="mine") == "agents-value-mine-7d.csv"
    assert roi_export.export_filename("7d", "csv", xtm_one=False, scope="platform") == "agents-value-7d.csv"
    fleet = uuid.UUID(int=201)
    assert roi_export.export_filename("7d", "csv", xtm_one=False, scope="fleet", scope_id=fleet) == (
        f"agents-value-fleet-{str(fleet)[:8]}-7d.csv"
    )


def test_assumption_facts_follow_the_pricing() -> None:
    currency, rates, label = roi_report.rates_for("USD")
    pricing = roi_report.Pricing(
        baselines=dict(roi.BASELINES_BY_UNIT),
        overridden=set(),
        currency=currency,
        rates=rates,
        rate_label=label,
    )
    rows = roi_report.assumption_rows_of(
        roi_export.assumption_facts(["threat_report_triaged", "nope"], pricing=pricing)
    )
    kinds = [r[0] for r in rows]
    assert kinds.count("unit") == 1 and "rate" in kinds


def test_the_export_route_reads_the_ledger_and_the_get_roi_rule() -> None:
    source = inspect.getsource(roi_api.export_roi_report)
    assert "roi_ledger.figures" in source and "load_report" not in source
    assert "_require_roi_reader" in source and "roi_org_totals_visible" in source


def test_get_roi_names_the_rows_of_its_figures_with_the_export_labels() -> None:
    """The Value page draws ``figures`` (slice L6a): the row names come from the export's own labels."""
    source = inspect.getsource(roi_api._with_ledger_figures)
    assert "roi_export.labels(db, figures)" in source
    assert '"names": names' in source
    labels = inspect.getsource(roi_export.labels)
    assert "User" not in labels and "user_names" not in labels  # never a person's name
