"""The gaps found producing a brand film on 20 Oct 2026, the ones not drawn on frames.

Synthetic ids and rows; the frames-side gaps (stray screens, kept regions)
are in ``test_video_studio_shot_cleanup.py``, the combobox in
``test_video_studio_combobox_chromium.py``.

* 2: a render records the music it played, and a save, ``get_video`` and a
  refit say whether the current render was made with the music the video
  plays now - a refit keeps the track's id and the video's revision; one
  made before renders recorded it says so once, from the save that changes
  the music;
* 3: a refit composes at the planned length plus 8 s by default, never
  shorter, a ``duration_seconds`` the length of the video included;
* 4: scene previews queue instead of being refused past 3 - and
  past any number: the renders' own limit bounds how many of a video run;
* 6: ``animate.keep`` is checked, sent with the animation, and a changed list
  starts a new one; the shot options that keep regions and clean screens are
  refused when they cannot work.
"""

from __future__ import annotations

import asyncio
import uuid
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from app.models.video_studio import JOB_PENDING, JOB_READY, JOB_RUNNING, VideoClip, VideoMusic, VideoRender
from app.services.video_studio import clips, jobs, kept_regions, pictures, projects, stray_screens
from app.services.video_studio import music as music_tracks
from app.services.video_studio.spec import Music, VideoSpecError, validate_video_spec
from app.tools.base import ToolCall

USER_ID = uuid.UUID("00000000-0000-0000-0000-00000000f001")
TRACK_ID = "00000000-0000-0000-0000-00000000f002"
OLD_FILE = uuid.UUID("00000000-0000-0000-0000-00000000f003")
NEW_FILE = uuid.UUID("00000000-0000-0000-0000-00000000f004")
PICTURE_ID = "00000000-0000-0000-0000-00000000f005"
CLIP_ID = "00000000-0000-0000-0000-00000000f006"


def _user() -> SimpleNamespace:
    return SimpleNamespace(id=USER_ID, role="user", email="maker@example.com")


class _Db:
    def __init__(self, rows: dict | None = None) -> None:
        self.rows = rows or {}

    async def get(self, model, key, **_options):
        return self.rows.get((model, str(key)))


# ── 2: the render's music ─────────────────────────────────────────────────


def _render(music: dict | None) -> VideoRender:
    return VideoRender(id=uuid.uuid4(), project_id=uuid.uuid4(), status=JOB_READY, quality="high", revision=4, music=music)


def _track(status: str, file_id: uuid.UUID | None) -> VideoMusic:
    return VideoMusic(id=uuid.UUID(TRACK_ID), user_id=USER_ID, provider="elevenlabs", model="music_v1", prompt="calm", status=status, file_id=file_id)


def _note(render, music, rows=None, **options):
    return asyncio.run(music_tracks.render_music_note(_Db(rows), render, music, **options))


def test_a_render_records_the_source_it_played_and_an_ai_tracks_file():
    ai = music_tracks.Bed(kind="music", ref=TRACK_ID, duration=180.0)
    assert music_tracks.played_record(ai, OLD_FILE) == {"kind": "music", "ref": TRACK_ID, "file_id": str(OLD_FILE)}
    assert music_tracks.played_record(music_tracks.Bed(kind="track", ref="ascent", duration=60.0), None) == {
        "kind": "track",
        "ref": "ascent",
    }
    assert music_tracks.played_record(None, None) == {"kind": "none"}


def test_a_refit_tells_the_current_render_plays_the_earlier_take():
    played = _render({"kind": "music", "ref": TRACK_ID, "file_id": str(OLD_FILE)})
    music = Music(music_id=TRACK_ID)
    composing = _note(played, music, {(VideoMusic, TRACK_ID): _track(JOB_RUNNING, None)})
    assert "plays an earlier take of the AI track" in composing and "being composed again" in composing
    refitted = _note(played, music, {(VideoMusic, TRACK_ID): _track(JOB_READY, NEW_FILE)})
    assert "earlier take" in refitted and "composed again since" in refitted and "render again" in refitted
    same = _note(played, music, {(VideoMusic, TRACK_ID): _track(JOB_READY, OLD_FILE)})
    assert same == f"The current render `{played.id}` was made with the music the video plays now."


def test_another_source_no_music_and_an_older_render_are_each_said():
    music = Music(music_id=TRACK_ID)
    other = _note(_render({"kind": "track", "ref": "ascent"}), music)
    assert "was made with the built-in track `ascent`, not with the AI track" in other
    # A render made before the column says it cannot tell once, from the save that changes the music.
    assert "does not record the music it played" in _note(_render(None), music, music_changed=True)
    assert _note(_render(None), music) is None
    assert "which the video no longer plays" in _note(_render({"kind": "track", "ref": "ascent"}), Music(enabled=False))
    assert _note(_render({"kind": "none"}), None) is None and _note(_render(None), None) is None


def test_the_line_is_said_only_for_a_current_render_that_is_ready():
    spec = SimpleNamespace(music=Music(track="ascent"))
    render = _render({"kind": "track", "ref": "ascent"})
    project = SimpleNamespace(latest_render_id=render.id)
    line = asyncio.run(projects.render_music_line(_Db({(VideoRender, str(render.id)): render}), project, _user(), spec))
    assert line.endswith("was made with the music the video plays now.")
    render.status = JOB_RUNNING
    assert asyncio.run(projects.render_music_line(_Db({(VideoRender, str(render.id)): render}), project, _user(), spec)) is None
    assert asyncio.run(projects.render_music_line(_Db(), SimpleNamespace(latest_render_id=None), _user(), spec)) is None


def test_a_save_changes_the_music_only_when_what_plays_changes():
    """What tells a render made before the column to say it cannot tell, once."""
    ascent = {"music": {"track": "ascent", "volume": 0.4}}
    assert projects._music_source(ascent) == ("track", "ascent")
    assert projects._music_source({"music": {"track": "ascent", "volume": 0.9}}) == projects._music_source(ascent)
    assert projects._music_source({"music": {"music_id": TRACK_ID}}) == ("music", TRACK_ID)
    assert projects._music_source({"music": {"track": "ascent", "enabled": False}}) is None
    assert projects._music_source({"music": {"volume": "loud"}}) is None and projects._music_source({}) is None


# ── 3: the refit's margin ─────────────────────────────────────────────────


def test_a_refit_composes_past_the_video_by_eight_seconds_never_shorter():
    assert music_tracks.FIT_MARGIN_SECONDS == 8.0
    seconds, said = music_tracks.refit_seconds(167.0)
    assert seconds == 175 and "167 s planned now plus 8 s" in said
    # The video's own length, given as the length: raised, and said.
    seconds, said = music_tracks.refit_seconds(167.0, 167)
    assert seconds == 175 and "(duration_seconds 167 s was shorter)" in said
    assert music_tracks.refit_seconds(167.0, 200)[0] == 200
    assert music_tracks.refit_seconds(167.0, None, music_tracks.fit_margin(0))[0] == 167
    for wrong in (-1, 61, "soon"):
        with pytest.raises(music_tracks.MusicError, match="margin_seconds"):
            music_tracks.fit_margin(wrong)


def test_generate_music_refits_at_the_margin_and_says_what_the_current_render_plays(monkeypatch):
    from app.services.builtin_tools import video_studio as tool

    project = SimpleNamespace(id=uuid.uuid4(), title="Brand film", revision=7, spec={"music": {"music_id": TRACK_ID}})
    row = _track(JOB_READY, OLD_FILE)
    asked: dict = {}

    class _Session(_Db):
        async def commit(self):
            return None

    @asynccontextmanager
    async def session():
        yield _Session({(VideoMusic, TRACK_ID): row})

    async def user_of(_db, _user_id):
        return _user()

    async def visible(*_args, **_kwargs):
        return project

    async def manages(*_args, **_kwargs):
        return True

    async def planned(*_args, **_kwargs):
        return 167.0

    async def restart(_db, _user, _row, **kwargs):
        asked.update(kwargs)
        return "ElevenLabs"

    async def line(*_args, **_kwargs):
        return "The current render `r1` plays an earlier take of the AI track, being composed again now."

    async def nothing(*_args, **_kwargs):
        return None

    async def waited(*_args, **_kwargs):
        return [SimpleNamespace(id=row.id, status=JOB_PENDING, params={"duration_seconds": 175})]

    monkeypatch.setattr(tool, "async_session", session)
    monkeypatch.setattr(tool, "_user", user_of)
    monkeypatch.setattr(tool, "emit_tool_event", nothing)
    monkeypatch.setattr(projects, "load_visible_project", visible)
    monkeypatch.setattr(projects, "can_manage_project", manages)
    monkeypatch.setattr(projects, "planned_seconds", planned)
    monkeypatch.setattr(projects, "render_music_line", line)
    monkeypatch.setattr(music_tracks, "restart_music", restart)
    monkeypatch.setattr("app.services.audit.log_audit", nothing)
    monkeypatch.setattr(jobs, "wake", nothing)
    monkeypatch.setattr(jobs, "wait_for", waited)
    _definitions, executor = tool.build_video_studio_tools(None, str(USER_ID), None)
    arguments = {"video_id": str(project.id), "refit": True, "duration_seconds": 167}
    result = asyncio.run(executor.execute_tool(ToolCall(id="m1", name="generate_music", arguments=arguments)))
    assert not result.is_error and asked["duration_seconds"] == 175 and asked["refit"] is True
    assert "Composed at 175 s" in result.content and "plays an earlier take" in result.content
    # The current render of this revision came out longer than the plan: its length is the one covered.
    render = VideoRender(id=uuid.uuid4(), status=JOB_READY, revision=7, duration_ms=180_000)
    project.latest_render_id = render.id
    monkeypatch.setattr(
        tool, "async_session", lambda: _session_with({(VideoMusic, TRACK_ID): row, (VideoRender, str(render.id)): render})
    )
    result = asyncio.run(executor.execute_tool(ToolCall(id="m2", name="generate_music", arguments=arguments)))
    assert not result.is_error and asked["duration_seconds"] == 188
    assert "180 s as its current render lasts (planned at 167 s) plus 8 s" in result.content


@asynccontextmanager
async def _session_with(rows: dict):
    class _Session(_Db):
        async def commit(self):
            return None

    yield _Session(rows)


def test_a_refit_covers_the_current_render_when_it_outran_the_plan():
    """The plan estimates the voice, a render measures it - a track fitted to the plan alone ended first."""
    seconds, said = music_tracks.refit_seconds(150.0, rendered=167.0)
    assert seconds == 175 and "the video's 167 s as its current render lasts (planned at 150 s) plus 8 s" in said
    # A render shorter than the plan changes nothing; nor does none.
    assert music_tracks.refit_seconds(150.0, rendered=140.0) == music_tracks.refit_seconds(150.0)
    assert music_tracks.video_seconds(150.0, None) == 150.0 and music_tracks.video_seconds(150.0, 167.0) == 167.0


def test_only_a_ready_render_of_the_current_revision_measures_the_video():
    render = VideoRender(id=uuid.uuid4(), status=JOB_READY, revision=4, duration_ms=167_000)
    project = SimpleNamespace(latest_render_id=render.id, revision=4)
    db = _Db({(VideoRender, str(render.id)): render})
    assert asyncio.run(projects.rendered_seconds(db, project)) == 167.0
    # A render of an older revision says nothing of the video now.
    project.revision = 5
    assert asyncio.run(projects.rendered_seconds(db, project)) is None
    project.revision, render.status = 4, JOB_RUNNING
    assert asyncio.run(projects.rendered_seconds(db, project)) is None
    assert asyncio.run(projects.rendered_seconds(db, SimpleNamespace(latest_render_id=None, revision=4))) is None


# ── 4: scene previews queue ───────────────────────────────────────────────


class _PreviewDb:
    """The start's reads, told apart by what they ask: the video's previews in flight, the queue, nothing else."""

    def __init__(self, in_flight: list) -> None:
        self.in_flight = in_flight
        self.asked: list[str] = []
        self.added: list = []

    async def execute(self, statement, _params=None):
        sql = str(statement)
        self.asked.append(sql)
        if "count(" in sql:
            return SimpleNamespace(scalar_one=lambda: 0)
        if "JOIN video_renders" in sql:
            return SimpleNamespace(all=lambda: [])
        if "video_renders.scene_id IS NOT NULL" in sql:
            return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: self.in_flight))
        return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: []))

    def add(self, row):
        self.added.append(row)

    async def flush(self):
        return None


def _previews(count: int) -> list:
    return [VideoRender(id=uuid.uuid4(), status=JOB_PENDING, scene_id=f"s{i}", progress=0) for i in range(count)]


@pytest.fixture
def preview_start(monkeypatch):
    queued: list = []

    async def effective(*_args, **_kwargs):
        return SimpleNamespace(spec={"scenes": [{"id": "hook"}]})

    async def enqueue(_db, kind, row, **kwargs):
        queued.append((kind, row, kwargs))

    async def nothing(*_args, **_kwargs):
        return None

    monkeypatch.setattr(projects, "effective_spec", effective)
    monkeypatch.setattr(jobs, "enqueue", enqueue)
    monkeypatch.setattr("app.services.audit.log_audit", nothing)
    return queued


def _start_preview(db):
    project = SimpleNamespace(id=uuid.uuid4(), title="Brand film", revision=2, spec={"scenes": [{"id": "hook"}]})
    return asyncio.run(projects.start_scene_render(db, project, _user(), scene="hook"))


def test_a_fourth_scene_preview_waits_in_the_queue_like_a_render(preview_start):
    started = _start_preview(_PreviewDb(_previews(3)))
    assert started.render.scene_id == "hook"
    # Queued in the preview lane, read from the row: no head start over the whole renders.
    assert preview_start[0][0] == "render" and "priority" not in preview_start[0][2]
    assert jobs.adapter_of("render", started.render) == jobs.PREVIEW_ADAPTER


def test_previews_past_the_videos_cap_are_queued_too(preview_start, monkeypatch):
    """The cap bounds how many previews of a video run at once (``jobs.preview_waits``), never how many are asked."""
    monkeypatch.setattr(jobs, "max_renders_per_user", lambda: 4)
    assert jobs.max_scene_previews_per_video() == 4
    started = _start_preview(_PreviewDb(_previews(10)))
    assert started.render.scene_id == "hook" and preview_start[0][0] == "render"


# ── 6: kept regions and the shot options ──────────────────────────────────


def _image_spec(animate) -> dict:
    return {
        "title": "Team",
        "freestyle": True,
        "scenes": [{"id": "team", "type": "image", "file_id": PICTURE_ID, "animate": animate}],
    }


def test_an_animation_keeps_up_to_four_boxes_inside_the_image():
    spec = validate_video_spec(_image_spec({"keep": [[0.1, 0.05, 0.8, 0.15]]}))
    assert spec["scenes"][0]["animate"]["keep"] == [[0.1, 0.05, 0.8, 0.15]]
    for wrong in ([[0.1, 0.05, 0.95, 0.15]], [[0.1, 0.1, 0.2]], [[True, 0.1, 0.2, 0.2]], [[0.1, 0.1, 0.1, 0.1]] * 5):
        with pytest.raises(VideoSpecError):
            validate_video_spec(_image_spec({"keep": wrong}))


def test_a_changed_keep_starts_a_new_animation_and_the_same_one_keeps_its_shot(monkeypatch):
    started: list[dict] = []
    clip = VideoClip(
        id=uuid.UUID(CLIP_ID),
        user_id=USER_ID,
        provider="gemini",
        model="veo-3.1-generate-preview",
        prompt="Bring this image to life",
        params={"reference_file_id": PICTURE_ID, kept_regions.KEY: {"boxes": [[0.1, 0.05, 0.8, 0.15]]}},
    )

    async def start(_db, _user, **kwargs):
        started.append(kwargs)
        return SimpleNamespace(id=uuid.uuid4()), "Veo"

    monkeypatch.setattr(clips, "start_clip", start)
    db = _Db({(VideoClip, CLIP_ID): clip})
    same = {"scenes": [{"id": "team", "type": "image", "file_id": PICTURE_ID, "animate": {"clip_id": CLIP_ID, "keep": [[0.1, 0.05, 0.8, 0.15]]}}]}
    count, notes = asyncio.run(projects.start_animations(db, _user(), same, {}))
    assert (count, notes, started) == (0, [], [])
    moved = {"scenes": [{"id": "team", "type": "image", "file_id": PICTURE_ID, "animate": {"clip_id": CLIP_ID, "keep": [[0.1, 0.1, 0.8, 0.15]]}}]}
    count, notes = asyncio.run(projects.start_animations(db, _user(), moved, {}))
    assert count == 1 and started[0]["keep_regions"] == [[0.1, 0.1, 0.8, 0.15]]
    assert notes == ["Scene team: its kept regions changed, so a new animation was started."]


def test_keep_regions_need_a_start_picture():
    assert clips.kept_regions_asked(None, start_frame=False) is None
    assert clips.kept_regions_asked([], start_frame=False) == []
    with pytest.raises(clips.ClipError, match="keeps parts of the shot's start picture"):
        clips.kept_regions_asked([[0.1, 0.1, 0.2, 0.2]], start_frame=False)
    with pytest.raises(clips.ClipError, match="keep_regions: "):
        clips.kept_regions_asked([[0.9, 0.1, 0.2, 0.2]], start_frame=True)


def test_stray_screens_need_a_negative_prompt_the_content_check_and_a_readable_screenshot(monkeypatch):
    def resolve(value, negative="screens, readable text", check=True):
        return asyncio.run(
            clips.resolve_stray_screens(None, _user(), value, negative_prompt=negative, content_check=check)
        )

    assert resolve(None) is None and resolve("fail") is None
    assert resolve("flatten") == {"mode": stray_screens.FLATTEN}
    with pytest.raises(clips.ClipError, match="give the shot a negative_prompt"):
        resolve("flatten", negative=None)
    with pytest.raises(clips.ClipError, match="needs content_check on"):
        resolve("flatten", check=False)

    async def unreadable(*_args, **_kwargs):
        raise pictures.PictureError("The screenshot cannot be read.")

    monkeypatch.setattr(pictures, "read_picture", unreadable)
    with pytest.raises(clips.ClipError, match="screenshot cannot be read"):
        resolve({"file_id": PICTURE_ID})
