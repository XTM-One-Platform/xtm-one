"""A screen insert's legibility advice keeps the page's layout, and a push on the insert.

The save-time advisory told an author to record a wall-display dashboard again with a viewport about
548 px wide: its mobile layout, stacked cards and a hamburger menu, read as cropped and over-zoomed on a 16:10
wall screen. Synthetic fixtures - a real Chromium only where it is installed. What is proven:

* the recorder's layout probe tells a page drawn narrower from one whose breakpoint stacks its blocks, hides a
  panel or scrolls sideways (``capture.layout_change``), finds the width a responsive page changes at
  (``capture.probe_layout``) and puts the viewport back;
* a 640 px recording of a responsive page saved as an insert is told no viewport; a 1600 px one whose layout
  stacks above the advised width neither, the reason said; a page the recorder saw hold is told the viewport;
* the advice offers a larger surface and a push on the insert, with the pixel ratio that keeps the push sharp;
* ``zoom: {start, end, scale, focus}`` is validated, planned onto the page's payload, drawn by cutting the
  source to the window it frames (eased, kept inside the source), counted by the soft gate and the legibility
  check at its peak;
* the advisory is said once per insert by a save's answer, not on every save.
"""

from __future__ import annotations

import asyncio
import os

import pytest
from PIL import Image

from app.services.video_studio import (
    capture,
    insert_checks,
    insert_legibility,
    quality,
    render_checks,
    screen_insert,
    timeline,
)
from app.services.video_studio.compositor_page import page_html
from app.services.video_studio.insert_legibility import TextSize
from app.services.video_studio.spec import VideoSpecError, parse_video_spec, validate_video_spec

CLIP = "00000000-0000-0000-0000-00000000c295"
TAKE = "00000000-0000-0000-0000-00000000a295"
QUAD = [[0.3, 0.3], [0.7, 0.3], [0.7, 0.7], [0.3, 0.7]]


# ── The recorder's layout probe ──────────────────────────────────────────

#: A dashboard at 1600 px: a sidebar, three cards side by side, a table under them.
_DESKTOP = {
    "width": 1600,
    "overflow": False,
    "boxes": [[0, 0, 240, 900], [260, 80, 420, 300], [700, 80, 420, 300], [1140, 80, 420, 300], [260, 400, 1300, 260]],
}


def test_the_layout_probe_tells_a_breakpoint_from_a_page_drawn_narrower():
    narrower = {
        "width": 1200,
        "overflow": False,
        "boxes": [[0, 0, 200, 900], [220, 80, 300, 330], [540, 80, 300, 330], [860, 80, 300, 330], [220, 430, 940, 280]],
    }
    assert capture.layout_change(_DESKTOP, narrower) is None
    stacked = {**narrower, "boxes": [[0, 0, 200, 900], [220, 80, 940, 300], [220, 400, 940, 300], [220, 720, 940, 300], [220, 1040, 940, 280]]}
    # The three cards stack, and the table falls below the sidebar.
    assert capture.layout_change(_DESKTOP, stacked) == "blocks it shows side by side stack (4 pairs)"
    hidden = {**narrower, "boxes": [None, *narrower["boxes"][1:]]}
    assert capture.layout_change(_DESKTOP, hidden) == "1 of its blocks are no longer shown (a menu or a panel collapsed)"
    assert capture.layout_change(_DESKTOP, {**narrower, "overflow": True}) == "the page scrolls sideways"


class _Page:
    """A page whose cards stack below *breakpoint* CSS px, read the way ``capture._LAYOUT_JS`` reads a real one."""

    def __init__(self, width: int, breakpoint: int | None):
        self.width, self.breakpoint, self.sizes = width, breakpoint, []

    async def set_viewport_size(self, size: dict) -> None:
        self.width = size["width"]
        self.sizes.append(size["width"])

    async def evaluate(self, _script: str, _mark: bool) -> dict:
        w = self.width
        if self.breakpoint is not None and w < self.breakpoint:
            cards = [[0, 80 + 320 * n, w, 300] for n in range(3)]
            return {"width": w, "overflow": False, "boxes": [None, *cards, [0, 1040, w, 260]]}
        side, card = 0.15 * w, 0.26 * w
        cards = [[side + 20 + (card + 20) * n, 80, card, 300] for n in range(3)]
        return {"width": w, "overflow": False, "boxes": [[0, 0, side, 900], *cards, [side + 20, 400, w - side - 40, 260]]}


def test_the_probe_finds_where_a_responsive_page_changes_and_puts_the_viewport_back(monkeypatch):
    monkeypatch.setattr(capture, "LAYOUT_SETTLE_SECONDS", 0.0)
    page = _Page(1600, breakpoint=1000)
    layout = asyncio.run(capture.probe_layout(page, (1600, 900)))
    assert 1000 <= layout["holds_down_to"] <= 1060 and 960 <= layout["changes_at"] < 1000
    assert layout["why"] == "1 of its blocks are no longer shown (a menu or a panel collapsed)"
    assert page.sizes[-1] == 1600 and layout["width"] == 1600
    # Recorded at the narrowest viewport there is: nothing narrower to probe.
    narrow = _Page(640, breakpoint=1000)
    assert asyncio.run(capture.probe_layout(narrow, (640, 900)))["holds_down_to"] == 640 and narrow.sizes == [640]
    # A page with no breakpoint holds down to the floor.
    fluid = asyncio.run(capture.probe_layout(_Page(1600, breakpoint=None), (1600, 900)))
    assert (fluid["holds_down_to"], fluid["changes_at"]) == (640, None)


_RESPONSIVE_HTML = """<!doctype html><html><head><style>
body { margin: 0; font: 14px sans-serif; }
.app { display: grid; grid-template-columns: 220px 1fr; }
.side { height: 900px; background: #223; }
.cards { display: grid; grid-template-columns: repeat(3, 1fr); gap: 16px; padding: 16px; }
.card { height: 260px; background: #ddd; }
@media (max-width: 999px) { .app { grid-template-columns: 1fr; } .side { display: none; } .cards { grid-template-columns: 1fr; } }
</style></head><body><div class="app"><nav class="side"></nav><main><div class="cards">
<div class="card">Alerts</div><div class="card">Cases</div><div class="card">Assets</div></div></main></div></body></html>"""


@pytest.mark.chromium
def test_a_real_chromium_probe_finds_the_breakpoint_of_a_responsive_page():
    async def run():
        from playwright.async_api import async_playwright

        async with async_playwright() as pw:
            try:
                browser = await pw.chromium.launch(headless=True)
            except Exception as exc:
                if os.environ.get("REQUIRE_CHROMIUM") == "1":
                    raise
                pytest.skip(f"Playwright's Chromium is missing: {str(exc)[:160]}")
            try:
                page = await (await browser.new_context(viewport={"width": 1600, "height": 900})).new_page()
                await page.set_content(_RESPONSIVE_HTML)
                return await capture.probe_layout(page, (1600, 900)), page.viewport_size
            finally:
                await browser.close()

    layout, restored = asyncio.run(run())
    assert 1000 <= layout["holds_down_to"] <= 1080 and layout["changes_at"] < 1000
    assert restored == {"width": 1600, "height": 900}


# ── The advice keeps the page's layout ───────────────────────────────────


def _insert_spec(**overlay) -> object:
    return parse_video_spec(
        {
            "title": "Wall display",
            "freestyle": True,
            "scenes": [
                {
                    "id": "s1",
                    "type": "clip",
                    "clip_id": CLIP,
                    "overlays": [{"type": "screen_insert", "source": {"take_id": TAKE}, "quad": QUAD, "duration": 6, **overlay}],
                }
            ],
        }
    )


async def _advice(viewport: tuple[int, int], ratio: float, text: float, layout: dict | None, **overlay) -> list[str]:
    """What a save says of a recording of *viewport* CSS px at pixel *ratio*, its smallest text *text* CSS px, drawn
    on a quad of 40% of the frame - through the save's own measure, the recording's probed *layout* on its row."""
    from unittest import mock

    size = (round(viewport[0] * ratio), round(viewport[1] * ratio))
    geometry = render_checks.InsertGeometry(
        {("take", TAKE): size, ("clip", CLIP): (1920, 1080)},
        {"s1": (1920, 1080)},
        {TAKE: ratio},
        {},
        layouts={TAKE: layout} if layout else {},
    )

    async def file_of(_db, _kind, _ref):
        return ("video-file", False)

    async def read(*_args):
        return TextSize(text * ratio, 40)

    with (
        mock.patch.object(render_checks, "insert_geometry", mock.AsyncMock(return_value=geometry)),
        mock.patch.object(insert_legibility, "_file_of", file_of),
        mock.patch.object(insert_legibility, "_read", read),
    ):
        measured = await insert_legibility.measure(None, _insert_spec(**overlay))
    return [words for _scene, words, _severity, _surface in insert_legibility.findings(measured)]


def test_a_640_px_recording_of_a_responsive_page_saved_as_an_insert_is_told_no_viewport(monkeypatch):
    monkeypatch.setattr(capture, "LAYOUT_SETTLE_SECONDS", 0.0)
    # Probed when it was recorded: the narrowest viewport there is, its layout changing below 1000 px.
    layout = asyncio.run(capture.probe_layout(_Page(640, breakpoint=1000), (640, 900)))
    [words] = asyncio.run(_advice((640, 900), 2.0, 7.0, layout))
    assert "reads about" in words and "record it again" not in words and "viewport" not in words
    assert "push in on it with zoom {start, end, scale:" in words


def test_a_dashboard_whose_layout_stacks_is_never_told_a_narrower_viewport_and_one_that_holds_is(monkeypatch):
    monkeypatch.setattr(capture, "LAYOUT_SETTLE_SECONDS", 0.0)
    # The issue's case: a 1600 px dashboard drawn on 40% of the frame - the advised width would be about 548 px.
    stacks = asyncio.run(capture.probe_layout(_Page(1600, breakpoint=1000), (1600, 900)))
    [words] = asyncio.run(_advice((1600, 900), 2.0, 12.0, stacks))
    assert "record it again" not in words
    assert "; a narrower viewport would change its layout (1 of its blocks are no longer shown" in words
    # Drawn at 0.24x it shows as a whole page: a larger surface, never a push that crops it.
    assert "of the 1080p frame's width (" in words and "push in on it" not in words
    assert "no push: drawn at 0.24x, under 0.3x" in words
    # A page with no breakpoint, its text a little small: the viewport it holds at is advice like any other.
    holds = asyncio.run(capture.probe_layout(_Page(1600, breakpoint=None), (1600, 900)))
    [words] = asyncio.run(_advice((1600, 900), 2.0, 18.0, holds))
    viewport = int(words.split("record it again with a viewport about ")[1].split(" ")[0])
    assert 640 <= viewport < 1600 and "(its layout holds down to 640 px, probed when it was recorded)" in words


def test_a_push_on_a_recording_at_pixel_ratio_1_names_the_ratio_that_keeps_it_sharp():
    full = [[0.0, 0.0], [1.0, 0.0], [1.0, 1.0], [0.0, 1.0]]
    [words] = asyncio.run(_advice((1600, 900), 1.0, 7.0, None, quad=full))
    # The whole frame's width is not enough: no larger surface; a push of 1.5x, sharp only at pixel ratio 1.5.
    assert "1080p frame's width" not in words
    assert "push in on it with zoom {start, end, scale: 1.5}" in words
    assert ", recorded again at pixel ratio 1.5 - the same layout, more pixels - so the push stays sharp at a high render (1080p)" in words
    # The call that sizes the recording from its saved quad, never a window and zoom re-derived by hand.
    assert 'record_browser_demo(target_render: "high", video_id, scene: "' in words and "insert: 0) records it at that size" in words


def test_a_push_already_on_the_insert_counts_at_its_peak_and_the_advisory_never_blocks():
    small = asyncio.run(_advice((1600, 900), 2.0, 12.0, None))
    assert len(small) == 1
    pushed = asyncio.run(_advice((1600, 900), 2.0, 12.0, None, zoom={"start": 0.5, "end": 2.5, "scale": 3, "focus": [0.2, 0.3]}))
    assert pushed == []


# ── The push on the insert ───────────────────────────────────────────────


def test_a_zoom_is_validated_and_kept_to_its_insert():
    overlay = _insert_spec(zoom={"end": 2, "scale": 2.5}).scenes[0].overlays[0]
    assert (overlay.zoom.start, overlay.zoom.end, overlay.zoom.scale, overlay.zoom.focus) == (0.0, 2.0, 2.5, (0.5, 0.5))
    for bad, said in (
        ({"start": 2, "end": 1, "scale": 2}, "end comes after its start"),
        ({"end": 7, "scale": 2}, "past its 6 s"),
        ({"end": 2, "scale": 1}, "greater than 1"),
        ({"end": 2, "scale": 4.5}, "less than or equal to 4"),
        ({"end": 2, "scale": 2, "focus": [1.2, 0.5]}, "less than or equal to 1"),
    ):
        with pytest.raises(VideoSpecError, match=said):
            validate_video_spec(
                {
                    "title": "Wall",
                    "freestyle": True,
                    "scenes": [
                        {
                            "id": "s1",
                            "type": "clip",
                            "clip_id": CLIP,
                            "overlays": [{"type": "screen_insert", "source": {"take_id": TAKE}, "quad": QUAD, "duration": 6, "zoom": bad}],
                        }
                    ],
                }
            )
    # Another overlay type takes none.
    callout = parse_video_spec(
        {
            "title": "C",
            "freestyle": True,
            "scenes": [{"id": "s1", "type": "clip", "clip_id": CLIP, "overlays": [{"type": "callout", "text": "Hi", "zoom": {"end": 1, "scale": 2}}]}],
        }
    ).scenes[0].overlays[0]
    assert callout.zoom is None


def test_a_zoom_is_eased_and_frames_its_region_inside_the_source():
    zoom = screen_insert.Zoom(start=1.0, end=3.0, scale=2.0, focus=(0.75, 0.25))
    assert [zoom.scale_at(t) for t in (0.0, 1.0, 2.0, 3.0, 9.0)] == [1.0, 1.0, 1.5, 2.0, 2.0]
    assert zoom.window((1600, 800), 2.0) == (800.0, 0.0, 1600.0, 400.0)
    # A focus at the corner pans as far as the source goes, never past it.
    assert screen_insert.Zoom(0.0, 1.0, 4.0, (1.0, 1.0)).window((1600, 800), 4.0) == (1200.0, 600.0, 1600.0, 800.0)
    # The source cut to that window: its top right quarter - red - fills the picture drawn.
    picture = Image.new("RGB", (1600, 800), (0, 0, 255))
    picture.paste((255, 0, 0), (800, 0, 1600, 400))
    shown = screen_insert.zoomed(picture, zoom, 3.0)
    assert shown.size == (800, 400) and shown.getpixel((10, 10)) == shown.getpixel((790, 390)) == (255, 0, 0)
    # Before the push, the picture as it is; and a dict round trip is the same push.
    assert screen_insert.zoomed(picture, zoom, 0.5) is picture
    assert screen_insert.Zoom.of(zoom.as_dict()) == zoom


def test_a_zoom_is_planned_onto_the_page_and_its_peak_counted_by_the_soft_gate():
    spec = _insert_spec(zoom={"start": 1, "end": 3, "scale": 3, "focus": [0.25, 0.5]})
    [part] = timeline.insert_sources(spec).values()
    assert timeline.insert_zoom(part.overlay) == {"start": 1.0, "end": 3.0, "scale": 3.0, "focus": [0.25, 0.5]}
    assert timeline.insert_zoom(_insert_spec().scenes[0].overlays[0]) is None
    sources = {("take", TAKE): (3200, 1800)}
    # Unpushed, a 3200 px recording on 40% of a 4K frame is sharp; pushed in 3x it is drawn soft at its peak.
    assert insert_checks.soft_before(_insert_spec(), (3840, 2160), sources=sources, footage={}, pixel_ratios={TAKE: 2.0}, screens_replaced={}) == []
    [(scene, said)] = insert_checks.soft_before(spec, (3840, 2160), sources=sources, footage={}, pixel_ratios={TAKE: 2.0}, screens_replaced={})
    assert scene == "s1" and "on a quad 1536x864 px at 3840x2160, pushed in 3x by its zoom: 0.69 source pixels per pixel" in said
    # The page names the insert's own second for a pushed one, and its stand-in preview draws the same push.
    html = page_html("")
    assert "&o=${z}" in html and "function zoomTransform(z, s)" in html


# ── Said once per insert ─────────────────────────────────────────────────


def test_the_advisory_is_said_once_per_insert_not_on_every_save(monkeypatch):
    from app.services.video_studio import render_cache

    kept: dict = {}

    async def get_json(kind, digest):
        return kept.get((kind, digest))

    async def put_json(kind, digest, value):
        kept[(kind, digest)] = value

    monkeypatch.setattr(render_cache, "get_json", get_json)
    monkeypatch.setattr(render_cache, "put_json", put_json)

    def finding(kind: str, insert: str | None = None, severity: str = "advisory") -> quality.QualityFinding:
        return quality.QualityFinding(0, "s1", "clip", "Wall", kind, severity, "words", "content", insert=insert)

    report = quality.QualityReport(scene_count=1, findings=(finding("fit"), finding("insert_legibility", "insert/s1.0|take:a")))
    first = asyncio.run(insert_legibility.said_once("video", report, shown=8))
    assert [f.kind for f in first.findings] == ["fit", "insert_legibility"]
    again = asyncio.run(insert_legibility.said_once("video", report, shown=8))
    assert [f.kind for f in again.findings] == ["fit"]
    # Another insert, or the same insert in another video, is said its own once.
    other = quality.QualityReport(scene_count=1, findings=(finding("insert_legibility", "insert/s1.1|take:a"),))
    assert len(asyncio.run(insert_legibility.said_once("video", other, shown=8)).findings) == 1
    assert len(asyncio.run(insert_legibility.said_once("another", report, shown=8)).findings) == 2
    # One the answer left out of its list is not remembered: a later save says it.
    late = quality.QualityReport(scene_count=1, findings=(finding("fit"), finding("insert_legibility", "insert/s2.0|take:b")))
    asyncio.run(insert_legibility.said_once("video", late, shown=1))
    assert len(asyncio.run(insert_legibility.said_once("video", late, shown=8)).findings) == 2
    # Text under 6 px is a finding the render waits for: said on every save.
    dense = quality.QualityReport(scene_count=1, findings=(finding("insert_legibility", "insert/s3.0|take:c", "dense"),))
    for _ in range(2):
        assert len(asyncio.run(insert_legibility.said_once("video", dense, shown=8)).findings) == 1


def test_a_wall_s_panes_told_the_same_are_said_together_and_one_with_another_problem_later(monkeypatch):
    """The panes of one wall the answer does not list are remembered only where their advice is the one it
    listed - a cut pane with another problem is said by a later save, never left untold."""
    from app.services.video_studio import render_cache

    kept: dict = {}

    async def get_json(kind, digest):
        return kept.get((kind, digest))

    async def put_json(kind, digest, value):
        kept[(kind, digest)] = value

    monkeypatch.setattr(render_cache, "get_json", get_json)
    monkeypatch.setattr(render_cache, "put_json", put_json)
    narrower = "record it again with a viewport about 900 px wide"

    def pane(index: int, advice: str) -> quality.QualityFinding:
        message = f"Scene s1: its screen insert 0 (pane {index}) draws recording `t{index}` at 0.4{index}x: reads about 8 px - {advice}"
        return quality.QualityFinding(0, "s1", "clip", "Wall", "insert_legibility", "advisory", message, "content", insert=f"insert/s1.0.{index}|take:t{index}")

    wall = quality.QualityReport(
        scene_count=1, findings=(pane(0, narrower), pane(1, narrower), pane(2, "give the insert a push (zoom: 1.6)"))
    )
    first = asyncio.run(insert_legibility.said_once("video", wall, shown=1))
    assert len(first.findings) == 3
    # Pane 1 was told with pane 0's advice; pane 2's own problem is said by the next save, once.
    again = asyncio.run(insert_legibility.said_once("video", wall, shown=8))
    assert [f.insert for f in again.findings] == ["insert/s1.0.2|take:t2"]
    assert asyncio.run(insert_legibility.said_once("video", wall, shown=8)).findings == ()
    assert insert_legibility.advice_of("a measure - its advice - more") == "its advice - more"
