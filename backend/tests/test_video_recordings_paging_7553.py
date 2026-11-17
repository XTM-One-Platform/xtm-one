"""list_video_recordings pages and filters, get_video_recording resolves an id prefix.

A recording older than the 50 most recent is listed through ``offset`` or found by its name; a video's recordings are
listed through ``video_id``; an unambiguous id prefix of 8 or more characters reads the recording, an ambiguous one is
refused naming the candidates; a full id reads it as before.

The session double runs the statements the tool builds against an in-memory list of takes: the filters are read from
the compiled statement's parameters (the owner, the ids of a video, the name pattern, the id prefix), and its offset
and limit from the statement itself, so the paging and the filters the tool writes are what is exercised.
"""

from __future__ import annotations

import fnmatch
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy.dialects import postgresql

from app.models.video_studio import JOB_READY, VideoProject, VideoTake
from app.services.builtin_tools import video_studio as tool
from app.tools.base import ToolCall

OWNER = uuid.UUID("11111111-1111-4111-8111-111111111111")
OTHER = uuid.UUID("22222222-2222-4222-8222-222222222222")
START = datetime(2026, 11, 1, 8, 0, tzinfo=UTC)


def _take(n: int, *, owner: uuid.UUID = OWNER, take_id: uuid.UUID | None = None, name: str | None = None) -> VideoTake:
    return VideoTake(
        id=take_id or uuid.UUID(int=(0xA0 << 120) + n),
        user_id=owner,
        name=name or f"Take {n}",
        status=JOB_READY,
        duration_ms=4000,
        script={"steps": [{"action": "wait", "seconds": 1}]},
        events=[],
        created_at=START + timedelta(minutes=n),
    )


def _like(pattern: str, text: str, *, fold: bool) -> bool:
    glob = pattern.replace("\\%", "\0p").replace("\\_", "\0u").replace("%", "*").replace("_", "?")
    glob = glob.replace("\0p", "%").replace("\0u", "_")
    return fnmatch.fnmatchcase(text.lower() if fold else text, glob.lower() if fold else glob)


class _Session:
    def __init__(self, takes: list[VideoTake], project: VideoProject | None = None):
        self.takes = takes
        self.project = project
        self.statements: list = []

    async def get(self, model, key):
        if model is VideoTake:
            return next((t for t in self.takes if str(t.id) == str(key)), None)
        if model is VideoProject:
            return self.project if self.project and str(self.project.id) == str(key) else None
        return SimpleNamespace(id=OWNER, role="member")

    async def execute(self, statement):
        self.statements.append(statement)
        sql = str(statement.compile(dialect=postgresql.dialect()))
        params = statement.compile(dialect=postgresql.dialect()).params
        rows = list(self.takes)
        for key, value in params.items():
            if isinstance(value, uuid.UUID) and key.startswith("user_id"):
                rows = [t for t in rows if t.user_id == value]
            elif isinstance(value, list | tuple):
                wanted = {str(v) for v in value}
                rows = [t for t in rows if str(t.id) in wanted]
            elif isinstance(value, str) and key.startswith("name"):
                rows = [t for t in rows if _like(value, t.name or "", fold=True)]
            elif isinstance(value, str) and "CAST(video_takes.id AS VARCHAR) LIKE" in sql and value.endswith("%"):
                rows = [t for t in rows if _like(value, str(t.id), fold=False)]
        rows.sort(key=lambda t: (t.created_at, str(t.id)), reverse=True)
        start = statement._offset or 0
        end = start + statement._limit if statement._limit is not None else None
        page = rows[start:end]
        return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: page))


def _executor(monkeypatch, session: _Session):
    @asynccontextmanager
    async def opened():
        yield session

    monkeypatch.setattr(tool, "async_session", opened)

    async def readable(_db, _kind, asset, user):
        return asset.user_id == user.id or asset.user_id == OTHER

    async def visible(_db, project_id, _user):
        return session.project if session.project and str(session.project.id) == str(project_id) else None

    from app.services.video_studio import jobs, projects

    monkeypatch.setattr(projects, "can_read_project_asset", readable)
    monkeypatch.setattr(projects, "load_visible_project", visible)

    async def told(*_args, **_kwargs):
        return None

    monkeypatch.setattr(jobs, "mark_told", told)
    return tool.build_video_studio_tools(None, str(OWNER))[1]


async def _call(executor, name: str, **arguments):
    return await executor.execute_tool(ToolCall(id="c1", name=name, arguments=arguments))


# ── The id prefix ─────────────────────────────────────────────────────────


def test_an_id_prefix_is_read_in_its_canonical_form():
    assert tool.recording_id_prefix("8A3F2B1C") == "8a3f2b1c"
    assert tool.recording_id_prefix("8a3f2b1c9d") == "8a3f2b1c-9d"
    assert tool.recording_id_prefix("8a3f2b1c-9d") == "8a3f2b1c-9d"
    assert tool.recording_id_prefix(" 8a3f2b1c- ") == "8a3f2b1c"
    assert tool.recording_id_prefix("8a3f2b1") is None  # too short
    assert tool.recording_id_prefix("8a3f-2b1c") is None  # a hyphen out of place
    assert tool.recording_id_prefix("8a3f2b1z") is None
    assert tool.recording_id_prefix("") is None


async def test_a_unique_prefix_reads_the_recording_and_a_full_id_is_unchanged(monkeypatch):
    target = _take(1, take_id=uuid.UUID("8a3f2b1c-9d00-4000-8000-000000000001"), name="Graph layout")
    others = [_take(n) for n in range(2, 6)]
    executor = _executor(monkeypatch, _Session([target, *others]))

    by_prefix = await _call(executor, "get_video_recording", recording_id="8a3f2b1c", include_contact_sheet=False)
    assert not by_prefix.is_error, by_prefix.content
    assert f"(`{target.id}`)" in by_prefix.content

    by_id = await _call(executor, "get_video_recording", recording_id=str(target.id), include_contact_sheet=False)
    assert not by_id.is_error and f"(`{target.id}`)" in by_id.content


async def test_an_ambiguous_prefix_is_refused_naming_the_candidates(monkeypatch):
    first = _take(1, take_id=uuid.UUID("8a3f2b1c-0000-4000-8000-000000000001"), name="First")
    second = _take(2, take_id=uuid.UUID("8a3f2b1c-1111-4000-8000-000000000002"), name="Second")
    executor = _executor(monkeypatch, _Session([first, second]))

    answer = await _call(executor, "get_video_recording", recording_id="8a3f2b1c")
    assert answer.is_error
    assert "is ambiguous" in answer.content
    assert str(first.id) in answer.content and str(second.id) in answer.content

    longer = await _call(executor, "get_video_recording", recording_id="8a3f2b1c-1", include_contact_sheet=False)
    assert not longer.is_error and f"(`{second.id}`)" in longer.content


async def test_a_prefix_never_resolves_another_user_s_recording(monkeypatch):
    theirs = _take(1, owner=uuid.uuid4(), take_id=uuid.UUID("8a3f2b1c-9d00-4000-8000-000000000001"))
    executor = _executor(monkeypatch, _Session([theirs]))

    answer = await _call(executor, "get_video_recording", recording_id="8a3f2b1c")
    assert answer.is_error and "No recording `8a3f2b1c` you can read." in answer.content


async def test_a_short_prefix_answers_as_before(monkeypatch):
    executor = _executor(monkeypatch, _Session([_take(1, take_id=uuid.UUID("8a3f2b1c-9d00-4000-8000-000000000001"))]))
    answer = await _call(executor, "get_video_recording", recording_id="8a3f")
    assert answer.is_error and "No recording `8a3f` you can read." in answer.content


# ── The listing ───────────────────────────────────────────────────────────


async def test_a_recording_200_recordings_ago_is_listed_through_offset(monkeypatch):
    takes = [_take(n) for n in range(1, 251)]
    executor = _executor(monkeypatch, _Session(takes))

    first = await _call(executor, "list_video_recordings", limit=50)
    assert "(1 to 50, newest first)" in first.content
    assert "More remain: call again with offset=50." in first.content
    assert "**Take 250**" in first.content and "**Take 200**" not in first.content

    old = await _call(executor, "list_video_recordings", limit=50, offset=200)
    assert "(201 to 250, newest first)" in old.content
    assert "**Take 50**" in old.content and "**Take 1**" in old.content
    assert "No more recordings past these." in old.content


async def test_the_limit_stays_capped_at_50(monkeypatch):
    executor = _executor(monkeypatch, _Session([_take(n) for n in range(1, 80)]))
    answer = await _call(executor, "list_video_recordings", limit=500)
    assert "(1 to 50, newest first)" in answer.content
    assert answer.content.count("\n- **") == 50


async def test_a_recording_is_found_by_its_name(monkeypatch):
    takes = [_take(n) for n in range(1, 251)]
    takes[10].name = "XTM blast requalify (retake)"
    executor = _executor(monkeypatch, _Session(takes))

    answer = await _call(executor, "list_video_recordings", name_contains="blast REQUALIFY")
    assert f"(`{takes[10].id}`)" in answer.content
    assert answer.content.count("\n- **") == 1
    assert "named like 'blast REQUALIFY'" in answer.content

    literal = await _call(executor, "list_video_recordings", name_contains="100%")
    assert "No recording named like '100%' from offset 0." in literal.content


async def test_a_video_s_recordings_are_listed_through_video_id(monkeypatch):
    placed = _take(3, name="Placed")
    shared = _take(4, owner=OTHER, name="Shared by a colleague")
    hidden = _take(5, owner=uuid.uuid4(), name="Not readable")
    project = VideoProject(
        id=uuid.uuid4(),
        title="Launch",
        spec={
            "scenes": [
                {"type": "recording", "take_id": str(placed.id)},
                {"type": "recording", "take_id": str(shared.id)},
                {"type": "recording", "take_id": str(hidden.id)},
            ]
        },
    )
    executor = _executor(monkeypatch, _Session([_take(1), placed, shared, hidden], project))

    answer = await _call(executor, "list_video_recordings", video_id=str(project.id))
    assert answer.content.startswith(f"Recordings in video `{project.id}`")
    assert f"(`{placed.id}`)" in answer.content and f"(`{shared.id}`)" in answer.content
    assert str(hidden.id) not in answer.content and "**Take 1**" not in answer.content

    missing = await _call(executor, "list_video_recordings", video_id=str(uuid.uuid4()))
    assert missing.is_error and "you can open" in missing.content


def test_the_listing_schema_takes_offset_name_and_video():
    definitions = tool.build_video_studio_tools(None, str(OWNER))[0]
    listing = next(d for d in definitions if d.name == "list_video_recordings")
    assert set(listing.input_schema["properties"]) == {"limit", "offset", "name_contains", "video_id"}


@pytest.mark.parametrize("bad", ["many", "1.5x"])
async def test_a_bad_offset_is_refused(monkeypatch, bad):
    executor = _executor(monkeypatch, _Session([_take(1)]))
    answer = await _call(executor, "list_video_recordings", offset=bad)
    assert answer.is_error and "whole numbers" in answer.content
