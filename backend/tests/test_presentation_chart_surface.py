"""A chart's surface: framed or bare, and steady while it builds.

A chart showed a filled box behind its plot on every slide entry and lost it a
second later: the stylesheet painted the waiting placeholder on any canvas
holding ``data-deck-pending``, the export's ready signal, which a drawing also
holds while it grows in. There was no way to choose the surface either.

Pinned here: the ``framed`` field (on by default), the stylesheet rules that
paint the card and keep the placeholder off a drawn canvas, and the export
that draws the card under the native chart. The pixels are read off the real
stylesheet by ``frontend/src/components/presentation/blocks/chart-surface.test.tsx``
and in a browser by ``frontend/tests_e2e/presentation-charts.spec.ts``.
"""

from __future__ import annotations

import re
from pathlib import Path

from app.services.presentation_export import EXTRACT_PRIMITIVES_JS
from app.services.presentation_spec import ChartBlock

FRONTEND_PRESENTATION = Path(__file__).resolve().parents[2] / "frontend" / "src" / "components" / "presentation"

_CHART = {"type": "chart", "kind": "bar", "series": [{"name": "ARR", "values": [1, 2]}], "categories": ["Q1", "Q2"]}


def _read(*parts: str) -> str:
    return FRONTEND_PRESENTATION.joinpath(*parts).read_text(encoding="utf-8")


def _css() -> str:
    """The deck stylesheet without its comments, so a rule's selector is all that precedes its body."""
    return re.sub(r"/\*.*?\*/", "", _read("presentation.css"), flags=re.S)


def _rules(css: str, selector_pattern: str) -> list[str]:
    """The bodies of the rules whose selector matches ``selector_pattern``."""
    return [m.group(2) for m in re.finditer(r"([^{}]+)\{([^{}]*)\}", css) if re.search(selector_pattern, m.group(1))]


def test_a_chart_is_framed_unless_it_says_otherwise():
    assert ChartBlock.model_validate(_CHART).framed is True
    assert ChartBlock.model_validate({**_CHART, "framed": False}).framed is False
    assert "framed" in ChartBlock.model_fields
    assert "dashboard panel" in (ChartBlock.model_fields["framed"].description or "")


def test_the_placeholder_is_never_painted_under_a_drawing():
    css = _css()
    painted = [
        m.group(1).strip()
        for m in re.finditer(r"([^{}]+)\{([^{}]*)\}", css)
        if ".deck-chart-canvas" in m.group(1) and re.search(r"\bbackground(-color)?:", m.group(2))
    ]
    assert painted == [
        ".deck-root .deck-chart-canvas:is([data-deck-pending], [data-deck-unfinished])"
        ":not(:has(> .recharts-wrapper)):not(:has(> .deck-chart-map))"
    ], painted
    # Every drawing mounts Recharts' wrapper straight into the canvas, a map its own SVG.
    canvas = _read("blocks", "ChartCanvas.tsx")
    assert canvas.count("<ComposedChart") >= 1 and canvas.count("<PieChart") == 2
    assert "ResponsiveContainer" not in canvas
    map_canvas = _read("blocks", "MapCanvas.tsx")
    assert re.search(r'return \(\s*<>\s*<svg\s+className="deck-chart-map"', map_canvas)


def test_a_framed_chart_stands_on_the_card_a_panel_does():
    css = _css()
    (frame,) = _rules(css, r'^\s*\.deck-root \.deck-chart\[data-framed="true"\]\s*$')
    (panel,) = _rules(css, r"^\s*\.deck-root \.deck-panel\s*$")
    for declaration in (
        "border-radius: var(--deck-radius);",
        "border: var(--deck-hairline) solid var(--deck-line);",
        "background: var(--deck-card-bg);",
        "--deck-chart-halo: var(--deck-card-bg);",
        "padding: var(--deck-space-2) var(--deck-space-3);",
    ):
        assert declaration in frame, declaration
        assert declaration in panel, declaration
    # Lit like every card, with the bloom and no beam, and never a second
    # time inside a panel, where the figure is `display: contents`.
    lit = re.search(r"\.deck-root :is\(([^)]*)\)::after \{", css)
    assert lit and '.deck-chart[data-framed="true"]' in lit.group(1)
    assert ".deck-root :is(.deck-panel, .deck-chart) { --deck-beam: 0px; }" in css
    assert ".deck-root .deck-panel-body > .deck-chart { display: contents; }" in css
    # Same specificity as the card light: it wins by coming after it.
    reset = css.index(".deck-root .deck-panel-body > .deck-chart::after { content: none; }")
    assert reset > lit.start(), "the panel's reset must follow the card light"


def test_the_figure_says_whether_it_is_framed():
    chart = _read("blocks", "chart.tsx")
    assert 'data-framed={block.framed === false ? undefined : "true"}' in chart
    schema = _read("block-schema.ts")
    assert (
        'flag("framed", "Framed", {\n        defaultOn: true,'
        in schema.split("chart: {", 1)[1].split("key_value:", 1)[0]
    )


def test_the_export_draws_a_framed_chart_s_card_under_the_chart():
    js = EXTRACT_PRIMITIVES_JS
    # One shape emitter for every element box, the chart's card included.
    assert "const emitRect = (el, cs, rect, opacity, k) => {" in js
    assert "emitRect(el, cs, rect, opacity, k);" in js
    emit_chart = js[js.index("const emitChart = (figure, opacity) => {") : js.index("const visit = (el) => {")]
    framed = emit_chart.index("emitRect(figure, fcs, figureRect, opacity, fitOf(figure))")
    # Before the chart, the picture or the legend: the card is under them.
    assert framed < emit_chart.index("const reason = pictureReason(chart);")
    assert 'fcs.display !== "contents"' in emit_chart
    # The chart sits on its card: what alpha blends onto in the writer.
    assert 'const surface = framed ? figure : canvas.closest(".deck-panel") || slide;' in emit_chart
