"""The native ``org_chart`` block, end to end on the backend.

The contract (``OrgChartBlock`` in ``presentation_spec.py``): a tree of seats,
every refusal naming the seat it is about. The agent's words (the
``generate_presentation`` / ``edit_presentation`` descriptions, the validated
examples) and the hint pointing a reporting tree drawn as an ``architecture``
at the block. The layout check's findings, from the engine's facts and from
what the page painted. The editable PPTX: native groups, ovals and
connectors, from the renderer's own drawing (``tests/data/org_chart``, pinned
to the renderer by ``blocks/org-chart-layout.test.tsx``). Every fixture is
synthetic.
"""

from __future__ import annotations

import copy
import io
import re
from pathlib import Path

import pytest
from pptx import Presentation as PptxPresentation
from pptx.enum.shapes import MSO_SHAPE, MSO_SHAPE_TYPE

from app.services import pptx_writer
from app.services.presentation_guidance import (
    BLOCK_EXAMPLES,
    ORG_CHART_HEADCOUNT_EXAMPLE,
    ORG_CHART_LEADERSHIP_EXAMPLE,
    build_generate_presentation_description,
)
from app.services.presentation_layout import LayoutReport, findings_from_observations, format_layout_report
from app.services.presentation_spec import (
    BLOCK_TYPES,
    MAX_ORG_CHART_NODES,
    ORG_CHART_HINT,
    ORG_CHART_READABLE_NODES,
    PANEL_BLOCK_TYPES,
    PresentationSpecError,
    architecture_reads_as_org_chart,
    block_catalog,
    org_chart_hint_note,
    org_chart_hints,
    spec_reference,
    validate_spec,
)

FIXTURE = Path(__file__).parent / "data" / "org_chart" / "figure.html"
FRONTEND = Path(__file__).resolve().parents[2] / "frontend" / "src" / "components" / "presentation"


def _deck(*blocks: dict, title: str = "Team") -> dict:
    return {"title": "Org", "slides": [{"layout": "content", "title": title, "blocks": list(blocks)}]}


def _person(node_id: str, name: str, role: str = "Engineer", manager: str | None = None, **extra) -> dict:
    node = {"id": node_id, "kind": "person", "name": name, "role": role, **extra}
    if manager:
        node["manager"] = manager
    return node


def _chart(*nodes: dict, **extra) -> dict:
    return {"type": "org_chart", "nodes": list(nodes), **extra}


def _refused(block: dict) -> str:
    with pytest.raises(PresentationSpecError) as exc:
        validate_spec(_deck(block))
    return " | ".join(problem.message for problem in exc.value.problems)


# ── The contract ──────────────────────────────────────────────────────────────


def test_org_chart_is_a_leaf_block_of_the_catalog_and_a_dashboard_panel():
    assert "org_chart" in BLOCK_TYPES
    assert "org_chart" in PANEL_BLOCK_TYPES
    fields = block_catalog()["org_chart"]
    for field in ("nodes", "orientation", "style", "depth_limit", "collapse_below", "expand", "show_counts", "count_mode",
                  "group_by_branch", "legend", "roots", "size", "fill", "label_size", "photo_size", "show_dotted", "spine"):
        assert field in fields, field
    reference = spec_reference()
    assert "- org_chart: nodes: OrgChartNode[]" in reference
    assert '- OrgNodeKind: "person"|"vacant"|"planned"|"group"|"bubble"|"external"' in reference
    assert '- OrgOrientation: "top_down"|"left_right"|"radial"' in reference
    # In a column, and as a dashboard panel.
    validate_spec(
        {
            "title": "Org",
            "slides": [
                {"layout": "content", "title": "Team", "columns": [[{"type": "text", "content": "Why"}], [ORG_CHART_LEADERSHIP_EXAMPLE]]},
                {
                    "layout": "dashboard",
                    "title": "Headcount",
                    "tiles": [{"value": "40", "label": "Today"}, {"value": "47", "label": "Target"}],
                    "panels": [{"title": "Engineering", "span": 2, "block": ORG_CHART_HEADCOUNT_EXAMPLE}],
                },
            ],
        }
    )


def test_the_guidance_examples_validate_and_the_root_takes_the_core_emphasis_by_default():
    assert BLOCK_EXAMPLES["org_chart"] is ORG_CHART_LEADERSHIP_EXAMPLE
    for example in (ORG_CHART_LEADERSHIP_EXAMPLE, ORG_CHART_HEADCOUNT_EXAMPLE):
        spec = validate_spec(_deck(example))
        stored = spec["slides"][0]["blocks"][0]
        assert stored["type"] == "org_chart"
        # Unset emphasis stays unset: the renderer gives the root its core emphasis.
        assert all("emphasis" not in node for node in stored["nodes"])


@pytest.mark.parametrize(
    ("block", "words"),
    [
        (
            _chart(_person("alice", "Alice", manager="bob"), _person("bob", "Bob", manager="carol"), _person("carol", "Carol", manager="alice")),
            ["alice -> bob -> carol -> alice", "cycle"],
        ),
        (_chart(_person("alice", "Alice"), _person("bob", "Bob", manager="zed")), ["node 'bob'", "manager 'zed' names no node"]),
        (
            _chart(_person("alice", "Alice"), {"id": "team", "kind": "bubble", "role": "Platform", "manager": "alice"}),
            ["node 'team' (bubble) needs a count"],
        ),
        (_chart(_person("alice", "Alice"), _person("bob", "Bob")), ["nodes 'alice', 'bob' have no manager", "roots"]),
        (_chart(_person("alice", "Alice"), _person("bob", "Bob", manager="alice", dotted_to=["bob"])), ["node 'bob'", "itself"]),
        (_chart(_person("alice", "Alice"), _person("bob", "Bob", manager="alice", dotted_to=["alice"])), ["node 'bob'", "SECOND manager"]),
        (_chart(_person("alice", "Alice"), _person("bob", "Bob", manager="alice", dotted_to=["zed"])), ["node 'bob'", "dotted_to 'zed'"]),
        (_chart(_person("alice", "Alice"), _person("bob", "Bob", manager="alice"), _person("bob", "Bob again", manager="alice")), ["unique: bob"]),
        (_chart({"id": "alice", "kind": "person", "role": "CEO"}, _person("bob", "Bob", manager="alice")), ["node 'alice' (person) needs a name"]),
        (_chart(_person("alice", "Alice", count=3), _person("bob", "Bob", manager="alice")), ["node 'alice' (person)", "group or a bubble"]),
        (
            _chart(_person("alice", "Alice"), {"id": "open", "kind": "vacant", "name": "Bob", "role": "CTO", "manager": "alice"}),
            ["node 'open' (vacant)", "drop the name"],
        ),
        (_chart(_person("alice", "Alice"), {"id": "hire", "kind": "planned", "manager": "alice"}), ["node 'hire' (planned) needs a role"]),
        (_chart(_person("alice", "Alice", period="Q3"), _person("bob", "Bob", manager="alice")), ["node 'alice' (person): period"]),
        (_chart(_person("alice", "Alice", collapsed=True), _person("bob", "Bob", manager="alice", collapsed=True)), ["node 'bob' is collapsed and has no reports"]),
        (_chart(_person("alice", "Alice"), _person("bob", "Bob", manager="alice"), expand=["zed"]), ["expand names no node: 'zed'"]),
        (_chart(_person("alice", "Alice"), _person("bob", "Bob", manager="alice"), legend="custom"), ['legend "custom" needs legend_items']),
        (
            _chart(_person("alice", "Alice"), _person("bob", "Bob", manager="alice"), legend_items=[{"label": "Open", "kind": "vacant"}]),
            ['drawn by legend "custom" only'],
        ),
        (
            _chart(_person("alice", "Alice"), {"id": "team", "kind": "group", "role": "Data", "photo_file_id": "00000000-0000-0000-0000-000000000001", "manager": "alice"}),
            ["node 'team' (group) is a team"],
        ),
    ],
    ids=[
        "cycle", "unknown-manager", "bubble-without-count", "two-roots", "dotted-to-self", "dotted-to-manager",
        "dotted-to-unknown", "duplicate-id", "person-without-name", "person-with-count", "vacant-with-name",
        "planned-without-role", "period-on-a-person", "collapsed-leaf", "unknown-expand", "custom-legend-empty",
        "legend-items-not-custom", "photo-on-a-team",
    ],
)
def test_an_org_chart_that_is_not_one_tree_is_refused_naming_the_seat(block, words):
    message = _refused(block)
    for word in words:
        assert word in message, (word, message)


def test_several_roots_are_drawn_only_when_listed():
    two = _chart(_person("alice", "Alice"), _person("bob", "Bob"), roots=["alice", "bob"])
    assert validate_spec(_deck(two))["slides"][0]["blocks"][0]["roots"] == ["alice", "bob"]
    assert "node 'bob' has no manager and is not in roots" in _refused(
        _chart(_person("alice", "Alice"), _person("bob", "Bob"), _person("carol", "Carol", manager="alice"), roots=["alice"])
    )
    assert "node 'carol' is listed in roots and reports to 'alice'" in _refused(
        _chart(_person("alice", "Alice"), _person("carol", "Carol", manager="alice"), roots=["alice", "carol"])
    )


def test_more_seats_than_the_hard_cap_are_refused_naming_the_first_one_past_it():
    nodes = [_person("root", "Alice")] + [_person(f"s{i}", f"Seat {i}", manager="root") for i in range(MAX_ORG_CHART_NODES)]
    message = _refused(_chart(*nodes))
    assert f"more than the {MAX_ORG_CHART_NODES}" in message
    assert f"the first past it is 's{MAX_ORG_CHART_NODES - 1}'" in message


# ── The agent's words ─────────────────────────────────────────────────────────


def test_both_deck_tools_teach_the_block_with_the_rule_and_two_minimal_examples():
    from app.services.builtin_tools.presentation import _EDIT_DESCRIPTION
    from app.services.presentation_guidance import _compact

    for description in (build_generate_presentation_description(), _EDIT_DESCRIPTION):
        assert ORG_CHART_HINT in description
        assert "never an architecture block, stacked pills or a drawn picture" in description
        assert _compact(ORG_CHART_LEADERSHIP_EXAMPLE) in description
        assert _compact(ORG_CHART_HEADCOUNT_EXAMPLE) in description
        assert str(ORG_CHART_READABLE_NODES) in description


def test_the_design_skill_teaches_the_rule_and_the_validated_compositions():
    skill = (Path(__file__).resolve().parents[1] / "app" / "agents" / "seed_data" / "skill_content" / "presentation-design.md").read_text(
        encoding="utf-8"
    )
    assert "never an architecture block, stacked pills or a drawn picture" in skill
    for composition in ("### 3.23 Leadership team", "### 3.24 Headcount plan", "### 3.25 Hiring plan", "### 3.26 Territory"):
        assert composition in skill, composition
    # Every org chart slide the skill shows validates, and none is drawn as anything else.
    import json

    slides = [json.loads(raw) for raw in re.findall(r"```json\n(.*?)\n```", skill, flags=re.S) if '"type":"org_chart"' in raw]
    assert len(slides) == 4
    kinds: set[str] = set()
    for slide in slides:
        spec = validate_spec({"title": "t", "slides": [slide]})
        (block,) = spec["slides"][0]["blocks"]
        kinds |= {node["kind"] for node in block["nodes"]}
    assert kinds == {"person", "vacant", "planned", "group", "bubble", "external"}


LEGACY = {
    "type": "architecture",
    "nodes": [
        {"id": "ceo", "label": "Alice Martin", "sublabel": "CEO", "row": 1, "col": 1, "col_span": 3},
        {"id": "a", "label": "Bob Stone", "row": 2, "col": 1},
        {"id": "b", "label": "Carol Diaz", "row": 2, "col": 2},
        {"id": "c", "label": "Dan Wells", "row": 2, "col": 3},
    ],
    "edges": [{"source": "ceo", "target": node, "arrows": "none"} for node in ("a", "b", "c")],
}


def test_a_reporting_tree_drawn_as_an_architecture_is_pointed_at_the_org_chart():
    assert architecture_reads_as_org_chart(LEGACY)
    # A real architecture is left alone: arrows, a second row above, a top node not spanning its row.
    flows = copy.deepcopy(LEGACY)
    flows["edges"][0]["arrows"] = "end"
    assert not architecture_reads_as_org_chart(flows)
    narrow = copy.deepcopy(LEGACY)
    narrow["nodes"][0]["col_span"] = 1
    assert not architecture_reads_as_org_chart(narrow)
    deeper = copy.deepcopy(LEGACY)
    deeper["nodes"][3]["row"] = 3
    assert not architecture_reads_as_org_chart(deeper)
    # The deck still validates; the hint names the slide.
    spec = validate_spec({"title": "Org", "slides": [{"layout": "content", "title": "Intro", "blocks": [{"type": "text", "content": "Hi"}]}, _deck(LEGACY)["slides"][0]]})
    assert org_chart_hints(spec) == [(1, org_chart_hints(spec)[0][1])]
    note = org_chart_hint_note(spec)
    assert note.startswith("Hint (slide 2):") and ORG_CHART_HINT in note
    # An edit is told about the slides it wrote only.
    assert org_chart_hint_note(spec, slide_ids={spec["slides"][0]["id"]}) == ""
    assert org_chart_hint_note(spec, slide_ids={spec["slides"][1]["id"]}) == note
    # And the layout check names the slide.
    findings = findings_from_observations([{"index": 1, "slide_id": spec["slides"][1]["id"], "layout": "content", "findings": []}], spec)
    assert [(f.kind, f.severity, f.slide_index) for f in findings] == [("org_hint", "advisory", 1)]
    report = format_layout_report(LayoutReport(slide_count=2, findings=tuple(findings)))
    assert 'slide 2 [content] "Team" (advisory)' in report


# ── The layout check ──────────────────────────────────────────────────────────

_SPEC = validate_spec(_deck(ORG_CHART_LEADERSHIP_EXAMPLE))
_CLEAN_FACTS = {
    "seats": 4, "drawn": 4, "folded": False, "spine": False, "broken": [], "unequal": [], "crossings": [], "overlaps": [],
    "counts": [], "undated": [], "scale": 1, "minType": 18, "minAllowed": 12, "refused": False, "wide": None,
}
_CLEAN_PAINTED = {"crossings": [], "unequal": [], "overflow": [], "overlaps": []}


def _org_findings(facts: dict | None = None, painted: dict | None = None, *extra: dict):
    observation = {"kind": "org_chart", "chart": 1, "block": "org-chart", "text": "", "facts": {**_CLEAN_FACTS, **(facts or {})},
                   "painted": {**_CLEAN_PAINTED, **(painted or {})}}
    slide = {"index": 0, "slide_id": _SPEC["slides"][0]["id"], "layout": "content", "findings": [observation, *extra]}
    return findings_from_observations([slide], _SPEC)


def test_a_clean_org_chart_is_no_finding():
    assert _org_findings() == []


@pytest.mark.parametrize(
    ("facts", "painted", "kind", "severity", "words"),
    [
        ({"refused": True, "minType": 9.4, "drawn": 48, "scale": 0.52}, {}, "org_scale", "overloaded", ["48 seats", "9.4px", "12px minimum", "collapse_below"]),
        ({"wide": {"level": 3, "width": 2400, "room": 1720}, "scale": 0.72}, {}, "org_width", "dense", ["level 3 is 2400px wide", "72%", "spine always"]),
        ({"broken": ["bob"]}, {}, "org_label", "dense", ["'bob'", "cut inside a word"]),
        ({}, {"overflow": ["carol|Chief Technology Officer"]}, "org_label", "dense", ["'carol'"]),
        ({"unequal": ["ceo"]}, {}, "org_siblings", "dense", ["under 'ceo'", "different heights"]),
        ({}, {"unequal": ["cto"]}, "org_siblings", "dense", ["under 'cto'"]),
        ({}, {"crossings": ["ceo|cfo"]}, "org_crossing", "dense", ["runs through 'cfo'"]),
        ({"crossings": ["dotted:cfo>cro|cto"]}, {}, "org_crossing", "dense", ["runs through 'cto'"]),
        ({"overlaps": ["eng|hire"]}, {}, "org_bubble", "dense", ["label of 'eng' meets 'hire'"]),
        ({"counts": [{"id": "team", "declared": 12, "computed": 9}]}, {}, "org_counts", "dense", ["'team' declares 12", "add up to 9"]),
        ({"drawn": ORG_CHART_READABLE_NODES + 5}, {}, "org_crowded", "advisory", [f"{ORG_CHART_READABLE_NODES + 5} seats", "folds none"]),
        ({"undated": [3]}, {}, "org_undated", "advisory", ["level 3", "period"]),
    ],
    ids=["refused", "wide", "broken", "painted-overflow", "unequal", "painted-unequal", "painted-crossing", "dotted-crossing",
         "bubble-overlap", "counts", "crowded", "undated"],
)
def test_each_org_chart_finding(facts, painted, kind, severity, words):
    findings = _org_findings(facts, painted)
    assert [(f.kind, f.severity) for f in findings] == [(kind, severity)]
    for word in words:
        assert word in findings[0].message, (word, findings[0].message)


def test_a_folded_chart_is_never_crowded_and_a_refused_one_is_not_also_too_wide_or_sparse():
    assert _org_findings({"drawn": ORG_CHART_READABLE_NODES + 5, "folded": True}) == []
    sparse = {"kind": "sparse", "unused": 0.6, "empty": 400, "slide": 1080}
    kinds = [f.kind for f in _org_findings({"refused": True, "minType": 9, "wide": {"level": 2, "width": 3000, "room": 1700}}, {}, sparse)]
    assert kinds == ["org_scale"]


# ── The editable PPTX ─────────────────────────────────────────────────────────


def _walk(shapes):
    for shape in shapes:
        yield shape
        if shape.shape_type == MSO_SHAPE_TYPE.GROUP:
            yield from _walk(shape.shapes)


def test_the_writer_makes_the_chart_one_group_each_seat_a_group_circles_ovals_and_lines_connectors():
    line = {"r": 120, "g": 130, "b": 150, "a": 1, "width": 2.5}
    fill = {"r": 20, "g": 30, "b": 50, "a": 1}
    text = lambda value, x, y, sub: {  # noqa: E731 - a fixture builder
        "kind": "text", "x": x, "y": y, "w": 200, "h": 30, "group": "o0-0", "subgroup": sub, "align": "left", "lineHeight": 28,
        "padding": {}, "valign": "middle", "runs": [{"text": value, "font": "IBM Plex Sans", "size": 23, "weight": 600, "color": {"r": 255, "g": 255, "b": 255, "a": 1}}],
    }
    items = [
        {"kind": "connector", "group": "o0-0", "points": [{"x": 500, "y": 200}, {"x": 500, "y": 240}, {"x": 300, "y": 240}, {"x": 300, "y": 280}], "line": line, "dash": None},
        {"kind": "rect", "group": "o0-0", "subgroup": "o0-0:alice", "x": 400, "y": 100, "w": 300, "h": 100, "fill": fill, "line": line, "radius": 16},
        text("Alice Martin", 470, 120, "o0-0:alice"),
        {"kind": "oval", "group": "o0-0", "subgroup": "o0-0:eng", "x": 200, "y": 280, "w": 200, "h": 200, "fill": fill, "line": line},
        text("24", 260, 360, "o0-0:eng"),
        {"kind": "rect", "group": "o0-0", "subgroup": "o0-0:open", "x": 700, "y": 280, "w": 300, "h": 90, "fill": fill, "line": line, "radius": 16, "dash": "dash"},
        {"kind": "connector", "group": "o0-0", "points": [{"x": 700, "y": 150}, {"x": 900, "y": 150}, {"x": 900, "y": 280}], "line": line, "dash": "dot"},
    ]
    data = pptx_writer.build_native_pptx([{"background": {"r": 7, "g": 13, "b": 25, "a": 1}, "items": items}], captures={}, aspect_ratio="16:9")
    slide = PptxPresentation(io.BytesIO(data)).slides[0]
    (chart,) = list(slide.shapes)
    assert chart.shape_type == MSO_SHAPE_TYPE.GROUP
    seats = [shape for shape in chart.shapes if shape.shape_type == MSO_SHAPE_TYPE.GROUP]
    assert len(seats) == 3
    shapes = list(_walk(chart.shapes))
    ovals = [s for s in shapes if s.shape_type == MSO_SHAPE_TYPE.AUTO_SHAPE and s.auto_shape_type == MSO_SHAPE.OVAL]
    assert len(ovals) == 1 and ovals[0].width == ovals[0].height
    connectors = [s for s in shapes if s.shape_type == MSO_SHAPE_TYPE.LINE or s._element.tag.endswith("cxnSp")]
    assert len(connectors) == 3 + 2
    texts = [s.text_frame.text for s in shapes if s.has_text_frame and s.text_frame.text]
    assert {"Alice Martin", "24"} <= set(texts)
    xml = slide.shapes._spTree.xml
    assert 'prstDash val="dash"' in xml and 'prstDash val="sysDot"' in xml


def _org_chart_page(page, css: str) -> None:
    figure = FIXTURE.read_text(encoding="utf-8")
    page.set_content(
        '<div class="deck-root" data-scheme="dark"><div class="deck-canvas deck-static-slide" data-scheme="dark" '
        'style="position:relative;width:1920px;height:1080px;overflow:hidden">'
        '<div class="deck-slide" data-layout="content" style="padding:80px 100px;box-sizing:border-box;width:1920px;height:1080px">'
        f'<div class="deck-blocks">{figure}</div></div></div></div>'
    )
    page.add_style_tag(content=css)


def _chromium():
    try:
        from playwright.sync_api import Error as PlaywrightError
        from playwright.sync_api import sync_playwright
    except ImportError:  # pragma: no cover - the export dependency is always installed
        pytest.skip("playwright is not installed")
    return sync_playwright, PlaywrightError


def test_the_renderers_drawing_reaches_the_editable_pptx_as_native_shapes_in_a_real_browser():
    """In Chromium on the real stylesheet, the renderer's own drawing (the
    fixture): every seat a group of native shapes, every circle an oval of
    its size, every line connectors, the words as text, the icons pictures."""
    from app.services.presentation_export import EXTRACT_PRIMITIVES_JS
    from tests.test_presentation_export import _deck_stylesheet

    css = _deck_stylesheet()
    sync_playwright, playwright_error = _chromium()
    with sync_playwright() as p:
        try:
            browser = p.chromium.launch()
        except playwright_error as exc:
            pytest.skip(f"no Chromium for Playwright here: {exc}")
        page = browser.new_page(viewport={"width": 1920, "height": 1080})
        _org_chart_page(page, css)
        seats = page.evaluate("() => document.querySelectorAll('.deck-org-node').length")
        circles = page.evaluate("() => document.querySelectorAll('.deck-org-node > circle.deck-org-shape').length")
        lines = page.evaluate(
            "() => Array.from(document.querySelectorAll('.deck-org-link')).reduce((n, p) => n + p.dataset.points.split(';').length, 0)"
        )
        (slide,) = page.evaluate(EXTRACT_PRIMITIVES_JS)
        browser.close()
    items = slide["items"]
    chart = [item for item in items if str(item.get("group", "")).startswith("o")]
    assert chart, items
    assert {item["group"] for item in chart} == {chart[0]["group"]}
    assert len({item["subgroup"] for item in chart if item.get("subgroup") and ":legend:" not in item["subgroup"]}) == seats
    assert sum(1 for item in chart if item["kind"] == "connector" and not item.get("subgroup")) == lines
    ovals = [item for item in chart if item["kind"] == "oval"]
    assert len([item for item in ovals if abs(item["w"] - item["h"]) < 1.5]) == len(ovals) >= circles
    names = {item["runs"][0]["text"] for item in chart if item["kind"] == "text"}
    assert {"Alice Martin", "Bob Stone", "Carol Diaz", "18", "Open role"} <= names
    # A vacant seat's card is dashed, an external one's dotted, a dotted line dotted.
    dashes = {item.get("dash") for item in chart if item["kind"] in ("rect", "oval", "connector")}
    assert {"dash", "dot"} <= dashes
    assert all(not item.get("chart") for item in items if item["kind"] == "image")

    data = pptx_writer.build_native_pptx([slide], captures={}, aspect_ratio="16:9")
    prs = PptxPresentation(io.BytesIO(data))
    groups = [shape for shape in prs.slides[0].shapes if shape.shape_type == MSO_SHAPE_TYPE.GROUP]
    assert len(groups) == 1
    shapes = list(_walk(groups[0].shapes))
    inner_groups = [s for s in shapes if s.shape_type == MSO_SHAPE_TYPE.GROUP]
    assert len(inner_groups) >= seats
    assert len([s for s in shapes if s.shape_type == MSO_SHAPE_TYPE.AUTO_SHAPE and s.auto_shape_type == MSO_SHAPE.OVAL]) == len(ovals)
    assert len([s for s in shapes if s._element.tag.endswith("cxnSp")]) >= lines
    assert {"Alice Martin", "Open role"} <= {s.text_frame.text for s in shapes if s.has_text_frame}


def test_the_layout_audit_measures_what_the_page_painted_in_a_real_browser():
    """The clean drawing reports nothing; a card moved onto a line, a card of
    one manager made taller, a name painted wider than its line and a bubble's
    label pushed onto a neighbour are each read off the page."""
    from app.services.presentation_export import LAYOUT_AUDIT_JS
    from tests.test_presentation_export import _deck_stylesheet

    css = _deck_stylesheet()
    sync_playwright, playwright_error = _chromium()
    with sync_playwright() as p:
        try:
            browser = p.chromium.launch()
        except playwright_error as exc:
            pytest.skip(f"no Chromium for Playwright here: {exc}")
        page = browser.new_page(viewport={"width": 1920, "height": 1080})
        _org_chart_page(page, css)
        (clean,) = page.evaluate(LAYOUT_AUDIT_JS)
        page.evaluate(
            """() => {
              const seat = (id) => document.querySelector(`[data-org-node="${id}"]`);
              // Carol's card dragged onto Bob's bus, which does not reach her.
              const bus = document.querySelector('.deck-org-link[data-from="bob"]');
              const [x, y] = bus.dataset.points.split(";")[1].trim().split(/\\s+/)[0].split(",").map(Number);
              const carol = seat("carol").querySelector(".deck-org-shape");
              seat("carol").setAttribute("transform", `translate(${x - Number(carol.getAttribute("x")) + 40} ${y - Number(carol.getAttribute("y")) - 20})`);
              // One of Carol's cards taller than its siblings.
              const open = seat("open").querySelector(".deck-org-shape");
              open.setAttribute("height", String(Number(open.getAttribute("height")) + 30));
              // Bob's name set larger than the line it was fitted to.
              seat("bob").querySelector(".deck-org-name").setAttribute("font-size", "60");
              // The bubble's label moved onto the planned seat beside it.
              const hire = seat("hire").querySelector(".deck-org-shape");
              const label = seat("eng").querySelector(".deck-org-name");
              label.setAttribute("x", String(Number(hire.getAttribute("x")) + Number(hire.getAttribute("width")) / 2));
              label.setAttribute("y", String(Number(hire.getAttribute("y")) + Number(hire.getAttribute("height")) / 2));
            }"""
        )
        (broken,) = page.evaluate(LAYOUT_AUDIT_JS)
        browser.close()
    (observation,) = [o for o in clean["findings"] if o["kind"] == "org_chart"]
    assert observation["facts"]["seats"] == 8 and observation["facts"]["refused"] is False
    assert observation["painted"] == {"crossings": [], "unequal": [], "overflow": [], "overlaps": []}
    (observation,) = [o for o in broken["findings"] if o["kind"] == "org_chart"]
    painted = observation["painted"]
    assert any(entry.endswith("|carol") for entry in painted["crossings"]), painted
    assert "carol" in painted["unequal"], painted
    assert any(entry.startswith("bob|") for entry in painted["overflow"]), painted
    assert any(entry.startswith("eng|") for entry in painted["overlaps"]), painted
    spec = validate_spec(_deck({"type": "org_chart", "nodes": [_person("alice", "Alice"), _person("bob", "Bob", manager="alice")]}))
    slide = {"index": 0, "slide_id": spec["slides"][0]["id"], "layout": "content", "findings": [observation]}
    assert {f.kind for f in findings_from_observations([slide], spec)} >= {"org_crossing", "org_siblings", "org_label", "org_bubble"}


# ── The renderer and the video figure ─────────────────────────────────────────


def test_the_org_chart_is_drawn_through_react_only_by_one_pure_engine():
    view = (FRONTEND / "blocks" / "OrgChart.tsx").read_text(encoding="utf-8")
    engine = (FRONTEND / "blocks" / "org-chart-layout.ts").read_text(encoding="utf-8")
    for forbidden in ("foreignObject", "innerHTML", "dangerouslySetInnerHTML", "mermaid"):
        assert forbidden not in view, forbidden
    for impure in ("document.", "window.", "Math.random", "Date.now", "getBoundingClientRect"):
        assert impure not in engine, impure
    # The viewer, the exports, the layout check and the video read the same engine output.
    assert "layoutOrgChart(block" in view
    for hook in ("data-deck-org-chart", "data-org-facts", "data-points", "data-max-w", "data-count-to", "data-level"):
        assert hook in view, hook
    build = (FRONTEND / "figure-build.ts").read_text(encoding="utf-8")
    assert "org_chart: {" in build and 'levels: "data-level"' in build


def test_an_org_chart_is_a_video_figure_scene():
    from app.services.video_studio.spec import FigureScene, figure_block_problems

    assert figure_block_problems(ORG_CHART_HEADCOUNT_EXAMPLE) == []
    scene = FigureScene.model_validate({"type": "figure", "block_spec": ORG_CHART_HEADCOUNT_EXAMPLE, "reveal": "narration"})
    assert scene.block_spec["type"] == "org_chart"
    bad = {**ORG_CHART_HEADCOUNT_EXAMPLE, "nodes": [{"id": "a", "kind": "bubble", "role": "Team"}, *ORG_CHART_HEADCOUNT_EXAMPLE["nodes"]]}
    assert any("node 'a' (bubble) needs a count" in problem for problem in figure_block_problems(bad))
