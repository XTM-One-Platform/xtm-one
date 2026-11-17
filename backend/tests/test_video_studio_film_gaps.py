"""The gaps a brand film hit on 19 Oct 2026.

* a save that shows one figure twice draws its still once;
* a slide is a page: its payload says so, so the compositor keeps the
  subtitles' band clear of it (a figure is drawn clear of it);
* ``reveal: narration``: a narrated figure lasts its whole-figure hold past its
  narration, so a step named last is still followed;
* a camera move is measured, never read off a still frame: the negative
  prompt's camera terms never reach the frames reading, and a tracked screen
  replacement is not flagged for its camera;
* a frame kept as a picture past its video's end is its last frame, said so;
* text an animation turned unreadable is a text drift, and an animation that
  failed is a quality finding (the scene plays its still);
* a shot made for a video that drops the clips' own sound is asked silent;
* the media a video shows have a key that moves when a shot is made again.
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.models.video_studio import JOB_FAILED, JOB_READY, JOB_RUNNING, VideoClip, VideoMusic, VideoTake
from app.services.video_studio import (
    clips,
    content_drift,
    encoder,
    figures,
    projects,
    quality,
    text_drift,
    timeline,
)
from app.services.video_studio.spec import parse_video_spec, validate_video_spec

FLOW = {"type": "process_flow", "steps": [{"title": "Collect"}, {"title": "Enrich"}, {"title": "Act"}]}
DECK_ID = "00000000-0000-0000-0000-00000000f001"
CLIP_ID = "00000000-0000-0000-0000-0000000000c1"
IMAGE_ID = "00000000-0000-0000-0000-0000000000f2"
USER_ID = uuid.UUID("00000000-0000-0000-0000-00000000e001")


def _spec(*scenes: dict, **top) -> object:
    return parse_video_spec(validate_video_spec({"title": "Brand film", "freestyle": True, "scenes": list(scenes), **top}))


def _still(digest: str, scene_id: str = "fig") -> figures.FigurePreview:
    source = figures.FigureSource("process_flow", {"layout": "content", "blocks": [FLOW]}, None, frozenset())
    return figures.FigurePreview(scene_id, source, {"kind": "process_flow"}, (1920, 1080), digest)


# ── 1: a figure's still ───────────────────────────────────────────────────


def test_a_save_showing_one_figure_twice_draws_it_once(monkeypatch, tmp_path):
    """One still per digest: the worker is asked to draw it once (``figure_stills.enqueue``), and draws it once."""
    from app.core import database
    from app.services import orchestration_engine
    from app.services.file_storage import file_storage
    from app.services.video_studio import figure_stills

    drawn: list = []
    uploaded: list = []
    payloads: list = []

    async def has_preview(_digest):
        return False

    async def claim(_digest):
        return True

    async def draw_still(source, _document, **_kwargs):
        drawn.append(source.kind)
        return b"JPEG", {"steps": []}

    async def upload_to(_data, name, _type):
        uploaded.append(name)

    class _Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return False

        async def commit(self):
            return None

    async def create_execution(_db, *, payload, **_kwargs):
        payloads.append(json.loads(json.dumps(payload)))

    async def wake():
        return None

    monkeypatch.setattr(figures, "has_preview", has_preview)
    monkeypatch.setattr(figures, "claim_drawing", claim)
    monkeypatch.setattr(figures, "_draw_still", draw_still)
    monkeypatch.setattr(file_storage, "upload_to", upload_to)
    monkeypatch.setattr(database, "async_session", _Session)
    monkeypatch.setattr(orchestration_engine, "create_execution", create_execution)
    monkeypatch.setattr(orchestration_engine, "notify_orchestration_wake", wake)
    reader = SimpleNamespace(id=USER_ID, role="user")
    assert asyncio.run(figure_stills.enqueue([_still("d1", "a"), _still("d1", "b")], reader)) == {"d1"}
    [payload] = payloads
    assert asyncio.run(figure_stills.draw(payload)) == 1
    assert drawn == ["process_flow"] and uploaded == [figures.steps_object("d1"), figures.preview_object("d1")]


# ── 2: a page keeps the subtitles' band clear ─────────────────────────────


def test_a_slide_is_a_framed_page_and_footage_and_a_figure_are_not():
    spec = _spec(
        {"id": "fig", "type": "figure", "block_spec": FLOW, "narration": "We collect, we enrich, we act."},
        {"id": "deck", "type": "slide", "presentation_id": DECK_ID, "slide": 1},
        {"id": "photo", "type": "image", "file_id": IMAGE_ID},
        {"id": "shot", "type": "clip", "clip_id": CLIP_ID},
    )
    plan = timeline.plan(spec, timeline.PlanMedia(), fps=30, theme={})
    framed = {scene["id"]: scene.get("framed", False) for scene in plan.document["scenes"]}
    # A figure keeps clear of the band in its own frame (``figures.caption_safe_area``): never scaled twice.
    assert framed == {"fig": False, "deck": True, "photo": False, "shot": False}


async def _page_and_band(framed: bool, cues: list) -> dict:
    """A 16:9 page in a 16:9 frame played in the compositor: where the page's box and the subtitles stand."""
    from playwright.async_api import async_playwright

    from app.services.headless_render import RENDER_CHROMIUM_ARGS
    from app.services.video_studio.compositor_page import ORIGIN, page_html
    from app.services.video_studio.themes import build_theme

    scene = {
        "id": "fig",
        "type": "image",
        "src": "image/fig",
        "motion": "none",
        "fit": "contain",
        "image_w": 1920,
        "image_h": 1080,
        "transition_in": {"type": "cut", "duration": 0},
        "overlays": [],
        "start": 0,
        "end": 6,
        **({"framed": True} if framed else {}),
    }
    document = {
        "version": 1,
        "width": 1920,
        "height": 1080,
        "fps": 30,
        "duration": 6,
        "frames": 180,
        "fade_in": 0.0,
        "fade_out": 0.0,
        "theme": build_theme(mode="dark", background_style="grid"),
        "captions": {"enabled": True, "style": "pill", "position": "bottom", "cues": cues},
        "watermark": "none",
        "scenes": [scene],
        "preview_media": {},
    }
    html = page_html("").encode("utf-8")
    index = f"{ORIGIN}/index.html"

    async def handle(route) -> None:
        if route.request.url == index:
            await route.fulfill(status=200, body=html, content_type="text/html; charset=utf-8")
        else:
            await route.abort("blockedbyclient")

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True, args=list(RENDER_CHROMIUM_ARGS))
        try:
            page = await (await browser.new_context(viewport={"width": 1920, "height": 1080})).new_page()
            await page.route("**/*", handle)
            await page.goto(index, wait_until="load")
            await page.evaluate("doc => window.__xtm.load(doc)", document)
            await page.evaluate("() => window.__xtm.render(45)")
            return await page.evaluate(
                "() => ({still: document.querySelector('.still').getBoundingClientRect().toJSON(),"
                " caption: document.getElementById('caption').getBoundingClientRect().toJSON()})"
            )
        finally:
            await browser.close()


@pytest.mark.chromium
async def test_a_page_stands_above_the_subtitles_said_over_it():
    cues = [[0.3, 5.9, "We collect every signal, we enrich it, and then we respond to what matters most."]]
    try:
        framed = await _page_and_band(True, cues)
    except Exception as exc:
        if "Executable doesn't exist" not in str(exc) and "playwright install" not in str(exc).lower():
            raise
        if os.environ.get("REQUIRE_CHROMIUM") == "1":
            pytest.fail(f"Playwright's Chromium is required here and is not installed: {exc}")
        pytest.skip("Playwright's Chromium is not installed (python -m playwright install chromium)")
    band_top = framed["caption"]["y"]
    assert framed["still"]["y"] + framed["still"]["height"] <= band_top
    assert framed["still"]["width"] / framed["still"]["height"] == pytest.approx(16 / 9, abs=0.01)
    # Footage of the frame's shape plays full bleed, the subtitles over it - as a page did earlier.
    footage = await _page_and_band(False, cues)
    assert footage["still"]["y"] + footage["still"]["height"] > band_top
    # Nothing said over it: a page is full bleed too.
    quiet = await _page_and_band(True, [])
    assert (quiet["still"]["width"], quiet["still"]["height"]) == (1920, 1080)


# ── 3: a narrated figure is built as it is said ───────────────────────────


NARRATION = "We collect every signal, we enrich it, and then we respond."
STEPS = [["Collect"], ["Enrich"], ["Respond"]]


def test_a_narrated_figure_lasts_its_last_step_and_its_hold_past_its_narration():
    spec = _spec({"id": "fig", "type": "figure", "block_spec": FLOW, "duration": 2, "reveal": "narration", "narration": NARRATION})
    plan = timeline.plan(spec, timeline.PlanMedia(), fps=30, theme={})
    scene = plan.document["scenes"][0]
    _said_from, said_to = plan.narration_spans["fig"]
    assert scene["end"] - said_to == pytest.approx(figures.narrated_hold(), abs=1e-3)
    # Spread evenly, a figure keeps its own length.
    even = _spec({"id": "fig", "type": "figure", "block_spec": FLOW, "duration": 2, "narration": NARRATION})
    planned = timeline.plan(even, timeline.PlanMedia(), fps=30, theme={})
    assert planned.document["scenes"][0]["end"] - planned.narration_spans["fig"][1] == pytest.approx(timeline.NARRATION_TAIL, abs=1e-3)


def test_a_step_named_last_is_built_when_it_is_said():
    from app.services.video_studio import speech_timing

    start, seconds = 0.5, 4.0
    words = speech_timing.paced_words(NARRATION, start, seconds)
    said = next(word.start for word in words if word.text.startswith("respond")) - figures.NARRATION_LEAD
    # As long as a narrated figure lasted earlier: the step named last has no room and is not followed.
    short = figures.reveal_times(STEPS, length=start + seconds + timeline.NARRATION_TAIL, mode="narration", words=words)
    assert short[-1] < said - 0.5
    # As long as it lasts now: it comes in as it is said.
    planned = figures.reveal_times(STEPS, length=start + seconds + figures.narrated_hold(), mode="narration", words=words)
    assert planned[-1] == pytest.approx(said, abs=1e-3) and planned == sorted(planned)


# ── 4: a camera move is measured ──────────────────────────────────────────


def test_only_the_negative_prompt_s_own_terms_are_camera_terms():
    answer = json.dumps({"camera_terms": ["Camera movement", "a herd of cows"]})
    assert content_drift.parse_camera_terms(answer, ["camera movement", "blurry text"]) == ["camera movement"]
    assert content_drift.parse_camera_terms("not json", ["camera movement"]) == []


def test_a_camera_term_is_never_asked_of_the_frames(monkeypatch, tmp_path):
    from app.core import database
    from app.llm import router

    asked: list[str] = []
    answers = [
        json.dumps({"subjects": [{"name": "monitor", "kind": "a screen"}], "camera": "locked", "camera_terms": ["camera movement"]}),
        json.dumps({"frames": {"first_frame": {"subjects": [{"name": "monitor", "present": True, "kind_matches": True}]}}}),
    ]

    class _Llm:
        async def chat(self, messages, **_options):
            asked.append(messages[0].content)
            return SimpleNamespace(content=answers[len(asked) - 1])

    async def llm_for_user(_db, _user_id, *_a, **_kw):
        return _Llm(), "a-vision-model"

    @asynccontextmanager
    async def session():
        yield None

    async def extract_still(_video, *, at, out_path, max_side, crop_to=None):
        Path(out_path).write_bytes(b"JPEG")

    monkeypatch.setattr(router, "get_llm_for_user", llm_for_user)
    monkeypatch.setattr(database, "async_session", session)
    monkeypatch.setattr(encoder, "extract_still", extract_still)
    params: dict = {}
    found = asyncio.run(
        content_drift.check(
            USER_ID,
            prompt="A locked-off shot of a monitor on a desk, no camera movement",
            negative_prompt="camera movement, blurry text",
            video_path="clip.mp4",
            duration=6.0,
            workdir=str(tmp_path),
            params=params,
        )
    )
    assert found.checked and found.findings == [] and found.camera_terms == ["camera movement"]
    assert "camera movement" in asked[0]
    assert "blurry text" in asked[1] and "camera movement" not in asked[1]
    # Kept with the shot: the same prompt generated again is not read again.
    assert params[content_drift.SUBJECTS_KEY]["camera_terms"] == ["camera movement"]


def test_a_tracked_screen_is_not_flagged_for_its_camera_unless_it_sets_a_tolerance():
    def shot(**fields) -> quality.ShotFacts:
        return quality.ShotFacts("monitor", JOB_READY, camera="locked", motion_ssim=0.3, **fields)

    assert quality.camera_moved(shot()) is True
    assert quality.camera_moved(shot(screen=True)) is False
    assert quality.camera_moved(shot(screen=True, max_camera_motion=0.5)) is True
    assert quality.camera_moved(shot(max_camera_motion=0.8)) is False
    assert quality.camera_moved(quality.ShotFacts("pan", JOB_READY, camera="moving", motion_ssim=0.3)) is False


# ── 6: a frame past the end ───────────────────────────────────────────────


def test_a_frame_kept_past_a_shot_s_end_is_its_last_frame_said_so(monkeypatch):
    from app.services.video_studio import projects as video_projects

    clip = VideoClip(
        id=uuid.UUID(CLIP_ID),
        user_id=USER_ID,
        status=JOB_READY,
        file_id=uuid.uuid4(),
        duration_ms=8000,
        width=1920,
        height=1080,
        name="Booth",
    )

    class _Db:
        async def get(self, _model, _key):
            return clip

    async def readable(*_args, **_kwargs):
        return True

    monkeypatch.setattr(video_projects, "can_read_project_asset", readable)
    source = asyncio.run(clips.resolve_frame(_Db(), SimpleNamespace(id=USER_ID), clip_id=CLIP_ID, at_seconds=9.5))
    assert source["at_seconds"] is None and (source["clamped_from"], source["lasts"]) == (9.5, 8.0)
    assert clips.frame_label(source) == 'the last frame of the AI shot "Booth" (asked at 9.5 s; it lasts 8.0 s)'
    # A shot asked to start past the end is still refused: what it would open on is not what was asked.
    with pytest.raises(clips.ClipError, match=r"lasts 8\.0 s"):
        asyncio.run(clips.resolve_reference(_Db(), SimpleNamespace(id=USER_ID), clip_id=CLIP_ID, at_seconds=9.5))


def test_a_video_file_s_frame_past_its_end_is_its_last(monkeypatch, tmp_path):
    from app.services.video_studio import assets

    stills: list = []

    async def download(_file_id, path):
        Path(path).write_bytes(b"MP4")
        return SimpleNamespace()

    async def probe(_path):
        return SimpleNamespace(duration=5.0, width=1280, height=720)

    async def extract_still(_video, *, at, out_path, max_side, crop_to=None):
        stills.append(at)
        Path(out_path).write_bytes(b"PNG")

    monkeypatch.setattr(assets, "download_file", download)
    monkeypatch.setattr(encoder, "probe", probe)
    monkeypatch.setattr(encoder, "extract_still", extract_still)
    source = {"kind": "file", "id": IMAGE_ID, "file_id": IMAGE_ID, "name": "scene.mp4", "at_seconds": 7.0}
    assert asyncio.run(clips.frame_picture(source, str(tmp_path))) == b"PNG"
    assert stills == [None] and source["clamped_from"] == 7.0 and "asked at 7.0 s; it lasts 5.0 s" in clips.frame_label(source)


# ── 7: text turned unreadable ─────────────────────────────────────────────


def test_text_an_animation_turned_unreadable_is_a_text_drift():
    reading = text_drift._parse(
        json.dumps(
            {
                "start_picture": {"text": "G2 Leader Fall 2026", "unreadable": 0},
                "first_frame": {"text": "G2 Leader Fall 2026", "unreadable": 0},
                "last_frame": {"text": "G2", "unreadable": 3},
            }
        ),
        ["start_picture", *text_drift.FRAMES],
    )
    assert reading is not None and reading.texts["last_frame"] == "G2"
    garbled = text_drift.unreadable_frames(reading.unreadable, reading.texts["start_picture"])
    assert garbled == [("last_frame", 3)]
    verdict = text_drift.TextCheck(True, texts=reading.texts, unreadable=garbled)
    assert verdict.failed and "drawn unreadable (3 places in the last frame)" in verdict.reason
    # A picture already holding unreadable text sets the bar; a picture with no words has none to lose.
    assert text_drift.unreadable_frames({"start_picture": 2, "last_frame": 2}, "Leader") == []
    assert text_drift.unreadable_frames({"last_frame": 4}, "") == []
    # The answer of a model reading the old way still reads.
    old = text_drift._parse('{"start_picture": "Orchestration", "first_frame": "", "last_frame": ""}', ["start_picture", *text_drift.FRAMES])
    assert old is not None and old.texts["start_picture"] == "Orchestration" and old.unreadable["last_frame"] == 0


def test_an_animation_that_failed_is_a_finding_and_the_scene_plays_its_still():
    spec = _spec({"id": "meetup", "type": "image", "file_id": IMAGE_ID, "animate": {"clip_id": CLIP_ID}})
    document = timeline.plan(spec, timeline.PlanMedia(), fps=30, theme={}).document
    facts = quality.ContentFacts(shots={CLIP_ID: quality.ShotFacts("Meetup", JOB_FAILED, error="text drift: the video model redrew the text")})
    found = quality.content_findings(spec, document, facts, None)
    assert [(f.scene_id, f.kind, f.severity) for f in found] == [("meetup", "shot_state", "advisory")]
    assert found[0].message.startswith('plays as its still image: its animation "Meetup" failed (text drift')
    assert f"clip_id `{CLIP_ID}`" in found[0].message


# ── 8: a shot made for a video that drops the clips' own sound ───────────


def test_a_shot_for_a_video_dropping_clip_sound_is_asked_silent(monkeypatch):
    project = SimpleNamespace(spec={"title": "x", "clip_sound": "off"})

    async def visible(_db, video_id, _user):
        return project if video_id else None

    async def effective(_db, _project, _user):
        return SimpleNamespace(spec={"clip_sound": "off"})

    monkeypatch.setattr(projects, "load_visible_project", visible)
    monkeypatch.setattr(projects, "effective_spec", effective)
    assert asyncio.run(clips.video_drops_clip_sound(None, None, "v1")) is True
    assert asyncio.run(clips.video_drops_clip_sound(None, None, None)) is False
    assert clips.sound_note({clips.SILENT_FOR_VIDEO: True}).startswith("Asked silent: the video it was made for drops")
    assert clips.sound_note({}) is None


# ── 9: the media a video shows ────────────────────────────────────────────


def test_the_media_key_moves_when_a_shot_is_made_again_in_place():
    take_id, music_id = uuid.uuid4(), uuid.uuid4()
    clip = VideoClip(id=uuid.UUID(CLIP_ID), status=JOB_READY, file_id=uuid.uuid4(), finished_at=datetime(2026, 10, 19, tzinfo=UTC), params={})
    rows = {
        uuid.UUID(CLIP_ID): clip,
        take_id: VideoTake(id=take_id, status=JOB_READY, file_id=uuid.uuid4()),
        music_id: VideoMusic(id=music_id, status=JOB_READY, file_id=uuid.uuid4()),
    }

    class _Db:
        async def get(self, _model, key):
            return rows.get(key)

    spec = {
        "title": "x",
        "music": {"music_id": str(music_id)},
        "scenes": [
            {"id": "rec", "type": "recording", "take_id": str(take_id)},
            {"id": "shot", "type": "clip", "clip_id": CLIP_ID},
        ],
    }
    first = asyncio.run(projects.media_state(_Db(), spec))
    assert first.regenerating == [] and asyncio.run(projects.media_state(_Db(), spec)).key == first.key
    # Generated again: running beside the take it keeps - watched, its key moved.
    clip.status, clip.params = JOB_RUNNING, {clips.KEPT_TAKE: {"prompt": "p"}}
    running = asyncio.run(projects.media_state(_Db(), spec))
    assert running.regenerating == [CLIP_ID] and running.key != first.key
    # Its new take is in: no longer watched, and the key moved again.
    clip.status, clip.params, clip.file_id = JOB_READY, {}, uuid.uuid4()
    clip.finished_at = datetime(2026, 10, 19, 1, tzinfo=UTC)
    made = asyncio.run(projects.media_state(_Db(), spec))
    assert made.regenerating == [] and made.key not in (first.key, running.key)
