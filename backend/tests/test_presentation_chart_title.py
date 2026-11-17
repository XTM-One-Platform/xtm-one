"""A chart's own ``title``, drawn inside its frame.

A chart on a content slide had no title of its own: a caption sat under the
plot, a heading stood outside the card, and a callout wrapped around the chart
put a title on top but kept the chart at its readable minimum over an empty
band - the callout handed its height to a logo grid only. The ``chart`` block
now takes a ``title``, drawn at the top inside its frame as a dashboard panel
draws its own (the legend beside it on the same row), and a ``fill`` callout
hands its height to a ``fill`` chart too.

Pinned here: the field and the one place it is refused (a dashboard panel
titles its chart), the stylesheet contracts the renderer keeps, the export
(the title an ordinary text primitive visited before the chart, the chart's
frame never the head's), the audit naming a chart by its title, the layout
switch moving a chart's title onto its panel, and the words the agent and the
inspector read. The pixels are read off the real stylesheet by
``frontend/src/components/presentation/blocks/chart-title.test.tsx`` /
``chart-fill.test.tsx`` and on a live deck by
``frontend/tests_e2e/presentation-chart-title.spec.ts``.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from app.services import presentation_guidance as guidance
from app.services.presentation_export import EXTRACT_PRIMITIVES_JS, LAYOUT_AUDIT_JS
from app.services.presentation_layout_switch import switch_slide_layout
from app.services.presentation_spec import (
    MAX_LABEL_CHARS,
    PANEL_CHART_TITLE_REFUSAL,
    ChartBlock,
    DashboardPanel,
    PresentationSpecError,
    validate_spec,
)

FRONTEND = Path(__file__).resolve().parents[2] / "frontend"
PRESENTATION = FRONTEND / "src" / "components" / "presentation"

_CHART = {
    "type": "chart",
    "kind": "line",
    "title": "Monthly signups",
    "fill": True,
    "categories": ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug"],
    "series": [{"name": "Signups", "values": [120, 135, 150, 149, 170, 182, 190, 205]}],
}


def _read(*parts: str) -> str:
    return PRESENTATION.joinpath(*parts).read_text(encoding="utf-8")


def _css() -> str:
    """The deck stylesheet without its comments, so a rule's selector is all that precedes its body."""
    return re.sub(r"/\*.*?\*/", "", _read("presentation.css"), flags=re.S)


def _rules(css: str, selector_pattern: str) -> list[str]:
    """The bodies of the rules whose selector matches ``selector_pattern``."""
    return [m.group(2) for m in re.finditer(r"([^{}]+)\{([^{}]*)\}", css) if re.search(selector_pattern, m.group(1))]


def _deck(*slides: dict) -> dict:
    return {"title": "Deck", "slides": list(slides)}


# ── The field ─────────────────────────────────────────────────────────────────


def test_a_chart_takes_a_title_of_the_panel_title_s_size_and_none_by_default():
    assert ChartBlock.model_validate({**_CHART, "title": None}).title is None
    assert "title" not in ChartBlock.model_validate({k: v for k, v in _CHART.items() if k != "title"}).model_dump(
        exclude_none=True
    )
    assert ChartBlock.model_validate(_CHART).title == "Monthly signups"
    # The same bound as `DashboardPanel.title`: one title style, one length.
    assert ChartBlock.model_fields["title"].annotation == DashboardPanel.model_fields["title"].annotation
    with pytest.raises(ValueError, match=str(MAX_LABEL_CHARS)):
        ChartBlock.model_validate({**_CHART, "title": "x" * (MAX_LABEL_CHARS + 1)})
    description = ChartBlock.model_fields["title"].description or ""
    assert "inside the chart's frame" in description and "dashboard panel title" in description


def test_the_2_x_2_of_the_brief_validates_on_a_content_slide():
    """Two columns blocks of two titled `fill` line charts - a 2 x 2 of charts."""
    column = lambda name: [{**_CHART, "title": name}]  # noqa: E731 - a one-line fixture
    spec = validate_spec(
        _deck(
            {
                "layout": "content",
                "title": "Signups by segment",
                "blocks": [
                    {"type": "columns", "columns": [column("Self-serve"), column("Sales-led")]},
                    {"type": "columns", "columns": [column("EU"), column("US")]},
                ],
            }
        )
    )
    titles = [chart["title"] for block in spec["slides"][0]["blocks"] for col in block["columns"] for chart in col]
    assert titles == ["Self-serve", "Sales-led", "EU", "US"]
    # In a callout and in a comparison side too: anywhere but a panel.
    validate_spec(
        _deck(
            {
                "layout": "content",
                "title": "Signups",
                "blocks": [{"type": "callout", "title": "Growth", "fill": True, "children": [_CHART]}],
            }
        )
    )


def test_a_dashboard_panel_refuses_a_chart_title_and_says_whose_the_title_is():
    """One title source per card: the panel's. The refusal names the move."""
    dashboard = {
        "layout": "dashboard",
        "title": "Signups",
        "tiles": [{"value": "205", "label": "August"}, {"value": "+8%", "label": "MoM"}],
        "panels": [{"title": "Monthly signups", "block": _CHART}],
    }
    with pytest.raises(PresentationSpecError) as refused:
        validate_spec(_deck(dashboard))
    assert PANEL_CHART_TITLE_REFUSAL in str(refused.value)
    assert "panel chart is titled by the panel's `title`" in PANEL_CHART_TITLE_REFUSAL
    assert "slides.0" in str(refused.value) and "panels" in str(refused.value)
    # Without the chart's own title the same panel is the dashboard it always was.
    untitled = {**_CHART}
    del untitled["title"]
    saved = validate_spec(_deck({**dashboard, "panels": [{"title": "Monthly signups", "block": untitled}]}))
    assert saved["slides"][0]["panels"][0]["title"] == "Monthly signups"
    assert "title" not in saved["slides"][0]["panels"][0]["block"]
    # A panel without a title of its own refuses the chart's just the same:
    # the title belongs on the panel, not on the figure.
    with pytest.raises(PresentationSpecError, match="panel chart is titled by the panel"):
        validate_spec(_deck({**dashboard, "panels": [{"block": _CHART}]}))


# ── The renderer's contracts ──────────────────────────────────────────────────


def test_the_figure_opens_with_a_head_holding_the_title_and_the_legend():
    chart = _read("blocks", "chart.tsx")
    head = chart[chart.index("{block.title ? (") : chart.index('className="deck-chart-canvas"')]
    assert '<div className="deck-chart-head">' in head
    assert '<InlineMarkup as="h3" className="deck-chart-title" value={block.title} field="title" />' in head
    # The legend goes INTO the head with a title and stays a direct child without one.
    assert head.index('className="deck-chart-title"') < head.index("{legendList}")
    assert re.search(r"\) : \(\s*legendList\s*\)", head), "an untitled chart keeps the legend a direct child"
    assert '<ul className="deck-chart-legend" data-split={split ? "true" : undefined}>' in chart
    # A butterfly's split legend is its own row after the head, never in it.
    assert head.rindex("{legendList}") < head.index("{legendSplit}")


def test_the_head_is_laid_out_as_a_panel_s_first_row_and_takes_no_share_of_the_fill():
    css = _css()
    (head,) = _rules(css, r"^\s*\.deck-root \.deck-chart-head\s*$")
    (panel,) = _rules(css, r"^\s*\.deck-root \.deck-panel\s*$")
    # The panel idiom: the title up to 60% of the row, the legend in the rest.
    assert "grid-template-columns: fit-content(60%) minmax(0, 1fr);" in head
    assert "grid-template-columns: fit-content(60%) minmax(0, 1fr);" in panel
    assert "column-gap: var(--deck-space-3);" in head and "column-gap: var(--deck-space-3);" in panel
    # Its own height in the figure's column: the canvas takes the rest.
    assert "flex: none;" in head
    (legend,) = _rules(css, r"^\s*\.deck-root \.deck-chart-head > \.deck-chart-legend\s*$")
    (panel_legend,) = _rules(css, r"^\s*\.deck-root \.deck-panel \.deck-chart-legend\s*$")
    for declaration in ("grid-column: 2;", "grid-row: 1;", "justify-content: flex-end;", "align-self: center;"):
        assert declaration in legend, declaration
        assert declaration in panel_legend, declaration


def test_the_title_s_type_is_the_panel_title_s_one_rule_for_both():
    css = _css()
    shared = _rules(css, r"^\s*\.deck-root \.deck-panel-title,\s*\.deck-root \.deck-chart-title\s*$")
    assert len(shared) == 1, "the panel title and the chart title are set by ONE rule"
    (body,) = shared
    for declaration in (
        "font-family: var(--deck-heading-font);",
        "font-size: var(--deck-fs-sm);",
        "font-weight: 500;",
        "line-height: 1.3;",
        "color: var(--deck-fg);",
        "grid-column: 1;",
        "grid-row: 1;",
    ):
        assert declaration in body, declaration
    # No second rule sets the chart title's type apart from the panel's.
    assert _rules(css, r"^\s*\.deck-root \.deck-chart-title\s*$") == []


def test_a_fill_callout_hands_its_height_to_a_fill_chart_and_a_column_hands_it_to_the_callout():
    css = _css()
    filler = r'\.deck-chart:is\(\[data-fill="true"\], \[data-size="full"\]\)'
    wrapped = r'\.deck-callout\[data-fill="true"\] > \.deck-callout-body > \.deck-blocks'
    (run,) = _rules(css, rf"^\s*\.deck-root {wrapped}:has\(> {filler}\)\s*$")
    assert "flex: 1 1 auto;" in run and "min-height: 0;" in run
    # The logo rule it sits next to is untouched.
    assert _rules(
        css,
        rf'^\s*\.deck-root {wrapped}:has\(> \.deck-logo-grid\[data-fill="true"\], > \.deck-logo-wall\[data-fill="true"\]\)\s*$',
    )
    # The column rules, one wrapper deeper - the originals unchanged.
    (row,) = _rules(
        css,
        rf"^\s*\.deck-root \.deck-blocks > \.deck-columns:has\(> \.deck-column > \.deck-blocks > {wrapped} > {filler}\)\s*$",
    )
    assert "flex: 1 1 auto;" in row and "min-height: 0;" in row
    (column,) = _rules(
        css, rf"^\s*\.deck-root \.deck-columns > \.deck-column:has\(> \.deck-blocks > {wrapped} > {filler}\)\s*$"
    )
    assert "align-self: stretch;" in column
    assert _rules(
        css, rf"^\s*\.deck-root \.deck-blocks > \.deck-columns:has\(> \.deck-column > \.deck-blocks > {filler}\)\s*$"
    )
    assert _rules(css, rf"^\s*\.deck-root \.deck-columns > \.deck-column:has\(> \.deck-blocks > {filler}\)\s*$")


def test_the_stage_draws_a_refused_panel_draft_as_the_panel_the_save_keeps():
    css = _css()
    assert ".deck-root .deck-panel-body > .deck-chart > .deck-chart-head { display: contents; }" in css
    assert ".deck-root .deck-panel-body > .deck-chart .deck-chart-title { display: none; }" in css
    # ...after the panel's own `display: contents` on the figure, in the same section.
    assert css.index(".deck-root .deck-panel-body > .deck-chart > .deck-chart-head") > css.index(
        ".deck-root .deck-panel-body > .deck-chart { display: contents; }"
    )


# ── The export and the audit ──────────────────────────────────────────────────


def test_the_export_visits_the_title_before_the_chart_and_frames_the_chart_off_the_legend_and_canvas():
    js = EXTRACT_PRIMITIVES_JS
    emit_chart = js[js.index("const emitChart = (figure, opacity) => {") : js.index("const visit = (el) => {")]
    assert 'const title = figure.querySelector(".deck-chart-head > .deck-chart-title");' in emit_chart
    visited = emit_chart.index("if (title) visit(title);")
    # Over the card (drawn first) and before the chart - native or picture.
    assert emit_chart.index("emitRect(figure, fcs, figureRect, opacity, fitOf(figure))") < visited
    assert visited < emit_chart.index("const reason = pictureReason(chart);")
    assert visited < emit_chart.index('kind: "chart", ...box(frameRect), opacity,')
    # The chart's frame is the legend's and the canvas' boxes alone: the head
    # (and so the title) is never part of it.
    frame = emit_chart[emit_chart.index("const frame = hasLegend") : emit_chart.index("const frameRect =")]
    assert "canvasRect" in frame and "legendRect" in frame
    assert "title" not in frame and "head" not in frame


def test_the_audit_names_a_chart_by_its_title_then_its_caption_then_the_heading_above():
    js = LAYOUT_AUDIT_JS
    naming = js[
        js.index('for (const canvas of slide.querySelectorAll(".deck-chart-canvas"))') : js.index(
            'kind: "chart", height:'
        )
    ]
    assert 'const title = figure.querySelector(".deck-chart-head > .deck-chart-title");' in naming
    assert "const name = title && !hidden(getComputedStyle(title)) ? title.textContent" in naming
    assert naming.index("title.textContent") < naming.index("caption.textContent") < naming.index("above.textContent")


# ── The layout switch ─────────────────────────────────────────────────────────


def _content_with(*blocks: dict) -> dict:
    return {
        "id": "s-signups",
        "layout": "content",
        "title": "Signups",
        "blocks": [
            {"type": "metrics_grid", "items": [{"value": "205", "label": "August"}, {"value": "+8%", "label": "MoM"}]},
            *blocks,
        ],
    }


def test_content_to_dashboard_moves_a_chart_s_title_onto_its_panel():
    outcome = switch_slide_layout(_content_with(_CHART), "dashboard")
    (panel,) = outcome.slide["panels"]
    assert panel["title"] == "Monthly signups"
    assert "title" not in panel["block"]
    validate_spec(_deck(outcome.slide))
    # And back: the panel's title is a heading over the chart, as for any panel.
    back = switch_slide_layout(outcome.slide, "content")
    assert [block["type"] for block in back.slide["blocks"]] == ["metrics_grid", "heading", "chart"]
    assert back.slide["blocks"][1]["content"] == "Monthly signups"
    assert "title" not in back.slide["blocks"][2]


def test_content_to_dashboard_keeps_the_heading_s_title_and_says_the_chart_s_was_dropped():
    heading = {"type": "heading", "content": "Signups per month", "level": 3}
    outcome = switch_slide_layout(_content_with(heading, _CHART), "dashboard")
    (panel,) = outcome.slide["panels"]
    assert panel["title"] == "Signups per month"
    assert "title" not in panel["block"]
    assert any('"Monthly signups"' in note and '"Signups per month"' in note for note in outcome.dropped), (
        outcome.dropped
    )
    validate_spec(_deck(outcome.slide))


# ── The words the agent and the inspector read ────────────────────────────────


def test_the_agent_is_told_to_title_a_chart_with_title_not_a_callout_and_the_example_shows_it():
    budgets = guidance.format_soft_budgets()
    assert "said in its title" in budgets and "not a callout around the chart" in budgets
    assert "the caption is the note under the plot" in budgets
    assert guidance.BLOCK_EXAMPLES["chart"]["title"] == "ARR by quarter"
    catalog = json.loads((PRESENTATION / "blocks" / "chart-catalog.json").read_text(encoding="utf-8"))
    assert catalog["chart"]["title"] == "ARR by quarter"
    # The model-derived reference carries the field with its description.
    reference = guidance.build_presentation_reference("chart")
    assert "title drawn at the top inside the chart's frame" in reference
    skill = (
        Path(__file__).resolve().parents[1]
        / "app"
        / "agents"
        / "seed_data"
        / "skill_content"
        / "presentation-design.md"
    ).read_text(encoding="utf-8")
    assert (
        "A chart outside a dashboard - on a `content` slide, in a column, in a callout - is titled with its own `title`"
        in skill
    )
    assert "never by wrapping the chart in a `callout` for a title" in skill


def test_the_inspector_offers_the_title_and_says_a_panel_refuses_it_in_every_language():
    schema = _read("block-schema.ts")
    chart_entry = schema.split("chart: {", 1)[1].split("key_value:", 1)[0]
    assert 'markup("title", "Title (heading)", {' in chart_entry
    assert chart_entry.index('markup("title"') < chart_entry.index('markup("caption"')
    applicability = _read("field-applicability.ts")
    reason = re.search(r'key === "title"\) \{\s*return "([^"]+)";', applicability)
    assert reason, "the inspector says why a chart title cannot be saved in a panel"
    assert reason.group(1).startswith("Not allowed in a dashboard panel")
    hint = re.search(r'markup\("title", "Title \(heading\)", \{\s*optional: true,\s*hint: "([^"]+)"', chart_entry)
    assert hint
    lang = FRONTEND / "src" / "i18n" / "lang"
    for code in ("en", "fr", "es", "ja"):
        catalog = json.loads((lang / f"{code}.json").read_text(encoding="utf-8"))
        assert reason.group(1) in catalog, code
        assert hint.group(1) in catalog, code
