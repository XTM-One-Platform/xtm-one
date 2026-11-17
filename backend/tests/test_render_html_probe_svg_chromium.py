"""The probe of ``render_html_to_image`` measures an inline SVG figure, in a real Chromium.

A graph drawn as one ``<svg>`` used to come back "clean" while a label sat on a
node, a label ran past its container and an edge crossed a label: the HTML pass
compares text with text and reads scroll boxes, which SVG shapes and paths do
not have. The SVG pass measures the labels with the fonts as rendered (no width
estimate), tests each shape's real outline (``isPointInFill``) and samples each
edge along its length (``getPointAtLength``).

Skipped where Playwright's Chromium is not installed; the ``Browser tool (Chromium)``
CI job installs it and sets ``REQUIRE_CHROMIUM=1``, which turns the skip into a failure.
"""

from __future__ import annotations

import os

import pytest

from app.services.builtin_tools.html_render import format_probe, format_svg_stats, render_html

pytestmark = pytest.mark.chromium

WIDTH, HEIGHT = 600, 220

# One defect of each kind, far from each other: a label overflowing its rect,
# a label drawn over a node it does not start in, an edge crossing a label;
# and two edges crossing each other (a statistic, not a finding).
BROKEN = f"""<svg xmlns="http://www.w3.org/2000/svg" width="{WIDTH}" height="{HEIGHT}" style="font-family: sans-serif">
  <rect x="10" y="10" width="120" height="40" fill="#eeeeee"/>
  <text x="15" y="35" font-size="16">A long label running out of its box</text>
  <circle cx="500" cy="40" r="20" fill="#0015a8"/>
  <text x="470" y="45" font-size="14">Node label</text>
  <path d="M 20 150 L 580 150" stroke="#000000" stroke-width="2" fill="none"/>
  <text x="300" y="155" font-size="14">Crossed</text>
  <line x1="20" y1="170" x2="200" y2="210" stroke="#000000"/>
  <line x1="20" y1="210" x2="200" y2="170" stroke="#000000"/>
</svg>"""

# The same parts laid out cleanly: the label fits its rect, the node's label
# sits beside it, the edge passes under the label, the edges do not cross.
CLEAN = f"""<svg xmlns="http://www.w3.org/2000/svg" width="{WIDTH}" height="{HEIGHT}" style="font-family: sans-serif">
  <rect x="0" y="0" width="{WIDTH}" height="{HEIGHT}" fill="#ffffff"/>
  <rect x="10" y="10" width="300" height="40" fill="#eeeeee"/>
  <text x="15" y="35" font-size="16">Short</text>
  <circle cx="500" cy="40" r="20" fill="#0015a8"/>
  <text x="530" y="45" font-size="14">Node</text>
  <path d="M 20 150 L 580 150" stroke="#000000" stroke-width="2" fill="none"/>
  <text x="300" y="130" font-size="14">Above</text>
  <line x1="20" y1="180" x2="200" y2="180" stroke="#000000"/>
  <line x1="20" y1="200" x2="200" y2="200" stroke="#000000"/>
</svg>"""


async def _probe(html: str, width: int = WIDTH, height: int = HEIGHT) -> dict:
    try:
        _png, probe = await render_html(html, width=width, height=height, scale=1, font_css="")
    except Exception as exc:
        text = str(exc)
        if "Executable doesn't exist" not in text and "playwright install" not in text.lower():
            raise
        if os.environ.get("REQUIRE_CHROMIUM") == "1":
            pytest.fail(f"Playwright's Chromium is required here and is not installed: {exc}")
        pytest.skip("Playwright's Chromium is not installed (python -m playwright install chromium)")
    return probe


async def test_an_svg_with_one_defect_of_each_kind_reports_each_once_with_its_text():
    probe = await _probe(BROKEN)
    svg = probe["svg"]

    assert [item["text"] for item in svg["ovf"]] == ['text "A long label running out of its box"']
    assert svg["ovf"][0]["shape"] == "rect"
    assert [item["text"] for item in svg["overlap"]] == ['text "Node label"']
    assert svg["overlap"][0]["shape"] == "circle"
    assert [item["text"] for item in svg["edge"]] == ['text "Crossed"']
    assert svg["edge"][0]["first"] == "path"
    assert svg["crossings"] == 1
    assert svg["truncated"] is False

    text, _total = format_probe(probe, width=WIDTH, height=HEIGHT)
    lines = text.splitlines()
    assert any(
        line.startswith('OVF text "A long label running out of its box": runs past the border of rect')
        for line in lines
    )
    assert any(line.startswith('OVERLAP text "Node label" x circle: label drawn over a shape') for line in lines)
    assert 'EDGE-ON-TEXT text "Crossed": crossed by 1 edge (first path)' in lines
    assert "1 edge-edge crossing(s)" in format_svg_stats(probe)


async def test_a_clean_svg_stays_clean():
    probe = await _probe(CLEAN)

    assert probe["svg"]["svgs"] == 1
    assert probe["svg"]["labels"] == 3
    assert probe["svg"]["crossings"] == 0
    assert format_probe(probe, width=WIDTH, height=HEIGHT) == ("", 0)


# One edge defect of each kind: an edge ending deep inside its target circle
# (the arrowhead hidden in the node), an edge stopping 10 px short of its
# target, two edges on one lane 1 px apart, an edge riding a rect's top border.
EDGES_BROKEN = """<svg xmlns="http://www.w3.org/2000/svg" width="800" height="400">
  <circle cx="100" cy="100" r="30" fill="#223344"/>
  <circle cx="400" cy="100" r="30" fill="#223344"/>
  <circle cx="700" cy="100" r="30" fill="#223344"/>
  <rect x="300" y="250" width="200" height="80" fill="#223344"/>
  <path data-edge="inside" d="M 130 100 L 390 100" stroke="#000000" stroke-width="2" fill="none"/>
  <path data-edge="short" d="M 430 100 L 660 100" stroke="#000000" stroke-width="2" fill="none"/>
  <path data-edge="lane-a" d="M 100 200 L 250 200" stroke="#000000" stroke-width="2" fill="none"/>
  <path data-edge="lane-b" d="M 120 201 L 270 201" stroke="#000000" stroke-width="2" fill="none"/>
  <path data-edge="border" d="M 300 249 L 500 249" stroke="#000000" stroke-width="2" fill="none"/>
</svg>"""

# The same graph drawn cleanly: edges meet their node's outline (one with an
# arrowhead whose tip touches it), lanes 20 px apart, the rect's edge clear of
# its border.
EDGES_CLEAN = """<svg xmlns="http://www.w3.org/2000/svg" width="800" height="400">
  <defs><marker id="tip" markerWidth="8" markerHeight="8" refX="0" refY="4" orient="auto">
    <path d="M0,0 L8,4 L0,8 z" fill="#000000"/></marker></defs>
  <circle cx="100" cy="100" r="30" fill="#223344"/>
  <circle cx="400" cy="100" r="30" fill="#223344"/>
  <circle cx="700" cy="100" r="30" fill="#223344"/>
  <rect x="300" y="250" width="200" height="80" fill="#223344"/>
  <path data-edge="ab" d="M 130 100 L 370 100" stroke="#000000" stroke-width="2" fill="none"/>
  <path data-edge="bd" d="M 430 100 L 654 100" stroke="#000000" stroke-width="2" fill="none" marker-end="url(#tip)"/>
  <path data-edge="one" d="M 100 200 L 250 200" stroke="#000000" stroke-width="2" fill="none"/>
  <path data-edge="two" d="M 120 220 L 270 220" stroke="#000000" stroke-width="2" fill="none"/>
  <path data-edge="to-rect" d="M 400 160 L 400 250" stroke="#000000" stroke-width="2" fill="none"/>
</svg>"""


async def test_an_svg_with_one_edge_defect_of_each_kind_reports_each_once():
    probe = await _probe(EDGES_BROKEN, 800, 400)
    svg = probe["svg"]

    assert [item["edge"] for item in svg["ends"]] == ["path [data-edge=inside]"]
    assert svg["ends"][0]["shape"] == "circle"
    assert svg["ends"][0]["px"] >= 15
    assert [item["edge"] for item in svg["short"]] == ["path [data-edge=short]"]
    assert 8 <= svg["short"][0]["px"] <= 12
    assert [(item["a"], item["b"]) for item in svg["lane"]] == [("path [data-edge=lane-a]", "path [data-edge=lane-b]")]
    assert [item["edge"] for item in svg["border"]] == ["path [data-edge=border]"]
    assert svg["border"][0]["shape"] == "rect"

    text, total = format_probe(probe, width=800, height=400)
    lines = text.splitlines()
    assert total == 4
    assert any(line.startswith("EDGE-END path [data-edge=inside]: ends ") for line in lines)
    assert any(line.startswith("EDGE-END path [data-edge=short]: stops ") for line in lines)
    assert any(line.startswith("EDGE-LANE path [data-edge=lane-a] x path [data-edge=lane-b]") for line in lines)
    assert any(line.startswith("EDGE-BORDER path [data-edge=border]: runs along the border of rect") for line in lines)


async def test_an_svg_with_clean_edges_stays_clean():
    probe = await _probe(EDGES_CLEAN, 800, 400)

    assert probe["svg"]["edges"] == 5
    assert format_probe(probe, width=800, height=400) == ("", 0)


async def test_the_html_probe_is_unchanged_and_reports_no_svg_without_one():
    probe = await _probe(
        '<div style="position:absolute;left:10px;top:10px;font-size:20px">First</div>'
        '<div style="position:absolute;left:12px;top:12px;font-size:20px">Second</div>'
    )

    assert len(probe["overlap"]) == 1
    assert probe["svg"]["svgs"] == 0
    assert format_svg_stats(probe) == ""


# An HTML badge positioned over an inline SVG figure, in the figure's own
# coordinates: the SVG pass used to test SVG <text> only, so a count badge
# sitting on a lit line came back clean.
def _badge_over_edge(top: float) -> str:
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" width="1200" height="700" '
        'style="position:absolute;left:0;top:0">'
        '<path d="M 868 502 L 1003 502" stroke="#000000" stroke-width="2" fill="none"/></svg>'
        f'<div style="position:absolute;left:903px;top:{top}px;width:22px;height:20px;'
        'background:#0015a8;color:#ffffff;font:12px sans-serif;text-align:center">4</div>'
    )


async def test_an_html_badge_sitting_on_an_svg_edge_is_reported_once():
    probe = await _probe(_badge_over_edge(495.5), 1200, 700)

    assert [item["text"] for item in probe["svg"]["edge"]] == ['div "4"']
    assert probe["svg"]["edge"][0]["first"] == "path"
    text, _total = format_probe(probe, width=1200, height=700)
    assert 'EDGE-ON-TEXT div "4": crossed by 1 edge (first path)' in text.splitlines()


async def test_an_html_badge_20_px_away_from_the_edge_reports_nothing():
    probe = await _probe(_badge_over_edge(455.5), 1200, 700)

    assert probe["svg"]["edge"] == []
    assert format_probe(probe, width=1200, height=700) == ("", 0)
