"""Video Studio job and AI shot ids given as an id prefix (8+ hexadecimal characters).

* ``jobs.resolve_job_ids`` keeps a full id, takes a prefix for the one job it
  starts among those the caller may read, refuses a prefix of several naming
  them, and answers anything else not found, never dropped in silence;
* ``get_video_jobs`` with ids never falls back to the in-flight listing, and
  echoes the full id a prefix stood for;
* ``generate_video_clip(clip_id=<prefix>)`` looks the prefix up among the
  caller's own AI shots.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy.dialects import postgresql

from app.services.builtin_tools import video_studio as tool
from app.services.video_studio import jobs
from app.tools.base import ToolCall

USER_ID = uuid.UUID("11111111-2222-4333-8444-555555555555")
FULL = "48b4e177-0b0c-4d1e-9f00-1234567890ab"
OTHER = "48b4e177-ffff-4d1e-9f00-1234567890ab"


class _Db:
    """Answers each query with *rows* and keeps the statements it ran."""

    def __init__(self, rows):
        self.rows = rows
        self.statements: list = []

    async def execute(self, stmt):
        self.statements.append(stmt)
        rows = self.rows
        return SimpleNamespace(all=lambda: rows)


def _user():
    return SimpleNamespace(id=USER_ID, role="user")


@pytest.mark.asyncio
async def test_a_full_id_is_kept_and_reads_nothing():
    db = _Db([])
    named = await jobs.resolve_job_ids(db, {"clip": [FULL.upper()]}, _user())
    assert named.ids == {"clip": [FULL]}
    assert (named.resolved, named.not_found, named.ambiguous) == ({}, [], None)
    assert db.statements == []


@pytest.mark.asyncio
async def test_a_prefix_of_one_own_shot_stands_for_its_full_id():
    db = _Db([(uuid.UUID(FULL), "Lobby")])
    named = await jobs.resolve_job_ids(db, {"clip": ["48b4e177"]}, _user())
    assert named.ids == {"clip": [FULL]}
    assert named.resolved == {"48b4e177": FULL}
    sql = str(db.statements[0].compile(dialect=postgresql.dialect()))
    # A range on the primary key, inside the owner clause the jobs are read under.
    assert "BETWEEN" in sql and "user_id" in sql


@pytest.mark.asyncio
async def test_a_prefix_of_two_jobs_is_refused_naming_both():
    db = _Db([(uuid.UUID(FULL), "One"), (uuid.UUID(OTHER), "Two")])
    named = await jobs.resolve_job_ids(db, {"music": ["48b4e177"]}, _user())
    assert named.ambiguous is not None
    assert FULL in named.ambiguous and OTHER in named.ambiguous
    assert "AI tracks" in named.ambiguous


@pytest.mark.asyncio
async def test_a_prefix_of_nothing_and_a_value_that_is_no_id_are_not_found():
    named = await jobs.resolve_job_ids(_Db([]), {"take": ["deadbeef", "nope"]}, _user())
    assert named.ids == {"take": []}
    assert named.not_found == ["deadbeef", "nope"]


@pytest.mark.asyncio
async def test_a_render_prefix_is_looked_up_through_the_videos_the_caller_can_open(monkeypatch):
    from app.api.v1 import helpers

    async def groups(_db, _user_id):
        return []

    monkeypatch.setattr(helpers, "get_user_group_ids", groups)
    db = _Db([(uuid.UUID(FULL), "")])
    named = await jobs.resolve_job_ids(db, {"render": ["48b4e177"]}, _user())
    assert named.ids == {"render": [FULL]}
    sql = str(db.statements[0].compile(dialect=postgresql.dialect()))
    assert "project_id IN" in sql


class _Session:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


@pytest.fixture
def jobs_tool(monkeypatch):
    """``get_video_jobs`` of a signed-in caller, the in-flight listing and the wait recorded."""
    seen: dict = {}

    async def find_user(_db, _user_id):
        return _user()

    async def in_flight(_user_id):
        seen["listed"] = True
        return {}

    async def wait_for_jobs(groups, *, timeout, told_to=None):
        seen.setdefault("waited", []).append(dict(groups))
        return {}, "all ended"

    monkeypatch.setattr(tool, "async_session", lambda: _Session())
    monkeypatch.setattr(tool, "_user", find_user)
    monkeypatch.setattr(jobs, "jobs_in_flight", in_flight)
    monkeypatch.setattr(jobs, "wait_for_jobs", wait_for_jobs)
    _definitions, executor = tool.build_video_studio_tools(None, str(USER_ID))

    async def call(**arguments):
        return await executor.execute_tool(ToolCall(id="c1", name="get_video_jobs", arguments=arguments))

    return call, seen


@pytest.mark.asyncio
async def test_get_video_jobs_with_an_unknown_prefix_answers_not_found_never_the_listing(monkeypatch, jobs_tool):
    call, seen = jobs_tool

    async def resolve(_db, asked, _user):
        return jobs.JobIds({k: [] for k in asked}, {}, ["deadbeef"])

    monkeypatch.setattr(jobs, "resolve_job_ids", resolve)
    result = await call(clip_ids=["deadbeef"])
    assert result.is_error
    assert "deadbeef" in result.content
    assert "listed" not in seen
    assert "in flight" not in result.content


@pytest.mark.asyncio
async def test_get_video_jobs_refuses_an_ambiguous_prefix_before_any_wait(monkeypatch, jobs_tool):
    call, seen = jobs_tool

    async def resolve(_db, asked, _user):
        return jobs.JobIds({k: [] for k in asked}, {}, [], f"Error: `48b4e177` starts {FULL} and {OTHER}.")

    monkeypatch.setattr(jobs, "resolve_job_ids", resolve)
    result = await call(clip_ids=["48b4e177"])
    assert result.is_error
    assert FULL in result.content and OTHER in result.content
    assert "waited" not in seen and "listed" not in seen


@pytest.mark.asyncio
async def test_get_video_jobs_waits_on_the_full_id_a_prefix_stands_for(monkeypatch, jobs_tool):
    call, seen = jobs_tool

    async def resolve(_db, asked, _user):
        return jobs.JobIds({**{k: [] for k in asked}, "clip": [FULL]}, {"48b4e177": FULL}, [])

    monkeypatch.setattr(jobs, "resolve_job_ids", resolve)
    await call(clip_ids=["48b4e177"])
    assert "listed" not in seen
    assert seen["waited"][0]["clip"] == [FULL]


@pytest.mark.asyncio
async def test_a_clip_id_prefix_is_looked_up_among_the_callers_own_shots():
    db = _Db([(uuid.UUID(FULL), "Lobby")])
    value, refusal = await tool._resolve_clip_id_prefix(db, "48b4e177", _user())
    assert (value, refusal) == (FULL, None)
    sql = str(db.statements[0].compile(dialect=postgresql.dialect()))
    assert "user_id" in sql


@pytest.mark.asyncio
async def test_a_clip_id_prefix_of_two_shots_is_refused_and_a_full_id_reads_nothing():
    db = _Db([(uuid.UUID(FULL), "One"), (uuid.UUID(OTHER), "Two")])
    value, refusal = await tool._resolve_clip_id_prefix(db, "48b4e177", _user())
    assert value == "48b4e177"
    assert refusal is not None and FULL in refusal and OTHER in refusal and "AI shots" in refusal
    empty = _Db([])
    assert await tool._resolve_clip_id_prefix(empty, FULL, _user()) == (FULL, None)
    assert empty.statements == []
