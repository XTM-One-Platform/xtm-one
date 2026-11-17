"""Motion design in the compositor page, read off the frames Chromium draws.

The brand's tokens drive the cards (``precise`` the numbers drawn before
them), every composition draws under every personality with nothing the
quality check would say, and a motion scene - its particles, its Lottie, its
morph, its path drawn, its 3D camera - is a pure function of time: two pages
draw the same frame, a sub-frame of motion blur included.
"""

from __future__ import annotations

import base64
import json
import os
from contextlib import asynccontextmanager

import pytest

from app.services.video_studio import motion_design, timeline
from app.services.video_studio.spec import MOTION_COMPOSITIONS, parse_video_spec, validate_video_spec
from app.services.video_studio.themes import build_theme

pytestmark = pytest.mark.chromium

LOTTIE_ID = "11111111-1111-1111-1111-111111111111"
LOTTIE = {
    "v": "5.7.4", "fr": 30, "ip": 0, "op": 60, "w": 400, "h": 400, "nm": "box", "ddd": 0, "assets": [],
    "layers": [{
        "ddd": 0, "ind": 1, "ty": 4, "nm": "box", "sr": 1, "ao": 0, "ip": 0, "op": 60, "st": 0, "bm": 0,
        "ks": {
            "o": {"a": 0, "k": 100},
            "r": {"a": 1, "k": [{"t": 0, "s": [0], "i": {"x": [0.5], "y": [1]}, "o": {"x": [0.5], "y": [0]}}, {"t": 60, "s": [360]}]},
            "p": {"a": 0, "k": [200, 200, 0]}, "a": {"a": 0, "k": [0, 0, 0]}, "s": {"a": 0, "k": [100, 100, 100]},
        },
        "shapes": [{"ty": "gr", "it": [
            {"ty": "rc", "d": 1, "s": {"a": 0, "k": [200, 120]}, "p": {"a": 0, "k": [0, 0]}, "r": {"a": 0, "k": 20}},
            {"ty": "fl", "c": {"a": 0, "k": [0.06, 0.74, 1, 1]}, "o": {"a": 0, "k": 100}},
            {"ty": "tr", "p": {"a": 0, "k": [0, 0]}, "a": {"a": 0, "k": [0, 0]}, "s": {"a": 0, "k": [100, 100]}, "r": {"a": 0, "k": 0}, "o": {"a": 0, "k": 100}},
        ]}],
    }],
}
# A 1x1 white PNG: the logo the cards and the logo elements draw.
LOGO = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMB/6X+gYgAAAAASUVORK5CYII=")
TEXTS = {
    "title": "Uncover threats. *Take action.*", "subtitle": "The open-source XTM suite", "kicker": "Filigran",
    "figure": "6,000+", "label": "community members", "items": ["Open source", "Community led", "Enterprise ready"],
    "lines": ["Too much *noise*.", "Not enough *context*."],
}


def _document(scenes: list[dict], *, personality: str = "precise", aspect: str = "16:9", logo: bool = True) -> dict:
    spec = parse_video_spec(
        validate_video_spec(
            {
                "title": "Motion",
                "format": {"aspect_ratio": aspect},
                "theme": {"background": "aurora", "motion": {"personality": personality}},
                "voiceover": {"enabled": False},
                "captions": {"enabled": False},
                "scenes": scenes,
            }
        )
    )
    theme = build_theme(mode="dark", background_style="aurora", has_logo=logo, motion=spec.theme.motion)
    return timeline.plan(spec, timeline.PlanMedia(), fps=30, theme=theme).document


@asynccontextmanager
async def _browser():
    from playwright.async_api import async_playwright

    from app.services.headless_render import RENDER_CHROMIUM_ARGS

    try:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True, args=list(RENDER_CHROMIUM_ARGS))
            try:
                yield browser
            finally:
                await browser.close()
    except Exception as exc:
        text = str(exc)
        if "Executable doesn't exist" not in text and "playwright install" not in text.lower():
            raise
        if os.environ.get("REQUIRE_CHROMIUM") == "1":
            pytest.fail(f"Playwright's Chromium is required here and is not installed: {exc}")
        pytest.skip("Playwright's Chromium is not installed (python -m playwright install chromium)")


async def _page(browser, document: dict):
    """A compositor page with *document* loaded, its errors collected in ``page.errors``."""
    from app.services.builtin_tools.html_render import font_face_css
    from app.services.video_studio.compositor_page import ORIGIN, page_html

    html = page_html(font_face_css()).encode("utf-8")
    lottie = json.dumps(LOTTIE).encode("utf-8")

    async def handle(route) -> None:
        url = route.request.url
        if url == f"{ORIGIN}/index.html":
            await route.fulfill(status=200, body=html, content_type="text/html; charset=utf-8")
        elif url == f"{ORIGIN}/logo":
            await route.fulfill(status=200, body=LOGO, content_type="image/png")
        elif url == f"{ORIGIN}/lottie/{LOTTIE_ID}":
            await route.fulfill(status=200, body=lottie, content_type="application/json")
        else:
            await route.abort("blockedbyclient")

    context = await browser.new_context(viewport={"width": document["width"], "height": document["height"]})
    await context.route("**/*", handle)
    page = await context.new_page()
    page.errors = []
    page.on("pageerror", lambda exc: page.errors.append(str(exc)))
    await page.goto(f"{ORIGIN}/index.html", wait_until="load")
    await page.evaluate("doc => window.__xtm.load(doc)", document)
    return page


async def _at(page, document: dict, scene: int, seconds: float) -> None:
    await page.evaluate("i => window.__xtm.render(i)", (document["scenes"][scene]["start"] + seconds) * document["fps"])


_FIRST_WORD = """
() => {
  const layer = [...document.querySelectorAll('.layer')].find(node => node.style.display !== 'none');
  const word = layer.querySelector('.title .w');
  return { opacity: word.style.opacity, transform: word.style.transform, filter: word.style.filter };
}
"""


async def test_a_precise_title_card_moves_as_the_page_drew_it_before_tokens():
    document = _document([{"type": "title", "title": "Motion that holds", "duration": 4.0}])
    async with _browser() as browser:
        page = await _page(browser, document)
        # The first word half-way in: 0.22 s into the card plus half of its 0.7 s, eased out (1 - 0.5^3).
        await _at(page, document, 0, 0.22 + 0.35)
        word = await page.evaluate(_FIRST_WORD)
        assert page.errors == []
    assert word == {"opacity": "0.875", "transform": "translateY(5.75px) rotate(0.25deg)", "filter": "blur(1.75px)"}


_PARTS = """
() => {
  const layer = [...document.querySelectorAll('.layer')].find(node => node.style.display !== 'none');
  const letters = [...layer.querySelectorAll('.title .w.l')];
  const whole = [...layer.querySelectorAll('.title .w')].filter(w => w.querySelector('.grad'));
  return { letters: letters.length, whole: whole.length, moving: letters.filter(l => l.style.opacity !== '1').length };
}
"""


async def test_an_energetic_title_comes_in_letter_by_letter():
    # A card's last word is its accent, drawn in the brand gradient: one part, a gradient through the word.
    document = _document([{"type": "title", "title": "Letters land", "duration": 4.0}], personality="energetic")
    async with _browser() as browser:
        page = await _page(browser, document)
        await _at(page, document, 0, 0.3)
        early = await page.evaluate(_PARTS)
        await _at(page, document, 0, 3.5)
        late = await page.evaluate(_PARTS)
        assert page.errors == []
    assert early["letters"] == len("Letters") and early["whole"] == 1 and early["moving"] > 0
    assert late["moving"] == 0


@pytest.mark.parametrize("aspect", ["16:9", "9:16"])
@pytest.mark.parametrize("personality", motion_design.PERSONALITIES)
async def test_every_composition_draws_under_every_personality_with_nothing_to_say(personality, aspect):
    scenes = [{"type": "motion", "composition": name, "duration": 8.0, "show_logo": True, **TEXTS} for name in MOTION_COMPOSITIONS]
    document = _document(scenes, personality=personality, aspect=aspect)
    async with _browser() as browser:
        page = await _page(browser, document)
        audit = await page.evaluate("() => window.__xtm.audit()")
        assert page.errors == []
    found = {scene["id"]: [(f["kind"], f.get("block"), f.get("text")) for f in scene["findings"]] for scene in audit["scenes"]}
    assert all(not items for items in found.values()), found
    assert all(scene["reveal_end"] > 0 for scene in audit["scenes"])


def _free_scene() -> dict:
    return {
        "type": "motion",
        "duration": 4.0,
        "camera": {"perspective": 1200, "keys": [{"t": 0, "zoom": 1, "rotate_y": -12}, {"t": 4, "zoom": 1.15, "rotate_y": 10}]},
        "elements": [
            {"kind": "particles", "number": 120, "connect": 0.12, "x": 0.5, "y": 0.5, "width": 1, "height": 1},
            {"kind": "text", "text": "Free *elements*", "role": "headline", "y": 0.2, "split": "letters",
             "enter": {"preset": "flip", "order": "center"}, "loop": {"preset": "shimmer", "period": 2}},
            {"kind": "shape", "shape": "star", "fill": "gradient", "x": 0.25, "y": 0.6, "width": 0.12, "z": 200,
             "enter": {"preset": "pop", "at": 0.4}, "loop": {"preset": "spin", "period": 6}},
            {"kind": "path", "d": "M100 500 C 300 100, 700 900, 900 500", "view_box": [1000, 1000], "stroke": "accent",
             "stroke_width": 6, "x": 0.5, "y": 0.62, "width": 0.5, "height": 0.4,
             "keys": [{"t": 0.3, "draw": 0}, {"t": 2.0, "draw": 1, "ease": "out_expo"}]},
            {"kind": "path", "d": "M0 0 L100 0 L100 100 L0 100 Z", "morph_to": "M50 0 L100 50 L50 100 L0 50 Z",
             "view_box": [100, 100], "fill": "accent_2", "stroke": "none", "x": 0.75, "y": 0.6, "width": 0.1,
             "keys": [{"t": 0.5, "morph": 0}, {"t": 2.5, "morph": 1, "ease": "spring(170, 12)"}]},
            {"kind": "lottie", "file_id": LOTTIE_ID, "x": 0.5, "y": 0.85, "width": 0.12, "z": -150},
            {"kind": "text", "text": "Along a path", "role": "label", "x": 0.1, "y": 0.9,
             "path": {"d": "M100 900 Q 500 700 900 900", "start": 0.2, "end": 3.5}},
        ],
    }


# Everything a motion scene sets on a frame: each element's style, each shape's and path's own, a morph's path data,
# a Lottie's transforms, the particles' pixels and the camera.
_STATE = """
() => {
  const layer = [...document.querySelectorAll('.layer')].find(node => node.style.display !== 'none');
  const world = layer.querySelector('.mworld');
  // A declaration removed on one frame and set again on a later one moves to the end of the style: order apart.
  const css = (style) => style.cssText.split(';').map(d => d.trim()).filter(Boolean).sort().join('; ');
  return {
    camera: world.style.transform,
    elements: [...layer.querySelectorAll('.mel')].map(node => ({
      style: css(node.style),
      parts: [...node.querySelectorAll('.w, .mtt, .wi, .mkr')].map(part => css(part.style)),
      shapes: [...node.querySelectorAll('rect, ellipse, line, polygon, path')].map(g => [g.getAttribute('d'), css(g.style)]),
      lottie: [...node.querySelectorAll('.mlot g')].map(g => g.getAttribute('transform')),
      canvas: [...node.querySelectorAll('canvas')].map(c => c.toDataURL()),
    })),
  };
}
"""


def _psnr(first: bytes, second: bytes) -> float:
    import io

    import numpy as np
    from PIL import Image

    a = np.asarray(Image.open(io.BytesIO(first)).convert("RGB"), dtype=np.float64)
    b = np.asarray(Image.open(io.BytesIO(second)).convert("RGB"), dtype=np.float64)
    mse = float(((a - b) ** 2).mean())
    return float("inf") if mse == 0 else 10 * np.log10(255**2 / mse)


async def test_a_motion_scene_is_a_pure_function_of_time_on_any_page():
    """Every frame - a sub-frame of motion blur too - is set the same on a page that drew every frame before it and
    on one that drew others: its pixels then differ only where Chromium rasterises after its own history (the edges
    of moving glyphs, the raster scale of a layer turned in 3D), the caveat a render's pages already carry."""
    document = _document([_free_scene()])
    frames = [12, 37.5, 61, 90]
    async with _browser() as browser:
        first = await _page(browser, document)
        state, drawn = {}, {}
        for index in [*range(0, 95), 37.5]:
            await first.evaluate("i => window.__xtm.render(i)", index)
            if index in frames:
                state[index], drawn[index] = await first.evaluate(_STATE), await first.screenshot()
        second = await _page(browser, document)
        again_state, again = {}, {}
        for index in reversed(frames):
            await second.evaluate("i => window.__xtm.render(i)", index)
            again_state[index], again[index] = await second.evaluate(_STATE), await second.screenshot()
        assert first.errors == [] and second.errors == []
    for index in frames:
        assert state[index] == again_state[index], f"frame {index} is set differently on another page"
        assert _psnr(drawn[index], again[index]) >= 40.0, f"frame {index} is drawn differently on another page"


_LOTTIE = """
() => {
  const host = document.querySelector('.mlot');
  const group = host && host.querySelector('svg g g');
  return group ? group.getAttribute('transform') : null;
}
"""


async def test_a_lottie_element_is_seeked_to_the_frame_s_time():
    document = _document([{"type": "motion", "duration": 3.0, "elements": [{"kind": "lottie", "file_id": LOTTIE_ID, "width": 0.3}]}])
    async with _browser() as browser:
        page = await _page(browser, document)
        await _at(page, document, 0, 0.5)
        half = await page.evaluate(_LOTTIE)
        await _at(page, document, 0, 1.2)
        later = await page.evaluate(_LOTTIE)
        await _at(page, document, 0, 0.5)
        back = await page.evaluate(_LOTTIE)
        assert page.errors == []
    assert half is not None and half != later and half == back


async def test_a_motion_blurred_render_averages_its_sub_frames(tmp_path):
    """A whole render at ``high`` with motion blur: as many frames as without, a moving edge drawn soft."""
    import subprocess

    import numpy as np

    from app.services.builtin_tools.html_render import font_face_css
    from app.services.video_studio import compositor, encoder
    from app.services.video_studio.spec import QUALITY_PRESETS

    if not encoder.encoder_available():
        pytest.skip("ffmpeg is not installed")
    moving = {
        "type": "motion",
        "duration": 1.0,
        "background": "solid",
        "elements": [{"kind": "shape", "shape": "rect", "fill": "white", "x": 0.1, "y": 0.5, "width": 0.05, "height": 0.2,
                      "keys": [{"t": 0, "x": 0.05}, {"t": 0.6, "x": 0.95, "ease": "linear"}]}],
    }
    reads = {}
    async with _browser():
        pass  # skipped where Chromium is not installed, as the render would fail without it
    for blur in ("off", "on"):
        spec = parse_video_spec(validate_video_spec({
            "title": "Blur", "theme": {"background": "solid", "motion": {"motion_blur": blur}}, "fade_in": False, "fade_out": False,
            "voiceover": {"enabled": False}, "captions": {"enabled": False}, "scenes": [moving]}))
        theme = build_theme(mode="dark", background_style="solid", motion=spec.theme.motion)
        document = timeline.plan(spec, timeline.PlanMedia(), fps=30, theme=theme).document
        workdir = tmp_path / blur
        workdir.mkdir()
        path = await compositor.render_frames(
            document, compositor.CompositorAssets(), preset=QUALITY_PRESETS["high"], output_size=(320, 180),
            workdir=str(workdir), font_css=font_face_css(), pages=1,
        )
        raw = subprocess.run(  # noqa: S603
            [encoder.ffmpeg_path(), "-v", "error", "-i", path, "-f", "rawvideo", "-pix_fmt", "gray", "-"],
            capture_output=True,
            check=True,
        ).stdout
        count = len(raw) // (320 * 180)
        row = np.frombuffer(raw, dtype=np.uint8).reshape(count, 180, 320)[9, 90]
        reads[blur] = (count, int(((row > 40) & (row < 215)).sum()))
    assert reads["on"][0] == reads["off"][0] == document["frames"]
    # Moving 16 px a frame: its two edges spread over half that each, the sharp render's over a pixel or two.
    assert reads["on"][1] >= reads["off"][1] + 8


async def test_a_mixed_video_begun_frame_by_frame_on_two_pages_draws_every_frame(tmp_path, monkeypatch):
    """Begin-frame capture on the render fixture's mix - a recording, a screen insert, a statement over a
    clip, a picture - drawn on two pages: a page starting mid-video gets its first pictures, and two pages begun
    together never stall."""
    from app.core.config import settings
    from app.services.builtin_tools.html_render import font_face_css
    from app.services.video_studio import compositor
    from app.services.video_studio.spec import QUALITY_PRESETS
    from tests import video_studio_render_fixture as fixture

    if not fixture.ffmpeg():
        pytest.skip("ffmpeg is not installed")
    async with _browser():
        pass  # skipped where Chromium is not installed, as the render would fail without it
    monkeypatch.setattr(settings, "video_studio_frame_capture", "begin_frame")
    monkeypatch.setenv(compositor.FRAME_CAPTURE_ENV, "begin_frame")
    begun: list[bool] = []
    frame_page = compositor._frame_page

    async def recorded(browser, context, **kwargs):
        page = await frame_page(browser, context, **kwargs)
        begun.append(page is not None)
        return page

    monkeypatch.setattr(compositor, "_frame_page", recorded)
    preset = QUALITY_PRESETS["draft"]
    made = fixture.build(tmp_path, seconds=6, fps=preset.fps)
    size = (1280, 720)
    await fixture.matte(made, size)
    tracker = compositor.DrawProgress()
    await compositor.render_frames(
        made.plan.document, made.media.assets, preset=preset, output_size=size, workdir=str(tmp_path),
        font_css=font_face_css(), pages=2, tracker=tracker,
    )
    assert begun == [True, True], f"pages begun: {begun}"
    assert tracker.done == made.plan.document["frames"]
