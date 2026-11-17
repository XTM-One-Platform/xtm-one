"""Zone logos and frameless nodes in the architecture block.

A zone takes ``logo_file_id`` - the platform's mark in its top-left corner, its
label then optional and drawn after the mark - and a node takes
``frame: false``, drawn as its content alone, its box kept for the grid and
its edges meeting the content. Pinned here: the contract, the save-time file
check (one wording for a zone and a node), what the agent is told, the
renderer's hooks, and in Chromium the renderer's own drawing of the 5-column
fixture (``tests/data/architecture``, pinned to the renderer by
``blocks/architecture-bare.test.tsx``) through the layout check and the
editable PPTX. Every fixture is synthetic, its assets placeholders.
"""

from __future__ import annotations

import io
import json
import uuid
from pathlib import Path

import pytest
from pptx import Presentation as PptxPresentation
from pptx.enum.shapes import MSO_SHAPE_TYPE

from app.services import pptx_writer
from app.services.presentation_guidance import ARCHITECTURE_BARE_HINT, build_generate_presentation_description
from app.services.presentation_spec import (
    PresentationSpecError,
    logo_file_ids,
    spec_reference,
    validate_spec,
)

DATA = Path(__file__).parent / "data" / "architecture"
FIXTURE = DATA / "zone-logo-frameless.html"
GEOMETRY = DATA / "zone-logo-frameless.json"
FRONTEND = Path(__file__).resolve().parents[2] / "frontend" / "src" / "components" / "presentation"

#: Placeholder file ids: no file, no deck behind them.
ZONE_LOGO = "00000000-0000-4000-8000-00000000a001"
NODE_LOGO = "00000000-0000-4000-8000-00000000b002"


def _block(*, nodes: list[dict] | None = None, edges: list[dict] | None = None, zones: list[dict] | None = None) -> dict:
    return {
        "type": "architecture",
        "nodes": nodes
        if nodes is not None
        else [
            {"id": "a", "label": "Product A", "icon": "brain", "row": 1, "col": 1},
            {"id": "b", "label": "Product B", "icon": "crosshair", "row": 1, "col": 2},
        ],
        "edges": edges if edges is not None else [{"source": "a", "target": "b"}],
        "zones": zones if zones is not None else [],
    }


def _deck(block: dict) -> dict:
    return {"title": "Platform", "slides": [{"layout": "content", "blocks": [block]}]}


def _stored(block: dict) -> dict:
    return validate_spec(_deck(block))["slides"][0]["blocks"][0]


def _refused(block: dict) -> str:
    with pytest.raises(PresentationSpecError) as exc:
        validate_spec(_deck(block))
    return str(exc.value)


# ── The contract ─────────────────────────────────────────────────────────────


def test_frame_defaults_to_true_and_false_is_kept():
    nodes = _stored(
        _block(
            nodes=[
                {"id": "a", "label": "Product A", "row": 1, "col": 1},
                {"id": "b", "label": "Partner", "logo_file_id": NODE_LOGO, "frame": False, "row": 1, "col": 2},
            ]
        )
    )["nodes"]
    assert nodes[0]["frame"] is True
    assert nodes[1]["frame"] is False


@pytest.mark.parametrize("emphasis", ["normal", "core", "ghost"])
@pytest.mark.parametrize(
    "shape",
    [
        {"shape": "card", "icon": "shield", "chips": ["One", "Two"]},
        {"shape": "card", "col_span": 2, "logo_file_id": NODE_LOGO},
        {"shape": "pill", "icon": "bell"},
        {"shape": "circle", "icon": "sparkles"},
    ],
)
def test_every_shape_and_emphasis_takes_frame_false(shape: dict, emphasis: str):
    node = {"id": "s", "label": "Subject", **shape, "emphasis": emphasis, "frame": False, "row": 1, "col": 1}
    stored = _stored(_block(nodes=[node, {"id": "t", "label": "Other", "row": 2, "col": 1}], edges=[{"source": "s", "target": "t"}]))
    assert stored["nodes"][0]["frame"] is False
    assert stored["nodes"][0]["emphasis"] == emphasis


def test_frame_takes_a_boolean_only():
    assert "nodes.0.frame" in _refused(
        _block(nodes=[{"id": "a", "label": "A", "frame": "none", "row": 1, "col": 1}, {"id": "b", "label": "B", "row": 1, "col": 2}])
    )


@pytest.mark.parametrize(
    ("zone", "kept"),
    [
        ({"logo_file_id": ZONE_LOGO}, {"logo_file_id": ZONE_LOGO}),
        ({"logo_file_id": ZONE_LOGO, "label": "Platform"}, {"logo_file_id": ZONE_LOGO, "label": "Platform"}),
        ({"label": "Platform", "icon": "layers"}, {"label": "Platform", "icon": "layers"}),
    ],
)
def test_a_zone_is_headed_by_a_label_a_logo_or_both(zone: dict, kept: dict):
    stored = _stored(_block(zones=[{**zone, "row": 1, "col": 1, "col_span": 2}]))["zones"][0]
    assert {key: stored[key] for key in kept} == kept
    # An absent label is never materialised: the renderer draws the logo alone.
    assert ("label" in stored) == ("label" in zone)


def test_a_zone_label_is_optional_only_when_a_logo_or_chips_head_the_zone():
    message = _refused(_block(zones=[{"icon": "layers", "row": 1, "col": 1, "col_span": 2}]))
    assert "zones.0" in message
    assert "a zone needs a label, a logo_file_id or chips" in message


def test_a_zone_takes_an_icon_or_a_logo_not_both():
    message = _refused(_block(zones=[{"label": "Platform", "icon": "layers", "logo_file_id": ZONE_LOGO, "row": 1, "col": 1, "col_span": 2}]))
    assert "give a zone an icon OR a logo_file_id, not both" in message


def test_a_zone_logo_id_is_bounded_like_a_node_logo_id():
    assert "zones.0.logo_file_id" in _refused(_block(zones=[{"logo_file_id": "x" * 37, "row": 1, "col": 1, "col_span": 2}]))


@pytest.mark.parametrize(
    ("zones", "fragment"),
    [
        ([{"logo_file_id": ZONE_LOGO, "row": 2, "col": 1}], "zones.0 runs past the grid"),
        (
            [{"logo_file_id": ZONE_LOGO, "row": 1, "col": 1}, {"label": "Right", "row": 1, "col": 1, "col_span": 2}],
            "zones.0 and zones.1 ('Right') overlap",
        ),
    ],
)
def test_a_refusal_names_an_unlabelled_zone_by_its_place(zones: list[dict], fragment: str):
    assert fragment in _refused(_block(zones=zones))


def test_the_zone_and_node_logos_are_read_for_their_geometry_and_walked_as_files():
    """The renderer lays a zone logo out by its mark's ratio before its bytes
    arrive, so its stored geometry is served with the deck (``asset_meta``);
    it is a regular ``*_file_id`` everywhere else - verified, served, packaged."""
    from app.services.presentation_export import referenced_file_ids

    spec = validate_spec(
        _deck(
            _block(
                nodes=[
                    {"id": "a", "label": "Partner", "logo_file_id": NODE_LOGO, "frame": False, "row": 1, "col": 1},
                    {"id": "b", "label": "Product B", "row": 1, "col": 2},
                ],
                zones=[{"logo_file_id": ZONE_LOGO, "row": 1, "col": 1, "col_span": 2}],
            )
        )
    )
    assert logo_file_ids(spec) == [ZONE_LOGO, NODE_LOGO]
    assert {ZONE_LOGO, NODE_LOGO} <= set(referenced_file_ids(spec))


def test_an_unknown_zone_logo_is_refused_on_save_with_the_words_a_node_logo_gets(monkeypatch):
    from tests.test_presentations import _verify

    def refusal(field: str, holder: str) -> str:
        ghost = str(uuid.uuid4())
        block = _block(
            nodes=[
                {"id": "a", "label": "A", "row": 1, "col": 1, **({"logo_file_id": ghost} if holder == "node" else {})},
                {"id": "b", "label": "B", "row": 1, "col": 2},
            ],
            zones=[{"label": "Z", "row": 1, "col": 1, "col_span": 2, **({"logo_file_id": ghost} if holder == "zone" else {})}],
        )
        spec = validate_spec(_deck(block))
        with pytest.raises(PresentationSpecError) as exc:
            _verify(spec, rows=[], forbidden=set(), monkeypatch=monkeypatch)
        message = str(exc.value)
        assert f"slides.0 (layout=content).blocks.0.{field}.0.logo_file_id = `{ghost}`" in message
        return message.replace(ghost, "<id>").replace(f"{field}.0", "<where>")

    assert refusal("zones", "zone") == refusal("nodes", "node")
    assert "is not a platform file you can read" in refusal("zones", "zone")


# ── What the agent is told ───────────────────────────────────────────────────


def test_the_type_lines_document_the_frame_and_the_zone_logo():
    reference = spec_reference()
    node_line = next(line for line in reference.splitlines() if line.startswith("- ArchitectureNode:"))
    zone_line = next(line for line in reference.splitlines() if line.startswith("- ArchitectureZone:"))
    assert "frame?: bool = true (false = the content alone" in node_line
    assert "label?: markup" in zone_line and "optional with a logo_file_id" in zone_line
    assert "logo_file_id?: file_id (a template asset (the platform's brand mark) in the top-left corner" in zone_line


def test_both_deck_tools_teach_the_zone_logo_and_the_frameless_node():
    from app.services.builtin_tools.presentation import _EDIT_DESCRIPTION

    assert ARCHITECTURE_BARE_HINT in build_generate_presentation_description()
    assert ARCHITECTURE_BARE_HINT in _EDIT_DESCRIPTION
    for words in ("logo_file_id", "frame: false", "no border, surface or glow"):
        assert words in ARCHITECTURE_BARE_HINT


# ── The renderer (frontend tripwires) ────────────────────────────────────────


def test_the_renderer_draws_a_frameless_node_with_no_shape_and_meets_it_on_its_content():
    view = (FRONTEND / "blocks" / "Architecture.tsx").read_text(encoding="utf-8")
    layout = (FRONTEND / "blocks" / "architecture-layout.ts").read_text(encoding="utf-8")
    css = (FRONTEND / "presentation.css").read_text(encoding="utf-8")
    assert 'data-frame={node.frame ? undefined : "false"}' in view
    assert '{!node.frame ? (' in view and 'className="deck-arch-node-hit"' in view
    # The hit area paints nothing until a keyboard focus rings it.
    assert ".deck-root .deck-arch-node-hit {\n  fill: transparent;\n  stroke: none;\n}" in css
    assert ".deck-root .deck-arch-node:focus-visible .deck-arch-node-hit {" in css
    # Edges meet the anchor: a frameless node's content box.
    assert "frame: node.frame !== false" in layout
    assert "if (!node.frame) {" in layout and "node.anchor" in layout
    # The zone logo is laid out from the deck's stored geometry, drawn cropped to its mark.
    assert "layoutArchitecture(block, { room, assets })" in view
    assert "crop={logoCrop(meta)}" in view
    # A video figure never breathes around a frameless core.
    build = (FRONTEND / "figure-build.ts").read_text(encoding="utf-8")
    assert "unframed: '.deck-arch-node[data-frame=\"false\"]'" in build


# ── In a real browser: the layout check and the editable PPTX ───────────────


def _chromium():
    try:
        from playwright.sync_api import Error as PlaywrightError
        from playwright.sync_api import sync_playwright
    except ImportError:  # pragma: no cover - the export dependency is always installed
        pytest.skip("playwright is not installed")
    return sync_playwright, PlaywrightError


def _page(p, scheme: str, fixture: Path = FIXTURE):
    """The fixture on a 1920x1080 slide whose body is as wide as the drawing, so it is drawn at 1:1."""
    from tests.test_presentation_export import _deck_stylesheet

    css = _deck_stylesheet()
    _sync, playwright_error = _chromium()
    try:
        browser = p.chromium.launch()
    except playwright_error as exc:
        pytest.skip(f"no Chromium for Playwright here: {exc}")
    page = browser.new_page(viewport={"width": 1920, "height": 1080})
    figure = fixture.read_text(encoding="utf-8")
    page.set_content(
        f'<div class="deck-root" data-scheme="{scheme}"><div class="deck-canvas deck-static-slide" data-scheme="{scheme}" '
        'style="position:relative;width:1920px;height:1080px;overflow:hidden">'
        '<div class="deck-slide" data-layout="content" style="padding:80px 40px;box-sizing:border-box;width:1920px;height:1080px">'
        f'<div class="deck-blocks">{figure}</div></div></div></div>'
    )
    # The slide at the page's origin: page pixels are the slide's, as the extractor measures them.
    page.add_style_tag(content=f"body {{ margin: 0; }}\n{css}")
    # The placeholder logos are data URLs: wait until every one is decoded.
    page.evaluate(
        """() => Promise.all(Array.from(document.querySelectorAll("image")).map((image) => {
              const probe = new Image();
              probe.src = image.getAttribute("href");
              return probe.decode().catch(() => undefined);
            }))"""
    )
    page.wait_for_timeout(100)
    return browser, page


def _svg_box(page) -> dict:
    return page.evaluate(
        """() => {
              const r = document.querySelector("svg.deck-architecture-svg").getBoundingClientRect();
              return { x: r.left, y: r.top, w: r.width, h: r.height };
            }"""
    )


def _pixels(png: bytes):
    from PIL import Image

    return Image.open(io.BytesIO(png)).convert("RGB")


def _close(a: tuple, b: tuple, tolerance: int = 4) -> bool:
    return all(abs(x - y) <= tolerance for x, y in zip(a, b, strict=True))


def _border_probes(box: list[float]) -> list[tuple[float, float]]:
    """A node box's top edge, left edge and right edge, each at its middle - the pixels a card's border paints."""
    x, y, w, h = box
    return [(x + w / 2, y), (x, y + h / 2), (x + w - 1, y + h / 2)]


@pytest.mark.parametrize("scheme", ["dark", "light"])
def test_frameless_nodes_paint_no_border_surface_shadow_or_glow_and_the_zone_logo_sits_in_its_corner(scheme: str):
    """On the real stylesheet, in both schemes: inside a frameless node's box
    and around its content the page shows the zone's panel and nothing else,
    where a framed node shows its border; the zone logo is painted where the
    layout put it."""
    geometry = json.loads(GEOMETRY.read_text(encoding="utf-8"))
    sync_playwright, _ = _chromium()
    with sync_playwright() as p:
        browser, page = _page(p, scheme)
        styles = page.evaluate(
            """() => Array.from(document.querySelectorAll('.deck-arch-node[data-frame="false"]')).map((g) => {
                  const hit = g.querySelector(".deck-arch-node-hit");
                  const cs = getComputedStyle(hit);
                  return {
                    shapes: g.querySelectorAll(".deck-arch-node-shape, .deck-arch-node-glow").length,
                    fill: cs.fill, stroke: cs.stroke, filter: cs.filter, nodeFilter: getComputedStyle(g).filter,
                  };
                })"""
        )
        title_left = page.evaluate("() => document.querySelector('.deck-arch-zone-title').getBoundingClientRect().left")
        svg = _svg_box(page)
        shot = _pixels(page.screenshot(type="png"))
        browser.close()
    assert len(styles) == 2
    for style in styles:
        assert style == {"shapes": 0, "fill": "rgba(0, 0, 0, 0)", "stroke": "none", "filter": "none", "nodeFilter": "none"}
    scale = svg["w"] / geometry["viewBox"][0]
    assert abs(scale - 1) < 1e-3, "the fixture page draws the figure at 1:1"

    def at(x: float, y: float) -> tuple:
        return shot.getpixel((round(svg["x"] + x), round(svg["y"] + y)))

    panel = at(900, 300)  # the zone's panel, clear of every node, edge and label

    # The mark as painted - cropped to its ink - fills the box the layout gave it, to 2 px.
    lx, ly, lw, lh = geometry["zone"]["logo"]

    def first_ink(points: list[tuple[float, float]]) -> tuple[float, float]:
        return next(point for point in points if not _close(at(*point), panel))

    middle = ly + lh / 2
    assert abs(first_ink([(lx - 10 + i, middle) for i in range(40)])[0] - lx) <= 2
    assert abs(first_ink([(lx + lw + 10 - i, middle) for i in range(40)])[0] - (lx + lw - 1)) <= 2
    assert abs(first_ink([(lx + lh / 4, ly - 10 + i) for i in range(40)])[1] - ly) <= 2
    # The label follows the mark on its row.
    assert title_left >= svg["x"] + lx + lw
    nodes = {node["id"]: node for node in geometry["nodes"]}
    for node_id in ("partner", "skills"):
        for px, py in _border_probes(nodes[node_id]["box"]):
            assert _close(at(px, py), panel), (scheme, node_id, px, py, at(px, py), panel)
    # The probes do see a border: a framed product's edges are not the panel.
    for px, py in _border_probes(nodes["alpha"]["box"]):
        assert not _close(at(px, py), panel), (scheme, px, py)


def test_the_layout_check_reports_nothing_on_the_five_column_grid():
    from app.services.presentation_export import LAYOUT_AUDIT_JS

    sync_playwright, _ = _chromium()
    with sync_playwright() as p:
        browser, page = _page(p, "dark")
        (slide,) = page.evaluate(LAYOUT_AUDIT_JS)
        browser.close()
    assert slide["findings"] == []


def test_the_editable_pptx_carries_the_drawing_where_the_page_painted_it_with_no_stray_shape():
    """The editable PPTX writes the architecture as the picture the page
    painted: one picture at the drawing's box (within 2 px at 1920x1080), no
    shape or text box of its own around a frameless node, and the picture's
    pixels at the zone logo and around the frameless nodes are the page's."""
    from app.services.presentation_export import EXTRACT_PRIMITIVES_JS

    geometry = json.loads(GEOMETRY.read_text(encoding="utf-8"))
    sync_playwright, _ = _chromium()
    with sync_playwright() as p:
        browser, page = _page(p, "dark")
        svg = _svg_box(page)
        (slide,) = page.evaluate(EXTRACT_PRIMITIVES_JS)
        captures = {}
        for item in slide["items"]:
            if item.get("kind") == "image" and item.get("id"):
                captures[item["id"]] = page.locator(f'[data-export-id="{item["id"]}"]').screenshot(type="png")
        shot = _pixels(page.screenshot(type="png"))
        browser.close()

    def inside_drawing(x: float, y: float, w: float, h: float) -> bool:
        return x >= svg["x"] - 1 and y >= svg["y"] - 1 and x + w <= svg["x"] + svg["w"] + 1 and y + h <= svg["y"] + svg["h"] + 1

    drawn = [item for item in slide["items"] if inside_drawing(item["x"], item["y"], item["w"], item["h"])]
    assert [item["kind"] for item in drawn] == ["image"], drawn
    (picture_item,) = drawn

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
        return image.getpixel((min(image.width - 1, round((svg["x"] + x - left) * sx)), min(image.height - 1, round((svg["y"] + y - top) * sy))))

    def page_at(x: float, y: float) -> tuple:
        return shot.getpixel((round(svg["x"] + x), round(svg["y"] + y)))

    lx, ly, lw, lh = geometry["zone"]["logo"]
    probes = [(lx + lw * fx, ly + lh * fy) for fx in (0.15, 0.5, 0.85) for fy in (0.3, 0.5, 0.7)]
    nodes = {node["id"]: node for node in geometry["nodes"]}
    for node_id in ("partner", "skills"):
        probes += _border_probes(nodes[node_id]["box"])
    for x, y in probes:
        assert _close(picture_at(x, y), page_at(x, y), tolerance=8), (x, y, picture_at(x, y), page_at(x, y))
    assert picture_item["w"] == pytest.approx(svg["w"], abs=2)


def test_the_fixture_carries_no_deck_content():
    """Placeholders only: no stored file id, no deck id, no product copy."""
    html = FIXTURE.read_text(encoding="utf-8")
    geometry = GEOMETRY.read_text(encoding="utf-8")
    assert ZONE_LOGO not in html and NODE_LOGO not in html
    assert "presentations/" not in html and "file_id" not in geometry
    texts = {"Platform", "Partner", "Capabilities", "Detect", "Respond", "Report", "Product A", "Product B", "Product C", "Feeds", "Uses"}
    for text in texts:
        assert f"<tspan>{text}</tspan>" in html, text
    assert html.count("<tspan>") == len(texts)
