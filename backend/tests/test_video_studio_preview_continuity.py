"""The editor's preview never blinks.

The editor plans its preview again every few seconds while a shot is being
made, and after every save: each plan is a new document. The stage must come
through that untouched - the same media keep the same URL within a token
window and say which file they play, the compositor keeps what it shows
across documents, swaps a new take in only once it can draw, loads the next
scene before it plays and reports a source that stopped answering.
"""

import asyncio
import io
import os
import uuid
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from app.models.video_studio import JOB_PENDING, JOB_READY, VideoClip, VideoProject, VideoTake
from app.services.video_studio import encoder, media, preview, projects, templates, timeline
from app.services.video_studio.spec import parse_video_spec, validate_video_spec

CLIP_A = "00000000-0000-0000-0000-00000000a001"
CLIP_B = "00000000-0000-0000-0000-00000000b002"
TAKE_ID = "00000000-0000-0000-0000-00000000c003"


def _user():
    return SimpleNamespace(id=uuid.uuid4(), role="user", is_admin=False, email="user@example.com", is_active=True)


class _DB:
    def __init__(self, rows):
        self._rows = rows

    async def get(self, model, key):
        return self._rows.get((model, str(key)))


def _clip(clip_id: str, *, poster: bool, status: str = JOB_READY) -> VideoClip:
    return VideoClip(
        id=uuid.UUID(clip_id),
        provider="openai",
        model="sora-2",
        prompt="A shot",
        params={},
        status=status,
        file_id=uuid.uuid4() if status == JOB_READY else None,
        poster_file_id=uuid.uuid4() if poster else None,
        duration_ms=4000,
        width=1920,
        height=1080,
    )


def _plan(monkeypatch, rows: dict, scenes: list[dict]) -> dict:
    async def nothing(*_args, **_kwargs):
        return []

    async def no_template(*_args, **_kwargs):
        return None

    async def plain_theme(*_args, **_kwargs):
        return {}, {}, [], {}

    monkeypatch.setattr(projects, "verify_references", nothing)
    monkeypatch.setattr(templates, "resolve_for_spec", no_template)
    monkeypatch.setattr(preview, "_theme", plain_theme)
    draft = {"title": "Film", "freestyle": True, "scenes": scenes}
    project = VideoProject(id=uuid.uuid4(), user_id=uuid.uuid4(), title="Film", spec=draft, revision=1)
    user = rows.pop("user")
    return asyncio.run(preview.build_preview(_DB(rows), project, user, draft))


# ── The URLs a preview hands the stage ────────────────────────────────────


def test_a_windowed_media_token_is_the_same_all_through_its_window():
    file_id, reader = uuid.uuid4(), uuid.uuid4()
    token, expires = media.mint_media_token(file_id, reader, windowed=True)
    again, _ = media.mint_media_token(file_id, reader, windowed=True)
    assert token == again and media.decode_media_token(token).file_id == file_id
    # Issued at the window's start: it still has the TTL less one window to live.
    import time

    assert expires - time.time() >= (media.MEDIA_TOKEN_TTL - media.MUSIC_TOKEN_WINDOW).total_seconds() - 5
    # Another file, another reader: another token.
    assert media.mint_media_token(uuid.uuid4(), reader, windowed=True)[0] != token
    assert media.mint_media_token(file_id, uuid.uuid4(), windowed=True)[0] != token


def test_every_plan_gives_a_media_the_same_url_and_names_the_file_it_plays(monkeypatch):
    user = _user()
    shot, pending = _clip(CLIP_A, poster=True), _clip(CLIP_B, poster=False, status=JOB_PENDING)
    take = VideoTake(
        id=uuid.UUID(TAKE_ID), name="Flow", status=JOB_READY, script={}, file_id=uuid.uuid4(), poster_file_id=uuid.uuid4()
    )
    rows = {(VideoClip, CLIP_A): shot, (VideoClip, CLIP_B): pending, (VideoTake, TAKE_ID): take}
    scenes = [
        {"id": "shot", "type": "clip", "clip_id": CLIP_A},
        {"id": "pending", "type": "clip", "clip_id": CLIP_B},
        {"id": "flow", "type": "recording", "take_id": TAKE_ID},
    ]
    first = _plan(monkeypatch, {**rows, "user": user}, scenes)["document"]
    second = _plan(monkeypatch, {**rows, "user": user}, scenes)["document"]
    # The poll after the first plan: the same URLs, so the stage loads nothing again.
    assert first["preview_media"] == second["preview_media"]
    played, ids = first["preview_media"], first["preview_media_ids"]
    assert set(played) == set(ids)
    assert ids["clip/shot"] == str(shot.file_id) and ids["take/flow"] == str(take.file_id)
    # A shot or a recording carries its poster, for the stage while it loads and for the timeline.
    assert ids["poster/shot"] == str(shot.poster_file_id) and ids["poster/flow"] == str(take.poster_file_id)
    # A shot still being made has neither: no URL the browser would ask for in vain.
    assert not any(key.endswith("/pending") for key in played)


def test_a_shot_without_a_poster_yet_has_none_in_the_plan(monkeypatch):
    shot = _clip(CLIP_A, poster=False)
    document = _plan(monkeypatch, {(VideoClip, CLIP_A): shot, "user": _user()}, [{"id": "shot", "type": "clip", "clip_id": CLIP_A}])[
        "document"
    ]
    assert "clip/shot" in document["preview_media"] and "poster/shot" not in document["preview_media"]


# ── The stage, in Chromium ────────────────────────────────────────────────

RED, BLUE, GREEN = (255, 0, 0), (0, 0, 255), (0, 255, 0)
W, H = 640, 360

STATE_JS = """
window.stageState = () => {
  const layers = [...document.querySelectorAll('#stage > .layer')];
  return {
    layers: layers.length,
    shown: layers.filter(l => l.style.display === 'block').map(l => [...l.querySelectorAll('video')].map(v => ({
      src: v.getAttribute('src'), ready: v.readyState, first: v === window.firstVideo }))),
    order: layers.map(l => (l.querySelector('video') || {}).getAttribute?.('src') || null),
    posters: document.querySelectorAll('#stage img.poster').length,
  };
};
// A load watched task by task: whether the browser could have painted the stage with no scene on it.
window.loadWatched = async (doc) => {
  const stage = document.getElementById('stage');
  let emptied = false;
  const check = () => { if (![...stage.children].some(l => l.style.display === 'block')) emptied = true; };
  const watch = new MutationObserver(check);
  watch.observe(stage, { childList: true, subtree: true, attributes: true, attributeFilter: ['style'] });
  await window.__xtm.load(doc);
  await new Promise(resolve => setTimeout(resolve, 50));
  watch.disconnect();
  return { emptied, state: window.stageState() };
};
window.firstVideo = null;
window.lostCount = 0;
if (window.__xtm.onSourceLost) window.__xtm.onSourceLost(() => { window.lostCount += 1; });
"""


def _require_chromium(exc: Exception) -> None:
    if "Executable doesn't exist" not in str(exc) and "playwright install" not in str(exc).lower():
        raise exc
    if os.environ.get("REQUIRE_CHROMIUM") == "1":
        pytest.fail(f"Playwright's Chromium is required here and is not installed: {exc}")
    pytest.skip("Playwright's Chromium is not installed (python -m playwright install chromium)")


@pytest.fixture(scope="module")
def files(tmp_path_factory):
    """Two-second solid videos (VP8: Playwright's Chromium plays no H.264) and a poster, by name."""
    if not encoder.encoder_available():
        pytest.skip("ffmpeg is not installed")
    from PIL import Image

    folder = tmp_path_factory.mktemp("continuity")
    for name, colour in (("red", "red"), ("blue", "blue")):
        asyncio.run(
            encoder.ffmpeg(
                ["-f", "lavfi", "-i", f"color=c={colour}:s={W}x{H}:d=2:r=30", "-c:v", "libvpx", "-b:v", "200k", str(folder / f"{name}.webm")]
            )
        )
    # A dark product recorded a few pixels narrower than its viewport, as a capture rounded to even sizes is.
    asyncio.run(
        encoder.ffmpeg(
            ["-f", "lavfi", "-i", f"color=c=0x141a28:s={W - 8}x{H}:d=4:r=30", "-c:v", "libvpx", "-b:v", "200k", str(folder / "dark.webm")]
        )
    )
    for name, colour in (("green", GREEN), ("red", RED), ("blue", BLUE)):
        Image.new("RGB", (W, H), colour).save(folder / f"{name}.png")
    return folder


def _document(scenes: list[dict], played: dict[str, str], ids: dict[str, str]) -> dict:
    """The planner's document for *scenes* (clips of 2 s, pictures of the frame's size), playing *played*."""
    from app.services.video_studio.themes import build_theme

    spec = parse_video_spec(validate_video_spec({"title": "Film", "freestyle": True, "scenes": scenes}))
    plan_media = timeline.PlanMedia()
    for scene in scenes:
        if scene["type"] == "clip":
            plan_media.clips[scene["id"]] = timeline.ClipMedia(duration=2.0, width=W, height=H, fps=30.0, frame_count=60)
        elif scene["type"] == "image":
            plan_media.images[scene["id"]] = timeline.ImageMedia(W, H)
    document = timeline.plan(spec, plan_media, fps=30, theme=build_theme(mode="dark", background_style="grid")).document
    document.update(
        width=W,
        height=H,
        fade_in=0.0,
        fade_out=0.0,
        watermark="none",
        preview_media=played,
        preview_media_ids=ids,
    )
    document["captions"] = {**document["captions"], "enabled": False, "cues": []}
    return document


@asynccontextmanager
async def _stage(files, *, slow: tuple[str, ...] = (), gone: tuple[str, ...] = ()):
    """The compositor page; ``/m/<file>?<token>`` serves *files* - after 1.5 s for *slow*, a 404 for *gone*."""
    from playwright.async_api import async_playwright

    from app.services.headless_render import RENDER_CHROMIUM_ARGS
    from app.services.video_studio.compositor_page import ORIGIN, page_html

    html = page_html("").encode("utf-8")
    asked: list[str] = []

    async def handle(route) -> None:
        url = route.request.url
        if url == f"{ORIGIN}/index.html":
            await route.fulfill(status=200, body=html, content_type="text/html; charset=utf-8")
            return
        if not url.startswith(f"{ORIGIN}/m/"):
            await route.abort("blockedbyclient")
            return
        asked.append(url)
        if any(name in url for name in gone):
            await route.fulfill(status=404, body=b"")
            return
        if any(name in url for name in slow):
            await asyncio.sleep(1.5)
        name = url.removeprefix(f"{ORIGIN}/m/").split("?", 1)[0]
        kind = "video/webm" if name.endswith(".webm") else "image/png"
        await route.fulfill(status=200, body=(files / name).read_bytes(), content_type=kind)

    try:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True, args=list(RENDER_CHROMIUM_ARGS))
            try:
                page = await (await browser.new_context(viewport={"width": W, "height": H})).new_page()
                await page.route("**/*", handle)
                await page.goto(f"{ORIGIN}/index.html", wait_until="load")
                await page.evaluate(STATE_JS)
                yield page, asked
            finally:
                await browser.close()
    except Exception as exc:  # a missing browser skips, anything else fails
        _require_chromium(exc)


def _url(name: str, token: str) -> str:
    return f"https://studio.render/m/{name}?t={token}"


async def _pixel(page) -> tuple[int, int, int]:
    from PIL import Image

    png = await page.screenshot(clip={"x": W // 2, "y": H // 2, "width": 1, "height": 1})
    return Image.open(io.BytesIO(png)).convert("RGB").getpixel((0, 0))


def _near(pixel: tuple[int, int, int], colour: tuple[int, int, int]) -> bool:
    return all(abs(a - b) <= 60 for a, b in zip(pixel, colour, strict=True))


SHOT = [{"id": "shot", "type": "clip", "clip_id": CLIP_A}]
DRAWN = "() => { const v = document.querySelector('#stage .layer video'); return !!v && v.readyState >= 2 && !v.seeking; }"


async def _first_plan(page) -> None:
    await page.evaluate("d => window.__xtm.load(d)", _document(SHOT, {"clip/shot": _url("red.webm", "1")}, {"clip/shot": "A"}))
    await page.evaluate("() => window.__xtm.render(15)")
    await page.wait_for_function(DRAWN)
    await page.evaluate("() => { window.firstVideo = document.querySelector('#stage .layer video'); }")


@pytest.mark.chromium
async def test_a_new_plan_of_the_same_media_keeps_the_frame_on_screen(files):
    async with _stage(files) as (page, asked):
        await _first_plan(page)
        assert _near(await _pixel(page), RED)
        # The next poll: a newer token of the same file, everything else alike.
        again = _document(SHOT, {"clip/shot": _url("red.webm", "2")}, {"clip/shot": "A"})
        watched = await page.evaluate("d => window.loadWatched(d)", again)
        # Never a task leaving the stage without its scene, and the new layer plays the very same element, drawn.
        assert watched["emptied"] is False
        after = watched["state"]
        assert after["layers"] == 1 and after["shown"][0][0]["first"] and after["shown"][0][0]["ready"] >= 2
        assert after["shown"][0][0]["src"] == _url("red.webm", "1")
        assert _near(await _pixel(page), RED)
        # The newer token was never fetched: nothing loaded again.
        assert not any("t=2" in url for url in asked)


@pytest.mark.chromium
async def test_a_new_take_plays_once_it_can_draw_the_old_one_standing_in_front(files):
    async with _stage(files, slow=("blue.webm",)) as (page, _asked):
        await _first_plan(page)
        # A shot generated again: another file under the same key, slow to arrive.
        watched = await page.evaluate(
            "d => window.loadWatched(d)", _document(SHOT, {"clip/shot": _url("blue.webm", "1")}, {"clip/shot": "C"})
        )
        assert watched["emptied"] is False
        assert [video["first"] for video in watched["state"]["shown"][0]] == [False, True]
        assert _near(await _pixel(page), RED)
        # Once the new take can draw, it alone is on screen and the old element is let go.
        await page.wait_for_function("() => !document.contains(window.firstVideo)")
        state = await page.evaluate("() => window.stageState()")
        assert len(state["shown"][0]) == 1 and state["shown"][0][0]["src"] == _url("blue.webm", "1")
        assert await page.evaluate("() => window.firstVideo.getAttribute('src')") is None
        assert _near(await _pixel(page), BLUE)


@pytest.mark.chromium
async def test_a_poll_while_a_new_take_loads_keeps_the_old_one_in_front(files):
    async with _stage(files, slow=("blue.webm",)) as (page, _asked):
        await _first_plan(page)
        await page.evaluate("d => window.__xtm.load(d)", _document(SHOT, {"clip/shot": _url("blue.webm", "1")}, {"clip/shot": "C"}))
        # The next poll lands before the new take has drawn: the old one still stands in front of it.
        watched = await page.evaluate(
            "d => window.loadWatched(d)", _document(SHOT, {"clip/shot": _url("blue.webm", "2")}, {"clip/shot": "C"})
        )
        assert watched["emptied"] is False
        assert [video["first"] for video in watched["state"]["shown"][0]] == [False, True]
        assert _near(await _pixel(page), RED)
        await page.wait_for_function("() => !document.contains(window.firstVideo)")
        assert _near(await _pixel(page), BLUE)


@pytest.mark.chromium
async def test_a_shot_seen_first_shows_its_poster_until_its_video_draws(files):
    async with _stage(files, slow=("blue.webm",)) as (page, _asked):
        played = {"clip/shot": _url("blue.webm", "1"), "poster/shot": _url("green.png", "1")}
        await page.evaluate("d => window.__xtm.load(d)", _document(SHOT, played, {"clip/shot": "B", "poster/shot": "P"}))
        await page.evaluate("() => window.__xtm.render(15)")
        await page.wait_for_function("() => { const p = document.querySelector('#stage img.poster'); return !!p && p.complete; }")
        assert _near(await _pixel(page), GREEN)
        await page.wait_for_function("() => !document.querySelector('#stage img.poster')")
        assert _near(await _pixel(page), BLUE)


@pytest.mark.chromium
async def test_the_next_scene_is_loaded_at_its_first_frame_before_it_plays(files):
    scenes = [*SHOT, {"id": "next", "type": "clip", "clip_id": CLIP_B}]
    played = {"clip/shot": _url("red.webm", "1"), "clip/next": _url("blue.webm", "1")}
    async with _stage(files) as (page, _asked):
        document = _document(scenes, played, {"clip/shot": "A", "clip/next": "B"})
        await page.evaluate("d => window.__xtm.load(d)", document)
        await page.evaluate("() => window.__xtm.render(5)")
        # Built hidden while the first scene plays, its video at its first frame.
        await page.wait_for_function(
            "() => { const l = document.querySelectorAll('#stage > .layer'); const v = l[1] && l[1].querySelector('video');"
            " return l.length === 2 && l[1].style.display === 'none' && !!v && v.readyState >= 2; }"
        )
        await page.evaluate("() => { window.firstVideo = document.querySelectorAll('#stage > .layer')[1].querySelector('video'); }")
        second = next(scene for scene in document["scenes"] if scene["id"] == "next")
        settled = round((second["start"] + (second.get("transition_in") or {}).get("duration", 0) + 0.1) * 30)
        await page.evaluate("i => window.__xtm.render(i)", settled)
        # The render seeks the scene's video to its time: read the stage once every shown video has drawn it.
        await page.wait_for_function(
            "() => { const v = [...document.querySelectorAll('#stage > .layer')].filter(l => l.style.display === 'block')"
            ".flatMap(l => [...l.querySelectorAll('video')]); return v.length > 0 && v.every(e => e.readyState >= 2 && !e.seeking); }"
        )
        state = await page.evaluate("() => window.stageState()")
        # The scene comes on screen with the element that was loading for it.
        assert any(video["first"] for layer in state["shown"] for video in layer)
        assert _near(await _pixel(page), BLUE)


@pytest.mark.chromium
async def test_a_scene_built_after_the_one_behind_it_still_comes_in_over_it(files):
    scenes = [*SHOT, {"id": "next", "type": "clip", "clip_id": CLIP_B}]
    played = {"clip/shot": _url("red.webm", "1"), "clip/next": _url("blue.webm", "1")}
    async with _stage(files) as (page, _asked):
        await page.evaluate("d => window.__xtm.load(d)", _document(scenes, played, {"clip/shot": "A", "clip/next": "B"}))
        # The editor opened late in the video, then the playhead went back: the second scene is built first.
        await page.evaluate("() => window.__xtm.render(110)")
        await page.evaluate("() => window.__xtm.render(5)")
        state = await page.evaluate("() => window.stageState()")
        assert state["order"] == [_url("red.webm", "1"), _url("blue.webm", "1")]


@pytest.mark.chromium
async def test_a_source_that_stops_answering_asks_for_a_new_plan_once(files):
    async with _stage(files, gone=("red.webm",)) as (page, _asked):
        document = _document(SHOT, {"clip/shot": _url("red.webm", "1")}, {"clip/shot": "A"})
        await page.evaluate("d => window.__xtm.load(d)", document)
        await page.evaluate("() => window.__xtm.render(15)")
        await page.wait_for_function("() => window.lostCount === 1")
        # The same plan again (the editor's new plan not there yet): reported once, not in a loop.
        await page.evaluate("d => window.__xtm.load(d)", document)
        await page.evaluate("() => window.__xtm.render(16)")
        await asyncio.sleep(0.5)
        assert await page.evaluate("() => window.lostCount") == 1


def _zooming_recording(played: dict[str, str], ids: dict[str, str]) -> dict:
    """A recording scene of 4 s whose camera holds the whole window for 0.8 s, then zooms in on a corner."""
    from app.services.video_studio.themes import build_theme

    spec = parse_video_spec(validate_video_spec({"title": "Film", "freestyle": True, "scenes": [{"id": "flow", "type": "recording", "take_id": TAKE_ID}]}))
    plan_media = timeline.PlanMedia()
    plan_media.takes[TAKE_ID] = timeline.TakeMedia(
        take_id=TAKE_ID, duration=4.0, css_width=1600, css_height=900, pixel_ratio=1.0, fps=30, frame_count=120
    )
    document = timeline.plan(spec, plan_media, fps=30, theme=build_theme(mode="dark", background_style="grid")).document
    scene = next(scene for scene in document["scenes"] if scene["type"] == "recording")
    scene.update(
        start=0.0,
        end=4.0,
        time_map=[[0.0, 0.0], [4.0, 4.0]],
        camera=[[0.0, 0.5, 0.5, 1.0], [0.8, 0.5, 0.5, 1.0], [3.2, 0.35, 0.4, 1.6]],
        cursor={**scene["cursor"], "visible": False},
    )
    document.update(
        width=W, height=H, duration=4.0, frames=120, fade_in=0.0, fade_out=0.0, watermark="none", preview_media=played, preview_media_ids=ids
    )
    document["captions"] = {**document["captions"], "enabled": False, "cues": []}
    return document


@pytest.mark.chromium
async def test_a_recording_window_keeps_its_edges_while_the_camera_zooms(files):
    from PIL import Image

    drawn = "() => { const v = document.querySelector('.vp video'); return !!v && v.readyState >= 2 && !v.seeking; }"
    edges = (
        "() => { const w = document.querySelector('.win').getBoundingClientRect(), v = document.querySelector('.vp').getBoundingClientRect();"
        " return [w.left, w.right, w.bottom, v.top]; }"
    )
    async with _stage(files) as (page, _asked):
        await page.evaluate("d => window.__xtm.load(d)", _zooming_recording({"take/flow": _url("dark.webm", "1")}, {"take/flow": "D"}))
        brightest = []
        for index in range(0, 100, 3):
            await page.evaluate("i => window.__xtm.render(i)", index)
            await page.wait_for_function(drawn)
            left, right, bottom, top = await page.evaluate(edges)
            shot = Image.open(io.BytesIO(await page.screenshot())).convert("L")
            pixels = shot.load()
            # A band across each edge of the footage: its left, right and bottom, clear of the rounded corners.
            band = [(x, y) for y in range(int(top) + 24, int(bottom) - 24) for x in (*range(int(left) - 3, int(left) + 4), *range(int(right) - 3, int(right) + 4))]
            band += [(x, y) for x in range(int(left) + 24, int(right) - 24) for y in range(int(bottom) - 3, int(bottom) + 4)]
            brightest.append(max(pixels[x, y] for x, y in band if 0 <= x < W and 0 <= y < H))
        # The dark footage, the window's faint border and the dark backdrop: no frame ever lights its edge up
        # (a fill under the footage leaking through an edge the camera put between two pixels; a letterbox).
        assert max(brightest) < 90, brightest


@pytest.mark.chromium
async def test_a_new_picture_under_a_key_shows_the_old_one_until_it_is_decoded(files):
    picture = [{"id": "pic", "type": "image", "file_id": str(uuid.uuid4())}]
    async with _stage(files, slow=("blue.png",)) as (page, _asked):
        await page.evaluate("d => window.__xtm.load(d)", _document(picture, {"image/pic": _url("red.png", "1")}, {"image/pic": "R"}))
        await page.evaluate("() => window.__xtm.render(15)")
        await page.wait_for_function("() => { const i = document.querySelector('#stage .still img'); return !!i && i.complete && i.naturalWidth > 0; }")
        assert _near(await _pixel(page), RED)
        await page.evaluate("d => window.__xtm.load(d)", _document(picture, {"image/pic": _url("blue.png", "1")}, {"image/pic": "B"}))
        assert _near(await _pixel(page), RED)
        await page.wait_for_function("() => (document.querySelector('#stage .still img') || {}).dataset?.src?.includes('blue')")
        await asyncio.sleep(0.1)
        assert _near(await _pixel(page), BLUE)
