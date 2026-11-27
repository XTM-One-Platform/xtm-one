"""The trace link of a turn resumed after a platform stop.

The resumed turn's root trace carries ``resumed_from`` (the cut turn's trace,
the cause, the attempt n of the ceiling), and the cut turn's stored trace is given
``resumed_as`` (the resumed turn's trace) once the resume opened it. Only the
first trace opened in the resume takes the link; a nested consult keeps its
own metadata.
"""

from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.dialects import postgresql

from app.services import cut_turn_resume, parent_wake, turn_checkpoint
from app.services import langfuse_tracing as lft
from app.services.cut_turn_resume import (
    MAX_TOTAL_AUTO_RESUMES,
    RESUMED_AS_TRACE_KEY,
    RESUMED_FROM_META_KEY,
    CutTurn,
)

CONV = uuid.UUID("11111111-1111-1111-1111-111111111111")
USER = uuid.UUID("22222222-2222-2222-2222-222222222222")
MSG = uuid.UUID("33333333-3333-3333-3333-333333333333")
CUT_TRACE = uuid.UUID("44444444-4444-4444-4444-444444444444")
MSG_AT = datetime(2026, 11, 1, 0, 59, tzinfo=UTC)


def _cut(resumes: int = 0, entry: str = "web") -> CutTurn:
    return CutTurn(str(CONV), str(USER), str(MSG), "turn-1", "stop", resumes, "2026-11-01T01:00:00+00:00", entry)


# ── The resumed turn's trace ─────────────────────────────────────────────


def test_the_resumed_trace_names_the_cut_trace_cause_and_attempt():
    meta = cut_turn_resume.resumed_from_trace_meta(_cut(resumes=1, entry="platform_api"), cut_trace_id=CUT_TRACE)

    link = meta[RESUMED_FROM_META_KEY]
    assert link["trace_id"] == str(CUT_TRACE)
    assert link["cause"] == "stop"
    assert link["resume"] == 2 and link["of"] == MAX_TOTAL_AUTO_RESUMES
    assert link["entry"] == "platform_api"
    assert link["turn_id"] == "turn-1"


def test_an_unknown_cut_trace_is_missing_never_wrong():
    meta = cut_turn_resume.resumed_from_trace_meta(_cut(), cut_trace_id=None)

    assert meta[RESUMED_FROM_META_KEY]["trace_id"] is None


@pytest.fixture
def local_tracing(monkeypatch):
    """Langfuse unavailable: the durable local trace alone (the path the Trace Explorer reads)."""

    async def _no_client(_db):
        return None

    monkeypatch.setattr(lft, "get_langfuse_client", _no_client)
    monkeypatch.setattr(lft, "_resolve_user_group_ids", AsyncMock(return_value=[]))


async def test_only_the_first_trace_of_the_resume_takes_the_link(local_tracing):
    with lft.next_root_trace({"resumed_from": {"trace_id": str(CUT_TRACE)}}) as link:
        first = await lft.trace_llm_call(None, name="agent-chat:a", user_id=str(USER), source="agent-chat")
        nested = await lft.trace_llm_call(None, name="agent-chat:b", user_id=str(USER), source="agent-chat")

    assert first["metadata"]["resumed_from"] == {"trace_id": str(CUT_TRACE)}
    assert first["local_metadata"]["resumed_from"] == {"trace_id": str(CUT_TRACE)}
    assert link.trace_id == first["local_trace_id"]
    assert "resumed_from" not in nested["metadata"]


async def test_a_trace_opened_outside_a_resume_carries_no_link(local_tracing):
    with lft.next_root_trace({"resumed_from": {"trace_id": "x"}}):
        pass

    ctx = await lft.trace_llm_call(None, name="agent-chat:a", user_id=str(USER), source="agent-chat")

    assert "resumed_from" not in ctx["metadata"]


# ── The cut turn's trace ─────────────────────────────────────────────────


class _Db:
    def __init__(self, rowcount: int = 1, fail: bool = False):
        self.statements: list = []
        self.commits = 0
        self.rollbacks = 0
        self._rowcount = rowcount
        self._fail = fail

    async def execute(self, stmt):
        if self._fail:
            raise RuntimeError("database gone")
        self.statements.append(stmt)
        return SimpleNamespace(rowcount=self._rowcount, scalar_one_or_none=lambda: CUT_TRACE)

    async def commit(self):
        self.commits += 1

    async def rollback(self):
        self.rollbacks += 1


async def test_the_cut_trace_is_given_resumed_as_by_a_jsonb_merge():
    db = _Db()
    resumed = uuid.uuid4()

    assert await cut_turn_resume.mark_trace_resumed_as(db, CUT_TRACE, resumed) is True

    [stmt] = db.statements
    sql = str(stmt.compile(dialect=postgresql.dialect()))
    # Merged into the stored metadata, never replacing it.
    assert "UPDATE llm_traces" in sql and "||" in sql and "coalesce" in sql
    params = stmt.compile(dialect=postgresql.dialect()).params
    assert {RESUMED_AS_TRACE_KEY: str(resumed)} in params.values()
    assert db.commits == 1


@pytest.mark.parametrize(("cut_trace", "resumed"), [(None, uuid.uuid4()), (CUT_TRACE, None)])
async def test_no_link_is_written_without_both_traces(cut_trace, resumed):
    db = _Db()

    assert await cut_turn_resume.mark_trace_resumed_as(db, cut_trace, resumed) is False
    assert db.statements == []


async def test_linking_the_traces_never_raises_and_leaves_the_session_usable():
    db = _Db(fail=True)
    assert await cut_turn_resume.mark_trace_resumed_as(db, CUT_TRACE, uuid.uuid4()) is False
    # The runner discards the checkpoint on the same session next.
    assert db.rollbacks == 1
    assert await cut_turn_resume.cut_turn_trace_id(_Db(fail=True), _cut()) is None


async def test_the_cut_trace_is_the_conversation_s_latest_root_trace_before_the_cut():
    db = _Db()

    assert await cut_turn_resume.cut_turn_trace_id(db, _cut()) == CUT_TRACE

    sql = str(db.statements[0].compile(dialect=postgresql.dialect()))
    assert "llm_traces.parent_trace_id IS NULL" in sql
    # A consult flushed before its parent names it in its metadata only.
    assert "llm_traces.metadata ->> " in sql and "IS NULL" in sql.split("llm_traces.metadata ->> ")[1]
    assert "llm_traces.started_at <=" in sql
    assert "ORDER BY llm_traces.started_at DESC" in sql


async def test_the_cut_trace_lookup_never_reaches_a_turn_before_the_cut_message():
    db = _Db()

    await cut_turn_resume.cut_turn_trace_id(db, _cut(), not_before=MSG_AT)

    sql = str(db.statements[0].compile(dialect=postgresql.dialect()))
    assert "llm_traces.started_at >=" in sql


# ── The runner links both ────────────────────────────────────────────────


class _Result:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


class _Session:
    def __init__(self, answers):
        self._answers = list(answers)

    async def execute(self, _stmt):
        return _Result(self._answers.pop(0))

    async def commit(self):
        return None


@pytest.fixture
def runner(monkeypatch):
    conv = SimpleNamespace(
        id=CONV, user_id=USER, agent_id=None, channel="web", channel_thread_id=None, tool_overrides_json=None
    )
    msg = SimpleNamespace(
        id=MSG, role="user", content="research the quarterly risks", sequence=7, created_at=MSG_AT
    )
    state = SimpleNamespace(links=[], opened=None, cancelled=False, not_before=None)

    @asynccontextmanager
    async def _session():
        yield _Session([conv, msg, None])

    import app.core.database as database

    monkeypatch.setattr(database, "async_session", _session)
    monkeypatch.setattr(parent_wake, "_stopped_since", AsyncMock(return_value=False))

    async def _load_replay(db, conversation_id, *, user_id=None, now=None, code_interpreter=False):
        return turn_checkpoint.ResumedWork(found=False), []

    monkeypatch.setattr(turn_checkpoint, "load_replay", _load_replay)

    async def _trace_of_cut(db, cut, *, not_before=None):
        state.not_before = not_before
        return CUT_TRACE

    async def _mark(db, cut_trace, resumed_trace):
        state.links.append((cut_trace, resumed_trace))
        return True

    monkeypatch.setattr(cut_turn_resume, "cut_turn_trace_id", _trace_of_cut)
    monkeypatch.setattr(cut_turn_resume, "mark_trace_resumed_as", _mark)

    async def _run(db, **kw):
        # What run_agent_chat does: the first trace it opens takes the link.
        link = lft._take_root_trace_link()
        state.opened = link
        if link is not None:
            link.trace_id = uuid.UUID("55555555-5555-5555-5555-555555555555")
        return False

    monkeypatch.setattr(parent_wake, "_run_parent_wake_turn", _run)
    return state


async def test_the_resume_links_its_trace_and_the_cut_one(runner):
    assert await parent_wake.resume_cut_turn(_cut(resumes=2)) is True

    link = runner.opened
    assert link is not None
    assert link.metadata[RESUMED_FROM_META_KEY]["trace_id"] == str(CUT_TRACE)
    assert link.metadata[RESUMED_FROM_META_KEY]["resume"] == 3
    assert runner.links == [(CUT_TRACE, uuid.UUID("55555555-5555-5555-5555-555555555555"))]
    # The lookup is bounded by the cut message: an older turn's trace is never taken.
    assert runner.not_before == MSG_AT


async def test_a_resume_that_opened_no_trace_writes_no_link(runner, monkeypatch):
    async def _run(db, **kw):
        return True

    monkeypatch.setattr(parent_wake, "_run_parent_wake_turn", _run)

    assert await parent_wake.resume_cut_turn(_cut()) is False
    assert runner.links == []


async def test_each_resume_logs_one_warning_naming_the_cut_trace(runner, caplog):
    with caplog.at_level("WARNING", logger=parent_wake.logger.name):
        await parent_wake.resume_cut_turn(_cut(resumes=1))

    [record] = [r for r in caplog.records if "Resuming the chat turn" in r.getMessage()]
    message = record.getMessage()
    assert str(CONV) in message and "stop" in message
    assert f"automatic resume 2 of {MAX_TOTAL_AUTO_RESUMES}" in message
    assert str(CUT_TRACE) in message
