"""An edit reverted renders without force, and an edit that changes nothing saves nothing, against PostgreSQL.

Through the real ``edit_video`` and ``render_video`` tools, as an ordinary
user (rule 19: an admin would short-circuit the visibility filter):

* every scene previewed; one scene edited (a new revision, its scene and its
  neighbours out of date), previewed as edited, then put back as it was: the
  save says the scene is back to its earlier preview and ``render_video``
  passes the gate without ``force`` - a video keeps each scene's drawings by
  what they drew, never only the last one;
* the same edit asked again gives the spec stored byte for byte: no revision,
  no audit entry, and the answer says nothing was saved.

``integration``-marked and skips when PostgreSQL is unreachable; the
temporary database is the shared ``tests/scratch_database.py``.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import func, select

import app.models  # noqa: F401 - registers every table on Base.metadata
from app.models.audit_log import AuditLog
from app.models.user import User
from app.models.video_studio import VideoProject, VideoRender
from app.services.builtin_tools import video_studio as tool
from app.services.video_studio import jobs, lexicon_store, projects, quality, render_checks
from app.services.video_studio.spec import parse_video_spec
from app.tools.base import ToolCall
from tests.scratch_database import ALL_TABLES, scratch_database

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("video_studio_enabled")]

_SPEC = {
    "title": "Brand film",
    "freestyle": True,
    "scenes": [
        {"id": "open", "type": "title", "title": "One platform", "duration": 4},
        {"id": "mid", "type": "title", "title": "Every alert triaged", "duration": 4},
        {"id": "close", "type": "title", "title": "Thank you", "duration": 4},
    ],
}


@pytest.fixture(autouse=True)
def quiet(monkeypatch):
    """No worker to wake, no wait inside a tool call, and a quality check with nothing to say (no compositor)."""

    async def nothing(*_args, **_kwargs):
        return None

    async def clean(_project_id, _user_id, *, expected_revision=None, **_kwargs):
        return quality.QualityReport(scene_count=3, findings=(), revision=expected_revision)

    monkeypatch.setattr(jobs, "wake", nothing)
    monkeypatch.setattr(tool, "_wait_budget", lambda _default: 0.0)
    monkeypatch.setattr(tool, "check_video_quality", clean)
    monkeypatch.setattr(quality, "check_video_quality", clean)


async def _previewed(sessions, project_id, scenes: list[str]) -> None:
    """What a passing preview of each of *scenes*, drawn from the spec as it is now, records on the video."""
    async with sessions() as db:
        project = await db.get(VideoProject, project_id)
        owner = await db.get(User, project.user_id)
        effective = (await projects.effective_spec(db, project, owner)).spec
        lexicon = await lexicon_store.for_voiceover(db, parse_video_spec(effective).voiceover)
        digests = await render_checks.scene_digests(db, effective, lexicon)
        seen = project.scene_previews
        for scene in scenes:
            seen = render_checks.record_seen(seen, {scene: digests[scene]}, uuid.uuid4(), checks={"findings": []})
        project.scene_previews = seen
        await db.commit()


def _duration(seconds: float) -> dict:
    return {"op": "update_scene", "scene": 1, "fields": {"duration": seconds}}


async def test_an_edit_previewed_then_reverted_renders_without_force() -> None:
    async with scratch_database(
        "video_revert", tables=ALL_TABLES, extensions=("vector",), skip_without_extensions=True
    ) as scratch:
        async with scratch.sessions() as db:
            owner = User(email="samuel@filigran.io", hashed_password="x", full_name="Samuel Hassine", role="user")
            db.add(owner)
            await db.flush()
            project = VideoProject(user_id=owner.id, title=_SPEC["title"], spec=dict(_SPEC), scene_count=3, revision=1)
            db.add(project)
            await db.commit()
            video_id = project.id
        await _previewed(scratch.sessions, video_id, ["open", "mid", "close"])
        _definitions, executor = tool.build_video_studio_tools(user_id=str(owner.id))

        async def edit(*operations: dict) -> str:
            answer = await executor.execute_tool(
                ToolCall(id="e", name="edit_video", arguments={"video_id": str(video_id), "operations": list(operations)})
            )
            assert not answer.is_error, answer.content
            return answer.content

        edited = await edit(_duration(6))
        assert "revision 2" in edited
        assert "Changed since their last preview: scene `open`, `mid`, `close`" in edited
        # Looked at as edited: the edited scene's preview is now the 6 s one.
        await _previewed(scratch.sessions, video_id, ["mid"])

        reverted = await edit(_duration(4))
        assert "revision 3" in reverted and "Changed since their last preview" not in reverted
        assert "Back to how an earlier preview drew them: scene `mid` - that preview counts again." in reverted

        again = await edit(_duration(4))
        assert again.startswith("No change: video **Brand film**")
        assert "at revision 3" in again and "nothing was saved, no revision was made" in again

        rendered = await executor.execute_tool(ToolCall(id="r", name="render_video", arguments={"video_id": str(video_id)}))
        assert not rendered.is_error, rendered.content

        async with scratch.sessions() as db:
            assert (await db.get(VideoProject, video_id)).revision == 3
            [render] = (await db.execute(select(VideoRender).where(VideoRender.project_id == video_id))).scalars().all()
            assert render.revision == 3 and render.gate_override is None
            updates = (
                await db.execute(
                    select(func.count())
                    .select_from(AuditLog)
                    .where(AuditLog.action == "update", AuditLog.entity_id == str(video_id))
                )
            ).scalar_one()
            assert updates == 2
