"""Edge sides, aligned pairs and zone ends in the architecture block.

An edge takes ``source_side`` / ``target_side`` - the side it leaves and enters,
honoured even when it faces away - and ``lane`` - the edges of one pair of
nodes on different lanes run parallel, each at one offset on both nodes - and
may end on a zone's border, ``zone:<id, label or index>`` (a zone takes an
``id`` for it). Pinned here: the contract and its refusals, what the agent is
told, the renderer's hooks, and in Chromium the renderer's own drawing of the
fixture (``tests/data/architecture/edge-sides.*``, pinned to the renderer by
``blocks/architecture-edge-sides.test.tsx``) through the stylesheet, the layout
check and the editable PPTX. Every fixture is synthetic.
"""

from __future__ import annotations

import copy
import io
import itertools
import json
import re
from pathlib import Path

import pytest
from pptx import Presentation as PptxPresentation
from pptx.enum.shapes import MSO_SHAPE_TYPE

from app.services import pptx_writer
from app.services.presentation_guidance import ARCHITECTURE_EDGE_HINT, build_generate_presentation_description
from app.services.presentation_spec import (
    MAX_ARCHITECTURE_LANE,
    ZONE_REF_PREFIX,
    ArchitectureZone,
    PresentationSpecError,
    resolve_zone_ref,
    spec_reference,
    validate_spec,
)
from tests.test_presentation_architecture_bare import _chromium, _close, _page, _pixels, _svg_box
from tests.test_presentation_architecture_zone_chips import _to_page

DATA = Path(__file__).parent / "data" / "architecture"
FIXTURE = DATA / "edge-sides.html"
GEOMETRY = DATA / "edge-sides.json"
FRONTEND = Path(__file__).resolve().parents[2] / "frontend" / "src"

#: The vitest twin's `EDGE_SIDES`: a context node above a platform zone feeding
#: its border, a centre node in the zone's first row with a squared exchange
#: to each bottom corner, every side pinned, and the corners on lanes 1 and 2.
BLOCK: dict = {
    "type": "architecture",
    "nodes": [
        {"id": "context", "label": "Context", "icon": "radar", "row": 1, "col": 3},
        {"id": "centre", "label": "Core", "icon": "sparkles", "row": 2, "col": 3},
        {"id": "left", "label": "Product A", "icon": "brain", "row": 3, "col": 1},
        {"id": "right", "label": "Product B", "icon": "crosshair", "row": 3, "col": 5},
    ],
    "edges": [
        {"source": "context", "target": "zone:platform", "label": "Feeds"},
        {"source": "centre", "target": "left", "source_side": "left", "target_side": "top", "label": "Tasks"},
        {"source": "left", "target": "centre", "source_side": "top", "target_side": "left", "label": "Results"},
        {"source": "centre", "target": "right", "source_side": "right", "target_side": "top", "label": "Tasks"},
        {"source": "right", "target": "centre", "source_side": "top", "target_side": "right", "label": "Results"},
        {"source": "left", "target": "right", "route": "straight", "lane": 1, "label": "Requests"},
        {"source": "right", "target": "left", "route": "straight", "lane": 2, "label": "Answers"},
    ],
    "zones": [{"id": "platform", "label": "Platform", "row": 2, "col": 1, "row_span": 2, "col_span": 5}],
    "size": "lg",
}


def _block(**changes) -> dict:
    block = copy.deepcopy(BLOCK)
    block.update(changes)
    return block


def _with_edge(index: int, **fields) -> dict:
    block = _block()
    block["edges"][index] = {**block["edges"][index], **fields}
    return block


def _deck(block: dict) -> dict:
    return {"title": "Platform", "slides": [{"layout": "content", "blocks": [block]}]}


def _stored(block: dict) -> dict:
    return validate_spec(_deck(block))["slides"][0]["blocks"][0]


def _refused(block: dict) -> str:
    with pytest.raises(PresentationSpecError) as exc:
        validate_spec(_deck(block))
    return str(exc.value)


# ── The contract ─────────────────────────────────────────────────────────────


def test_the_fixture_is_stored_as_written_and_the_new_fields_stay_absent_when_unset():
    stored = _stored(_block())
    edges = stored["edges"]
    assert edges[0]["target"] == "zone:platform"
    assert {key: edges[1][key] for key in ("source_side", "target_side")} == {
        "source_side": "left",
        "target_side": "top",
    }
    assert [edges[5]["lane"], edges[6]["lane"]] == [1, 2]
    for key in ("source_side", "target_side", "lane"):
        assert key not in edges[0]
    assert stored["zones"][0]["id"] == "platform"
    unnamed = _block(
        zones=[{**BLOCK["zones"][0], "id": None}],
        edges=[{**BLOCK["edges"][0], "target": "zone:0"}, *BLOCK["edges"][1:]],
    )
    assert "id" not in _stored(unnamed)["zones"][0]


@pytest.mark.parametrize("side", ["top", "right", "bottom", "left"])
@pytest.mark.parametrize("end", ["source_side", "target_side"])
def test_every_side_can_be_pinned_at_either_end(end: str, side: str):
    assert _stored(_with_edge(1, **{end: side}))["edges"][1][end] == side


@pytest.mark.parametrize("end", ["source_side", "target_side"])
def test_a_side_is_one_of_the_four(end: str):
    assert f"edges.1.{end}" in _refused(_with_edge(1, **{end: "middle"}))


@pytest.mark.parametrize("lane", [1, MAX_ARCHITECTURE_LANE])
def test_a_lane_is_an_int_from_1_to_6(lane: int):
    assert MAX_ARCHITECTURE_LANE == 6
    assert _stored(_with_edge(5, lane=lane))["edges"][5]["lane"] == lane


@pytest.mark.parametrize("lane", [0, MAX_ARCHITECTURE_LANE + 1, 1.5, "one"])
def test_a_lane_outside_1_to_6_or_not_an_int_is_refused(lane: object):
    assert "edges.5.lane" in _refused(_with_edge(5, lane=lane))


def test_two_edges_of_a_pair_on_one_lane_are_refused_naming_both():
    message = _refused(_with_edge(6, lane=1))
    assert "edges.6: lane 1 between right and left is already edges.5's" in message
    assert "give each edge of the pair its own lane" in message


def test_a_lone_edge_may_name_a_lane():
    block = _block(edges=[*BLOCK["edges"][:5], {**BLOCK["edges"][5], "lane": 3}])
    assert _stored(block)["edges"][5]["lane"] == 3


def test_a_lane_on_an_edge_to_a_zone_is_refused():
    assert "edges.0: lane orders the edges between two nodes" in _refused(_with_edge(0, lane=1))


def test_zone_ids_are_unique():
    zones = [
        {"id": "platform", "label": "Platform", "row": 2, "col": 1, "row_span": 2, "col_span": 2},
        {"id": "platform", "label": "Other", "row": 2, "col": 3, "row_span": 2, "col_span": 3},
    ]
    assert "zone ids must be unique: platform." in _refused(_block(zones=zones))


@pytest.mark.parametrize("zone_id", ["1platform", "plat form", "zone:platform", "x" * 33])
def test_a_zone_id_is_a_short_identifier(zone_id: str):
    assert "zones.0.id" in _refused(_block(zones=[{**BLOCK["zones"][0], "id": zone_id}]))


@pytest.mark.parametrize("ref", ["zone:platform", "zone:0", "zone:Platform"])
def test_a_zone_is_named_by_its_id_its_index_or_its_label(ref: str):
    stored = _stored(_with_edge(0, target=ref))
    assert stored["edges"][0]["target"] == ref


@pytest.mark.parametrize("label", ["2026", "7"])
def test_a_digit_only_label_past_the_last_index_names_its_zone(label: str):
    """``zone:2026`` read as an index finds no zone; past the last index it is a label."""
    block = _block(zones=[{**BLOCK["zones"][0], "id": None, "label": label}])
    stored = _stored({**block, "edges": [{**BLOCK["edges"][0], "target": f"zone:{label}"}, *BLOCK["edges"][1:]]})
    assert stored["edges"][0]["target"] == f"zone:{label}"
    assert resolve_zone_ref(f"zone:{label}", [ArchitectureZone(**block["zones"][0])]) == [0]


def test_a_digit_only_key_within_the_zones_stays_an_index():
    zones = [
        ArchitectureZone(label="1", row=1, col=1),
        ArchitectureZone(label="0", row=2, col=1),
    ]
    assert resolve_zone_ref("zone:0", zones) == [0]
    assert resolve_zone_ref("zone:1", zones) == [1]
    assert resolve_zone_ref("zone:9", zones) == []


def test_a_zone_end_may_be_the_source():
    assert _stored(_with_edge(0, source="zone:platform", target="context"))["edges"][0]["source"] == "zone:platform"


def test_an_unknown_zone_is_refused_with_the_edge_index_and_the_zones_listed():
    message = _refused(_with_edge(0, target="zone:plat"))
    assert "edges.0.target names no zone: 'zone:plat' (zones: zone:platform ('Platform'))." in message
    unnamed = _block(zones=[{**BLOCK["zones"][0], "id": None}])
    assert "edges.0.target names no zone: 'zone:9' (zones: zone:0 ('Platform'))." in _refused(
        {**unnamed, "edges": [{**BLOCK["edges"][0], "target": "zone:9"}, *BLOCK["edges"][1:]]}
    )


def test_a_label_two_zones_share_is_refused_as_ambiguous():
    zones = [
        {"label": "Tier", "row": 2, "col": 1, "row_span": 2, "col_span": 2},
        {"label": "Tier", "row": 2, "col": 3, "row_span": 2, "col_span": 3},
    ]
    message = _refused(_block(zones=zones, edges=[{**BLOCK["edges"][0], "target": "zone:Tier"}, *BLOCK["edges"][1:]]))
    assert "edges.0.target = 'zone:Tier' names 2 zones by their label (zones.0 ('Tier'), zones.1 ('Tier'))" in message
    assert "give them ids" in message


def test_an_edge_between_two_zones_is_refused():
    zones = [
        {"id": "west", "label": "West", "row": 2, "col": 1, "row_span": 2, "col_span": 2},
        {"id": "east", "label": "East", "row": 2, "col": 3, "row_span": 2, "col_span": 3},
    ]
    edges = [{"source": "zone:west", "target": "zone:east"}, *BLOCK["edges"][1:]]
    assert "edges.0: zone:west -> zone:east joins two zones" in _refused(_block(zones=zones, edges=edges))


def test_an_edge_from_a_node_to_the_zone_that_holds_it_is_refused():
    message = _refused(_with_edge(0, source="centre"))
    assert "edges.0: node 'centre' sits in zones.0 ('Platform'), so the zone's border is not between them" in message


def test_one_zone_spelled_two_ways_is_still_one_edge():
    block = _block(edges=[*BLOCK["edges"], {"source": "context", "target": "zone:0"}])
    assert "edges.7: context -> zone:0 is declared twice (edges.0)" in _refused(block)


def test_a_node_named_zone_is_a_node():
    """Only the ``zone:`` prefix names a zone: a node id cannot hold the colon."""
    renamed = {"right": "zone"}
    nodes = [{**node, "id": renamed.get(node["id"], node["id"])} for node in BLOCK["nodes"]]
    edges = [
        {
            **edge,
            "source": renamed.get(edge["source"], edge["source"]),
            "target": renamed.get(edge["target"], edge["target"]),
        }
        for edge in BLOCK["edges"]
    ]
    assert _stored(_block(nodes=nodes, edges=edges))["edges"][3]["target"] == "zone"
    assert ZONE_REF_PREFIX == "zone:"


def test_a_node_reference_says_how_a_zone_is_named():
    assert "edges.1.target names no node: 'nowhere'" in (message := _refused(_with_edge(1, target="nowhere")))
    assert "a zone is named zone:ID, ID being its id, label or index" in message


# ── What the agent is told ───────────────────────────────────────────────────


def _type_line(name: str) -> str:
    return next(line for line in spec_reference().splitlines() if line.startswith(f"- {name}:"))


def test_the_type_lines_document_the_sides_the_lane_and_the_zone_end():
    edge_line = _type_line("ArchitectureEdge")
    assert "source: text<=45 (node id the edge leaves, or zone:ID (ID = a zone's id, label or index)" in edge_line
    assert "target: text<=45 (node id it reaches, or zone:ID: it ends on that zone's border, nearest the node)" in edge_line
    assert "source_side?: EdgeSide (the side it leaves, honoured even facing away" in edge_line
    assert "target_side?: EdgeSide (the side it enters (a zone end: the side of the zone's border))" in edge_line
    assert "lane?: int (1-6: the edges of one pair of nodes on different lanes run parallel" in edge_line
    assert "no side is pinned" in edge_line
    assert "id?: text<=32 (unique among the zones; an edge end names the zone zone:ID)" in _type_line("ArchitectureZone")
    assert _type_line("EdgeSide") == '- EdgeSide: "top"|"right"|"bottom"|"left"'


def test_both_deck_tools_teach_the_edge_sides():
    from app.services.builtin_tools.presentation import _EDIT_DESCRIPTION

    assert ARCHITECTURE_EDGE_HINT in build_generate_presentation_description()
    assert ARCHITECTURE_EDGE_HINT in _EDIT_DESCRIPTION
    for words in (
        "source_side / target_side",
        "honoured even facing away",
        "lane 1, 2",
        "zone:ID, ID being the zone's id",
        "never an invisible anchor node",
    ):
        assert words in ARCHITECTURE_EDGE_HINT


# ── The renderer (frontend tripwires) ────────────────────────────────────────


def test_the_frontend_mirrors_the_side_alias_and_resolves_zone_ends_like_the_validator():
    types = (FRONTEND / "types" / "index.ts").read_text(encoding="utf-8")
    layout = (FRONTEND / "components" / "presentation" / "blocks" / "architecture-layout.ts").read_text(
        encoding="utf-8"
    )
    assert 'export type DeckEdgeSide = "top" | "right" | "bottom" | "left";' in types
    for field in ("source_side?: DeckEdgeSide;", "target_side?: DeckEdgeSide;", "lane?: number;"):
        assert field in types
    assert f'export const ZONE_REF_PREFIX = "{ZONE_REF_PREFIX}";' in layout
    # By id, else by index, else by a label one zone alone wears - as `resolve_zone_ref`.
    assert "const byId = zones.findIndex((zone) => zone.id === key);" in layout
    assert "return byLabel.length === 1 ? byLabel[0] : null;" in layout
    # A digit-only key past the last zone falls through to the labels.
    assert 'if (/^[0-9]+$/.test(key) && Number(key) < zones.length) return Number(key);' in layout


# ── In a real browser: the paint, the layout check and the editable PPTX ────


def _drawn_paths(page) -> list[dict]:
    """Each edge as the page paints it: its ends in page px, its path data and its end marker."""
    return page.evaluate(
        """() => Array.from(document.querySelectorAll(".deck-arch-edge-line")).map((path) => {
              const m = path.getScreenCTM();
              const at = (length) => {
                const p = path.getPointAtLength(length);
                return [m.a * p.x + m.c * p.y + m.e, m.b * p.x + m.d * p.y + m.f];
              };
              return { start: at(0), end: at(path.getTotalLength()), d: path.getAttribute("d"), marker: getComputedStyle(path).markerEnd };
            })"""
    )


def _panel(page) -> dict:
    """The zone's panel in page px, from its geometry - not its painted box, which grows by the stroke."""
    return page.evaluate(
        """() => {
              const rect = document.querySelector(".deck-arch-zone-panel");
              const m = rect.getScreenCTM();
              const x = Number(rect.getAttribute("x")), y = Number(rect.getAttribute("y"));
              return { x: m.a * x + m.e, y: m.d * y + m.f, w: m.a * Number(rect.getAttribute("width")), h: m.d * Number(rect.getAttribute("height")) };
            }"""
    )


def _ink(shot, x: float, y: float, background: tuple, reach: int = 16) -> int:
    """How many pixels of the row at ``y`` within ``reach`` of ``x`` are not the background."""
    return sum(
        1
        for dx in range(-reach, reach + 1)
        if not _close(shot.getpixel((round(x) + dx, round(y))), background, tolerance=24)
    )


@pytest.mark.parametrize("scheme", ["dark", "light"])
def test_the_page_paints_the_routes_the_layout_computed(scheme: str):
    """On the real stylesheet, in both schemes: every edge starts and ends
    where the layout put it (within 2 px at 1920x1080); the squared exchanges
    are elbows, the bottom pair two exactly horizontal arrows a lane apart, and
    the context arrow ends on the zone's top border with its head painted."""
    geometry = json.loads(GEOMETRY.read_text(encoding="utf-8"))
    sync_playwright, _ = _chromium()
    with sync_playwright() as p:
        browser, page = _page(p, scheme, FIXTURE)
        to_page, scale = _to_page(page)
        paths = _drawn_paths(page)
        panel = _panel(page)
        shot = _pixels(page.screenshot(type="png"))
        browser.close()
    assert abs(scale - 1) < 1e-3, "the fixture page draws the figure at 1:1"
    assert len(paths) == len(geometry["edges"]) == len(BLOCK["edges"])
    for path, expected in zip(paths, geometry["edges"], strict=True):
        first, last = expected["points"][0], expected["points"][-1]
        for measured, (x, y) in ((path["start"], first), (path["end"], last)):
            px, py = to_page(x, y)
            assert abs(measured[0] - px) <= 2 and abs(measured[1] - py) <= 2, (
                expected["source"],
                expected["target"],
                measured,
                (px, py),
            )
        assert path["marker"].startswith("url("), path
    # The elbows have one corner, the bottom pair none.
    assert [path["d"].count(" Q ") for path in paths] == [0, 1, 1, 1, 1, 0, 0]
    forth, back = paths[5], paths[6]
    for path in (forth, back):
        assert path["start"][1] == path["end"][1], path
    assert abs(back["start"][1] - forth["start"][1] - 44 * scale) <= 1
    # The context arrow ends on the panel's top border, inside its width.
    feed = paths[0]
    assert abs(feed["end"][1] - panel["y"]) <= 2 and panel["x"] < feed["end"][0] < panel["x"] + panel["w"]
    # Its head is painted: wider than the line just behind the tip, the line alone further up.
    background = shot.getpixel((round(feed["end"][0]) + 60, round(feed["end"][1]) - 10))
    head = _ink(shot, feed["end"][0], feed["end"][1] - 9, background)
    line = _ink(shot, feed["end"][0], feed["end"][1] - 17, background)
    assert head >= line + 4, (scheme, head, line)


def test_the_layout_check_reports_nothing_on_the_fixture():
    from app.services.presentation_export import LAYOUT_AUDIT_JS

    sync_playwright, _ = _chromium()
    with sync_playwright() as p:
        browser, page = _page(p, "dark", FIXTURE)
        (slide,) = page.evaluate(LAYOUT_AUDIT_JS)
        browser.close()
    assert slide["findings"] == []


@pytest.mark.parametrize("scheme", ["dark", "light"])
def test_the_editable_pptx_carries_the_routes_where_the_page_drew_them(scheme: str):
    """The editable PPTX writes the architecture as the picture the page
    painted: one picture at the drawing's box (within 2 px at 1920x1080), and
    the picture's pixels along every route - each run's middle, each arrow's
    head - are the page's."""
    from app.services.presentation_export import EXTRACT_PRIMITIVES_JS

    geometry = json.loads(GEOMETRY.read_text(encoding="utf-8"))
    sync_playwright, _ = _chromium()
    with sync_playwright() as p:
        browser, page = _page(p, scheme, FIXTURE)
        svg = _svg_box(page)
        to_page, _scale = _to_page(page)
        (slide,) = page.evaluate(EXTRACT_PRIMITIVES_JS)
        captures = {}
        for item in slide["items"]:
            if item.get("kind") == "image" and item.get("id"):
                captures[item["id"]] = page.locator(f'[data-export-id="{item["id"]}"]').screenshot(type="png")
        shot = _pixels(page.screenshot(type="png"))
        browser.close()

    def inside_drawing(x: float, y: float, w: float, h: float) -> bool:
        return (
            x >= svg["x"] - 1
            and y >= svg["y"] - 1
            and x + w <= svg["x"] + svg["w"] + 1
            and y + h <= svg["y"] + svg["h"] + 1
        )

    drawn = [item for item in slide["items"] if inside_drawing(item["x"], item["y"], item["w"], item["h"])]
    assert [item["kind"] for item in drawn] == ["image"], drawn

    data = pptx_writer.build_native_pptx([slide], captures=captures, aspect_ratio="16:9")
    prs = PptxPresentation(io.BytesIO(data))
    px = prs.slide_width / 1920
    shapes = [
        shape
        for shape in prs.slides[0].shapes
        if inside_drawing(shape.left / px, shape.top / px, shape.width / px, shape.height / px)
    ]
    assert [shape.shape_type for shape in shapes] == [MSO_SHAPE_TYPE.PICTURE]
    (picture,) = shapes
    left, top, width, height = (picture.left / px, picture.top / px, picture.width / px, picture.height / px)
    for measured, expected in ((left, svg["x"]), (top, svg["y"]), (width, svg["w"]), (height, svg["h"])):
        assert abs(measured - expected) <= 2, (left, top, width, height, svg)
    image = _pixels(picture.image.blob)
    sx, sy = image.width / width, image.height / height

    def picture_at(x: float, y: float) -> tuple:
        return image.getpixel(
            (min(image.width - 1, round((x - left) * sx)), min(image.height - 1, round((y - top) * sy)))
        )

    def page_at(x: float, y: float) -> tuple:
        return shot.getpixel((round(x), round(y)))

    probes: list[tuple[float, float]] = []
    for edge in geometry["edges"]:
        points = edge["points"]
        for (x0, y0), (x1, y1) in itertools.pairwise(points):
            probes.append(to_page(x0 + (x1 - x0) / 4, y0 + (y1 - y0) / 4))
        (xa, ya), (xb, yb) = points[-2], points[-1]
        length = max(1.0, ((xb - xa) ** 2 + (yb - ya) ** 2) ** 0.5)
        probes.append(to_page(xb - (xb - xa) / length * 6, yb - (yb - ya) / length * 6))
    for x, y in probes:
        assert _close(picture_at(x, y), page_at(x, y), tolerance=8), (scheme, x, y, picture_at(x, y), page_at(x, y))


def test_the_fixture_carries_no_deck_content():
    """Placeholders only: no file id, no deck id, no product copy."""
    html = FIXTURE.read_text(encoding="utf-8")
    geometry = GEOMETRY.read_text(encoding="utf-8")
    assert "presentations/" not in html and "file_id" not in geometry
    texts = {
        "Platform",
        "Context",
        "Core",
        "Product A",
        "Product B",
        "Feeds",
        "Tasks",
        "Results",
        "Requests",
        "Answers",
    }
    for text in texts:
        assert f"<tspan>{text}</tspan>" in html, text
    assert set(re.findall(r"<tspan>([^<]*)</tspan>", html)) == texts
