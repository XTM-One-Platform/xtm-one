"""Sharing a video, its public link, its downloads and its render retention: the pure pieces.

What a public link shows is the video's CURRENT render and nothing about who
made it or who may open it; it answers nothing while the admin switch is off
or for a token of the wrong shape. A download is named after the video and its
quality, the name signed into its token. The render retention keeps the
render the public link plays. The routes, the tools and the grants against
PostgreSQL are ``test_video_studio_sharing_postgres.py``.
"""

from __future__ import annotations

import asyncio
import re
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app.api.v1 import video_public
from app.api.v1.schemas.video_studio import PublicVideoResponse
from app.core.config import settings
from app.core.security_controls import SECURITY_CONTROLS
from app.core.security_options import SECURITY_OPTIONS
from app.models.video_studio import JOB_FAILED, JOB_READY, JOB_RUNNING, VideoProject, VideoPublicLink, VideoRender
from app.services.presentation_public_links import OUTCOME_CREATED, OUTCOME_DISABLED, OUTCOME_REGENERATED
from app.services.video_studio import media, projects, public_links, render_retention

pytestmark = pytest.mark.usefixtures("video_studio_enabled")

ROOT = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 10, 17, 12, 0, tzinfo=UTC)
TOKEN = "A" * 43


def _project(**columns) -> VideoProject:
    base = {
        "id": uuid.uuid4(),
        "user_id": uuid.uuid4(),
        "title": "Launch film",
        "spec": {"title": "Launch film", "scenes": []},
        "revision": 3,
        "scene_count": 2,
        "is_company_managed": False,
        "access_restricted": False,
    }
    base.update(columns)
    return VideoProject(**base)


def _render(project: VideoProject, minutes_ago: int, *, status: str = JOB_READY, **columns) -> VideoRender:
    created = NOW - timedelta(minutes=minutes_ago)
    base = {
        "id": uuid.uuid4(),
        "project_id": project.id,
        "user_id": project.user_id,
        "revision": 3,
        "quality": "high",
        "status": status,
        "keep": False,
        "created_at": created,
        "finished_at": created + timedelta(minutes=2) if status != JOB_RUNNING else None,
        "width": 1920,
        "height": 1080,
        "fps": 60,
        "duration_ms": 39_800,
        "size_bytes": 80_300_000,
        "file_id": uuid.uuid4() if status == JOB_READY else None,
        "poster_file_id": uuid.uuid4() if status == JOB_READY else None,
        "captions_file_id": uuid.uuid4() if status == JOB_READY else None,
        "captions_burned_in": False,
    }
    base.update(columns)
    return VideoRender(**base)


def _standing(project: VideoProject, renders: list[VideoRender], *, public_link: bool = False):
    return render_retention.standing_of(project, renders, public_link=public_link, policy=render_retention.Policy(), now=NOW)


# ── The render retention (the policy itself: test_video_studio_render_cleanup.py) ──


def test_the_render_a_public_link_plays_is_kept_by_it_and_a_running_one_is_never_judged():
    project = _project()
    older_current, newer = _render(project, 60 * 24 * 90), _render(project, 10)
    project.latest_render_id = older_current.id
    renders = [older_current, newer, running := _render(project, 5, status=JOB_RUNNING)]
    linked = _standing(project, renders, public_link=True)
    assert render_retention.kept_by(older_current, linked) == render_retention.KEPT_PUBLIC_LINK
    assert render_retention.expires_at(older_current, linked) is None
    # Without a link, a current render picked by hand is the author's; the newest ready one keeps itself.
    unlinked = _standing(project, renders)
    assert render_retention.kept_by(older_current, unlinked) == render_retention.KEPT_AUTHOR
    assert render_retention.kept_by(newer, unlinked) == render_retention.KEPT_LATEST_READY
    assert render_retention.kept_by(running, unlinked) is None and render_retention.expires_at(running, unlinked) is None


def test_a_render_says_whether_it_is_current_what_keeps_it_and_until_when():
    project = _project()
    current, older = _render(project, 10), _render(project, 70)
    project.latest_render_id = current.id
    held = _standing(project, [current, older])
    served = projects.serialize_render(current, project=project, standing=held)
    assert (served["is_current"], served["kept_by"], served["expires_at"], served["keep"]) == (True, "latest_ready", None, False)
    served = projects.serialize_render(older, project=project, standing=held)
    assert (served["is_current"], served["kept_by"]) == (False, "retention")
    assert served["expires_at"] == (older.finished_at + timedelta(days=30)).isoformat()
    # Without its video (the live jobs list), a render carries no retention fields.
    assert "is_current" not in projects.serialize_render(current)


def test_a_render_owns_its_three_files():
    project = _project()
    render = _render(project, 10)
    assert render_retention.render_files(render) == [render.file_id, render.poster_file_id, render.captions_file_id]
    assert render_retention.render_files(_render(project, 1, status=JOB_FAILED)) == []


# ── Sharing ───────────────────────────────────────────────────────────────


def test_a_video_serves_its_grants_its_owner_and_the_reader_s_tier():
    owner = uuid.uuid4()
    project = _project(
        user_id=owner,
        shared_user_ids=["11111111-1111-1111-1111-111111111111"],
        shared_user_permissions={"11111111-1111-1111-1111-111111111111": "config"},
        shared_group_ids=["22222222-2222-2222-2222-222222222222"],
        shared_group_permissions={"22222222-2222-2222-2222-222222222222": "read_only"},
    )
    payload = projects.serialize_project(
        project, can_manage=False, permission="read_only", owner_name="Samuel Hassine", has_public_link=True
    )
    assert payload["shared_user_permissions"] == {"11111111-1111-1111-1111-111111111111": "config"}
    assert payload["shared_group_permissions"] == {"22222222-2222-2222-2222-222222222222": "read_only"}
    assert (payload["access_restricted"], payload["permission"], payload["owner_name"]) == (
        False,
        "read_only",
        "Samuel Hassine",
    )
    assert payload["has_public_link"] is True

    class _User:
        def __init__(self, user_id):
            self.id = user_id

    assert projects.video_permission(project, _User(owner), can_manage=True) == "owner"
    assert projects.video_permission(project, _User(uuid.uuid4()), can_manage=True) == "config"
    assert projects.video_permission(project, _User(uuid.uuid4()), can_manage=False) == "read_only"


def test_videos_and_decks_share_one_sharing_write():
    from app.services import presentation_sharing

    source = (ROOT / "backend" / "app" / "api" / "v1" / "video_studio.py").read_text(encoding="utf-8")
    assert "apply_sharing(db, user, project, body)" in source
    assert "apply_user_share_fields" not in source
    tools = (ROOT / "backend" / "app" / "services" / "builtin_tools" / "video_studio.py").read_text(encoding="utf-8")
    assert "apply_sharing(session, user, project, plan.changes)" in tools
    assert "SharedEntity" in presentation_sharing.__dict__


# ── The public link ───────────────────────────────────────────────────────


def _link(**columns) -> VideoPublicLink:
    base = {"id": uuid.uuid4(), "project_id": uuid.uuid4(), "token_hash": "x", "token_enc": "y", "allow_download": False}
    base.update(columns)
    return VideoPublicLink(**base)


def test_a_public_link_shows_the_current_render_and_nothing_about_who_made_it():
    project = _project(shared_user_ids=["11111111-1111-1111-1111-111111111111"])
    render = _render(project, 10)
    payload = public_links.public_video_payload(project, render, _link())
    assert set(payload) == set(PublicVideoResponse.model_fields)
    for private in ("user_id", "spec", "owner_name", "shared_user_ids", "can_manage", "file_id"):
        assert private not in payload
    assert (payload["title"], payload["rendered"], payload["width"], payload["fps"]) == ("Launch film", True, 1920, 60)
    assert (payload["has_captions"], payload["captions_burned_in"]) == (True, False)
    # Download only when the link offers it - and then its size.
    assert (payload["download"], payload["size_bytes"]) == (False, None)
    offered = public_links.public_video_payload(project, render, _link(allow_download=True))
    assert (offered["download"], offered["size_bytes"]) == (True, 80_300_000)
    # Not rendered yet: the page says so, nothing to download.
    empty = public_links.public_video_payload(project, None, _link(allow_download=True))
    assert (empty["rendered"], empty["download"], empty["has_poster"]) == (False, False, False)


def test_a_public_link_serves_the_current_render_s_three_files_only():
    render = _render(_project(), 10)
    assert public_links.public_file_id(render, "video") == render.file_id
    assert public_links.public_file_id(render, "poster") == render.poster_file_id
    assert public_links.public_file_id(render, "captions") == render.captions_file_id
    for asset in ("contact_sheet", "mfa_screenshot", "../video"):
        assert public_links.public_file_id(render, asset) is None
    assert public_links.public_file_id(None, "video") is None
    assert {"video", "captions"} == set(public_links.DOWNLOADABLE_ASSETS)


class _NoLookups:
    async def execute(self, *_a, **_kw):
        raise AssertionError("refused before any lookup")


def test_the_switch_off_and_a_malformed_token_answer_before_any_lookup(monkeypatch):
    monkeypatch.setattr(settings, "video_public_links_enabled", False)
    assert asyncio.run(public_links.resolve_public_link(_NoLookups(), TOKEN)) is None
    assert asyncio.run(public_links.public_video_ids(_NoLookups(), [uuid.uuid4()])) == set()
    monkeypatch.setattr(settings, "video_public_links_enabled", True)
    for token in ("short", "A" * 44, "A" * 42 + "/", None):
        assert asyncio.run(public_links.resolve_public_link(_NoLookups(), token)) is None


def test_the_tool_hands_out_the_url_and_says_what_the_link_plays(monkeypatch):
    monkeypatch.setattr("app.services.email.platform_url", lambda: "https://xtm.example.com")
    created = public_links.format_public_link_result("Launch film", OUTCOME_CREATED, TOKEN, rendered=False)
    assert f"https://xtm.example.com/public/videos/{TOKEN}" in created
    assert "Anyone with this link can watch the video without signing in" in created
    assert "has not been rendered yet" in created and "Visitors cannot download it" in created
    rotated = public_links.format_public_link_result(
        "Launch film", OUTCOME_REGENERATED, TOKEN, rendered=True, allow_download=True, download_changed=True
    )
    assert "previous link no longer works" in rotated and "can now also download it" in rotated
    assert "not been rendered" not in rotated
    off = public_links.format_public_link_result("Launch film", OUTCOME_DISABLED, None, rendered=True)
    assert "turned off" in off and "keep their access" in off
    lost = public_links.format_public_link_result("Launch film", OUTCOME_CREATED, None, rendered=True)
    assert "regenerate" in lost


def test_get_video_says_whether_the_link_plays(monkeypatch):
    assert public_links.public_link_status_line(has_link=False) == ""
    monkeypatch.setattr(settings, "video_public_links_enabled", True)
    assert "(no download)" in public_links.public_link_status_line(has_link=True)
    assert "and download it" in public_links.public_link_status_line(has_link=True, allow_download=True)
    monkeypatch.setattr(settings, "video_public_links_enabled", False)
    assert "kept but not working" in public_links.public_link_status_line(has_link=True)


def test_the_public_routes_need_no_account_and_are_mounted_beside_the_public_decks():
    paths = {route.path for route in video_public.router.routes}
    assert paths == {"/videos/{token}", "/videos/{token}/media/{asset}"}
    for route in video_public.router.routes:
        names = {dep.call.__name__ for dep in route.dependant.dependencies if dep.call}
        assert "get_current_user" not in names
    main = (ROOT / "backend" / "app" / "main.py").read_text(encoding="utf-8")
    assert re.search(r'include_router\(\s*video_public\.router,\s*prefix="/api/v1/public"', main)
    assert video_public.PUBLIC_HEADERS == {"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"}


def test_the_admin_switch_is_a_security_option_and_an_audit_control():
    option = SECURITY_OPTIONS["video_public_links_enabled"]
    assert (option.kind, option.group, option.label) == ("bool", "access", "Public video links")
    control = next(c for c in SECURITY_CONTROLS if c.id == "access.video_public_links")
    assert (control.setting, control.hardened) == ("video_public_links_enabled", "false")
    sample = (ROOT / ".env.sample").read_text(encoding="utf-8")
    assert "# VIDEO_PUBLIC_LINKS_ENABLED=true" in sample


# ── Downloads ─────────────────────────────────────────────────────────────


def test_a_download_is_named_after_the_video_and_its_quality():
    assert media.download_filename("Launch film", "video", height=1080, fps=60) == "Launch film - 1080p60.mp4"
    assert media.download_filename('Q3: "launch" / v2', "video", height=2160, fps=60) == "Q3 launch v2 - 2160p60.mp4"
    assert media.download_filename("Launch film", "captions", height=1080, fps=60) == "Launch film.vtt"
    assert media.download_filename("Launch film", "poster") == "Launch film.jpg"
    assert media.download_filename("", "video") == "video.mp4"
    assert len(media.download_filename("x" * 400, "video", height=720, fps=30)) < 200


def test_a_download_token_signs_its_file_name_and_a_playing_token_carries_none(monkeypatch):
    monkeypatch.setattr(settings, "secret_key", settings.secret_key or "test-secret-key-for-media-tokens")
    file_id, user_id = uuid.uuid4(), uuid.uuid4()
    token, _expires = media.mint_media_token(file_id, user_id, filename="Launch film - 1080p60.mp4")
    claims = media.decode_media_token(token)
    assert (claims.file_id, claims.user_id, claims.filename) == (file_id, user_id, "Launch film - 1080p60.mp4")
    playing, _expires = media.mint_media_token(file_id, user_id)
    assert media.decode_media_token(playing).filename is None


# ── The tools and the settings ────────────────────────────────────────────


def test_the_sharing_tools_are_in_the_video_studio_suite():
    from app.services.builtin_tools.video_studio import build_video_studio_tools

    definitions, executor = build_video_studio_tools(user_id="")
    by_name = {d.name: d for d in definitions}
    share, link = by_name["share_video"], by_name["set_video_public_link"]
    assert share.input_schema["properties"]["permission"]["enum"] == ["read_only", "config"]
    assert share.input_schema["properties"]["action"]["enum"] == ["add", "remove", "replace"]
    assert link.input_schema["properties"]["action"]["enum"] == ["enable", "regenerate", "disable"]
    assert link.input_schema["properties"]["download"]["type"] == "boolean"
    assert {"share_video", "set_video_public_link"} <= set(executor._handlers)


def test_the_public_link_switch_is_documented():
    docs = (ROOT / "docs" / "docs" / "deployment" / "configuration.md").read_text(encoding="utf-8")
    assert "`VIDEO_PUBLIC_LINKS_ENABLED`" in docs
