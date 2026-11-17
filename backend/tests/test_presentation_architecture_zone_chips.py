"""Zone chips in the architecture block.

A zone takes ``chips`` - one row of pills on its head row, in its top-right
corner by default (``chips_align`` start puts them after the logo / label,
center on the zone's middle), never wrapped, taking no grid cell - and the
editable PPTX writes them as rounded rectangles with their text where the page
draws them, lifted out of the drawing's picture. Pinned here: the contract,
what the agent is told, the renderer's hooks, the layout-check finding, and in
Chromium the renderer's own drawing of the fixtures (``tests/data/architecture``,
pinned to the renderer by ``blocks/architecture-zone-chips.test.tsx``) through
the stylesheet, the layout check and the editable PPTX. Every fixture is
synthetic, its assets placeholders.
"""

from __future__ import annotations

import asyncio
import io
import json
from pathlib import Path

import pytest
from pptx import Presentation as PptxPresentation
from pptx.enum.shapes import MSO_AUTO_SHAPE_TYPE, MSO_SHAPE_TYPE

from app.services import pptx_writer
from app.services.presentation_guidance import ARCHITECTURE_BARE_HINT, build_generate_presentation_description
from app.services.presentation_layout import findings_from_observations
from app.services.presentation_spec import (
    MAX_ARCHITECTURE_ZONE_CHIPS,
    PresentationSpecError,
    spec_reference,
    validate_spec,
)
from tests.test_presentation_architecture_bare import _chromium, _close, _page, _pixels, _svg_box

DATA = Path(__file__).parent / "data" / "architecture"
FIXTURE = DATA / "zone-chips.html"
GEOMETRY = DATA / "zone-chips.json"
OVERFLOW = DATA / "zone-chips-overflow.html"
FRONTEND = Path(__file__).resolve().parents[2] / "frontend" / "src" / "components" / "presentation"

#: Placeholder file id: no file, no deck behind it.
ZONE_LOGO = "00000000-0000-4000-8000-00000000a001"
CHIPS = ["AI agents", "Shared context", "Automation", "Integrations"]


def _block(zone: dict) -> dict:
    return {
        "type": "architecture",
        "nodes": [
            {"id": "a", "label": "Product A", "icon": "brain", "row": 1, "col": 1},
            {"id": "b", "label": "Product B", "icon": "crosshair", "row": 1, "col": 2},
        ],
        "edges": [{"source": "a", "target": "b"}],
        "zones": [{"row": 1, "col": 1, "col_span": 2, **zone}],
    }


def _deck(block: dict) -> dict:
    return {"title": "Platform", "slides": [{"layout": "content", "blocks": [block]}]}


def _stored_zone(zone: dict) -> dict:
    return validate_spec(_deck(_block(zone)))["slides"][0]["blocks"][0]["zones"][0]


def _refused(zone: dict) -> str:
    with pytest.raises(PresentationSpecError) as exc:
        validate_spec(_deck(_block(zone)))
    return str(exc.value)


# ── The contract ─────────────────────────────────────────────────────────────


def test_chips_default_to_none_at_the_zone_end():
    stored = _stored_zone({"label": "Platform"})
    assert stored["chips"] == []
    assert stored["chips_align"] == "end"


def test_a_zone_keeps_its_chips_in_order_with_its_logo():
    stored = _stored_zone({"logo_file_id": ZONE_LOGO, "chips": CHIPS})
    assert stored["chips"] == CHIPS
    assert stored["chips_align"] == "end"
    assert "label" not in stored


@pytest.mark.parametrize("align", ["start", "center", "end"])
def test_chips_align_takes_start_center_or_end(align: str):
    assert _stored_zone({"label": "Platform", "chips": CHIPS, "chips_align": align})["chips_align"] == align


def test_chips_align_takes_nothing_else():
    assert "zones.0.chips_align" in _refused({"label": "Platform", "chips": CHIPS, "chips_align": "right"})


def test_at_most_six_chips():
    assert MAX_ARCHITECTURE_ZONE_CHIPS == 6
    six = [f"Capability {n}" for n in range(6)]
    assert _stored_zone({"label": "Platform", "chips": six})["chips"] == six
    assert "zones.0.chips" in _refused({"label": "Platform", "chips": [*six, "Seventh"]})


def test_a_chip_is_a_markup_of_at_most_24_characters():
    assert _stored_zone({"label": "Platform", "chips": ["x" * 24]})["chips"] == ["x" * 24]
    assert "zones.0.chips.0" in _refused({"label": "Platform", "chips": ["x" * 25]})
    assert _stored_zone({"label": "Platform", "chips": ["**Core** agents"]})["chips"] == ["**Core** agents"]
    assert "zones.0.chips.0" in _refused({"label": "Platform", "chips": ["<b>Agents</b>"]})


def test_chips_alone_can_head_a_zone_and_nothing_cannot():
    assert _stored_zone({"chips": CHIPS})["chips"] == CHIPS
    assert "a zone needs a label, a logo_file_id or chips" in _refused({"icon": "layers"})


def test_chips_align_without_chips_is_refused():
    message = _refused({"label": "Platform", "chips_align": "start"})
    assert "chips_align places the zone's chips" in message


# ── What the agent is told ───────────────────────────────────────────────────


def test_the_zone_type_line_documents_the_chips():
    zone_line = next(line for line in spec_reference().splitlines() if line.startswith("- ArchitectureZone:"))
    assert "chips?: markup<=24[] (0-6) (one row of small pills on the head row" in zone_line
    assert 'chips_align?: Align = "end" (where the chips sit on the head row' in zone_line
    assert "label?: markup<=40 (the title; optional with a logo_file_id or chips)" in zone_line


def test_both_deck_tools_teach_the_zone_chips():
    from app.services.builtin_tools.presentation import _EDIT_DESCRIPTION

    assert ARCHITECTURE_BARE_HINT in build_generate_presentation_description()
    assert ARCHITECTURE_BARE_HINT in _EDIT_DESCRIPTION
    for words in ("up to 6 chips", "top-right corner", "chips_align start", "taking no grid cell"):
        assert words in ARCHITECTURE_BARE_HINT


# ── The renderer and the exporter (tripwires) ────────────────────────────────


def test_the_renderer_draws_the_chips_in_the_zone_and_publishes_an_overflow():
    view = (FRONTEND / "blocks" / "Architecture.tsx").read_text(encoding="utf-8")
    layout = (FRONTEND / "blocks" / "architecture-layout.ts").read_text(encoding="utf-8")
    css = (FRONTEND / "presentation.css").read_text(encoding="utf-8")
    # Inside the zone's group, so `.deck-arch-chip rect` reads the zone's `--tone`.
    assert 'className="deck-arch-chip deck-arch-zone-chip"' in view
    assert "data-chips-overflow={zone.chipsOverflow > 0 ? Math.ceil(zone.chipsOverflow) : undefined}" in view
    # The router and the label placer keep off the row.
    assert "for (const row of bounds.chipRows) if (segmentIntersectsRect(a, b, inflate(row, 10))) conflicts += 1;" in layout
    assert "for (const row of bounds.chipRows) if (rectsOverlap(rect, inflate(row, 6))) score += 1e6;" in layout
    # The capture hides what the PPTX writes as shapes over the picture.
    assert ".deck-root [data-export-lifting] [data-export-lifted] {\n  visibility: hidden;\n}" in css


def test_the_capture_hides_the_lifted_chips_for_the_shot_and_shows_them_again():
    from app.services import presentation_export
    from app.services.presentation_export import _LIFT_JS, CAPTURE_TIMEOUT_MS, EXTRACT_PRIMITIVES_JS, _capture_lifted

    assert 'el.setAttribute("data-export-lifting", "")' in _LIFT_JS
    assert 'el.removeAttribute("data-export-lifting")' in _LIFT_JS
    assert 'chip.setAttribute("data-export-lifted", "")' in EXTRACT_PRIMITIVES_JS
    assert "if (picture) picture.lifted = true;" in EXTRACT_PRIMITIVES_JS
    calls: list = []

    class _Locator:
        def __init__(self, fail: bool):
            self.fail = fail

        async def screenshot(self, **kwargs):
            calls.append(("shot", kwargs))
            if self.fail:
                raise RuntimeError("detached")
            return b"png"

    class _Page:
        def __init__(self, fail: bool = False):
            self.fail = fail

        async def evaluate(self, expression, arg=None):
            calls.append(("evaluate", arg))
            return True

        def locator(self, selector):
            calls.append(("locator", selector))
            return _Locator(self.fail)

    assert asyncio.run(_capture_lifted(_Page(), "x0-2")) == b"png"
    assert calls == [
        ("evaluate", ["x0-2", True]),
        ("locator", '[data-export-id="x0-2"]'),
        ("shot", {"type": "png", "timeout": CAPTURE_TIMEOUT_MS}),
        ("evaluate", ["x0-2", False]),
    ]
    calls.clear()
    with pytest.raises(RuntimeError):
        asyncio.run(_capture_lifted(_Page(fail=True), "x0-2"))
    assert calls[-1] == ("evaluate", ["x0-2", False])
    source = Path(presentation_export.__file__).read_text(encoding="utf-8")
    assert 'if item.get("lifted"):\n                            result.captures[capture_id] = await _capture_lifted(page, capture_id)' in source


# ── The layout check's words ─────────────────────────────────────────────────


def _slide(findings: list[dict]) -> list[dict]:
    return [{"index": 0, "slide_id": "s1", "layout": "content", "findings": findings}]


def test_a_zone_whose_chips_do_not_fit_is_a_dense_finding_naming_the_zone():
    spec = _deck(_block({"label": "Platform", "chips": CHIPS}))
    (finding,) = findings_from_observations(
        _slide([{"kind": "zone_chips", "block": "architecture", "architecture": 1, "zone": 0, "label": "Platform", "amount": 42, "chips": 4}]),
        spec,
    )
    assert finding.kind == "zone_chips"
    assert finding.severity == "dense"
    assert finding.amount == 42
    assert finding.message.startswith("architecture 1: the chips of zones.0 ('Platform') need 42px more than its head row")
    assert "never wrapped" in finding.message and "widen the zone (col_span)" in finding.message
    assert "shorten its label" in finding.message


def test_an_unlabelled_zone_is_named_by_its_place_and_a_fitting_row_says_nothing():
    spec = _deck(_block({"chips": CHIPS}))
    (finding,) = findings_from_observations(
        _slide([{"kind": "zone_chips", "architecture": 2, "zone": 1, "label": "", "amount": 7, "chips": 4}]), spec
    )
    assert finding.message.startswith("architecture 2: the chips of zones.1 need 7px more")
    assert "shorten its label" not in finding.message
    assert findings_from_observations(_slide([{"kind": "zone_chips", "zone": 0, "amount": 0}]), spec) == []


# ── In a real browser: the paint, the layout check and the editable PPTX ────


def _to_page(page):
    """viewBox coordinates -> page px, through the drawing's own transform (an 806px drawing meets an 800px stage)."""
    m = page.evaluate(
        """() => {
              const m = document.querySelector("svg.deck-architecture-svg").getScreenCTM();
              return { a: m.a, e: m.e, f: m.f };
            }"""
    )
    return lambda x, y: (m["a"] * x + m["e"], m["a"] * y + m["f"]), m["a"]


def _chip_boxes(page) -> list[dict]:
    """Each chip's pill as the page paints it, in page px, with its text."""
    return page.evaluate(
        """() => Array.from(document.querySelectorAll(".deck-arch-zone-chip")).map((chip) => {
              const r = chip.querySelector("rect").getBoundingClientRect();
              return { x: r.left, y: r.top, w: r.width, h: r.height, text: chip.querySelector("text").textContent };
            })"""
    )


@pytest.mark.parametrize("scheme", ["dark", "light"])
def test_the_logo_sits_top_left_and_the_four_chips_in_one_row_top_right(scheme: str):
    """On the real stylesheet, in both schemes: the four pills painted on the
    logo's row, right-aligned inside the zone's padding, each tinted (its
    inside is not the panel), the logo where the layout put it."""
    geometry = json.loads(GEOMETRY.read_text(encoding="utf-8"))
    sync_playwright, _ = _chromium()
    with sync_playwright() as p:
        browser, page = _page(p, scheme, FIXTURE)
        to_page, scale = _to_page(page)
        chips = _chip_boxes(page)
        shot = _pixels(page.screenshot(type="png"))
        browser.close()
    zone = geometry["zone"]
    assert [chip["text"] for chip in chips] == CHIPS
    for chip, expected in zip(chips, zone["chips"], strict=True):
        x, y, w, h = expected["box"]
        left, top = to_page(x, y)
        for measured, value in ((chip["x"], left), (chip["y"], top), (chip["w"], w * scale), (chip["h"], h * scale)):
            assert abs(measured - value) <= 1, (chip, expected)
    assert len({round(chip["y"]) for chip in chips}) == 1, "one row"
    panel_x, _panel_y, panel_w, _panel_h = zone["panel"]
    right, _ = to_page(panel_x + panel_w - 24, 0)
    assert abs(chips[-1]["x"] + chips[-1]["w"] - right) <= 1
    lx, ly, lw, lh = zone["logo"]
    assert abs(chips[0]["y"] + chips[0]["h"] / 2 - to_page(0, ly + lh / 2)[1]) <= 1, "centred on the logo's row"
    assert lx == panel_x + 24 and chips[0]["x"] > to_page(lx + lw, 0)[0]

    def at(x: float, y: float) -> tuple:
        return shot.getpixel((round(x), round(y)))

    panel = at(*to_page(500, 480))  # the zone's panel, clear of every node, edge and label
    for chip in chips:
        inside = at(chip["x"] + 4, chip["y"] + chip["h"] / 2)
        assert not _close(inside, panel, tolerance=2), (scheme, chip["text"], inside, panel)


@pytest.mark.parametrize("paint", [{"color": "brand_1"}, *({"tone": tone} for tone in ("accent", "info", "success", "warning", "error", "muted"))])
def test_the_chips_are_tinted_in_the_zone_colour_or_tone(paint: dict):
    """Whatever the zone wears - a palette colour or any tone - its chips are a
    tint of it: the pill's fill is the zone's own colour, translucent."""
    sync_playwright, _ = _chromium()
    with sync_playwright() as p:
        browser, page = _page(p, "dark", FIXTURE)
        tinted = page.evaluate(
            """(paint) => {
                  const zone = document.querySelector(".deck-arch-zone");
                  zone.removeAttribute("data-color");
                  zone.removeAttribute("data-tone");
                  zone.setAttribute(paint.color ? "data-color" : "data-tone", paint.color || paint.tone);
                  const probe = document.createElement("span");
                  probe.style.color = getComputedStyle(zone).getPropertyValue("--tone");
                  document.body.appendChild(probe);
                  const tone = getComputedStyle(probe).color;
                  return { tone, fills: Array.from(zone.querySelectorAll(".deck-arch-zone-chip rect")).map((r) => getComputedStyle(r).fill) };
                }""",
            paint,
        )
        browser.close()

    def channels(value: str) -> list[float]:
        """``rgb(a)()`` or ``color(srgb ...)`` as 0-255 channels and an alpha."""
        import re

        numbers = [float(n) for n in re.findall(r"-?\d*\.?\d+(?:e-?\d+)?", value.split("(", 1)[1])]
        rgb = [n * 255 for n in numbers[:3]] if value.startswith("color(") else numbers[:3]
        return [*rgb, numbers[3] if len(numbers) > 3 else 1.0]

    tone = channels(tinted["tone"])
    assert len(tinted["fills"]) == len(CHIPS)
    for fill in tinted["fills"]:
        value = channels(fill)
        assert all(abs(a - b) <= 1.5 for a, b in zip(value[:3], tone[:3], strict=True)), (paint, fill, tinted["tone"])
        assert 0 < value[3] < 0.5, (paint, fill)


def test_the_layout_check_reports_nothing_on_the_five_column_grid_and_names_a_zone_whose_chips_do_not_fit():
    from app.services.presentation_export import LAYOUT_AUDIT_JS

    sync_playwright, _ = _chromium()
    with sync_playwright() as p:
        browser, page = _page(p, "dark", FIXTURE)
        (fits,) = page.evaluate(LAYOUT_AUDIT_JS)
        browser.close()
        browser, page = _page(p, "dark", OVERFLOW)
        (narrow,) = page.evaluate(LAYOUT_AUDIT_JS)
        drawn = page.evaluate("() => Array.from(document.querySelectorAll('.deck-arch-zone-chip text')).map((t) => t.textContent)")
        browser.close()
    assert fits["findings"] == []
    (observation,) = narrow["findings"]
    assert {key: observation[key] for key in ("kind", "architecture", "zone", "label", "chips")} == {
        "kind": "zone_chips",
        "architecture": 1,
        "zone": 0,
        "label": "Platform",
        "chips": 6,
    }
    assert observation["amount"] > 0
    # Reported, not cut: every chip is drawn whole.
    assert drawn == ["Detect", "Respond", "Report", "Hunt", "Share", "Learn"]
    spec = _deck(_block({"label": "Platform", "chips": drawn}))
    (finding,) = findings_from_observations([{**narrow, "index": 0}], spec)
    assert finding.kind == "zone_chips"
    assert "zones.0 ('Platform')" in finding.message


@pytest.mark.parametrize("scheme", ["dark", "light"])
def test_the_editable_pptx_writes_the_chips_as_rounded_rectangles_with_their_text_where_the_page_draws_them(scheme: str):
    """The drawing stays one picture at its box, captured WITHOUT the chips;
    each chip is a rounded rectangle and a text box at the page's position
    (within 2 px at 1920x1080), its fill the colour the page paints, its text
    the chip's, one PowerPoint group per zone and one per chip."""
    from app.services.presentation_export import _LIFT_JS, EXTRACT_PRIMITIVES_JS

    sync_playwright, _ = _chromium()
    with sync_playwright() as p:
        browser, page = _page(p, scheme, FIXTURE)
        svg = _svg_box(page)
        to_page, _scale = _to_page(page)
        chips = _chip_boxes(page)
        shot = _pixels(page.screenshot(type="png"))
        (slide,) = page.evaluate(EXTRACT_PRIMITIVES_JS)
        captures = {}
        for item in slide["items"]:
            if item.get("kind") == "image" and item.get("id"):
                # What `_capture_lifted` does, on the sync page.
                lifted = bool(item.get("lifted")) and page.evaluate(_LIFT_JS, [item["id"], True])
                captures[item["id"]] = page.locator(f'[data-export-id="{item["id"]}"]').screenshot(type="png")
                if lifted:
                    page.evaluate(_LIFT_JS, [item["id"], False])
        # Shown again once the capture is done.
        visible = page.evaluate("() => Array.from(document.querySelectorAll('.deck-arch-zone-chip')).map((g) => getComputedStyle(g).visibility)")
        browser.close()
    assert visible == ["visible"] * len(CHIPS)

    def inside_drawing(x: float, y: float, w: float, h: float) -> bool:
        return x >= svg["x"] - 1 and y >= svg["y"] - 1 and x + w <= svg["x"] + svg["w"] + 1 and y + h <= svg["y"] + svg["h"] + 1

    drawn = [item for item in slide["items"] if inside_drawing(item["x"], item["y"], item["w"], item["h"])]
    assert [item["kind"] for item in drawn] == ["image", *(["rect", "text"] * len(CHIPS))], [item["kind"] for item in drawn]
    picture_item = drawn[0]
    assert picture_item["lifted"] is True
    assert len({item["group"] for item in drawn[1:]}) == 1
    assert len({item["subgroup"] for item in drawn[1:]}) == len(CHIPS)

    data = pptx_writer.build_native_pptx([slide], captures=captures, aspect_ratio="16:9")
    prs = PptxPresentation(io.BytesIO(data))
    px = prs.slide_width / 1920

    def box(shape) -> tuple[float, float, float, float]:
        return shape.left / px, shape.top / px, shape.width / px, shape.height / px

    top = [shape for shape in prs.slides[0].shapes if inside_drawing(*box(shape))]
    assert [shape.shape_type for shape in top] == [MSO_SHAPE_TYPE.PICTURE, MSO_SHAPE_TYPE.GROUP]
    picture, row = top
    left, top_y, width, height = box(picture)
    for measured, expected in ((left, svg["x"]), (top_y, svg["y"]), (width, svg["w"]), (height, svg["h"])):
        assert abs(measured - expected) <= 2, (box(picture), svg)
    pills = [list(group.shapes) for group in row.shapes]
    assert len(pills) == len(CHIPS)
    image = _pixels(picture.image.blob)
    sx, sy = image.width / width, image.height / height

    def picture_at(x: float, y: float) -> tuple:
        return image.getpixel((min(image.width - 1, round((x - left) * sx)), min(image.height - 1, round((y - top_y) * sy))))

    def page_at(x: float, y: float) -> tuple:
        return shot.getpixel((round(x), round(y)))

    panel = page_at(*to_page(1100, 175))  # the zone's head row, clear of the logo and the chips
    for chip, (shape, text) in zip(chips, pills, strict=True):
        assert shape.shape_type == MSO_SHAPE_TYPE.AUTO_SHAPE
        assert shape.auto_shape_type == MSO_AUTO_SHAPE_TYPE.ROUNDED_RECTANGLE
        for measured, expected in zip(box(shape), (chip["x"], chip["y"], chip["w"], chip["h"]), strict=True):
            assert abs(measured - expected) <= 2, (chip["text"], box(shape), chip)
        tx, ty, tw, th = box(text)
        assert abs(tx + tw / 2 - (chip["x"] + chip["w"] / 2)) <= 2 and abs(ty + th / 2 - (chip["y"] + chip["h"] / 2)) <= 2
        assert text.text_frame.text == chip["text"]
        # The pill's fill is the colour the page paints inside it.
        inside = (chip["x"] + 4, chip["y"] + chip["h"] / 2)
        assert _close(tuple(shape.fill.fore_color.rgb), page_at(*inside), tolerance=6), (chip["text"], shape.fill.fore_color.rgb, page_at(*inside))
        # The picture under it carries the panel there, not a second chip.
        assert _close(picture_at(*inside), panel, tolerance=6), (chip["text"], picture_at(*inside), panel)


def test_the_fixtures_carry_no_deck_content():
    """Placeholders only: no stored file id, no deck id, no product copy."""
    html = FIXTURE.read_text(encoding="utf-8")
    narrow = OVERFLOW.read_text(encoding="utf-8")
    geometry = GEOMETRY.read_text(encoding="utf-8")
    for markup in (html, narrow):
        assert ZONE_LOGO not in markup and "presentations/" not in markup
    assert "file_id" not in geometry
    texts = {*CHIPS, "Context", "Signals", "Findings", "Feeds", "Acts", "Core", "Product A", "Product B", "Product C"}
    for text in texts:
        assert f"<tspan>{text}</tspan>" in html, text
    assert html.count("<tspan>") == len(texts)
    assert narrow.count("<tspan>") == len({"Platform", "Detect", "Respond", "Report", "Hunt", "Share", "Learn", "Product A", "Product B"})
