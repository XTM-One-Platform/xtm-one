"""A browser zoom is refused before recording when the page's layout does not hold at it.

``record_browser_demo`` with ``browser_zoom: 190`` on a 1600x900 viewport lays the page out at 842 CSS px: a
three-column dashboard with a minimum width above that overflowed, its third column cut by the page's edge - seen
only on the 4K frames. Synthetic fixtures, a real Chromium where it is installed. What is proven:

* at a zoom whose laid-out width is narrower than the page's minimum width, the start page is read overflowing
  (its document, or its main container when the app shell clips it) and the take is refused before its
  screencast starts, in one line naming the width, the overflow and the largest zoom that keeps the layout;
* the largest zoom named holds; a zoom that keeps the layout is recorded without the page being resized;
* the layout probe a take runs once recorded says the page's minimum width exactly, so the take's report reads
  "its page keeps this layout down to 1000 px".
"""

from __future__ import annotations

import asyncio
import contextlib
import os

import pytest

from app.services.video_studio import capture
from app.services.video_studio.spec import parse_take_script, zoomed_window

WINDOW = (1600, 900)


class _MinWidthPage:
    """A page whose content is *least* CSS px wide whatever the viewport, read the way ``capture._LAYOUT_JS`` reads a
    real one: past its document (*clipped* False) or inside a main container its app shell clips."""

    def __init__(self, width: int, *, least: int = 1000, clipped: bool = False) -> None:
        self.width, self.least, self.clipped, self.sizes = width, least, clipped, []

    async def set_viewport_size(self, size: dict) -> None:
        self.width = size["width"]
        self.sizes.append(size["width"])

    async def evaluate(self, _script: str, _mark: bool) -> dict:
        spill = max(0, self.least - self.width)
        document, main = (0, spill) if self.clipped else (spill, 0)
        return {"width": self.width, "overflow": document > capture.SPILL_TOLERANCE, "spill": document, "main_spill": main, "boxes": []}


@pytest.fixture(autouse=True)
def _no_settle(monkeypatch):
    monkeypatch.setattr(capture, "LAYOUT_SETTLE_SECONDS", 0.0)


def _fit(zoom: int, **page) -> tuple[capture.ZoomFit, _MinWidthPage]:
    css, _ratio = zoomed_window(WINDOW, 2.0, zoom)
    laid_out = _MinWidthPage(css[0], **page)
    return asyncio.run(capture.zoom_fit(laid_out, css, WINDOW, zoom)), laid_out


@pytest.mark.parametrize("clipped", [False, True], ids=["document", "main container"])
def test_a_zoom_narrower_than_the_page_is_refused_naming_the_largest_zoom_that_keeps_it(clipped):
    refused, page = _fit(190, clipped=clipped)
    assert refused.refused and refused.css_width == 842 and refused.min_width == 1000
    assert refused.words() == (
        "at zoom 190 the page lays out at 842 px and overflows by 158 px; the largest zoom that keeps its layout is 160"
    )
    # Read unzoomed once - a page wider than any window is not the zoom's doing - then put back.
    assert page.sizes == [1600, 842] and refused.read_unzoomed
    # The zoom it names keeps the layout; one more does not.
    assert not _fit(160, clipped=clipped)[0].refused
    assert _fit(161, clipped=clipped)[0].refused
    # 150 lays it out at 1067 px: accepted, the page never resized.
    accepted, untouched = _fit(150, clipped=clipped)
    assert not accepted.refused and untouched.sizes == [] and not accepted.read_unzoomed


def test_the_issue_s_dashboard_names_the_zoom_its_numbers_give():
    # 842 px, 225 px past: a 1067 px page, whose largest zoom on a 1600 px window is 150.
    fit, _page = _fit(190, least=1067)
    assert fit.words() == (
        "at zoom 190 the page lays out at 842 px and overflows by 225 px; the largest zoom that keeps its layout is 150"
    )


def test_a_page_wider_than_its_window_unzoomed_is_refused_only_for_what_the_zoom_adds():
    # 1800 px of content: 200 px past even at 100%; the zoom of 150 cuts 533 px more.
    fit, _page = _fit(150, least=1800)
    assert fit.refused and round(fit.overflow) == 533 and fit.largest_zoom() == 100


@pytest.mark.parametrize(("clipped", "why"), [(False, "the page scrolls sideways"), (True, "its main container overflows its width")])
def test_the_layout_probe_says_the_minimum_width_exactly(clipped, why):
    # Recorded at a zoom of 150: laid out at 1067 px, the page 1000 px wide at least.
    page = _MinWidthPage(1067, clipped=clipped)
    layout = asyncio.run(capture.probe_layout(page, (1067, 600)))
    assert (layout["holds_down_to"], layout["changes_at"], layout["why"]) == (1000, 999, why)
    assert page.sizes[-1] == 1067
    assert capture.layout_words(layout).startswith("Its page keeps this layout down to 1000 px (laid out: the window")


# ── The take is refused before its screencast starts ─────────────────────


class _Cdp:
    def __init__(self) -> None:
        self.sent: list[str] = []

    def on(self, *_args) -> None:
        return None

    async def send(self, method: str, _params: dict | None = None) -> dict:
        self.sent.append(method)
        return {}


class _Mouse:
    async def move(self, *_args, **_kwargs) -> None:
        return None


class _BrowserPage(_MinWidthPage):
    mouse = _Mouse()

    async def wait_for_load_state(self, *_args, **_kwargs) -> None:
        return None


def test_record_take_refuses_an_overflowing_zoom_before_its_screencast(monkeypatch, tmp_path):
    cdp = _Cdp()
    opened: dict = {}

    class _Context:
        async def add_init_script(self, _script: str) -> None:
            return None

        async def new_cdp_session(self, _page) -> _Cdp:
            return cdp

    @contextlib.asynccontextmanager
    async def browser(*, viewport, pixel_ratio, **_kwargs):
        opened.update(viewport=viewport, pixel_ratio=pixel_ratio)
        page = _BrowserPage(viewport[0])

        async def goto(_url: str) -> None:
            return None

        yield type("Rb", (), {"page": page, "context": _Context(), "goto": staticmethod(goto)})()

    monkeypatch.setattr(capture, "profile_browser", browser)
    script = parse_take_script(
        {
            "start_url": "https://app.example.test/dash",
            "steps": [{"action": "wait", "seconds": 0.3}],
            "browser_zoom": 190,
            "viewport": {"width": 1600, "height": 900},
            "settle_ms": 0,
        }
    )
    with pytest.raises(capture.TakeError) as refused:
        asyncio.run(
            capture.record_take(
                script, workdir=str(tmp_path), profile=None, start_url="https://app.example.test/dash", policy=None, max_seconds=30
            )
        )
    assert str(refused.value) == (
        "Refused before recording: at zoom 190 the page lays out at 842 px and overflows by 158 px; the largest zoom "
        "that keeps its layout is 160."
    )
    assert opened["viewport"] == (842, 474) and "Page.startScreencast" not in cdp.sent


# ── In a real Chromium ───────────────────────────────────────────────────

_DASHBOARD = """<!doctype html><html><head><style>
body { margin: 0; font: 14px sans-serif; __BODY__ }
main { display: block; __MAIN__ }
.columns { display: grid; grid-template-columns: repeat(3, 1fr); gap: 16px; min-width: 1000px; }
.column { height: 300px; background: #ddd; }
</style></head><body><main><div class="columns"><div class="column">Alerts</div><div class="column">Cases</div>
<div class="column">Assets</div></div></main></body></html>"""


@pytest.mark.chromium
@pytest.mark.parametrize("clipped", [False, True], ids=["document", "main container"])
def test_a_real_three_column_dashboard_is_measured_at_its_zoom(clipped):
    html = _DASHBOARD.replace("__BODY__", "overflow: hidden;" if clipped else "").replace("__MAIN__", "overflow: hidden;" if clipped else "")

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
                found = {}
                for zoom in (150, 190):
                    css, ratio = zoomed_window(WINDOW, 2.0, zoom)
                    context = await browser.new_context(viewport={"width": css[0], "height": css[1]}, device_scale_factor=ratio)
                    page = await context.new_page()
                    await page.set_content(html)
                    found[zoom] = await capture.zoom_fit(page, css, WINDOW, zoom), page.viewport_size
                    if zoom == 150:
                        found["layout"] = await capture.probe_layout(page, css)
                    await context.close()
                return found
            finally:
                await browser.close()

    found = asyncio.run(run())
    accepted, refused = found[150][0], found[190][0]
    assert not accepted.refused
    assert refused.refused and refused.largest_zoom() == 160 and round(refused.overflow) == 158
    assert found[190][1] == {"width": 842, "height": 474}
    assert found["layout"]["holds_down_to"] == 1000
