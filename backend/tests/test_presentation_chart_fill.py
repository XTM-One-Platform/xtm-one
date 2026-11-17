"""A ``fill`` chart in a column of a ``columns`` block, and charts too short to read.

A ``content`` slide holding a ``metrics_grid`` and then a ``columns`` block of
two equal columns - a level-4 heading over a ``fill`` chart in each - drew
every chart 0px high: the canvas has no height of its own, the ``columns``
block hugged its content, so the column had no room to hand it. On the export
page the empty canvas also held ``data-deck-pending`` for good, so the
headless render timed out and the layout check behind ``generate_presentation``
/ ``edit_presentation`` was skipped instead of reporting it.

What is pinned here is everything but the pixels, which
``frontend/src/components/presentation/blocks/chart-fill.test.tsx`` reads off
the real stylesheet:

* the audit's ``chart`` finding: graded against ``CHART_MIN_HEIGHT_PX``, its
  remedy read off where the chart sits, an excess that silences ``sparse``;
* the renderer contracts it relies on: the canvas floor IS that value, a
  ``columns`` block holding a filling chart takes the room, and the export
  page does not wait on a canvas with no room.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from app.services.presentation_export import LAYOUT_AUDIT_JS
from app.services.presentation_layout import (
    CHART_MIN_HEIGHT_PX,
    LayoutReport,
    findings_from_observations,
    format_layout_report,
)

FRONTEND_PRESENTATION = Path(__file__).resolve().parents[2] / "frontend" / "src" / "components" / "presentation"


def _read(*parts: str) -> str:
    return FRONTEND_PRESENTATION.joinpath(*parts).read_text(encoding="utf-8")


def _spec(titles: list[str]) -> dict:
    return {"title": "Deck", "slides": [{"layout": "content", "title": t} for t in titles]}


def _slide(observations: list[dict]) -> dict:
    return {"index": 0, "slide_id": "s0", "layout": "content", "findings": observations}


def _chart(height: float, **fields) -> dict:
    return {"kind": "chart", "height": height, "chart": "bar", "fill": True, "panel": False, "block": "chart", **fields}


def _findings(*observations: dict) -> list:
    return findings_from_observations([_slide(list(observations))], _spec(["Q3 at a glance"]))


@pytest.mark.parametrize(
    ("height", "severity"),
    [
        (0, "overloaded"),
        (CHART_MIN_HEIGHT_PX / 2 - 1, "overloaded"),
        (CHART_MIN_HEIGHT_PX / 2, "dense"),
        (CHART_MIN_HEIGHT_PX - 1, "dense"),
        (CHART_MIN_HEIGHT_PX, None),
        (396, None),
    ],
)
def test_a_chart_under_the_readable_height_is_a_finding(height, severity):
    findings = _findings(_chart(height))
    if severity is None:
        assert findings == []
        return
    (finding,) = findings
    assert (finding.kind, finding.severity, finding.amount) == ("chart", severity, round(height))
    assert finding.title == "Q3 at a glance"


def test_a_collapsed_chart_says_there_is_no_plot_and_names_the_chart():
    (finding,) = _findings(_chart(0, text="EUR millions"))
    assert finding.message.startswith(
        'the bar chart "EUR millions" is drawn 0px high - its legend and caption show over no plot at all.'
    )


def test_a_short_chart_says_its_height_against_the_minimum():
    (finding,) = _findings(_chart(126.6, chart="stacked_bar"))
    assert f"the stacked bar chart is drawn 127px high (under {CHART_MIN_HEIGHT_PX}px) - too short to read." in (
        finding.message
    )


@pytest.mark.parametrize(
    ("fields", "remedy"),
    [
        (
            {"fill": True},
            "It takes the height its column or the slide leaves after the blocks around it: cut or shorten "
            "those blocks, or move the chart to a slide of its own.",
        ),
        ({"fill": False}, "Cut what shares the slide with it, or move the chart to a slide of its own."),
        (
            {"fill": True, "panel": True},
            "Give its panel more height: fewer key figures or panels on the slide, or split the slide.",
        ),
    ],
)
def test_the_remedy_is_read_off_where_the_chart_sits(fields, remedy):
    (finding,) = _findings(_chart(40, **fields))
    assert finding.message.endswith(remedy)


def test_a_page_brief_is_told_about_its_page():
    spec = {"title": "Brief", "aspect_ratio": "A4", "slides": [{"layout": "content", "title": "Page"}]}
    (finding,) = findings_from_observations([_slide([_chart(0)])], spec)
    assert "a page of its own" in finding.message
    assert "slide" not in finding.message


@pytest.mark.parametrize(("height", "amounts"), [("not-a-number", []), (None, [0]), (-3, [0])])
def test_a_garbled_height_is_ignored_and_a_missing_one_reads_as_no_room(height, amounts):
    assert [finding.amount for finding in _findings(_chart(height))] == amounts


def test_a_short_chart_is_never_also_told_the_slide_is_sparse():
    sparse = {"kind": "sparse", "unused": 0.6, "empty": 500, "slide": 1080}
    assert [finding.kind for finding in _findings(_chart(0), sparse)] == ["chart"]


def test_the_agent_reads_the_chart_in_the_layout_check_section():
    report = LayoutReport(slide_count=1, findings=tuple(_findings(_chart(0, text="EUR millions"))))
    text = format_layout_report(report)
    assert text.startswith("Layout check: 1 slide issue(s) the audience would see. Fix them and save again:")
    assert '- slide 1 [content] "Q3 at a glance" (overloaded): the bar chart "EUR millions" is drawn 0px high' in text


# ── Renderer contracts ────────────────────────────────────────────────────────


def test_the_audit_measures_every_chart_canvas_as_painted():
    assert 'slide.querySelectorAll(".deck-chart-canvas")' in LAYOUT_AUDIT_JS
    assert 'kind: "chart", height: Math.round(canvas.getBoundingClientRect().height * 10) / 10' in LAYOUT_AUDIT_JS
    # A canvas that is not rendered at all (an ancestor at display: none) has
    # no client rect and is not measured; an empty one in the flow is.
    assert "!canvas.getClientRects().length" in LAYOUT_AUDIT_JS
    for field in (
        "chart: figure.dataset.kind",
        'figure.dataset.size === "full"',
        'panel: !!figure.closest(".deck-panel")',
    ):
        assert field in LAYOUT_AUDIT_JS, field


def test_the_canvas_floor_is_the_audit_minimum():
    css = _read("presentation.css")
    rule = re.search(
        r"\.deck-root \.deck-blocks > \.deck-chart:is\(\[data-size=\"full\"\], \[data-fill=\"true\"\]\) > "
        r"\.deck-chart-canvas \{\s*min-height: (\d+)px;",
        css,
    )
    assert rule, "the floor of a chart handed the room is gone from presentation.css"
    assert int(rule.group(1)) == CHART_MIN_HEIGHT_PX


def test_a_columns_block_holding_a_filling_chart_takes_the_room():
    css = _read("presentation.css")
    filler = r'\.deck-chart:is\(\[data-fill="true"\], \[data-size="full"\]\)'
    assert re.search(
        rf"\.deck-root \.deck-blocks > \.deck-columns:has\(> \.deck-column > \.deck-blocks > {filler}\) \{{\s*"
        r"flex: 1 1 auto;\s*min-height: 0;",
        css,
    )
    assert re.search(
        rf"\.deck-root \.deck-columns > \.deck-column:has\(> \.deck-blocks > {filler}\) \{{\s*align-self: stretch;",
        css,
    )


def test_the_export_page_does_not_wait_on_a_canvas_with_no_room():
    chart = _read("blocks", "chart.tsx")
    assert "ref.current?.closest('[data-deck-static=\"true\"]')" in chart
    assert "const roomless = exportPage && box === null;" in chart
    assert "const settled = complete || failed || gaveUpKey === buildKey || roomless;" in chart
    # Never unfinished: that would fail the export instead of reporting the chart.
    assert "const unfinished = failed || (gaveUpKey === buildKey && !complete);" in chart


def test_the_panel_names_the_finding_in_every_language():
    import json

    panel = _read("PresentationLayoutPanel.tsx")
    assert 'chart: "Chart too short",' in panel and "chart: ChartColumnDecreasing," in panel
    lang = FRONTEND_PRESENTATION.parents[1] / "i18n" / "lang"
    for code in ("en", "fr", "es", "ja"):
        assert "Chart too short" in json.loads((lang / f"{code}.json").read_text(encoding="utf-8")), code
    types_ts = (FRONTEND_PRESENTATION.parents[1] / "types" / "index.ts").read_text(encoding="utf-8")
    kinds = types_ts.split("export type PresentationLayoutFindingKind = ", 1)[1].split(";", 1)[0]
    assert '"chart"' in kinds.split(" | ")
