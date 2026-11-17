"""A framed recording's browser window fills the frame, in a real Chromium (dev:video-recording-window-bigger).

The window used to stand inside a 5.5 % margin and above the whole subtitles' band, about 66 % of a 16:9 frame's
width. It now takes the room a 2 % margin leaves, its page's bottom edge allowed under the subtitle pill
(``WINDOW_UNDER_SUBTITLES`` of the page at most), so the product reads large and the subtitles never reach the middle
of the page. Measured on the page the renders draw (``compositor_page``), at each aspect ratio a video renders in.

Skipped where Playwright's Chromium is not installed; ``REQUIRE_CHROMIUM=1`` (the CI job) turns the skip into a failure.
"""

from __future__ import annotations

import os

import pytest

from app.services.template_container_seed import BUILTIN_TEMPLATE_CONTAINERS
from app.services.video_studio.spec import Frame
from app.services.video_studio.themes import build_theme

pytestmark = pytest.mark.chromium

_CUE = "Every alert is enriched, routed and closed before your analysts even open their queue in the morning."


def _document(width: int, height: int, *, frame: dict, cues: list[list]) -> dict:
    scene = {
        "id": "demo",
        "type": "recording",
        "transition_in": {"type": "cut", "duration": 0},
        "overlays": [],
        "start": 0,
        "end": 4,
        "src": "take/demo",
        "src_fps": 30,
        "src_frames": 120,
        "src_w": 1600,
        "src_h": 900,
        "css_w": 1600,
        "css_h": 900,
        "time_map": [[0, 0], [4, 4]],
        "camera": [[0, 0.5, 0.5, 1]],
        "cursor": {"visible": False, "size": 1, "style": "arrow", "click_effect": "none", "samples": [], "clicks": []},
        "highlights": [],
        "urls": [[0, "https://demo.example.com/"]],
        "frame": frame,
        "preview_label": "Recording",
    }
    return {
        "version": 1,
        "width": width,
        "height": height,
        "fps": 30,
        "duration": 4,
        "frames": 120,
        "fade_in": 0.0,
        "fade_out": 0.0,
        "theme": build_theme(mode="dark", background_style="aurora"),
        "captions": {"enabled": bool(cues), "style": "pill", "position": "bottom", "cues": cues},
        "watermark": "none",
        "scenes": [scene],
    }


async def _boxes(document: dict) -> dict:
    """The window's, its page's and the subtitle pill's boxes on frame 30, as the page lays them out."""
    from playwright.async_api import async_playwright

    from app.services.builtin_tools.html_render import font_face_css
    from app.services.headless_render import RENDER_CHROMIUM_ARGS, RENDER_SERVICE_WORKERS
    from app.services.video_studio.compositor_page import ORIGIN, page_html

    html = page_html(font_face_css()).encode("utf-8")
    index = f"{ORIGIN}/index.html"

    async def handle(route) -> None:
        if route.request.url == index:
            await route.fulfill(status=200, body=html, content_type="text/html; charset=utf-8")
        else:
            await route.abort("blockedbyclient")

    try:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True, args=list(RENDER_CHROMIUM_ARGS))
            try:
                context = await browser.new_context(
                    viewport={"width": int(document["width"]), "height": int(document["height"])},
                    service_workers=RENDER_SERVICE_WORKERS,
                )
                await context.route("**/*", handle)
                page = await context.new_page()
                await page.goto(index, wait_until="load")
                await page.evaluate("doc => window.__xtm.load(doc)", {**document, "preview_media": {}})
                await page.evaluate("() => window.__xtm.render(30)")
                return await page.evaluate(
                    """() => {
                      const box = (n) => { if (!n) return null; const r = n.getBoundingClientRect();
                        return r.width ? { x: r.left, y: r.top, w: r.width, h: r.height } : null; };
                      const cap = document.getElementById('caption');
                      return { win: box(document.querySelector('.win')), vp: box(document.querySelector('.win .vp')),
                               pill: cap.style.display === 'block' ? box(cap.firstChild) : null };
                    }"""
                )
            finally:
                await browser.close()
    except Exception as exc:
        text = str(exc)
        if "Executable doesn't exist" not in text and "playwright install" not in text.lower():
            raise
        if os.environ.get("REQUIRE_CHROMIUM") == "1":
            pytest.fail(f"Playwright's Chromium is required here and is not installed: {exc}")
        pytest.skip("Playwright's Chromium is not installed (python -m playwright install chromium)")


def _recording_frame(entry: dict) -> dict:
    video = (entry.get("extra_metadata") or {}).get("video") or {}
    return ((video.get("scene_defaults") or {}).get("recording") or {}).get("frame") or {}


def _frame_of(slug: str) -> dict:
    return _recording_frame(next(e for e in BUILTIN_TEMPLATE_CONTAINERS if e.get("slug") == slug))


_FRAMED = [
    e["slug"]
    for e in BUILTIN_TEMPLATE_CONTAINERS
    if e["kind"] == "video" and _recording_frame(e).get("style") == "window" and not _recording_frame(e).get("fill")
]


def test_every_framed_template_keeps_a_narrow_margin():
    assert _FRAMED, "no built-in video template frames its recordings in a window"
    for slug in _FRAMED:
        assert _frame_of(slug)["padding"] <= 0.03, slug
    assert Frame().padding == 0.02


@pytest.mark.parametrize("slug", _FRAMED)
async def test_a_16_9_window_takes_at_least_80_percent_of_the_width_with_subtitles(slug):
    boxes = await _boxes(_document(1920, 1080, frame=_frame_of(slug), cues=[[0.3, 3.9, _CUE]]))
    win, vp, pill = boxes["win"], boxes["vp"], boxes["pill"]
    assert win["w"] / 1920 >= 0.80, (slug, win)
    assert win["x"] >= 0 and win["x"] + win["w"] <= 1920 and win["y"] >= 0 and win["y"] + win["h"] <= 1080
    # The subtitles reach at most the page's bottom tenth: never the middle of the product.
    assert pill is not None
    under = max(0.0, vp["y"] + vp["h"] - pill["y"])
    assert under <= 0.1 * vp["h"] + 1, (slug, under, vp)


@pytest.mark.parametrize(
    ("width", "height", "least"), [(1920, 1080, 0.86), (1080, 1920, 0.94), (1080, 1080, 0.94), (1080, 1350, 0.94)]
)
async def test_the_window_fills_every_aspect_ratio(width, height, least):
    frame = {"style": "window", "url_bar": True, "padding": 0.02, "radius": 18, "shadow": True}
    win = (await _boxes(_document(width, height, frame=frame, cues=[])))["win"]
    assert win["w"] / width >= least, (width, height, win)
    assert abs((win["x"] + win["w"] / 2) - width / 2) <= 1
