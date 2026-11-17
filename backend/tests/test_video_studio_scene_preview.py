"""A draft preview of one scene. A two-scene synthetic video, synthetic ids and media.

* the scene is named by its index in the spec as written or by its id (a
  card the template adds included);
* the window is the scene with its neighbours, planned as in the whole video:
  it is drawn from the start of its transition in to the end of its
  transition out, its sound cut to the same span in the master's one encode
  and checked by the render's own checks, the video's fades kept only
  where the window reaches them (its settled frames out of them), a
  music file started where it plays;
* a preview waits in a lane of its own as long as a render;
* ``run_render`` draws only the window at draft quality, keeps three frames
  of the scene and never makes the preview what the video plays;
* previews are kept apart from the whole renders by the retention;
* in Chromium, the compositor draws exactly the window's frames.
"""

from __future__ import annotations

import asyncio
import math
import os
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.models.user import User
from app.models.video_studio import JOB_PENDING, JOB_READY, VideoProject, VideoRender
from app.services.video_studio import (
    assets,
    compositor,
    encoder,
    jobs,
    projects,
    render,
    render_checks,
    render_retention,
    timeline,
    voice,
)
from app.services.video_studio.spec import VideoSpecError, parse_video_spec, validate_video_spec

TRACK = "00000000-0000-0000-0000-00000000a0a1"


def _two_scenes(**top) -> dict:
    """A two-scene synthetic video: an opening card, then a statement crossfading in."""
    return validate_video_spec(
        {
            "title": "Synthetic preview",
            "freestyle": True,
            "scenes": [
                {"id": "open", "type": "title", "title": "Hello", "duration": 3},
                {
                    "id": "claim",
                    "type": "statement",
                    "lines": ["One platform"],
                    "duration": 4,
                    "transition": {"type": "crossfade", "duration": 0.8},
                },
            ],
            **top,
        }
    )


def _plan(effective: dict) -> timeline.TimelinePlan:
    return timeline.estimate_plan(parse_video_spec(effective), {})


# ── Which scene ───────────────────────────────────────────────────────────


def test_a_scene_is_named_by_its_index_as_written_or_by_its_id():
    authored = {"scenes": [{"id": "claim", "type": "statement"}]}
    effective = {"scenes": [{"id": "intro-card"}, {"id": "claim"}, {"id": "outro-card"}]}
    assert projects.resolve_scene(authored, effective, 0) == "claim"
    assert projects.resolve_scene(authored, effective, "0") == "claim"
    # A card the template adds is previewed by its id.
    assert projects.resolve_scene(authored, effective, "outro-card") == "outro-card"
    with pytest.raises(VideoSpecError, match="no scene 3: the video's scenes are 0 to 0"):
        projects.resolve_scene(authored, effective, 3)
    with pytest.raises(VideoSpecError, match="give its id or its 0-based index"):
        projects.resolve_scene(authored, effective, "nowhere")


# ── The window ────────────────────────────────────────────────────────────


def test_the_window_of_a_two_scene_video_is_the_whole_video_and_keeps_its_fades():
    effective = _two_scenes()
    full = _plan(effective).document
    for scene_id, target in (("open", 0), ("claim", 1)):
        window, index = render.scene_window(effective, scene_id, full)
        assert index == target and [s["id"] for s in window["scenes"]] == ["open", "claim"]
        assert window.get("fade_in", True) and window.get("fade_out", True)
    with pytest.raises(LookupError):
        render.scene_window(effective, "gone", full)


def test_the_window_spans_the_scenes_transition_in_and_out():
    effective = _two_scenes(fade_in=False, fade_out=False)
    document = _plan(effective).document
    claim = document["scenes"][1]
    # The opening card: from 0 to the end of the crossfade into the claim.
    start, end, settled_from, settled_to = render.window_times(document, 0)
    assert (start, settled_from) == (0.0, 0.0)
    assert settled_to == claim["start"] and end == pytest.approx(claim["start"] + claim["transition_in"]["duration"])
    # The claim: from the start of its crossfade in to the end of the video.
    start, end, settled_from, settled_to = render.window_times(document, 1)
    assert start == claim["start"] and settled_from == pytest.approx(claim["start"] + 0.8)
    assert end == settled_to == document["duration"]


def test_a_scene_is_settled_out_of_the_videos_fades_from_and_to_black():
    """The first scene's frames come after the fade in, the last scene's before the fade out."""
    document = _plan(_two_scenes()).document
    assert document["fade_in"] > 0 and document["fade_out"] > 0
    start, _end, settled_from, _to = render.window_times(document, 0)
    assert start == 0.0 and settled_from == document["fade_in"]
    _start, end, _from, settled_to = render.window_times(document, 1)
    assert end == document["duration"] and settled_to == pytest.approx(document["duration"] - document["fade_out"])


def test_a_longer_video_previews_a_scene_with_its_neighbours_and_its_music_in_place():
    effective = validate_video_spec(
        {
            "title": "Longer",
            "freestyle": True,
            "scenes": [
                {"id": f"s{i}", "type": "statement", "lines": [f"Line {i}"], "duration": 4} for i in range(5)
            ],
            "music": {"music_id": TRACK, "start": 2.0, "ending": "natural"},
        }
    )
    full = _plan(effective).document
    window, index = render.scene_window(effective, "s2", full)
    assert [s["id"] for s in window["scenes"]] == ["s1", "s2", "s3"] and index == 1
    assert window["fade_in"] is False and window["fade_out"] is False
    # The track starts where it plays when s1 starts in the whole video, and fades out at the window's end.
    s1_start = next(s for s in full["scenes"] if s["id"] == "s1")["start"]
    assert window["music"]["start"] == pytest.approx(2.0 + s1_start) and window["music"]["ending"] == "fade"
    # The last scene's window reaches the video's end: its fade out stays.
    last, _ = render.scene_window(effective, "s4", full)
    assert [s["id"] for s in last["scenes"]] == ["s3", "s4"] and last.get("fade_out", True) is True


# ── The queue ─────────────────────────────────────────────────────────────


def test_a_preview_has_a_lane_of_its_own_and_waits_like_a_render(monkeypatch):
    """The preview lane, and a render's deadline: previews queue up to the renders' limit."""
    created: list[dict] = []

    async def create_execution(_db, **kwargs):
        created.append(kwargs)
        return SimpleNamespace(id=uuid.uuid4())

    # Nothing is dealt when a preview is queued (its lane is dealt as its slots claim it).
    monkeypatch.setattr("app.services.orchestration_engine.create_execution", create_execution)
    before = datetime.now(UTC)
    whole = VideoRender(id=uuid.uuid4(), quality="high")
    preview = VideoRender(id=uuid.uuid4(), quality="draft", scene_id="claim")
    asyncio.run(jobs.enqueue(None, "render", whole))
    asyncio.run(jobs.enqueue(None, "render", preview))
    assert [c["adapter"] for c in created] == [jobs.RENDER_ADAPTER, jobs.PREVIEW_ADAPTER]
    assert created[1]["dedup_key"] == f"{jobs.PREVIEW_ADAPTER}:{preview.id}"
    # Due when asked, in its own lane: no head start over anyone's whole render.
    assert created[0]["run_at"] is None and created[1]["run_at"] is None
    # The tenth preview queued behind nine others is never dead-lettered while it waits.
    assert created[1]["deadline_at"] - before > timedelta(hours=3)
    assert created[0]["deadline_at"] - before > timedelta(hours=3)


# ── The render ────────────────────────────────────────────────────────────


@pytest.fixture
def preview(monkeypatch, tmp_path):
    """``run_render`` of a scene preview of the two-scene synthetic video, its heavy steps recorded."""
    effective = _two_scenes()
    maker = User(id=uuid.uuid4(), email="maker@example.com", role="user")
    current = uuid.uuid4()
    project = VideoProject(id=uuid.uuid4(), title="Synthetic preview", spec=effective, revision=3, latest_render_id=current)
    row = VideoRender(
        id=uuid.uuid4(), project_id=project.id, user_id=maker.id, status=JOB_PENDING, quality="draft", scene_id="claim"
    )
    rows = {VideoRender: row, VideoProject: project, User: maker}
    world = SimpleNamespace(row=row, project=project, current=current, drawn=None, cut=None, stored=[], marks=[])

    class _Db:
        async def get(self, model, _key, **_options):
            return rows.get(model)

        async def commit(self):
            return None

        async def flush(self):
            return None

        def expunge(self, _row):
            return None

    @asynccontextmanager
    async def session():
        yield _Db()

    async def effective_spec(_db, _project, _user):
        return SimpleNamespace(spec=effective, template=None, notes=[])

    async def estimate(_db, spec, **_kwargs):
        return _plan(spec)

    async def gather(spec, **_kwargs):
        return render._Media()

    async def render_frames(document, assets_, *, preset, output_size, workdir, frame_range=None, **_kwargs):
        world.drawn = {"document": document, "preset": preset, "size": output_size, "range": frame_range}
        out = Path(workdir) / "video.mp4"
        out.write_bytes(b"MP4")
        return str(out)

    async def mix_audio(*, out_path, duration, span=None, **_kwargs):
        world.mixed = duration
        world.span = span
        Path(out_path).write_bytes(b"AAC")

    async def check_render(**kwargs):
        world.checked = kwargs
        return render_checks.Checks(audio={"status": "passed"}, frames={"status": "passed"})

    async def mux(*, video_path, audio_path, out_path):
        world.muxed = audio_path
        Path(out_path).write_bytes(b"MP4")

    async def probe(_path):
        first, last = world.drawn["range"]
        return SimpleNamespace(duration=(last - first) / 30, width=1280, height=720, has_audio=True, frame_count=last - first)

    async def extract_still(_path, *, at, out_path, max_side, crop_to=None):
        world.stills = [*getattr(world, "stills", []), at]
        Path(out_path).write_bytes(b"JPG")

    async def store_path(path, **kwargs):
        stored = SimpleNamespace(id=uuid.uuid4(), size=10, path=path, **kwargs)
        world.stored.append(stored)
        return stored

    async def mark(_kind, _job_id, **changes):
        world.marks.append(changes)

    async def nothing(*_args, **_kwargs):
        return None

    async def none_replaced(*_args, **_kwargs):
        return render_retention.Deletion()

    monkeypatch.setattr(render, "async_session", session)
    monkeypatch.setattr(render, "bind_llm_caller", nothing)
    monkeypatch.setattr(projects, "effective_spec", effective_spec)
    monkeypatch.setattr(projects, "estimate", estimate)
    monkeypatch.setattr(render, "_gather_media", gather)
    monkeypatch.setattr(compositor, "render_frames", render_frames)
    monkeypatch.setattr(encoder, "mix_audio", mix_audio)
    monkeypatch.setattr(render, "check_render", check_render)
    monkeypatch.setattr(encoder, "mux", mux)
    monkeypatch.setattr(encoder, "probe", probe)
    monkeypatch.setattr(encoder, "extract_still", extract_still)
    monkeypatch.setattr(assets, "store_path", store_path)
    monkeypatch.setattr(jobs, "mark", mark)
    monkeypatch.setattr(render.retention, "touch", nothing)
    monkeypatch.setattr(render_retention, "replaced_previews", none_replaced)
    monkeypatch.setattr(render, "font_face_css", lambda: "", raising=False)
    world.workdir = tmp_path
    return world


def test_a_scene_preview_draws_only_its_window_at_draft_quality(preview):
    outcome = asyncio.run(render.run_render(str(preview.row.id), workdir=preview.workdir))
    assert outcome.kind == "done"
    drawn = preview.drawn
    assert drawn["preset"].name == "draft" and drawn["size"] == (1280, 720)
    document = drawn["document"]
    start, end, _from, _to = render.window_times(document, 1)
    fps = document["fps"]
    assert fps == 30 and drawn["range"] == (math.floor(start * fps), math.ceil(end * fps))
    # Its sound is mixed whole, then cut to the frames drawn in the master's one encode.
    assert preview.mixed == document["duration"]
    assert preview.span == pytest.approx((drawn["range"][0] / fps, (drawn["range"][1] - drawn["range"][0]) / fps))
    assert preview.muxed.endswith("audio.m4a")
    # The render's own checks run on it, for the scene, over the same span.
    assert preview.checked["scene_id"] == "claim" and preview.checked["span"] == preview.span
    assert preview.row.checks["status"] == "passed"
    # The scene it drew is recorded on the video, never on the preview the retention deletes.
    seen = preview.project.scene_previews
    assert set(seen) == {"claim"} and seen["claim"]["render_id"] == str(preview.row.id)
    assert len(seen["claim"]["digest"]) == 40


def test_a_scene_preview_keeps_three_frames_and_never_becomes_what_the_video_plays(preview):
    asyncio.run(render.run_render(str(preview.row.id), workdir=preview.workdir))
    row = preview.row
    assert row.status == JOB_READY and row.scene_id == "claim"
    frames = [s for s in preview.stored if s.filename.endswith(("-first.jpg", "-mid.jpg", "-last.jpg"))]
    assert row.frame_file_ids == [str(s.id) for s in frames] and len(frames) == 3
    assert row.poster_file_id is None and row.captions_file_id is None
    assert preview.project.latest_render_id == preview.current
    assert any("previewed at draft quality" in note for note in row.notes)
    # It records the music it played: this video plays none.
    assert row.music == {"kind": "none"}
    # Its frames are taken out of both transitions: after the one in, before the end.
    assert preview.stills[0] > 0.8 and preview.stills[0] < preview.stills[1] < preview.stills[2]


def test_a_scene_preview_speaks_its_narrations_with_their_neighbours_in_the_whole_video(preview, monkeypatch):
    """A window cut at scenes that say nothing gave the narration other neighbours, so a digest - a check -
    the whole render never read: it spoke the narration anew and handed it to another voice."""
    effective = validate_video_spec(
        {
            "title": "Synthetic preview",
            "freestyle": True,
            "scenes": [
                {"id": "s1", "type": "title", "title": "One", "duration": 3, "narration": "First."},
                {"id": "s2", "type": "title", "title": "Two", "duration": 3},
                {"id": "s3", "type": "title", "title": "Three", "duration": 3, "narration": "Third."},
                {"id": "s4", "type": "title", "title": "Four", "duration": 3},
                {"id": "s5", "type": "title", "title": "Five", "duration": 3, "narration": "Fifth."},
            ],
        }
    )
    asked: dict = {}

    async def effective_spec(_db, _project, _user):
        return SimpleNamespace(spec=effective, template=None, notes=[])

    async def gather(spec, **kwargs):
        asked.update(kwargs, spec=spec)
        return render._Media()

    monkeypatch.setattr(projects, "effective_spec", effective_spec)
    monkeypatch.setattr(render, "_gather_media", gather)
    preview.row.scene_id = "s3"
    asyncio.run(render.run_render(str(preview.row.id), workdir=preview.workdir))
    assert [scene.id for scene in asked["spec"].scenes] == ["s2", "s3", "s4"]
    # The window alone would give it none; it is spoken as the whole video speaks it.
    assert voice.lines_of(asked["spec"]) == [voice.Line("s3", "Third.", None, None)]
    assert asked["lines"] == [voice.Line("s3", "Third.", "First.", "Fifth.")]
    assert asked["lines"] == voice.lines_of(parse_video_spec(effective), within={"s2", "s3", "s4"})
    assert asked["origin"] == f"render {str(preview.row.id)[:8]} (a preview of scene s3)"


def test_a_preview_of_a_scene_removed_since_fails_and_says_so(preview):
    preview.row.scene_id = "gone"
    asyncio.run(render.run_render(str(preview.row.id), workdir=preview.workdir))
    assert preview.row.error == "Scene `gone` is no longer in the video: preview one it has." and preview.drawn is None


# ── The retention ─────────────────────────────────────────────────────────


def test_previews_have_their_own_days_and_are_never_the_newest_ready_render():
    now = datetime.now(UTC)
    project = VideoProject(id=uuid.uuid4(), latest_render_id=None)
    preview = VideoRender(
        id=uuid.uuid4(), status=JOB_READY, scene_id="s1", quality="standard", created_at=now - timedelta(days=2),
        finished_at=now - timedelta(days=2), keep=False, frame_file_ids=[str(uuid.uuid4())],
    )
    held = render_retention.standing_of(project, [preview], public_link=False, policy=render_retention.Policy(), now=now)
    # A video's only ready preview is no render of its quality to keep: its own rule decides, its frames go with it.
    assert held.latest_ready == {}
    assert render_retention.expires_at(preview, held) == preview.finished_at + timedelta(days=7)
    assert render_retention.kept_by(preview, held) == render_retention.KEPT_RETENTION
    assert uuid.UUID(preview.frame_file_ids[0]) in render_retention.render_files(preview)


def test_the_sweep_query_judges_previews_apart():
    from sqlalchemy.dialects import postgresql

    query = render_retention._due_videos(render_retention.Policy(), datetime.now(UTC))
    sql = str(query.compile(dialect=postgresql.dialect()))
    assert "video_renders.scene_id IS NOT NULL" in sql and "video_renders.scene_id IS NULL" in sql


# ── The tool and the route ────────────────────────────────────────────────


def test_the_tool_and_the_route_preview_a_scene():
    from app.api.v1 import video_studio as api
    from app.services.builtin_tools.video_studio import _definitions, build_video_studio_tools

    tool = next(d for d in _definitions(["high"]) if d.name == "render_video_scene")
    assert tool.input_schema["required"] == ["video_id", "scene"]
    assert "720p30" in tool.description and "transitions in and out" in tool.description
    assert "first, middle and last frames" in tool.description
    _defs, executor = build_video_studio_tools(user_id=str(uuid.uuid4()))
    assert "render_video_scene" in executor._handlers
    assert any(r.path == "/projects/{project_id}/scene-renders" and "POST" in r.methods for r in api.router.routes)


# ── In Chromium: only the window's frames are drawn ───────────────────────


@pytest.mark.chromium
async def test_the_compositor_draws_exactly_the_windows_frames(tmp_path):
    if not encoder.encoder_available():
        pytest.skip("ffmpeg is not installed")
    from app.services.builtin_tools.html_render import font_face_css
    from app.services.video_studio.spec import QUALITY_PRESETS
    from app.services.video_studio.themes import build_theme

    effective = _two_scenes()
    spec = parse_video_spec(effective)
    plan = timeline.plan(spec, timeline.PlanMedia(), fps=30, theme=build_theme(mode="dark", background_style="grid"))
    start, end, _from, _to = render.window_times(plan.document, 1)
    frame_range = (math.floor(start * 30), math.ceil(end * 30))
    try:
        video = await compositor.render_frames(
            plan.document,
            compositor.CompositorAssets(),
            preset=QUALITY_PRESETS["draft"],
            output_size=(640, 360),
            workdir=str(tmp_path),
            font_css=font_face_css(),
            pages=2,
            frame_range=frame_range,
        )
    except Exception as exc:
        text = str(exc)
        if "Executable doesn't exist" not in text and "playwright install" not in text.lower():
            raise
        if os.environ.get("REQUIRE_CHROMIUM") == "1":
            pytest.fail(f"Playwright's Chromium is required here and is not installed: {exc}")
        pytest.skip("Playwright's Chromium is not installed (python -m playwright install chromium)")
    info = await encoder.probe(video)
    assert info.frame_count == frame_range[1] - frame_range[0]
    assert info.frame_count < plan.document["frames"]
