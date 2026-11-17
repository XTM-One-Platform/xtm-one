"""``get_video_jobs`` never prints its pictures' internals into the agent-facing text. Fakes only.

Retest of xtm-one #10480 (9 Nov 2026): a ready render with check frames of its
scenes answered a raw ``_Pictures(... every=[FileRef(..., s3_object_name=...)])``
after "The poster frame is attached.", compact and verbose alike: the variable
holding the count of linked files was rebound to the scene frames' pictures, so
the "N more file(s)" sentence printed their repr. Three renders made an answer
of well over 15,000 characters, S3 object names included.
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from types import SimpleNamespace

import pytest

from app.llm.base import FileRef
from app.models.video_studio import JOB_READY, VideoRender
from app.services.builtin_tools import video_studio as tool
from app.services.video_studio import jobs, projects
from app.tools.base import ToolCall

READER = uuid.UUID("00000000-0000-0000-0000-00000000a171")
VIDEO = uuid.UUID("00000000-0000-0000-0000-00000000c171")
#: The answer budget of a multi-render call the retest named.
BUDGET = 15_000


def _render(frames: int) -> VideoRender:
    return VideoRender(
        id=uuid.uuid4(),
        project_id=VIDEO,
        user_id=READER,
        status=JOB_READY,
        quality="ultra",
        revision=3,
        width=3840,
        height=2160,
        fps=60,
        duration_ms=185_000,
        size_bytes=412_000_000,
        file_id=uuid.uuid4(),
        poster_file_id=uuid.uuid4(),
        frame_file_ids=[uuid.uuid4() for _ in range(frames)],
        scene_id=None,
        keep=False,
    )


async def _fake_file_refs(file_ids, *, show=True, shown=None, **_kwargs):
    """Every asked id as a file of the conversation, with the S3 name a real row carries."""
    pictures = tool._Pictures()
    for fid in (str(f) for f in file_ids if f):
        ref = FileRef(
            file_id=fid,
            filename=f"frame-{fid[:8]}.jpg",
            content_type="image/jpeg",
            size=180_000,
            s3_object_name=f"generated/{fid}.jpg",
            source="agent_check",
        )
        pictures.ids.append(fid)
        pictures.copies[fid] = fid
        pictures.every.append(ref)
        if show:
            pictures.refs.append(ref)
    return pictures


def _answer(monkeypatch, rows: list[VideoRender], **arguments) -> str:
    async def wait_for_jobs(_groups, *, timeout, told_to=None):
        return {"render": rows}, jobs.WAIT_BUDGET

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
    monkeypatch.setattr(tool, "_file_refs", _fake_file_refs)
    monkeypatch.setattr(projects, "load_visible_project", visible)
    _defs, executor = tool.build_video_studio_tools(user_id=str(READER), conversation_id=str(uuid.uuid4()))
    call = ToolCall(id="j", name="get_video_jobs", arguments={"render_ids": [str(r.id) for r in rows], **arguments})
    return asyncio.run(executor.execute_tool(call)).content


@pytest.mark.parametrize("verbose", [False, True])
def test_a_ready_renders_answer_names_its_scene_frames_never_their_repr(monkeypatch, verbose):
    rows = [_render(21), _render(9), _render(12)]
    said = _answer(monkeypatch, rows, verbose=verbose)
    assert "The poster frame is attached." in said
    for leaked in ("FileRef(", "_Pictures(", "s3_object_name", "generated/"):
        assert leaked not in said
    if not verbose:
        assert len(said) < BUDGET, len(said)
    else:
        # The verbose answer still names each scene's check frames by their file ids.
        assert all(f"`{rows[0].frame_file_ids[i]}`" in said for i in range(21))
