"""A figure scene's still in the editor's preview: drawn by the worker once saved, never a blank placeholder.

* the still is kept under the digest of what it shows (block, palette,
  scheme, size, the subtitles' band it keeps clear), never the throwaway deck
  id the figure page is drawn under;
* a save hands its stills to the worker and answers at once, one still per
  digest, queued once however often it is asked; the worker draws
  only those not drawn yet, never failing for one, in a process held to the
  render's memory ceiling, its claims freed however it ends;
* the preview plays a drawn still where the figure plays, for a reader who
  may read every picture it draws; otherwise it names the figure as before,
  and a manager's draft has the missing still handed to the worker;
* a still the preview shows is kept, its steps with it, and one nobody opened
  is swept;
* the still is served by a token naming its digest for one reader;
* the steps the page read of the figure are kept beside its still, and a save
  says when each comes in as it plans them;
* once its still shows, the preview plays the figure's build where the scene
  plays - drawn in the background for the scene as the preview times it, kept
  under the digest of the still and of that timing, served by ranges - its
  still the poster.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.services.video_studio import figure_stills, figures, media, preview, timeline
from app.services.video_studio.spec import parse_video_spec, validate_video_spec

FLOW = {"type": "process_flow", "steps": [{"title": "Collect"}, {"title": "Enrich"}, {"title": "Act"}]}
READER = SimpleNamespace(id=uuid.UUID("00000000-0000-0000-0000-00000000d001"), role="user")


@pytest.fixture(autouse=True)
def _secret(monkeypatch):
    from app.core.config import settings

    if not settings.secret_key:
        monkeypatch.setattr(settings, "secret_key", "figure-still-test-secret-0123456789abcdef0123456789")


def _scene(**fields):
    raw = {"title": "Explainer", "freestyle": True, "scenes": [{"id": "fig", "type": "figure", "block_spec": FLOW}], **fields}
    return parse_video_spec(validate_video_spec(raw))


def _still(palette: dict | None = None, viewport=(1920, 1080), safe: dict | None = None, **scene) -> figures.FigurePreview:
    parsed = _scene(scenes=[{"id": "fig", "type": "figure", "block_spec": FLOW, **scene}]).scenes[0]
    source = figures.source_from_spec(parsed)
    palette = palette or {"primary": "#00bbff"}
    safe = safe or dict(figures.NO_SAFE_AREA)
    document = figures.figure_document(source, scheme="dark", palette=palette)
    digest = figures.preview_digest(source, scheme="dark", palette=palette, viewport=viewport, safe=safe)
    return figures.FigurePreview("fig", source, document, viewport, digest, safe, parsed)


def test_a_still_is_kept_under_what_it_shows_never_the_deck_id_it_is_drawn_under():
    first, again = _still(), _still()
    # The figure page's document differs on every read (a new deck id, a new slide id)...
    assert first.document != again.document
    # ...the digest of what the still is drawn from does not.
    assert first.digest == again.digest and len(first.digest) == 64
    assert _still({"primary": "#ff8800"}).digest != first.digest
    assert _still(viewport=(1080, 1920)).digest != first.digest
    assert _still(safe={"top": 0.0, "bottom": 200.0}).digest != first.digest
    assert figures.preview_object(first.digest) == f"{figures.PREVIEW_PREFIX}/{first.digest}.jpg"


def test_a_figure_keeps_clear_of_its_scenes_subtitles_only():
    from app.services.video_studio.compositor_page import caption_extent

    def band(narration: str | None = None, **captions):
        scene = {"id": "fig", "type": "figure", "block_spec": FLOW, **({"narration": narration} if narration else {})}
        spec = parse_video_spec(validate_video_spec({"title": "x", "captions": captions, "scenes": [scene]}))
        return figures.caption_safe_area(spec, spec.scenes[0], 1080)

    assert band() == figures.NO_SAFE_AREA, "a scene that says nothing has no subtitles"
    assert band("We collect, we enrich, we act.", enabled=False) == figures.NO_SAFE_AREA
    below = band("We collect, we enrich, we act.")
    # The caption pill's top at the bottom, the safe margin at the top.
    assert below == {"top": 43.2, "bottom": caption_extent(1080, style="pill", position="bottom")} and below["bottom"] > 150
    above = band("We collect, we enrich, we act.", position="top", style="bar")
    assert above["bottom"] == 43.2 and above["top"] > below["bottom"], "a bar is taller than a pill"


def test_the_subtitles_band_is_measured_on_the_pages_own_subtitles():
    """The reserve a figure keeps reads the numbers the compositor page draws its subtitles with."""
    from app.services.video_studio import compositor_page as page

    css, js = page.PAGE_CSS, page.PAGE_JS
    assert f"font: 600 {page.CAPTION_FONT_PX}px/{page.CAPTION_LINE_HEIGHT}" in css
    assert "#caption.cap-pill span { padding: 6px 18px;" in css and page.CAPTION_PADDING_PX["pill"] == 12
    assert "#caption.cap-bar { left: 0; right: 0; transform: none; max-width: none; padding: 18px 9%;" in css
    assert page.CAPTION_PADDING_PX["bar"] == 36
    edges = page.CAPTION_EDGE_OFFSET
    assert f"S.capOffset = S.H * (S.capEdge === 'top' ? {edges['top']} : {edges['bottom']});" in js
    assert f"room[S.capEdge] = S.capOffset + h + S.H * {page.CAPTION_GAP};" in js
    assert f"while (lines() > {page.CAPTION_MAX_LINES} && size > 26)" in js


def test_a_still_is_the_figure_built_in_whole_with_the_camera_on_all_of_it(monkeypatch):
    from app.services import presentation_export

    evaluated: list = []
    opened: dict = {}

    class _Page:
        async def evaluate(self, script, arg=None):
            evaluated.append((script, arg))
            return {"steps": [{"labels": ["Collect"]}, {"labels": ["Enrich"]}]} if "describe" in script else None

        async def screenshot(self, **kw):
            opened["shot"] = kw
            return b"JPEG"

    @asynccontextmanager
    async def open_page(url, **kw):
        opened.update(kw, url=url)
        yield _Page()

    monkeypatch.setattr(presentation_export, "open_render_page", open_page)
    still = _still()
    band = {"top": 0.0, "bottom": 206.4}
    data = asyncio.run(figures.draw_still(still.source, still.document, user=READER, viewport=(1920, 1080), safe=band))
    assert data == b"JPEG" and opened["url"].endswith(figures.FIGURE_PAGE_PATH) and opened["width"] == 1920
    planned = next(arg for script, arg in evaluated if "plan" in script)
    assert planned["camera"] == "still" and planned["length"] == evaluated[-1][1]
    # The scene's last frame: the same hold and the same band as the render's, the figure at rest.
    assert planned["hold"] == figures.WHOLE_HOLD and planned["safe"] == band and planned["motion"] == "none"
    assert max(planned["times"]) < planned["length"] and opened["shot"]["type"] == "jpeg"


DESCRIBED = {"steps": [{"labels": ["Collect"], "settle": 0.6}, {"labels": ["Enrich"], "settle": 0.6}, {"labels": ["Act"], "settle": 0.6}]}


def _queue(monkeypatch) -> SimpleNamespace:
    """The worker's queue in memory: the claims, the executions created and the wakes."""
    from app.core import database
    from app.services import orchestration_engine

    state = SimpleNamespace(claims=set(), executions=[], wakes=0)

    async def claim(digest):
        if digest in state.claims:
            return False
        state.claims.add(digest)
        return True

    async def release(digest):
        state.claims.discard(digest)

    async def being_drawn(digest):
        return digest in state.claims

    class _Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return False

        async def commit(self):
            return None

    async def create_execution(_db, *, adapter, payload, **_kwargs):
        # As the execution row keeps it: JSON.
        state.executions.append((adapter, json.loads(json.dumps(payload))))
        return SimpleNamespace(id=uuid.uuid4())

    async def wake():
        state.wakes += 1

    monkeypatch.setattr(figures, "claim_drawing", claim)
    monkeypatch.setattr(figures, "release_drawing", release)
    monkeypatch.setattr(figures, "being_drawn", being_drawn)
    monkeypatch.setattr(database, "async_session", _Session)
    monkeypatch.setattr(orchestration_engine, "create_execution", create_execution)
    monkeypatch.setattr(orchestration_engine, "notify_orchestration_wake", wake)
    return state


def test_a_save_hands_its_stills_to_the_worker_without_drawing_one(monkeypatch):
    """The save answers at once - nothing is drawn in the request - and two scenes of one figure are one still."""
    queue = _queue(monkeypatch)
    one, other = _still(), _still({"primary": "#ff8800"})
    twin = dataclasses.replace(one, scene_id="twin")

    async def has_preview(_digest):
        return False

    async def draw_still(*_args, **_kwargs):
        raise AssertionError("a still drawn in the request")

    monkeypatch.setattr(figures, "has_preview", has_preview)
    monkeypatch.setattr(figures, "_draw_still", draw_still)
    assert asyncio.run(figure_stills.enqueue([one, twin, other], READER)) == {one.digest, other.digest}
    [(adapter, payload)] = queue.executions
    assert adapter == figure_stills.ADAPTER and queue.wakes == 1
    assert [still["digest"] for still in payload["stills"]] == [one.digest, other.digest]
    assert (payload["user_id"], payload["role"]) == (str(READER.id), READER.role)
    # What the worker reads back is the still asked for (its scene stays with the save): a draft's included.
    assert figure_stills._preview(payload["stills"][0]) == dataclasses.replace(one, scene=None)
    # Asked again before it is drawn (the next autosave, a preview): queued once, still said to be drawing.
    assert asyncio.run(figure_stills.enqueue([twin], READER)) == {one.digest} and len(queue.executions) == 1


def test_the_worker_draws_only_the_stills_not_drawn_yet_and_never_fails_for_one(monkeypatch):
    from app.services import file_storage as storage_module

    kept, drawn = _still(), _still({"primary": "#ff8800"})
    broken = _still(viewport=(1080, 1920))
    uploaded: dict = {}
    readers: list = []

    async def has_preview(digest):
        return digest == kept.digest

    async def draw_still(_source, document, *, user, viewport, safe=None):
        readers.append((user.id, user.role))
        if viewport == (1080, 1920):
            raise figures.FigureError("no Chromium")
        return b"JPEG", DESCRIBED

    async def upload_to(data, object_name, content_type=None):
        uploaded[object_name] = (data, content_type)

    monkeypatch.setattr(figures, "has_preview", has_preview)
    monkeypatch.setattr(figures, "_draw_still", draw_still)
    monkeypatch.setattr(storage_module.file_storage, "upload_to", upload_to)
    stills = [figure_stills._carried(p) for p in (kept, drawn, broken)]
    payload = json.loads(json.dumps({"user_id": str(READER.id), "role": READER.role, "stills": stills}))
    progress: list = []
    assert asyncio.run(figure_stills.draw(payload, on_drawn=progress.append)) == 1
    # The still, and beside it the steps the page read of the figure.
    assert list(uploaded) == [figures.steps_object(drawn.digest), figures.preview_object(drawn.digest)]
    assert uploaded[figures.preview_object(drawn.digest)] == (b"JPEG", "image/jpeg")
    assert json.loads(uploaded[figures.steps_object(drawn.digest)][0]) == DESCRIBED
    # Every still reported, the one that failed included; the pictures read as who asked.
    assert progress == [1, 2, 3] and readers == [(READER.id, READER.role)] * 2


def test_the_worker_runs_the_stills_isolated_and_frees_them_however_it_ends(monkeypatch):
    from app.services.orchestration_adapters.video_studio import VideoFigureStillsAdapter

    released: list = []
    ended: list = [SimpleNamespace(kind="done"), SimpleNamespace(kind="memory", peak_memory=5 << 30)]

    async def release(digest):
        released.append(digest)

    async def run_isolated(_execution_id, *, cancel_event=None):
        return ended.pop(0)

    async def commit():
        return None

    monkeypatch.setattr(figures, "release_drawing", release)
    monkeypatch.setattr(figure_stills, "run_isolated", run_isolated)
    adapter = VideoFigureStillsAdapter()
    ctx = SimpleNamespace(db=SimpleNamespace(commit=commit))
    execution = SimpleNamespace(id=uuid.uuid4(), payload={"user_id": str(READER.id), "stills": [{"digest": "d1"}, {"digest": "d2"}]})
    assert adapter.concurrency_limit == 1
    assert asyncio.run(adapter.execute(ctx, execution)).kind == "completed" and released == ["d1", "d2"]
    # Past the render's memory ceiling: failed, never retried - the next preview asks again - and freed all the same.
    failed = asyncio.run(adapter.execute(ctx, execution))
    assert (failed.kind, failed.retryable) == ("failed", False) and "memory ceiling" in failed.error
    assert released == ["d1", "d2", "d1", "d2"]


def test_a_still_shown_is_kept_with_its_steps_and_one_nobody_opened_is_swept(monkeypatch):
    """The stills had no retention; the preview, their one reader, keeps those it shows."""
    from app.core.config import settings
    from app.services.file_storage import file_storage

    now = datetime(2026, 10, 20, tzinfo=UTC)
    ages = {"shown": 20, "idle": 31, "fresh": 2}
    store = {}
    for digest, days in ages.items():
        store[figures.preview_object(digest)] = now - timedelta(days=days)
        store[figures.steps_object(digest)] = now - timedelta(days=days)

    async def read_range(name, offset, length):
        assert name in store and (offset, length) == (0, 4)
        return b"JPEG"

    async def read_steps(digest):
        return DESCRIBED if figures.steps_object(digest) in store else None

    async def upload_to(data, name, content_type=None):
        assert data == (b"JPEG" if content_type == "image/jpeg" else json.dumps(DESCRIBED).encode("utf-8"))
        store[name] = now

    async def list_objects_modified(prefix):
        return [(name, moment) for name, moment in store.items() if name.startswith(prefix)]

    async def delete(name):
        store.pop(name)

    for name, fake in (("read_range", read_range), ("upload_to", upload_to), ("delete", delete)):
        monkeypatch.setattr(file_storage, name, fake)
    monkeypatch.setattr(file_storage, "list_objects_modified", list_objects_modified)
    monkeypatch.setattr(figures, "read_steps", read_steps)
    monkeypatch.setattr(settings, "video_studio_figure_still_retention_days", 30)

    def stat(days: int) -> dict:
        return {"size": 4, "last_modified": (now - timedelta(days=days)).isoformat()}

    # Shown 20 days after it was drawn: past half the retention, written again with its steps; one 2 days old is left.
    assert asyncio.run(figure_stills.keep("shown", stat(20), now=now)) is True
    assert store[figures.preview_object("shown")] == store[figures.steps_object("shown")] == now
    assert asyncio.run(figure_stills.keep("fresh", stat(2), now=now)) is False
    assert asyncio.run(figure_stills.sweep(now=now)) == 2
    assert {name.rsplit("/", 1)[1].split(".")[0] for name in store} == {"shown", "fresh"}
    # 0 keeps every still, and nothing is written again.
    monkeypatch.setattr(settings, "video_studio_figure_still_retention_days", 0)
    assert asyncio.run(figure_stills.sweep(now=now + timedelta(days=400))) == 0
    assert asyncio.run(figure_stills.keep("fresh", stat(200), now=now)) is False


def test_a_save_says_when_each_step_of_its_figures_comes_in(monkeypatch):
    still = _still(reveal="narration", narration="We collect the signal, then we act on it.")
    timing = figures.FigureTiming(length=12.0, said_from=0.6, said_seconds=4.0)

    async def read_steps(digest):
        return DESCRIBED if digest == still.digest else None

    monkeypatch.setattr(figures, "read_steps", read_steps)
    lines = asyncio.run(figures.reveal_lines([still, _still({"primary": "#ff8800"})], {"fig": timing}))
    # One line per figure whose steps are kept: each step's time, and what the narration says there.
    assert len(lines) == 1
    line = lines[0]
    words = figures.paced_words(still.scene, timing)
    times = figures.reveal_times([["Collect"], ["Enrich"], ["Act"]], length=12.0, mode="narration", words=words, settle=[0.6] * 3)
    assert line.startswith("Scene fig: its figure's 3 steps come in on its narration over 12.0 s")
    assert f'"Collect" {times[0]:.2f} s (said "collect")' in line
    # "Enrich" is never said, "Act" too short to be looked for alone: both spread after the step it names.
    assert f'"Enrich" {times[1]:.2f} s (spread)' in line and f'"Act" {times[2]:.2f} s (spread)' in line
    assert "the render times it on the spoken voice" in line
    # Spread evenly, a figure says so without a narration's words.
    even = _still()
    monkeypatch.setattr(figures, "read_steps", lambda _d: asyncio.sleep(0, result=DESCRIBED))
    assert asyncio.run(figures.reveal_lines([even], {"fig": figures.FigureTiming(9.0)}))[0].startswith(
        "Scene fig: its figure's 3 steps come in evenly over 9.0 s: "
    )


def test_a_save_plans_its_figures_stills_and_queues_them_once_committed(monkeypatch):
    from app.services.video_studio import projects

    still = _still()
    queued: list = []

    async def figure_previews(_db, user, spec, palette):
        assert user is READER and palette == {} and spec.scenes[0].type == "figure"
        return {"fig": still}

    async def enqueue(previews, user):
        queued.append((previews, user))
        return {p.digest for p in previews}

    async def reveal_lines(previews, timings):
        return [f"{len(previews)} figure(s), timed {sorted(timings)}"]

    monkeypatch.setattr(figures, "figure_previews", figure_previews)
    monkeypatch.setattr(figure_stills, "enqueue", enqueue)
    monkeypatch.setattr(figures, "reveal_lines", reveal_lines)
    effective = validate_video_spec({"title": "Explainer", "freestyle": True, "scenes": [{"id": "fig", "type": "figure", "block_spec": FLOW}]})
    planned = asyncio.run(projects.figure_stills_of(None, READER, effective, None))
    assert planned == [still]
    # The save's plan times each figure scene: its length, its narration's span.
    plan = timeline.estimate_plan(parse_video_spec(effective), {}, {})
    timings = projects.figure_timings_of(plan, effective)
    assert timings == {"fig": figures.figure_timing(plan, "fig")} and timings["fig"].length == pytest.approx(6.0)
    saved = SimpleNamespace(figure_stills=planned, figure_timings=timings)
    # Handed to the worker once committed, said so, then the steps' times said.
    queued_note, timed = asyncio.run(projects.queue_figure_stills(saved, READER))
    assert "still of 1 figure is being drawn in the background" in queued_note and timed == "1 figure(s), timed ['fig']"
    assert queued == [([still], READER)]
    # A video without a figure plans nothing and queues nothing.
    plain = validate_video_spec({"title": "Plain", "freestyle": True, "scenes": [{"type": "title", "title": "Hello"}]})
    assert asyncio.run(projects.figure_stills_of(None, READER, plain, None)) == []
    assert asyncio.run(projects.queue_figure_stills(SimpleNamespace(figure_stills=[], figure_timings={}), READER)) == []
    assert len(queued) == 1


def _states() -> tuple[preview._PreviewMedia, dict, set]:
    return preview._PreviewMedia(READER), {"fig": {"state": "figure", "block": "process_flow"}}, {"fig"}


def _stills(monkeypatch, *, drawn: bool, readable: bool) -> figures.FigurePreview:
    still = _still()

    async def figure_previews(_db, _user, _spec, _palette):
        return {"fig": still}

    async def has_preview(_digest):
        return drawn

    async def preview_stat(_digest):
        return {"size": 4, "last_modified": datetime.now(UTC).isoformat()} if drawn else None

    async def not_queued(_digest):
        return False

    async def pictures_readable(_db, _source, _user):
        return readable

    monkeypatch.setattr(figures, "figure_previews", figure_previews)
    monkeypatch.setattr(figures, "has_preview", has_preview)
    monkeypatch.setattr(figures, "preview_stat", preview_stat)
    monkeypatch.setattr(figures, "being_drawn", not_queued)
    monkeypatch.setattr(figures, "pictures_readable", pictures_readable)
    return still


def test_the_preview_plays_a_drawn_still_where_the_figure_plays(monkeypatch):
    still = _stills(monkeypatch, drawn=True, readable=True)
    played, states, unsized = _states()
    asyncio.run(preview._figure_stills(None, _scene(), {}, READER, played=played, states=states, unsized=unsized, draw=True))
    assert played.urls["image/fig"].startswith("/api/v1/video-studio/figure-preview/") and states == {} and unsized == set()
    token = played.urls["image/fig"].rsplit("/", 1)[1]
    assert media.decode_figure_token(token) == (still.digest, READER.id)
    # What it plays whatever its token: a later preview's newer URL of the same still loads nothing again.
    assert played.ids["image/fig"] == still.digest


@pytest.mark.parametrize(("drawn", "readable"), [(False, True), (True, False)], ids=["not-drawn", "picture-unreadable"])
def test_the_preview_names_a_figure_whose_still_it_may_not_show(monkeypatch, drawn, readable):
    _stills(monkeypatch, drawn=drawn, readable=readable)
    played, states, unsized = _states()
    asyncio.run(preview._figure_stills(None, _scene(), {}, READER, played=played, states=states, unsized=unsized, draw=False))
    assert played.urls == {} and states["fig"] == {"state": "figure", "block": "process_flow"} and unsized == {"fig"}


def test_a_managers_preview_hands_a_missing_still_to_the_worker_once(monkeypatch):
    still = _stills(monkeypatch, drawn=False, readable=True)
    queue = _queue(monkeypatch)
    first, second = _states(), _states()
    for played, states, unsized in (first, second):
        asyncio.run(preview._figure_stills(None, _scene(), {}, READER, played=played, states=states, unsized=unsized, draw=True))
    # Both previews say it is being drawn - the editor polls - and the worker is asked once, for who reads it.
    assert first[1]["fig"] == second[1]["fig"] == {"state": "figure", "block": "process_flow", "drawing": True}
    [(_adapter, payload)] = queue.executions
    assert [s["digest"] for s in payload["stills"]] == [still.digest] and payload["user_id"] == str(READER.id)
    # A reader's preview, or the saved video's, never asks - but polls a still already queued, by a save say.
    played, states, unsized = _states()
    asyncio.run(preview._figure_stills(None, _scene(), {}, READER, played=played, states=states, unsized=unsized, draw=False))
    assert states["fig"].get("drawing") is True and len(queue.executions) == 1
    queue.claims.clear()
    played, states, unsized = _states()
    asyncio.run(preview._figure_stills(None, _scene(), {}, READER, played=played, states=states, unsized=unsized, draw=False))
    assert "drawing" not in states["fig"] and len(queue.executions) == 1


def test_a_figure_token_is_neither_a_voice_nor_a_media_token():
    digest = "a" * 64
    token, _expires = media.mint_figure_token(digest, READER.id)
    assert media.decode_figure_token(token) == (digest, READER.id)
    with pytest.raises(media.MediaTokenError):
        media.decode_voice_token(token)
    voice, _expires = media.mint_voice_token(digest, READER.id)
    with pytest.raises(media.MediaTokenError):
        media.decode_figure_token(voice)


def test_the_still_is_served_by_its_token(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.api.v1 import video_studio as api
    from app.services import file_storage as storage_module

    digest = "b" * 64

    class _Db:
        async def get(self, _model, _key):
            return READER

    @asynccontextmanager
    async def session():
        yield _Db()

    async def download(object_name):
        if object_name == figures.preview_object(digest):
            return b"JPEG"
        raise storage_module.FileNotFoundInStorageError(object_name)

    monkeypatch.setattr(api, "async_session", session)
    monkeypatch.setattr(storage_module.file_storage, "download", download)
    app = FastAPI()
    app.include_router(api.router, prefix="/api/v1/video-studio")
    client = TestClient(app)
    token, _expires = media.mint_figure_token(digest, READER.id)
    shown = client.get(f"/api/v1/video-studio/figure-preview/{token}")
    assert shown.status_code == 200 and shown.content == b"JPEG" and shown.headers["content-type"] == "image/jpeg"
    gone, _expires = media.mint_figure_token("c" * 64, READER.id)
    assert client.get(f"/api/v1/video-studio/figure-preview/{gone}").status_code == 404
    assert client.get("/api/v1/video-studio/figure-preview/not-a-token").status_code == 404


# ── The editor's build ────────────────────────────────────────────


def test_a_build_is_kept_under_its_still_and_how_the_scene_times_it():
    still = _still()
    timing = figures.FigureTiming(length=12.0)
    digest = figures.build_digest(still, timing)
    assert digest == figures.build_digest(_still(), figures.FigureTiming(length=12.0)) and digest != still.digest
    # Another length, motion or camera is another build; another still too.
    assert figures.build_digest(still, figures.FigureTiming(length=14.0)) != digest
    assert figures.build_digest(_still(motion="none"), timing) != digest
    assert figures.build_digest(_still(camera="still"), timing) != digest
    assert figures.build_digest(_still({"primary": "#ff8800"}), timing) != digest
    # A narration times the build only when the steps follow it.
    said = figures.FigureTiming(length=12.0, said_from=0.6, said_seconds=5.0)
    assert figures.build_digest(_still(narration="We collect."), said) == figures.build_digest(_still(narration="We act."), said)
    narrated = _still(reveal="narration", narration="We collect.")
    assert figures.build_digest(narrated, said) != figures.build_digest(_still(reveal="narration", narration="We act."), said)
    assert figures.build_object(digest) == f"{figures.BUILD_PREFIX}/{digest}.mp4"


BUILD = {"duration": 6.0, "width": 960, "height": 540, "fps": 15, "frames": 90, "whole": 4.2}


def _shown(monkeypatch, *, built: bool) -> tuple[figures.FigurePreview, preview._PreviewMedia, timeline.PlanMedia, timeline.TimelinePlan]:
    still = _still()
    spec = _scene()
    played = preview._PreviewMedia(READER)
    played.put("image/fig", preview.figure_still_url(still.digest, READER), still.digest)
    media_ = timeline.PlanMedia()
    plan = timeline.plan(spec, media_, fps=30, theme={})

    async def read_build(digest):
        return dict(BUILD) if built and digest == figures.build_digest(still, figures.figure_timing(plan, "fig")) else None

    monkeypatch.setattr(figures, "read_build", read_build)
    return still, played, media_, plan


def test_the_preview_plays_a_drawn_build_where_the_figure_plays_its_still_the_poster(monkeypatch):
    still, played, media_, plan = _shown(monkeypatch, built=True)
    whole, building = asyncio.run(preview._figure_builds(plan, {"fig": still}, READER, plan_media=media_, played=played, draw=True))
    assert whole == {"fig": 4.2} and building == set()
    assert played.urls["clip/fig"].startswith("/api/v1/video-studio/figure-build/")
    assert played.urls["poster/fig"] == played.urls["image/fig"] and played.ids["poster/fig"] == still.digest
    digest, reader = media.decode_figure_token(played.urls["clip/fig"].rsplit("/", 1)[1])
    assert reader == READER.id and played.ids["clip/fig"] == digest != still.digest
    # Planned again with it, the figure plays its build as the render plays a drawn figure: the same scene.
    again = timeline.plan(_scene(), media_, fps=30, theme={})
    scene = again.document["scenes"][0]
    assert (scene["type"], scene["src"], scene["src_frames"]) == ("clip", "clip/fig", 90) and again.duration == plan.duration


def test_a_managers_preview_draws_a_missing_build_in_the_background_and_a_readers_never(monkeypatch):
    still, played, media_, plan = _shown(monkeypatch, built=False)
    asked: list = []

    async def prepare_builds(builds, user):
        asked.append(([(b[0].scene_id, b[2]) for b in builds], user))

    monkeypatch.setattr(figures, "prepare_builds", prepare_builds)
    whole, building = asyncio.run(preview._figure_builds(plan, {"fig": still}, READER, plan_media=media_, played=played, draw=True))
    digest = figures.build_digest(still, figures.figure_timing(plan, "fig"))
    assert whole == {} and building == {"fig"} and asked == [([("fig", digest)], READER)]
    # The stage keeps the still meanwhile: no clip, no poster.
    assert "clip/fig" not in played.urls and media_.clips == {}
    whole, building = asyncio.run(preview._figure_builds(plan, {"fig": still}, READER, plan_media=media_, played=played, draw=False))
    assert (whole, building) == ({}, set()) and len(asked) == 1


def test_a_build_is_drawn_as_the_render_draws_its_figure_lighter_and_kept_with_its_meta(monkeypatch, tmp_path):
    import json

    from app.services import file_storage as storage_module
    from app.services import presentation_export
    from app.services.video_studio import encoder

    still = _still(reveal="narration", narration="We collect, we enrich, we act.")
    timing = figures.FigureTiming(length=6.0, said_from=0.6, said_seconds=3.0)
    opened: dict = {}
    evaluated: list = []

    class _Page:
        async def evaluate(self, script, arg=None):
            evaluated.append((script, arg))
            if "describe" in script:
                return DESCRIBED
            if "plan" in script:
                return {"active": [[0.0, 6.0]], "whole": 4.2}
            return None

        async def screenshot(self, *, path, **_kw):
            Path(path).write_bytes(b"jpeg")

    @asynccontextmanager
    async def open_page(url, **kw):
        opened.update(kw, url=url)
        yield _Page()

    encoded: dict = {}
    uploads: list = []

    async def frames_to_mezzanine(frames, **kw):
        encoded.update(kw, frames=frames)
        Path(kw["out_path"]).write_bytes(b"mp4")

    async def upload_path(path, object_name, content_type=None):
        uploads.append((object_name, Path(path).read_bytes(), content_type))

    async def upload_to(data, object_name, content_type=None):
        uploads.append((object_name, data, content_type))

    monkeypatch.setattr(presentation_export, "open_render_page", open_page)
    monkeypatch.setattr(encoder, "frames_to_mezzanine", frames_to_mezzanine)
    monkeypatch.setattr(encoder, "probe", lambda _p: asyncio.sleep(0, result=SimpleNamespace(width=960, height=540)))
    monkeypatch.setattr(storage_module.file_storage, "upload_path", upload_path)
    monkeypatch.setattr(storage_module.file_storage, "upload_to", upload_to)
    digest = figures.build_digest(still, timing)
    asyncio.run(figures._store_build(still, timing, digest, READER))
    assert opened["device_scale_factor"] == figures.BUILD_SCALE and opened["width"] == 1920
    planned = next(arg for script, arg in evaluated if "plan" in script)
    words = figures.paced_words(still.scene, timing)
    assert planned == figures._plan_of(DESCRIBED, scene=still.scene, length=6.0, words=words, safe=still.safe)
    assert planned["motion"] == "drift" and encoded["fps"] == figures.BUILD_FPS
    assert [t for t, _ in encoded["frames"]] == figures.frame_times([[0.0, 6.0]], 6.0, figures.BUILD_FPS)
    # The video first, then its meta: a build that says it is drawn has its video.
    assert [u[0] for u in uploads] == [figures.build_object(digest), figures.build_meta_object(digest)]
    assert json.loads(uploads[1][1]) == {"duration": 6.0, "width": 960, "height": 540, "fps": 15, "frames": 90, "whole": 4.2}


def test_a_build_is_served_by_its_token_by_ranges(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.api.v1 import video_studio as api
    from app.services import file_storage as storage_module

    digest = "d" * 64
    video = b"0123456789" * 10

    class _Db:
        async def get(self, _model, _key):
            return READER

    @asynccontextmanager
    async def session():
        yield _Db()

    async def stat(object_name):
        return {"size": len(video), "content_type": "video/mp4"} if object_name == figures.build_object(digest) else None

    async def read_range(object_name, offset, length):
        assert object_name == figures.build_object(digest)
        return video[offset : offset + length]

    monkeypatch.setattr(api, "async_session", session)
    monkeypatch.setattr(storage_module.file_storage, "stat", stat)
    monkeypatch.setattr(storage_module.file_storage, "read_range", read_range)
    app = FastAPI()
    app.include_router(api.router, prefix="/api/v1/video-studio")
    client = TestClient(app)
    token, _expires = media.mint_figure_token(digest, READER.id)
    part = client.get(f"/api/v1/video-studio/figure-build/{token}", headers={"Range": "bytes=10-19"})
    assert part.status_code == 206 and part.content == video[10:20] and part.headers["content-type"] == "video/mp4"
    assert part.headers["content-range"] == f"bytes 10-19/{len(video)}"
    whole = client.get(f"/api/v1/video-studio/figure-build/{token}")
    assert whole.status_code == 200 and whole.content == video
    # A still's token names no build; a build not drawn is not found.
    gone, _expires = media.mint_figure_token("e" * 64, READER.id)
    assert client.get(f"/api/v1/video-studio/figure-build/{gone}").status_code == 404
    assert client.get("/api/v1/video-studio/figure-build/not-a-token").status_code == 404
