"""Text drawn in the brand gradient keeps its whole glyphs, in a real Chromium.

A gradient through the glyphs (``background-clip: text``) is painted inside
its element's box only, so a glyph reaching past that box loses what is
outside: the "g" of a title's gradient word, cut flat by a box as tall as its
line. Each frame is drawn by the compositor page itself and every element it
paints with ``background-clip: text`` is read back: its glyphs (the font's
content area) and their ink (the font's own metrics of the text) must stand
inside the box the gradient is painted in. Read on every heading font a
built-in video template sets, on the texts that reach furthest: descenders,
diacritics above capitals, a figure with letters.

Skipped where Playwright's Chromium is not installed; the ``Browser tool
(Chromium)`` CI job installs it and sets ``REQUIRE_CHROMIUM=1``, which turns
the skip into a failure.
"""

from __future__ import annotations

import os

import pytest

from app.services.template_container_seed import BUILTIN_TEMPLATE_CONTAINERS
from app.services.video_studio.themes import HEADING_FONT, build_theme

pytestmark = pytest.mark.chromium

FPS = 30
#: Sub-pixel rounding between a box and the glyphs it holds.
TOLERANCE_PX = 0.5

_HEADING_FONTS = sorted(
    {HEADING_FONT}
    | {
        entry["theme_config"]["heading_font"]
        for entry in BUILTIN_TEMPLATE_CONTAINERS
        if entry["kind"] == "video" and (entry.get("theme_config") or {}).get("heading_font")
    }
)


def _scene(sid: str, kind: str, start: float, end: float, **fields) -> dict:
    return {"id": sid, "type": kind, "transition_in": {"type": "cut", "duration": 0}, "overlays": [], "start": start, "end": end, **fields}


def _statement(sid: str, start: float, end: float, figure: str) -> dict:
    return _scene(
        sid,
        "statement",
        start,
        end,
        lines=["organisations run it"],
        kicker=None,
        figure=figure,
        secondary=[],
        chips=[],
        reveal="together",
        align="left",
        show_logo=False,
        backdrop=None,
    )


#: Each scene, and the texts it draws in the gradient.
_SCENES = [
    (
        _scene("intro", "title", 0, 4, title="eXtended Threat\n*Management*", kicker="XTM Platform", style="gradient", align="center", show_logo=False),
        ["Management"],
    ),
    (
        _scene("reach", "title", 4, 8, title="Équipe Ångström *jazz gypsy staff*", style="gradient", align="left", show_logo=False),
        ["jazz", "gypsy", "staff"],
    ),
    (_scene("close", "outro", 8, 12, title="Know your Ågency", url="filigran.io"), ["Ågency"]),
    (_statement("days", 12, 16, "3 days"), ["3 days"]),
    (_statement("count", 16, 20, "6,000+"), ["6,000+"]),
]


def _document(heading_font: str) -> dict:
    scenes = [scene for scene, _ in _SCENES]
    duration = max(scene["end"] for scene in scenes)
    return {
        "version": 1,
        "width": 1920,
        "height": 1080,
        "fps": FPS,
        "duration": duration,
        "frames": int(duration * FPS),
        "fade_in": 0.0,
        "fade_out": 0.0,
        "theme": build_theme(mode="dark", background_style="grid", template_palette={"heading_font": heading_font}),
        "captions": {"enabled": False, "style": "pill", "position": "bottom", "cues": []},
        "watermark": "none",
        "scenes": scenes,
    }


# Rects are read on screen, scaled with their card; the font's metrics are in CSS pixels,
# so they are scaled by the ratio of the glyphs' rect to the font's content area.
_READ = """
() => {
  const layer = [...document.querySelectorAll('.layer')].find(node => node.style.display !== 'none');
  const box = r => [r.left, r.top, r.right, r.bottom];
  const out = [];
  for (const node of layer.querySelectorAll('*')) {
    const cs = getComputedStyle(node);
    if (cs.backgroundClip !== 'text' && cs.webkitBackgroundClip !== 'text') continue;
    const text = node.textContent;
    if (!text.trim()) continue;
    const range = document.createRange();
    range.selectNodeContents(node);
    const glyphs = [...range.getClientRects()].filter(r => r.width > 1 && r.height > 1);
    const ctx = document.createElement('canvas').getContext('2d');
    ctx.font = `${cs.fontStyle} ${cs.fontWeight} ${cs.fontSize} ${cs.fontFamily}`;
    ctx.letterSpacing = cs.letterSpacing;
    const m = ctx.measureText(text);
    const first = glyphs[0];
    const scale = first.height / (m.fontBoundingBoxAscent + m.fontBoundingBoxDescent);
    const baseline = first.top + m.fontBoundingBoxAscent * scale;
    out.push({
      text,
      painted: [...node.getClientRects()].map(box),
      glyphs: glyphs.map(box),
      ink: [first.left - m.actualBoundingBoxLeft * scale, baseline - m.actualBoundingBoxAscent * scale,
        first.left + m.actualBoundingBoxRight * scale, baseline + m.actualBoundingBoxDescent * scale],
    });
  }
  return out;
}
"""


async def _gradient_texts(document: dict, indexes: list[int]) -> list[list[dict]]:
    """What each frame of *indexes* paints with ``background-clip: text``, drawn by the compositor page."""
    from playwright.async_api import async_playwright

    from app.services.builtin_tools.html_render import font_face_css
    from app.services.headless_render import RENDER_CHROMIUM_ARGS
    from app.services.video_studio.compositor_page import ORIGIN, page_html

    html = page_html(font_face_css()).encode("utf-8")
    index_url = f"{ORIGIN}/index.html"

    async def handle(route) -> None:
        if route.request.url == index_url:
            await route.fulfill(status=200, body=html, content_type="text/html; charset=utf-8")
        else:
            await route.abort("blockedbyclient")

    try:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True, args=list(RENDER_CHROMIUM_ARGS))
            try:
                context = await browser.new_context(viewport={"width": document["width"], "height": document["height"]})
                await context.route("**/*", handle)
                page = await context.new_page()
                await page.goto(index_url, wait_until="load")
                await page.evaluate("doc => window.__xtm.load(doc)", {**document, "preview_media": {}})
                read = []
                for index in indexes:
                    await page.evaluate("i => window.__xtm.render(i)", index)
                    read.append(await page.evaluate(_READ))
                return read
            finally:
                await browser.close()
    except Exception as exc:
        text = str(exc)
        if "Executable doesn't exist" not in text and "playwright install" not in text.lower():
            raise
        if os.environ.get("REQUIRE_CHROMIUM") == "1":
            pytest.fail(f"Playwright's Chromium is required here and is not installed: {exc}")
        pytest.skip("Playwright's Chromium is not installed (python -m playwright install chromium)")


def _inside(inner: list[float], outer: list[float]) -> bool:
    left, top, right, bottom = outer
    return (
        inner[0] >= left - TOLERANCE_PX
        and inner[1] >= top - TOLERANCE_PX
        and inner[2] <= right + TOLERANCE_PX
        and inner[3] <= bottom + TOLERANCE_PX
    )


def _bounds(rects: list[list[float]]) -> list[float]:
    return [min(r[0] for r in rects), min(r[1] for r in rects), max(r[2] for r in rects), max(r[3] for r in rects)]


@pytest.mark.parametrize("heading_font", _HEADING_FONTS)
async def test_text_drawn_in_the_gradient_keeps_its_whole_glyphs(heading_font):
    # Each scene once all in, just before it ends.
    indexes = [int((scene["end"] - 0.2) * FPS) for scene, _ in _SCENES]
    frames = await _gradient_texts(_document(heading_font), indexes)
    for (scene, expected), drawn in zip(_SCENES, frames, strict=True):
        assert [item["text"] for item in drawn] == expected, scene["id"]
        for item in drawn:
            cut = [glyph for glyph in item["glyphs"] if not any(_inside(glyph, box) for box in item["painted"])]
            assert not cut, f'{scene["id"]}: the gradient of "{item["text"]}" is painted in {item["painted"]}, its glyphs reach {cut}'
            assert _inside(item["ink"], _bounds(item["painted"])), (
                f'{scene["id"]}: the ink of "{item["text"]}" reaches {item["ink"]}, its gradient is painted in {item["painted"]}'
            )
