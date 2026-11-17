"""A statement as the compositor draws it, in a real Chromium (the behaviour behind the source checks of
``test_video_studio_mixed``).

Each frame is drawn by the compositor page itself, its DOM read back:

* ``*words*`` are drawn in the accent, a lone asterisk as typed, and an accent
  opened but never closed as typed too - the very pattern a save's note reads
  (``spec.ACCENT_PATTERN``);
* a figure counts up to its number by the end of its reveal;
* a backdrop whose shot is not made yet plays the brand backdrop, never a hole;
* an AI shot that failed is marked failed where it plays.

Skipped where Playwright's Chromium is not installed; the ``Browser tool
(Chromium)`` CI job installs it and sets ``REQUIRE_CHROMIUM=1``, which turns
the skip into a failure.
"""

from __future__ import annotations

import os

import pytest

from app.services.video_studio.spec import unclosed_accents
from app.services.video_studio.themes import build_theme

pytestmark = pytest.mark.chromium

FPS = 30


def _scene(sid: str, kind: str, start: float, end: float, **fields) -> dict:
    return {"id": sid, "type": kind, "transition_in": {"type": "cut", "duration": 0}, "overlays": [], "start": start, "end": end, **fields}


def _statement(sid: str, start: float, end: float, lines: list[str], **fields) -> dict:
    statement = {
        "lines": lines,
        "kicker": None,
        "figure": None,
        "secondary": [],
        "chips": [],
        "reveal": "together",
        "align": "center",
        "show_logo": False,
        "backdrop": None,
    }
    return _scene(sid, "statement", start, end, **{**statement, **fields})


def _document(scenes: list[dict]) -> dict:
    duration = max(scene["end"] for scene in scenes)
    return {
        "version": 1,
        "width": 1280,
        "height": 720,
        "fps": FPS,
        "duration": duration,
        "frames": int(duration * FPS),
        "fade_in": 0.0,
        "fade_out": 0.0,
        "theme": build_theme(mode="dark", background_style="grid"),
        "captions": {"enabled": False, "style": "pill", "position": "bottom", "cues": []},
        "watermark": "none",
        "scenes": scenes,
    }


_READ = """
(index) => {
  const layer = [...document.querySelectorAll('.layer')].find(node => node.style.display !== 'none');
  const words = [...layer.querySelectorAll('.stmt .lines .w')].filter(w => w.textContent.trim());
  return {
    words: words.map(w => [w.textContent, w.classList.contains('hl')]),
    figure: (layer.querySelector('.stmt .figure') || {}).textContent || null,
    videos: layer.querySelectorAll('video').length,
    failed: [...layer.querySelectorAll('.ph.failed')].map(node => node.textContent),
  };
}
"""


async def _frames(document: dict, indexes: list[int]) -> list[dict]:
    """Each frame of *indexes* drawn by the compositor page (the preview's mode, no media), its layer read."""
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
                    read.append(await page.evaluate(_READ, index))
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


async def test_accents_are_drawn_from_balanced_asterisks_and_an_unclosed_one_as_typed():
    lines = {
        "marked": ["Stop *every* breach"],
        "lone": ["5* rated by analysts"],
        "unclosed": ["We see *everything"],
    }
    scenes = [_statement(sid, n * 4, n * 4 + 4, text) for n, (sid, text) in enumerate(lines.items())]
    marked, lone, unclosed = await _frames(_document(scenes), [3 * FPS, 7 * FPS, 11 * FPS])
    assert marked["words"] == [["Stop", False], ["every", True], ["breach", False]]
    assert lone["words"] == [["5*", False], ["rated", False], ["by", False], ["analysts", False]]
    assert unclosed["words"] == [["We", False], ["see", False], ["*everything", False]]
    # What the page draws as typed is what a save names, and only that.
    noted = unclosed_accents({"scenes": [{"type": "statement", "id": sid, "lines": text} for sid, text in lines.items()]})
    assert len(noted) == 1 and noted[0].startswith('Scene unclosed: the * before "everything" in line 1')


async def test_a_figure_counts_up_to_its_number():
    scene = _statement("proof", 0, 6, ["organisations run it"], figure="6,000+")
    start, end = await _frames(_document([scene]), [0, 5 * FPS])
    assert end["figure"] == "6,000+"
    assert start["figure"] != "6,000+"


async def test_a_backdrop_not_made_yet_is_the_brand_backdrop_and_a_failed_shot_says_so_where_it_plays():
    waiting = _statement(
        "words",
        0,
        4,
        ["Uncover threats"],
        backdrop={"kind": "video", "src": "backdrop/words", "dim": 0.55, "time_map": [[0.0, 0.0], [4.0, 4.0]], "fps": 24, "frames": 96},
    )
    failed = _scene(
        "shot",
        "clip",
        4,
        8,
        src="clip/shot",
        fit="cover",
        preview_label="The AI shot did not come out: the prompt was filtered.",
        preview_state={"state": "failed", "kind": "clip"},
    )
    backdrop, shot = await _frames(_document([waiting, failed]), [2 * FPS, 6 * FPS])
    assert backdrop["videos"] == 0 and backdrop["words"] == [["Uncover", False], ["threats", False]]
    assert shot["failed"] == ["The AI shot did not come out: the prompt was filtered."]
