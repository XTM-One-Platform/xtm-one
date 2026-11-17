"""The editor's stand-in for a screen insert keeps what stands in front and follows the render's track (#11754).

Without its build (``insert_builds``), the editor's live preview maps an insert's source onto its quad
(``compositor_page.previewInsert``). Two things make that stand-in the render's picture rather than an approximation:

* a keyed insert is given its key (``preview_key``), which the page tests on the footage frame shown with
  ``screens.key_matte``'s own arithmetic (``compositor_page.keyMatte``) and paints what is not the surface back over
  the insert (``keyMask``): a subject in front of the screen stays in front;
* a tracked insert over video is given the quads the last render of the same scene followed (``preview_track``,
  kept by ``render._keep_preview_track`` in ``render_cache``), drawn frame by frame (``previewQuad``).

Checked here in a real Chromium on the page the render draws with, and on the server side that attaches them.
"""

from __future__ import annotations

import asyncio
import base64
import io
import os
import uuid
from collections.abc import Awaitable, Callable
from types import SimpleNamespace
from typing import Any

import numpy as np
import pytest
from PIL import Image, ImageDraw

W, H = 960, 540
QUAD = [[0.30, 0.22], [0.70, 0.22], [0.70, 0.70], [0.30, 0.70]]
GREEN = (0, 177, 64)
BACKDROP = (90, 90, 90)
SUBJECT = (230, 30, 30)
INSERT = (30, 60, 230)
#: The subject's box on the frame (pixels): it stands across the screen's lower left.
SUBJECT_BOX = (330, 250, 450, 400)


def _data_url(image: Image.Image) -> str:
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buffer.getvalue()).decode("ascii")


def _footage() -> Image.Image:
    """A grey frame, a green screen on the quad, a red subject standing in front of part of it."""
    image = Image.new("RGB", (W, H), BACKDROP)
    draw = ImageDraw.Draw(image)
    draw.rectangle([QUAD[0][0] * W, QUAD[0][1] * H, QUAD[2][0] * W, QUAD[2][1] * H], fill=GREEN)
    draw.rectangle(SUBJECT_BOX, fill=SUBJECT)
    return image


def _document(keyed: bool) -> dict[str, Any]:
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
        **({"preview_key": {"rgb": list(GREEN), "tolerance": 0.3}} if keyed else {}),
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
        "preview_media": {
            "image/s1": _data_url(_footage()),
            "insert/s1.0": _data_url(Image.new("RGB", (1280, 720), INSERT)),
        },
    }


async def _with_page(work: Callable[[Any], Awaitable[Any]]) -> Any:
    from playwright.async_api import async_playwright

    from app.services.builtin_tools.html_render import font_face_css
    from app.services.video_studio.compositor_page import ORIGIN, page_html

    html = page_html(font_face_css()).encode("utf-8")
    index = f"{ORIGIN}/index.html"

    async def handle(route: Any) -> None:
        if route.request.url == index:
            await route.fulfill(status=200, body=html, content_type="text/html; charset=utf-8")
        else:
            await route.abort("blockedbyclient")

    try:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True)
            try:
                page = await browser.new_page(viewport={"width": W, "height": H})
                await page.route("**/*", handle)
                await page.goto(index)
                return await work(page)
            finally:
                await browser.close()
    except Exception as exc:
        if "Executable doesn't exist" not in str(exc) and "playwright install" not in str(exc).lower():
            raise
        if os.environ.get("REQUIRE_CHROMIUM") == "1":
            pytest.fail(f"Playwright's Chromium is required here and is not installed: {exc}")
        pytest.skip("Playwright's Chromium is not installed (python -m playwright install chromium)")


async def _shot(page: Any, document: dict[str, Any], frame: int) -> np.ndarray:
    await page.evaluate("doc => window.__xtm.load(doc)", document)
    for _ in range(3):
        await page.evaluate("i => window.__xtm.render(i)", frame)
        await page.wait_for_function("() => [...document.images].every(i => i.complete)")
    raw = await page.screenshot(type="png")
    return np.asarray(Image.open(io.BytesIO(raw)).convert("RGB"))


def _near(shot: np.ndarray, colour: tuple[int, int, int], box: tuple[int, int, int, int]) -> float:
    x0, y0, x1, y1 = box
    region = shot[y0:y1, x0:x1].astype(int)
    return float((np.abs(region - np.asarray(colour)).max(axis=-1) < 40).mean())


@pytest.mark.chromium
async def test_the_page_tests_a_key_with_the_render_s_own_matte() -> None:
    from app.services.video_studio import screens

    rng = np.random.default_rng(11754)
    pixels = rng.integers(0, 256, size=(64, 64, 3), dtype=np.uint8)
    pixels[:16] = GREEN  # the flat surface
    pixels[16:24] = (20, 150, 60)  # a darker shade of it
    pixels[24:28] = (0, 0, 0)  # too dark to be the key
    key = screens.Key("green", GREEN)

    async def work(page: Any) -> Any:
        rgba = np.concatenate([pixels, np.full((64, 64, 1), 255, np.uint8)], axis=-1).reshape(-1).tolist()
        return await page.evaluate(
            "([data, key]) => Array.from(window.__xtm.standIn.keyMatte(data, key))",
            [rgba, {"rgb": list(GREEN), "tolerance": 0.3}],
        )

    page_matte = np.asarray(await _with_page(work), dtype=np.float32).reshape(64, 64)
    render_matte = screens.key_matte(pixels, key, 0.3)
    assert float(np.abs(page_matte - render_matte).max()) < 1e-3
    assert page_matte[:16].min() > 0.99 and page_matte[24:28].max() == 0.0


@pytest.mark.chromium
async def test_a_keyed_stand_in_keeps_the_subject_in_front_and_shows_no_key() -> None:
    async def work(page: Any) -> Any:
        return await _shot(page, _document(keyed=True), 15), await _shot(page, _document(keyed=False), 15)

    keyed, plain = await _with_page(work)
    sx0, sy0, sx1, sy1 = SUBJECT_BOX
    # The subject's part inside the screen's quad (it stands past the screen's lower edge too).
    inner_subject = (sx0 + 6, sy0 + 6, sx1 - 6, min(sy1, int(QUAD[2][1] * H)) - 4)
    surface = (int(0.55 * W), int(0.26 * H), int(0.68 * W), int(0.42 * H))
    # Unkeyed, the stand-in covers the subject; keyed, the subject stays in front of it.
    assert _near(plain, INSERT, inner_subject) > 0.95
    assert _near(keyed, SUBJECT, inner_subject) > 0.95
    # The surface shows the insert, never the key colour.
    assert _near(keyed, INSERT, surface) > 0.95
    x0, y0, x1, y1 = int(QUAD[0][0] * W) + 3, int(QUAD[0][1] * H) + 3, int(QUAD[2][0] * W) - 3, int(QUAD[2][1] * H) - 3
    assert _near(keyed, GREEN, (x0, y0, x1, y1)) < 0.01


@pytest.mark.chromium
async def test_a_tracked_stand_in_takes_the_quad_the_render_followed_on_the_frame_shown() -> None:
    quads = [
        [[0.1 + 0.01 * i, 0.2], [0.5 + 0.01 * i, 0.2], [0.5 + 0.01 * i, 0.6], [0.1 + 0.01 * i, 0.6]] for i in range(30)
    ]
    ins = {"quad": QUAD, "preview_track": {"from": 10, "fps": 30.0, "quads": quads}}

    async def work(page: Any) -> Any:
        return await page.evaluate(
            "ins => { const q = window.__xtm.standIn.previewQuad;"
            " return [q(ins, 0.5), q(ins, 0.0), q(ins, 9.0), q(ins, undefined), q({quad: ins.quad}, 0.5)]; }",
            ins,
        )

    at_half, before, after, unknown, untracked = await _with_page(work)
    assert at_half == quads[5]  # footage frame 15, the 6th followed
    assert before == quads[0] and after == quads[-1]
    assert unknown == QUAD and untracked == QUAD


def test_a_preview_attaches_the_key_and_the_render_s_track_and_says_approximate_only_without_a_track(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services.video_studio import insert_builds, insert_tracking, media, render_cache, timeline
    from app.services.video_studio.spec import parse_video_spec, validate_video_spec

    clip, take = str(uuid.uuid4()), str(uuid.uuid4())
    spec = parse_video_spec(
        validate_video_spec(
            {
                "title": "T",
                "scenes": [
                    {
                        "id": "demo",
                        "type": "clip",
                        "clip_id": clip,
                        "overlays": [
                            {
                                "type": "screen_insert",
                                "source": {"take_id": take},
                                "quad": QUAD,
                                "key": {"color": "green"},
                                "track": True,
                            }
                        ],
                    }
                ],
            }
        )
    )
    payload: dict[str, Any] = {
        "src": "insert/demo.0",
        "kind": "video",
        "quad": QUAD,
        "fit": "cover",
        "glow": 0.2,
        "t0": 0.5,
        "t1": 1.5,
        "time_map": [[0.5, 0.0], [1.5, 1.0]],
    }
    scene_map = [[0.0, 0.0], [3.0, 3.0]]

    def document() -> dict[str, Any]:
        scene = {"id": "demo", "type": "clip", "src": "clip/demo", "kind": "video", "time_map": scene_map}
        return {"scenes": [{**scene, "inserts": [dict(payload)]}]}

    played = SimpleNamespace(ids={"clip/demo": "footage-file", "insert/demo.0": "take-file"}, urls={})
    played.put = lambda key, url, identity: played.urls.__setitem__(key, url)
    kept: dict[tuple[str, str], object] = {}

    async def get_json(kind: str, digest: str) -> object:
        return kept.get((kind, digest))

    async def missing(_digest: str) -> None:
        return None

    async def prepare(builds: list[insert_builds.InsertBuild]) -> set[str]:
        return {b.digest() for b in builds}

    monkeypatch.setattr(render_cache, "get_json", get_json)
    monkeypatch.setattr(media, "mint_insert_token", lambda digest, user_id: (f"tok-{digest[:6]}", 0))
    monkeypatch.setattr(insert_builds, "read_build", missing)
    monkeypatch.setattr(insert_builds, "prepare", prepare)
    reader = SimpleNamespace(id=uuid.uuid4())

    # Never rendered: the key is attached, the track is not, so the scene says approximate.
    first = document()
    asyncio.run(insert_builds.attach(first, spec, played, reader, draw=True))
    ins = first["scenes"][0]["inserts"][0]
    assert ins["preview_key"]["rgb"] == [0, 177, 64] and ins["preview_key"]["tolerance"] >= 0.3
    assert "preview_track" not in ins and ins["approximate"] is True
    assert first["scenes"][0]["preview_inserts_approximate"] is True

    # A render kept the quads it followed for this scene: the preview draws on them, nothing said approximate.
    corners = tuple((float(x), float(y)) for x, y in QUAD)
    track = insert_tracking.InsertTrack(15, tuple(corners for _ in range(31)), (0.3, 0.22, 0.7, 0.7))
    digest = insert_tracking.preview_track_digest("footage-file", "demo", payload, scene_map)
    kept[(insert_tracking.PREVIEW_TRACKS, digest)] = insert_tracking.preview_track_payload(track, 30.0)
    second = document()
    asyncio.run(insert_builds.attach(second, spec, played, reader, draw=True))
    ins = second["scenes"][0]["inserts"][0]
    assert ins["preview_track"]["from"] == 15 and len(ins["preview_track"]["quads"]) == 31
    assert "approximate" not in ins and "preview_inserts_approximate" not in second["scenes"][0]

    # A quad moved since that render: its quads are not this insert's, the badge comes back.
    moved = {**payload, "quad": [[x + 0.01, y] for x, y in QUAD]}
    assert insert_tracking.preview_track_digest("footage-file", "demo", moved, scene_map) != digest
    assert timeline.insert_drawn(payload) == timeline.insert_drawn(dict(payload))


def test_a_kept_track_the_page_cannot_draw_is_refused() -> None:
    from app.services.video_studio import insert_tracking

    assert insert_tracking.preview_track_read(None) is None
    assert insert_tracking.preview_track_read({"from": 0, "fps": 30, "quads": []}) is None
    assert insert_tracking.preview_track_read({"from": 0, "fps": 30, "quads": [[[0, 0], [1, 0], [1, 1]]]}) is None
    good = insert_tracking.preview_track_read({"from": "3", "fps": 25, "quads": [[[0, 0], [1, 0], [1, 1], [0, 1]]]})
    assert good == {"from": 3, "fps": 25.0, "quads": [[[0.0, 0.0], [1.0, 0.0], [1.0, 1.0], [0.0, 1.0]]]}
