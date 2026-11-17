"""Video Studio editor: the live preview, the media retention, the studio binding.

The preview is the render's own compositor fed the render's own plan of the
DRAFT, its recordings and clips played from confined media URLs; what the
preview cannot know before a render (a recording or a shot still being made,
a deck slide) is stated, never invented. The retention deletes a recording's
or a shot's media once no video uses them, never while one does. The
studio rail binds a conversation to the video it edits.
"""

import asyncio
import shutil
import subprocess
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.dialects import postgresql

from app.api import deps
from app.api.v1 import video_studio as video_studio_api
from app.core import csp
from app.core.config import settings
from app.core.database import get_db
from app.models.video_studio import JOB_PENDING, JOB_READY, VideoClip, VideoProject, VideoTake
from app.services.video_studio import compositor_page, jobs, preview, projects, retention, templates
from app.services.video_studio.studio import VideoStudioContext, format_video_studio_prompt, load_video_studio_context

TAKE_ID = "00000000-0000-0000-0000-0000000000a1"
CLIP_ID = "00000000-0000-0000-0000-0000000000b2"
FILE_ID = "00000000-0000-0000-0000-0000000000c3"
DECK_ID = "00000000-0000-0000-0000-0000000000d4"


def _user():
    return SimpleNamespace(id=uuid.uuid4(), role="user", is_admin=False, email="user@example.com", is_active=True)


# ── The compositor's preview mode ─────────────────────────────────────────


def test_the_compositor_plays_media_in_preview_and_frames_in_a_render():
    js = compositor_page.PAGE_JS
    # The preview hands the page its media URLs; a render leaves them unset.
    assert "const media = doc.preview_media || null;" in js and "S.media = media;" in js
    assert "if (S.media) return previewSource(parent, sc);" in js
    # A render still draws the extracted frame under each output frame; a slowed sparse stretch shows the
    # nearest painted one, never two cross-dissolved.
    assert "setImg(img, url(Math.round(t * sc.src_fps)));" in js
    assert "nearestPainted(sc.painted, t)" in js
    assert "blendPair" not in js and "sc.blend" not in js
    assert "const url = (index) => `${ORIGIN}/${sc.src}/${clamp(index, 0, Math.max(sc.src_frames - 1, 0))}.jpg`;" in js
    # The preview's transport: playing, muting, a scene off screen is silent.
    for hook in (
        "setPlaying(playing)",
        "setMuted(muted)",
        "for (const s of S.sources) if (!s.active) for (const video of videosOf(s)) if (!video.paused) video.pause();",
    ):
        assert hook in js
    # A new document leaves no layer of the previous one: a render drops them at once, the preview once the
    # new frame is drawn (the browser tests of test_video_studio_preview_continuity.py).
    assert "if (media) S.retired.push(b.layer);" in js and "else b.layer.remove();" in js
    assert "for (const layer of S.retired) layer.remove();" in js


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_the_compositor_script_parses(tmp_path):
    script = tmp_path / "compositor.js"
    script.write_text(compositor_page.PAGE_JS, encoding="utf-8")
    node = shutil.which("node") or "node"
    result = subprocess.run([node, "--check", str(script)], capture_output=True, text=True, timeout=60, check=False)  # noqa: S603
    assert result.returncode == 0, result.stderr


def test_the_preview_page_is_served_without_data_and_framed_only_by_the_platform():
    app = FastAPI()
    app.include_router(video_studio_api.router, prefix="/api/v1/video-studio")
    response = TestClient(app).get("/api/v1/video-studio/preview-page")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert "window.__xtm" in response.text
    policy = csp.page_policy_for("/api/v1/video-studio/preview-page")
    assert policy is not None and policy.frame_ancestors == "self"
    assert csp.inline_script_hashes(response.text)[0] in tuple(policy.script_hashes())


def test_the_preview_page_is_revalidated_so_a_release_reaches_the_editor_at_once():
    """A page kept for an hour drew new templates with the previous release's compositor."""
    app = FastAPI()
    app.include_router(video_studio_api.router, prefix="/api/v1/video-studio")
    client = TestClient(app)
    first = client.get("/api/v1/video-studio/preview-page")
    assert first.headers["cache-control"] == "no-cache"
    etag = first.headers["etag"]
    again = client.get("/api/v1/video-studio/preview-page", headers={"If-None-Match": etag})
    assert again.status_code == 304 and again.content == b"" and again.headers["etag"] == etag
    changed = client.get("/api/v1/video-studio/preview-page", headers={"If-None-Match": '"a-previous-release"'})
    assert changed.status_code == 200 and "window.__xtm" in changed.text


# ── Planning a draft ──────────────────────────────────────────────────────


class _PreviewDB:
    """``db.get`` answers the scripted rows by model and id."""

    def __init__(self, rows):
        self._rows = rows

    async def get(self, model, key):
        return self._rows.get((model, str(key)))


def _project(spec: dict) -> VideoProject:
    return VideoProject(id=uuid.uuid4(), user_id=uuid.uuid4(), title=spec["title"], spec=spec, revision=3)


def test_a_draft_previews_with_its_media_and_says_what_is_still_being_made(monkeypatch):
    async def no_problems(*_args, **_kwargs):
        return []

    async def no_template(*_args, **_kwargs):
        return None

    async def plain_theme(*_args, **_kwargs):
        return {}, {}, [], {}

    monkeypatch.setattr(projects, "verify_references", no_problems)
    monkeypatch.setattr(templates, "resolve_for_spec", no_template)
    monkeypatch.setattr(preview, "_theme", plain_theme)
    pending_take = VideoTake(id=uuid.UUID(TAKE_ID), name="Flow", status=JOB_PENDING, script={})
    ready_clip = VideoClip(
        id=uuid.UUID(CLIP_ID),
        provider="openai",
        model="sora-2",
        prompt="An opening shot",
        params={},
        status=JOB_READY,
        file_id=uuid.uuid4(),
        duration_ms=6000,
        width=1920,
        height=1080,
        has_audio=True,
    )
    db = _PreviewDB({(VideoTake, TAKE_ID): pending_take, (VideoClip, CLIP_ID): ready_clip})
    draft = {
        "title": "Demo",
        "freestyle": True,
        "scenes": [
            {"id": "intro", "type": "clip", "clip_id": CLIP_ID, "narration": "Welcome to the demo."},
            {"id": "flow", "type": "recording", "take_id": TAKE_ID},
            {"id": "pic", "type": "image", "file_id": FILE_ID},
            {"id": "deck", "type": "slide", "presentation_id": DECK_ID, "slide": 4},
        ],
    }
    result = asyncio.run(preview.build_preview(db, _project(draft), _user(), draft))

    document = result["document"]
    scenes = {scene["id"]: scene for scene in document["scenes"]}
    # Ready media play from confined URLs; a recording still being made has none and says so.
    media = document["preview_media"]
    assert (
        media["clip/intro"].startswith("/api/v1/video-studio/media/") or "/video-studio/media/" in media["clip/intro"]
    )
    assert "image/pic" in media and "take/flow" not in media
    assert scenes["flow"]["preview_state"] == {"state": "recording"}
    assert scenes["deck"]["preview_state"] == {"state": "slide", "slide": 4}
    # The timeline calls a scene by the recording or the shot it plays, never "Recording" once it is made.
    assert scenes["flow"]["preview_name"] == "Flow" and scenes["intro"]["preview_name"] == "An opening shot"
    assert "preview_name" not in scenes["pic"]
    # A clip with sound plays it; an image's size is the page's to fit.
    assert scenes["intro"]["preview_audio"]["volume"] == pytest.approx(0.8)
    assert scenes["pic"]["preview_unsized"] is True
    # The planner's timing: scenes in order, the narration paced (no voice before the render).
    starts = [scene["start"] for scene in document["scenes"]]
    assert starts == sorted(starts) and result["duration"] == document["duration"]
    assert result["narration"][0]["scene_id"] == "intro" and result["narration"][0]["seconds"] > 0
    assert not any("no voice generated" in note for note in result["notes"])


# ── Media retention ───────────────────────────────────────────────────────


def _asset(**columns) -> VideoTake:
    base = {"id": uuid.uuid4(), "name": "Flow", "status": JOB_READY, "keep": False, "script": {}}
    base.update(columns)
    return VideoTake(**base)


def test_media_expire_after_their_last_use_or_after_they_were_made(monkeypatch):
    monkeypatch.setattr(settings, "video_studio_media_retention_days", 30)
    monkeypatch.setattr(settings, "video_studio_unused_media_retention_days", 14)
    made = datetime(2026, 10, 1, tzinfo=UTC)
    used = datetime(2026, 10, 10, tzinfo=UTC)
    assert retention.expires_at(_asset(created_at=made)) == made + timedelta(days=14)
    assert retention.expires_at(_asset(created_at=made, last_used_at=used)) == used + timedelta(days=30)
    # Kept, already expired, or not ready: nothing to expire.
    assert retention.expires_at(_asset(created_at=made, keep=True)) is None
    assert retention.expires_at(_asset(created_at=made, expired_at=used)) is None
    assert retention.expires_at(_asset(created_at=made, status=JOB_PENDING)) is None
    # 0 keeps them forever.
    monkeypatch.setattr(settings, "video_studio_unused_media_retention_days", 0)
    assert retention.expires_at(_asset(created_at=made)) is None
    fields = retention.retention_fields(_asset(created_at=made, last_used_at=used))
    assert fields["expires_at"] == (used + timedelta(days=30)).isoformat() and fields["keep"] is False


def test_media_a_video_uses_show_no_expiry():
    """A recording or a shot in a video never expires, so nothing counts it down."""
    used = datetime(2026, 10, 10, tzinfo=UTC)
    take = _asset(created_at=used, last_used_at=used)
    assert retention.expires_at(take) is not None
    assert retention.expires_at(take, in_use=True) is None
    assert retention.retention_fields(take, in_use=True)["expires_at"] is None
    assert jobs.serialize_take(take, in_use=True)["expires_at"] is None
    clip = VideoClip(id=uuid.uuid4(), status=JOB_READY, prompt="Cats", keep=False, created_at=used, last_used_at=used)
    assert jobs.serialize_clip(clip)["expires_at"] is not None
    assert jobs.serialize_clip(clip, in_use=True)["expires_at"] is None


class _RecordingDB:
    def __init__(self):
        self.statements = []
        self.committed = False

    async def execute(self, statement, *_args, **_kwargs):
        self.statements.append(statement)
        return SimpleNamespace(all=lambda: [], scalars=lambda: SimpleNamespace(all=lambda: [], first=lambda: None))

    async def commit(self):
        self.committed = True


def _indexed_answer(statement, specs: list[dict]):
    """What PostgreSQL answers a read of ``video_projects`` holding *specs*, indexed (``spec.asset_refs``)."""
    from app.services.video_studio.spec import asset_refs

    if "asset_refs IS NULL" in str(statement.compile()):
        return SimpleNamespace(all=lambda: [])
    asked = {value for param in statement.compile().params.values() if isinstance(param, list) for value in param}
    matches = [index for index, spec in enumerate(specs) if asked & set(asset_refs(spec))]
    return SimpleNamespace(
        all=lambda: [],
        scalars=lambda: SimpleNamespace(all=lambda: matches, first=lambda: matches[0] if matches else None),
    )


def test_a_save_or_a_render_stamps_the_media_the_video_uses():
    db = _RecordingDB()
    spec = {
        "scenes": [{"type": "recording", "take_id": TAKE_ID}, {"type": "clip", "clip_id": CLIP_ID}, {"type": "title"}]
    }
    asyncio.run(retention.touch(db, spec))
    tables = sorted(statement.table.name for statement in db.statements)
    assert tables == ["video_clips", "video_takes"]


def test_a_save_stamps_what_it_drops_so_the_retention_counts_from_then():
    """The spec a save replaces used its media until now: one it drops gets the whole period from here."""
    db = _RecordingDB()
    dropped = "00000000-0000-0000-0000-0000000000a9"
    replaced = {"scenes": [{"type": "recording", "take_id": dropped}, {"type": "recording", "take_id": TAKE_ID}]}
    stored = {"scenes": [{"type": "recording", "take_id": TAKE_ID}]}
    asyncio.run(retention.touch(db, replaced, stored))
    (statement,) = db.statements
    stamped = {
        str(value) for param in statement.compile().params.values() if isinstance(param, list) for value in param
    }
    assert stamped == {dropped, TAKE_ID}


def test_media_kept_forever_are_never_expired_but_failed_ones_are_still_purged(monkeypatch):
    monkeypatch.setattr(settings, "video_studio_media_retention_days", 0)
    monkeypatch.setattr(settings, "video_studio_unused_media_retention_days", 0)
    db = _RecordingDB()
    assert asyncio.run(retention.sweep(db)) == {"purged": 0, "expired": 0, "in_use": 0}
    # The videos an older replica left unindexed, then the two failed-media reads: nothing ready is looked at.
    assert [str(s.compile()).count("status IN") for s in db.statements] == [0, 1, 1]
    assert "asset_refs IS NULL" in str(db.statements[0].compile())


class _SweepDB:
    """Failed rows by table, the specs of the videos, and what the sweep deleted."""

    def __init__(self, failed: dict[str, list], specs: list[dict]):
        self.failed, self.specs = failed, specs
        self.deleted: list = []
        self.committed = False

    async def execute(self, statement, *_args, **_kwargs):
        table = statement.get_final_froms()[0].name
        if table == "video_projects":
            return _indexed_answer(statement, self.specs)
        rows = self.failed.get(table, [])
        return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: list(rows)))

    async def delete(self, row):
        self.deleted.append(row)

    async def commit(self):
        self.committed = True


def test_a_failed_shot_no_video_uses_is_deleted_and_one_a_video_uses_stays(monkeypatch):
    """A failed shot made nothing: it is not kept as media, unless a scene shows it failed."""
    monkeypatch.setattr(settings, "video_studio_media_retention_days", 0)
    monkeypatch.setattr(settings, "video_studio_unused_media_retention_days", 0)
    poster = uuid.uuid4()
    unused = VideoClip(id=uuid.uuid4(), status="failed", prompt="Cats", poster_file_id=poster, keep=False)
    placed = VideoClip(id=uuid.UUID(CLIP_ID), status="failed", prompt="Dogs", keep=False)
    lost = VideoTake(id=uuid.uuid4(), name="Flow", status="cancelled", script={}, keep=False)
    db = _SweepDB(
        {"video_clips": [unused, placed], "video_takes": [lost]},
        [{"scenes": [{"type": "clip", "clip_id": CLIP_ID}]}],
    )
    removed: list = []

    async def delete_files(files):
        removed.extend(files)

    monkeypatch.setattr("app.services.video_studio.assets.delete_files", delete_files)
    counts = asyncio.run(retention.sweep(db))
    assert counts == {"purged": 2, "expired": 0, "in_use": 0}
    assert db.deleted == [lost, unused] and db.committed
    assert str(poster) in {str(file_id) for file_id in removed if file_id}


class _ReadyDB(_SweepDB):
    """No failed rows: the ready rows past their period by table, and the specs of the videos."""

    async def execute(self, statement, *_args, **_kwargs):
        table = statement.get_final_froms()[0].name
        if table == "video_projects":
            return _indexed_answer(statement, self.specs)
        rows = [] if "status IN" in str(statement.compile()) else self.failed.get(table, [])
        return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: list(rows)))


def test_a_recording_a_video_uses_is_never_expired_however_long_the_video_is_left_alone(monkeypatch):
    """The retention is for media no video uses; a video left alone keeps its recordings and shots."""
    monkeypatch.setattr(settings, "video_studio_media_retention_days", 30)
    monkeypatch.setattr(settings, "video_studio_unused_media_retention_days", 14)
    now = datetime(2026, 12, 1, tzinfo=UTC)
    long_ago = now - timedelta(days=90)
    kept_file, dropped_file = uuid.uuid4(), uuid.uuid4()
    placed = _asset(id=uuid.UUID(TAKE_ID), created_at=long_ago, last_used_at=long_ago, file_id=kept_file)
    dropped = _asset(created_at=long_ago, last_used_at=long_ago, file_id=dropped_file)
    db = _ReadyDB({"video_takes": [placed, dropped]}, [{"scenes": [{"type": "recording", "take_id": TAKE_ID}]}])
    removed: list = []

    async def delete_files(files):
        removed.extend(files)

    monkeypatch.setattr("app.services.video_studio.assets.delete_files", delete_files)
    counts = asyncio.run(retention.sweep(db, now=now))
    assert counts == {"purged": 0, "expired": 1, "in_use": 1}
    # The one in a video keeps its files and counts from now; the one no video uses goes.
    assert placed.expired_at is None and placed.file_id == kept_file and placed.last_used_at == now
    assert dropped.expired_at == now and dropped.file_id is None
    assert dropped_file in removed and kept_file not in removed


def test_failed_media_get_the_grace_of_a_retry():
    db = _RecordingDB()
    now = datetime(2026, 10, 17, 12, tzinfo=UTC)
    asyncio.run(retention.sweep(db, now=now))
    first = db.statements[1].compile()
    assert "coalesce" in str(first).lower()
    assert now - retention.FAILED_MEDIA_GRACE in first.params.values()


def test_which_videos_use_a_medium_is_one_indexed_predicate():
    """``jsonb ?| text[]`` on ``asset_refs``, the GIN index's operator - no cap, no spec scan."""
    keys = retention.media_keys([("clip", CLIP_ID), ("take", TAKE_ID), ("take", "not-a-uuid"), ("clip", CLIP_ID)])
    assert keys == [f"clip:{CLIP_ID}", f"take:{TAKE_ID}"]
    compiled = retention.uses_media(keys).compile(dialect=postgresql.dialect())
    assert "CAST(video_projects.asset_refs AS JSONB) ?| CAST(" in str(compiled)
    assert "TEXT[]" in str(compiled)


def test_the_retention_settings_are_documented():
    from pathlib import Path

    docs = (Path(__file__).resolve().parents[2] / "docs" / "docs" / "deployment" / "configuration.md").read_text(
        encoding="utf-8"
    )
    for name in ("VIDEO_STUDIO_MEDIA_RETENTION_DAYS", "VIDEO_STUDIO_UNUSED_MEDIA_RETENTION_DAYS"):
        assert name in docs


# ── The studio binding ────────────────────────────────────────────────────


def _video_context(**overrides) -> VideoStudioContext:
    project = _project(
        {
            "title": "Onboarding tour",
            "scenes": [
                {"id": "s1", "type": "title", "title": "Welcome"},
                {"id": "s2", "type": "recording", "take_id": TAKE_ID, "narration": "Open the case."},
            ],
        }
    )
    project.estimated_duration_ms = 42_000
    values = {"project": project, "can_edit": True, "focused_scene": None}
    values.update(overrides)
    return VideoStudioContext(**values)


def test_the_studio_tells_the_agent_which_video_and_which_scene():
    ctx = _video_context(focused_scene="s2")
    prompt = ctx.prompt()
    assert prompt == format_video_studio_prompt(ctx)
    assert prompt.startswith("\n[Video Studio]")
    assert f"video_id: {ctx.project.id}" in prompt and f"edit_video with video_id={ctx.project.id}" in prompt
    assert "scene s2 (recording" in prompt
    assert "[s1] title" in prompt
    # The video tools, and the template lookup that reads a brand's logos and pictures.
    assert ctx.tool_ids == ("video_studio", "template_search")
    assert "READ-ONLY" in _video_context(can_edit=False).prompt()
    # The format is the spec's own, not a guess.
    portrait = _project({"title": "Teaser", "format": {"aspect_ratio": "9:16"}, "scenes": [{"type": "title", "title": "x"}]})
    assert "9:16" in format_video_studio_prompt(VideoStudioContext(project=portrait, can_edit=True))


def test_no_binding_is_no_studio():
    assert asyncio.run(load_video_studio_context(_PreviewDB({}), user_id=uuid.uuid4(), video_project_id=None)) is None


def test_the_chat_api_carries_the_video_binding():
    from app.api.v1 import chat

    assert "video_project_id" in chat.ConversationCreate.model_fields
    assert "video_project_id" in chat.ConversationResponse.model_fields
    assert "video_project_id" in chat.ConversationDetailResponse.model_fields
    assert "studio_scene" in chat.SendMessageRequest.model_fields
    paths = {route.path for route in chat.router.routes}
    assert "/conversations/{conversation_id}/video" in paths


# ── Editor routes ─────────────────────────────────────────────────────────


def _client(user) -> TestClient:
    app = FastAPI()
    app.include_router(video_studio_api.router, prefix="/api/v1/x")

    async def _get_db():
        yield SimpleNamespace()

    app.dependency_overrides[get_db] = _get_db
    app.dependency_overrides[deps.get_current_user] = lambda: user
    return TestClient(app)


def test_an_upload_into_a_video_takes_pictures_sounds_and_videos_only():
    response = _client(_user()).post("/api/v1/x/files", files={"file": ("notes.txt", b"hello", "text/plain")})
    assert response.status_code == 415


def test_only_the_ai_shot_spends_ai_providers():
    import inspect

    def gated(fn) -> bool:
        return any(
            getattr(param.default, "dependency", None) is deps.require_ai_enabled
            for param in inspect.signature(fn).parameters.values()
        )

    assert gated(video_studio_api.create_clip)
    for fn in (
        video_studio_api.create_project,
        video_studio_api.duplicate_project,
        video_studio_api.preview_project,
        video_studio_api.update_take,
        video_studio_api.update_clip,
        video_studio_api.upload_file,
    ):
        assert not gated(fn), fn.__name__
