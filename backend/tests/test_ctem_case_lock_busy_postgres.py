"""A CTEM case write that waits on a row lock answers busy, not an opaque error (#2517).

Every write of a case reads its row ``FOR UPDATE`` (#2373). Without a lock timeout
the wait lasted the connection's whole 30 s ``statement_timeout`` and the tool
answered "the case could not be changed right now" with no cause. The wait is now
bounded by ``ctem_cases_store.CASE_LOCK_TIMEOUT`` and answered "Busy ... retry",
with nothing written. Long notes are stored whole, in parallel calls too.
The lock needs two real transactions: it does not reproduce on SQLite or a fake.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import app.models  # noqa: F401 - registers every mapper on Base.metadata
from app.models.ctem_case import CtemCase, CtemCaseEvent, CtemCaseIssue
from app.models.organization import Organization
from app.services import ctem_cases as rules
from app.services import ctem_cases_store as store
from app.services import ctem_cases_view as view_of
from app.services.builtin_tools import build_ctem_case_tools
from app.tools.base import ToolCall

pytestmark = pytest.mark.integration

ISSUE = rules.SourceIssue(source="opencti", external_id="vuln--1", name="CVE-2026-1", cvss=9.8)


@asynccontextmanager
async def _database() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    from tests.scratch_database import ALL_TABLES, scratch_database

    async with scratch_database("ctemlockbusy", tables=ALL_TABLES, extensions=("vector",)) as db:
        yield db.sessions


async def _seed(sessions: async_sessionmaker[AsyncSession]) -> tuple[uuid.UUID, uuid.UUID]:
    async with sessions() as session:
        org = Organization(id=uuid.uuid4(), name="Org", role="primary")
        session.add(org)
        await session.flush()
        case = CtemCase(
            organization_id=org.id,
            vertical="cyber",
            title="Upgrade Apache Tomcat to 9.0.98",
            remediation_kind="upgrade",
            keys=["upgrade:apache/tomcat@9.0.98"],
            pinned_issues=[],
            state="analyzing",
            state_since=datetime.now(UTC) - timedelta(days=1),
        )
        session.add(case)
        await session.flush()
        session.add(CtemCaseIssue(case_id=case.id, source=ISSUE.source, external_id=ISSUE.external_id, open=True))
        await session.commit()
        return org.id, case.id


@pytest.fixture
def world(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """The acting user sees the case's issue and may act on it."""
    seen = SimpleNamespace(org=None)

    async def _primary(_db: object) -> object:
        return SimpleNamespace(id=seen.org)

    async def _viewer(_db: object, _user_id: object, _vertical: object, **_kw: object) -> view_of.ViewerView:
        return view_of.ViewerView(issues={ISSUE.member_id: ISSUE}, open_ids={ISSUE.member_id})

    async def _acts(*_a: object, **_kw: object) -> bool:
        return True

    monkeypatch.setattr("app.services.organizations.get_primary", _primary)
    monkeypatch.setattr(view_of, "viewer_view", _viewer)
    monkeypatch.setattr(view_of, "acts_on_case", _acts)
    return seen


def _caller(name: str = "update_ctem_case") -> Callable[..., Awaitable[str]]:
    _defs, executor = build_ctem_case_tools(user_id=str(uuid.uuid4()), agent_id=str(uuid.uuid4()))

    async def call(**args: object) -> str:
        result = await executor.execute_tool(ToolCall(id=uuid.uuid4().hex, name=name, arguments=args))
        return result.content if isinstance(result.content, str) else str(result.content)

    return call


async def _notes(sessions: async_sessionmaker[AsyncSession], case_id: uuid.UUID) -> list[str]:
    async with sessions() as session:
        rows = (
            await session.execute(
                select(CtemCaseEvent).where(CtemCaseEvent.case_id == case_id, CtemCaseEvent.kind == "note")
            )
        ).scalars()
        return [str(r.reason) for r in rows]


async def test_a_write_waiting_on_a_held_case_row_answers_busy_fast_and_writes_nothing(
    world: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(store, "CASE_LOCK_TIMEOUT", "500ms")
    async with _database() as sessions:
        world.org, case_id = await _seed(sessions)
        call = _caller()
        async with sessions() as holder:
            # Another transaction holds the case row (a background pass, another session).
            await holder.execute(select(CtemCase).where(CtemCase.id == case_id).with_for_update())
            started = time.monotonic()
            answer = await call(case_id=str(case_id), note="checked the advisory", state="remediating")
            waited = time.monotonic() - started
            await holder.rollback()

        assert answer.startswith(f"Busy: case {case_id} is being written by another process"), answer
        assert "nothing was changed" in answer and "Retry" in answer
        assert waited < 10, waited
        assert await _notes(sessions, case_id) == []
        async with sessions() as session:
            case = await session.get(CtemCase, case_id)
            assert case is not None and case.state == "analyzing"


async def test_every_locked_write_path_answers_busy(world: SimpleNamespace, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(store, "CASE_LOCK_TIMEOUT", "300ms")
    async with _database() as sessions:
        world.org, case_id = await _seed(sessions)
        calls = {
            "record_ctem_case_validation": {"provider": "openaev", "ref": "run-1", "result": "pass"},
            "record_ctem_case_ticket": {"integration": "jira", "key": "SEC-1"},
        }
        async with sessions() as holder:
            await holder.execute(select(CtemCase).where(CtemCase.id == case_id).with_for_update())
            answers = {name: await _caller(name)(case_id=str(case_id), **args) for name, args in calls.items()}
            await holder.rollback()
        for name, answer in answers.items():
            assert answer.startswith("Busy: case "), (name, answer)


async def test_two_parallel_long_notes_on_one_case_are_both_written_whole(world: SimpleNamespace) -> None:
    async with _database() as sessions:
        world.org, case_id = await _seed(sessions)
        call = _caller()
        first, second = "a" * 12_000, "b" * 12_000
        answers = await asyncio.gather(call(case_id=str(case_id), note=first), call(case_id=str(case_id), note=second))
        assert all('"updated"' in a for a in answers), answers
        assert sorted(await _notes(sessions, case_id)) == [first, second]


async def test_a_fifty_thousand_character_note_is_stored_whole(world: SimpleNamespace) -> None:
    async with _database() as sessions:
        world.org, case_id = await _seed(sessions)
        note = "evidence " * 6_000
        answer = await _caller()(case_id=str(case_id), note=note)
        assert '"updated"' in answer, answer
        assert await _notes(sessions, case_id) == [note.strip()]
