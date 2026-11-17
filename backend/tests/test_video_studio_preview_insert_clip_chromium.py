"""The editor's stand-in for a screen insert stays inside its quad, its camera push included (#11754, slice 2).

Without its build (``insert_builds``), the editor's live preview maps an insert's source onto its quad
(``compositor_page.previewInsert``): a box of the quad's size, drawn onto the quad by ``quadMatrix``, its
``overflow: hidden`` the quad's edge. The push (``zoom``) scales the source inside that box, never the box, as
``screen_insert.zoomed`` cuts the source to the window it frames before the render draws it on the quad. So at the
push's peak the insert covers its quad and nothing past it, as before the push - checked here in a real Chromium on
the page the render draws with, the preview's media given as data URLs:

* an insert pushed in 1.3x on a picture covers its quad before the push and at its peak, and paints nothing
  outside the quad's edge;
* the same insert without a push paints the same pixels but on the quad's anti-aliased edge: the push changes
  what the quad shows, never where.
"""

from __future__ import annotations

import base64
import io
import os

import numpy as np
import pytest
from PIL import Image

pytestmark = pytest.mark.chromium

W, H = 960, 540
#: A tilted quad on the picture (0-1 of the picture's box), the surface the insert is drawn on.
QUAD = [[0.30, 0.22], [0.70, 0.25], [0.68, 0.70], [0.32, 0.66]]
ZOOM = {"start": 0.2, "end": 1.0, "scale": 1.3, "focus": [0.8, 0.8]}
BACKDROP = (20, 40, 200)
INSERT = (230, 30, 30)


def _png(size: tuple[int, int], colour: tuple[int, int, int]) -> str:
    buffer = io.BytesIO()
    Image.new("RGB", size, colour).save(buffer, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buffer.getvalue()).decode("ascii")


def _document(zoom: dict | None) -> dict:
    from app.services.video_studio.themes import build_theme

    insert = {
        "src": "insert/s1.0",
        "kind": "image",
        "quad": QUAD,
        "fit": "cover",
        "glow": 0,
        "t0": 0.0,
        "t1": 2.0,
        "to_end": True,
        "src_w": 1280,
        "src_h": 720,
        **({"zoom": zoom} if zoom else {}),
    }
    scene = {
        "id": "s1",
        "type": "image",
        "transition_in": {"type": "cut", "duration": 0},
        "overlays": [],
        "start": 0.0,
        "end": 2.0,
        "src": "image/s1",
        "fit": "cover",
        "motion": "none",
        "image_w": W,
        "image_h": H,
        "inserts": [insert],
    }
    return {
        "version": 1,
        "width": W,
        "height": H,
        "fps": 30,
        "duration": 2.0,
        "frames": 60,
        "fade_in": 0.0,
        "fade_out": 0.0,
        "theme": build_theme(mode="dark", background_style="grid"),
        "captions": {"enabled": False, "style": "pill", "position": "bottom", "cues": []},
        "watermark": "none",
        "scenes": [scene],
        "preview_media": {"image/s1": _png((W, H), BACKDROP), "insert/s1.0": _png((1280, 720), INSERT)},
    }


def _quad_mask(margin: float) -> np.ndarray:
    """The pixels of the frame inside the quad, grown (*margin* > 0) or shrunk (< 0) by *margin* pixels."""
    from PIL import ImageDraw

    corners = np.array([(x * W, y * H) for x, y in QUAD])
    centre = corners.mean(axis=0)
    grown = [tuple(centre + (p - centre) * (1 + margin / np.linalg.norm(p - centre))) for p in corners]
    mask = Image.new("L", (W, H), 0)
    ImageDraw.Draw(mask).polygon(grown, fill=255)
    return np.asarray(mask) > 0


def _red(shot: np.ndarray) -> np.ndarray:
    return (shot[..., 0] > 180) & (shot[..., 1] < 90) & (shot[..., 2] < 90)


async def _frames(documents: list[dict], frames: list[int]) -> list[list[np.ndarray]]:
    from playwright.async_api import async_playwright

    from app.services.builtin_tools.html_render import font_face_css
    from app.services.video_studio.compositor_page import ORIGIN, page_html

    html = page_html(font_face_css()).encode("utf-8")
    index = f"{ORIGIN}/index.html"

    async def handle(route) -> None:
        if route.request.url == index:
            await route.fulfill(status=200, body=html, content_type="text/html; charset=utf-8")
        else:
            await route.abort("blockedbyclient")

    out: list[list[np.ndarray]] = []
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        try:
            page = await browser.new_page(viewport={"width": W, "height": H})
            await page.route("**/*", handle)
            await page.goto(index)
            for document in documents:
                await page.evaluate("doc => window.__xtm.load(doc)", document)
                shots = []
                for frame in frames:
                    await page.evaluate("i => window.__xtm.render(i)", frame)
                    await page.wait_for_function("() => [...document.images].every(i => i.complete)")
                    await page.evaluate("i => window.__xtm.render(i)", frame)
                    raw = await page.screenshot(type="png")
                    shots.append(np.asarray(Image.open(io.BytesIO(raw)).convert("RGB")))
                out.append(shots)
        finally:
            await browser.close()
    return out


async def test_a_pushed_insert_in_the_preview_covers_its_quad_and_never_paints_past_it():
    # Frame 3 (0.1 s): before the push; frame 45 (1.5 s): past its end, at its 1.3x peak.
    frames = [3, 45]
    try:
        (pushed, flat) = await _frames([_document(ZOOM), _document(None)], frames)
    except Exception as exc:
        if "Executable doesn't exist" not in str(exc) and "playwright install" not in str(exc).lower():
            raise
        if os.environ.get("REQUIRE_CHROMIUM") == "1":
            pytest.fail(f"Playwright's Chromium is required here and is not installed: {exc}")
        pytest.skip("Playwright's Chromium is not installed (python -m playwright install chromium)")
    inside, outside = _quad_mask(-3.0), ~_quad_mask(3.0)
    for name, shot in [("before the push", pushed[0]), ("at the push's peak", pushed[1]), ("without a push", flat[1])]:
        red = _red(shot)
        covered = red[inside].mean()
        spilled = int(red[outside].sum())
        assert covered > 0.98, f"{name}: the insert covers {100 * covered:.1f}% of its quad"
        assert spilled == 0, f"{name}: {spilled} insert pixels painted past the quad's edge"
    # The push moves what the quad shows, never where: pushed or not, the insert's pixels differ only on the quad's
    # anti-aliased edge (its source drawn on the edge's sub-pixels at another place).
    moved = _red(pushed[1]) ^ _red(flat[1])
    assert int(moved[inside].sum()) == 0 and int(moved[outside].sum()) == 0
