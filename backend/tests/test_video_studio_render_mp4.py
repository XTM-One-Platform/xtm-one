"""A whole render's MP4 is reachable for QA before the video is called final. Fakes only.

XTM Blast, 20 Oct 2026: ``render_video`` and ``get_video_jobs`` gave the poster
only, so no frame of the 185 s render could be looked at but that one.

* the answer of a ready render - and of a failed one, its MP4 kept - names
  its MP4's file_id with how to save the frame at any moment; ``get_video``'s
  render list names each render's MP4 and tells a scene preview apart;
* ``save_video_frame`` takes a render's MP4 from anyone who can open its
  video, as the media route serves it, whoever rendered it - never from
  someone who cannot, and never a video file no render made.
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from types import SimpleNamespace

import pytest

from app.models.file_attachment import FileAttachment
from app.models.video_studio import JOB_FAILED, JOB_READY, VideoRender
from app.services.builtin_tools import video_studio as tool
from app.services.video_studio import clips, jobs, projects
from app.tools.base import ToolCall

READER = uuid.UUID("00000000-0000-0000-0000-00000000a170")
MAKER = uuid.UUID("00000000-0000-0000-0000-00000000b170")
VIDEO = uuid.UUID("00000000-0000-0000-0000-00000000c170")
MP4 = uuid.UUID("00000000-0000-0000-0000-00000000d170")


def _render(status: str = JOB_READY, **values) -> VideoRender:
    fields = {
        "id": uuid.uuid4(),
        "project_id": VIDEO,
        "user_id": MAKER,
        "status": status,
        "quality": "ultra",
        "revision": 12,
        "width": 3840,
        "height": 2160,
        "fps": 60,
        "duration_ms": 185_000,
        "size_bytes": 412_000_000,
        "file_id": MP4,
        "poster_file_id": None,
        "scene_id": None,
        "keep": False,
    }
    return VideoRender(**{**fields, **values})


def _answer(monkeypatch, row: VideoRender) -> str:
    """``get_video_jobs(render_ids=[...])`` on *row*, as the person who asked."""

    async def wait_for_jobs(_groups, *, timeout, told_to=None):
        return {"render": [row]}, jobs.WAIT_BUDGET

    @contextlib.asynccontextmanager
    async def session():
        yield None

    async def find_user(_db, _user_id):
        return SimpleNamespace(id=READER, role="user")

    async def visible(_db, _video_id, _user):
        return SimpleNamespace(id=VIDEO)

    monkeypatch.setattr(jobs, "wait_for_jobs", wait_for_jobs)
    monkeypatch.setattr(tool, "async_session", session)
    monkeypatch.setattr(tool, "_user", find_user)
    monkeypatch.setattr(tool, "_wait_budget", lambda _default: 0.0)
    monkeypatch.setattr(projects, "load_visible_project", visible)
    _defs, executor = tool.build_video_studio_tools(user_id=str(READER))
    result = asyncio.run(executor.execute_tool(ToolCall(id="j", name="get_video_jobs", arguments={"render_ids": [str(row.id)]})))
    return result.content


def test_a_ready_render_names_its_mp4_and_how_to_look_at_any_moment(monkeypatch):
    said = _answer(monkeypatch, _render())
    assert f"Its MP4 is file `{MP4}`: save_video_frame(file_id='{MP4}', at_seconds=t) saves its frame at any moment t" in said
    assert "up to 185.0 s as a picture to look at." in said and "The poster frame is attached." in said


def test_get_videos_list_names_each_renders_mp4():
    ready = _render()
    line = tool.render_list_line(ready, SimpleNamespace(status=jobs.STATE_READY), current=ready.id)
    assert line == f"- render `{ready.id}`: ready ultra rev 12, 185.0 s 3840x2160, MP4 file `{MP4}` - current (what the video plays)"
    # A failed render keeps its MP4 to look at; a scene preview is said to be one.
    failed = _render(JOB_FAILED, scene_id="s05", quality="draft")
    said = tool.render_list_line(failed, SimpleNamespace(status=jobs.STATE_FAILED, reason="its narration failed"), current=None)
    assert said == f"- render `{failed.id}`: failed draft rev 12: its narration failed, MP4 file `{MP4}` (scene `s05` preview)"
    unmade = _render(JOB_FAILED, file_id=None)
    assert "MP4" not in tool.render_list_line(unmade, SimpleNamespace(status=jobs.STATE_FAILED, reason="no voice"), current=None)


class _Db:
    """The MP4's attachment row, and the render that made it (``None``: no render did)."""

    def __init__(self, render: VideoRender | None) -> None:
        self.render = render

    async def get(self, model, _key):
        if model is FileAttachment:
            return FileAttachment(id=MP4, user_id=MAKER, filename="xtm-blast-1a2b3c4d.mp4", content_type="video/mp4")
        return None

    async def execute(self, _statement):
        return SimpleNamespace(scalars=lambda: SimpleNamespace(first=lambda: self.render))


@pytest.fixture
def opens(monkeypatch):
    """Who can open the video: the reader, through its sharing; a file they do not own is otherwise unreadable."""
    world = SimpleNamespace(readers={READER})

    async def can_read_file(row, user, _db):
        return row.user_id == user.id

    async def visible(_db, project_id, user):
        return SimpleNamespace(id=project_id) if user.id in world.readers else None

    monkeypatch.setattr("app.services.file_access.can_read_file", can_read_file)
    monkeypatch.setattr(projects, "load_visible_project", visible)
    return world


def test_a_renders_mp4_is_framed_for_anyone_who_can_open_its_video(opens):
    reader = SimpleNamespace(id=READER, role="user")
    source = asyncio.run(clips.resolve_frame(_Db(_render()), reader, file_id=str(MP4), at_seconds=42.0))
    assert source == {
        "kind": "file",
        "id": str(MP4),
        "file_id": str(MP4),
        "name": "xtm-blast-1a2b3c4d.mp4",
        "at_seconds": 42.0,
    }
    # Someone the video is not shared with, and a video file no render made, are refused alike.
    stranger = SimpleNamespace(id=uuid.uuid4(), role="user")
    with pytest.raises(clips.ClipError, match="you can read"):
        asyncio.run(clips.resolve_frame(_Db(_render()), stranger, file_id=str(MP4)))
    with pytest.raises(clips.ClipError, match="you can read"):
        asyncio.run(clips.resolve_frame(_Db(None), reader, file_id=str(MP4)))
    # Its maker reads it as a file of theirs.
    maker = SimpleNamespace(id=MAKER, role="user")
    assert asyncio.run(clips.resolve_frame(_Db(None), maker, file_id=str(MP4)))["file_id"] == str(MP4)
