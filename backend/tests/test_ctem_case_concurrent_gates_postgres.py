"""Concurrent writes of one CTEM case on PostgreSQL (#2373): one decision, one state move.

Two ``update_ctem_case`` calls that write the same ``waited`` gate at the same
time used to read the case unlocked, both see ``analyzing`` and both record
the gate and the ``analyzing -> waiting_decision`` move. Every write now reads
the case row locked until its commit, so the second call waits, reads what the
first wrote and records nothing twice. The race needs two real transactions:
it does not reproduce on SQLite or on a fake session.

The gate refusal's decision-item read is the one stub: it is where both calls
meet (each waits a moment for the other), so the interleaving that raced is
forced instead of left to the scheduler.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
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
from app.services import ctem_cases_view as view_of
from app.services import ctem_gate_order
from app.services.builtin_tools import build_ctem_case_tools
from app.tools.base import ToolCall

pytestmark = pytest.mark.integration

ISSUE = rules.SourceIssue(source="opencti", external_id="vuln--1", name="CVE-2026-1", cvss=9.8)
SINCE = datetime.now(UTC) - timedelta(days=1)


@asynccontextmanager
async def _database() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    from tests.scratch_database import ALL_TABLES, scratch_database

    async with scratch_database("ctemconcurrentgates", tables=ALL_TABLES, extensions=("vector",)) as db:
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
            state_since=SINCE,
        )
        session.add(case)
        await session.flush()
        session.add(CtemCaseIssue(case_id=case.id, source=ISSUE.source, external_id=ISSUE.external_id, open=True))
        await session.commit()
        return org.id, case.id


@pytest.fixture
def world(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """The acting user sees the case's issue and may act on it; each gate has its open decision item."""
    seen = SimpleNamespace(org=None, arrived=0, both=asyncio.Event())

    async def _primary(_db: object) -> object:
        return SimpleNamespace(id=seen.org)

    async def _viewer(_db: object, _user_id: object, _vertical: object, **_kw: object) -> view_of.ViewerView:
        return view_of.ViewerView(issues={ISSUE.member_id: ISSUE}, open_ids={ISSUE.member_id})

    async def _acts(*_a: object, **_kw: object) -> bool:
        return True

    async def _open_keys(_db: object, case_id: str) -> set[str]:
        # Where the two calls meet: each waits a moment for the other, then goes on.
        seen.arrived += 1
        if seen.arrived >= 2:
            seen.both.set()
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(seen.both.wait(), timeout=0.5)
        return {
            key
            for gate in ("ctem.mobilize", "ctem.analyst_review")
            for key in ctem_gate_order.waited_item_keys(gate, case_id)
        }

    monkeypatch.setattr("app.services.organizations.get_primary", _primary)
    monkeypatch.setattr(view_of, "viewer_view", _viewer)
    monkeypatch.setattr(view_of, "acts_on_case", _acts)
    monkeypatch.setattr(ctem_gate_order, "open_gate_item_keys", _open_keys)
    return seen


def _caller() -> Callable[..., Awaitable[str]]:
    _defs, executor = build_ctem_case_tools(user_id=str(uuid.uuid4()), agent_id=str(uuid.uuid4()))

    async def call(**args: object) -> str:
        result = await executor.execute_tool(ToolCall(id=uuid.uuid4().hex, name="update_ctem_case", arguments=args))
        return result.content if isinstance(result.content, str) else str(result.content)

    return call


async def _history(
    sessions: async_sessionmaker[AsyncSession], case_id: uuid.UUID
) -> tuple[CtemCase, list[CtemCaseEvent]]:
    async with sessions() as session:
        case = await session.get(CtemCase, case_id)
        assert case is not None
        events = list((await session.execute(select(CtemCaseEvent).where(CtemCaseEvent.case_id == case_id))).scalars())
        return case, events


async def test_the_same_waited_gate_written_twice_at_once_is_recorded_once(world: SimpleNamespace) -> None:
    async with _database() as sessions:
        world.org, case_id = await _seed(sessions)
        call = _caller()
        gate = {"key": "ctem.mobilize", "outcome": "waited", "by": "the security lead"}
        answers = await asyncio.gather(
            call(case_id=str(case_id), gate=gate), call(case_id=str(case_id), gate=dict(gate))
        )
        assert all('"updated"' in a for a in answers), answers

        case, events = await _history(sessions, case_id)
        gates = [e for e in events if e.kind == "gate"]
        states = [e for e in events if e.kind == "state"]
        assert [(g.from_value, g.to_value) for g in gates] == [("ctem.mobilize", "waited")]
        assert [(s.from_value, s.to_value) for s in states] == [("analyzing", "waiting_decision")]
        assert case.state == "waiting_decision"
        assert case.state_since == states[0].at
        # The second call says it recorded nothing new.
        unchanged = [json.loads(a)["updated"]["unchanged"] for a in answers]
        assert sorted(unchanged) == [[], ["gate"]]


async def test_two_gates_waited_at_once_move_the_case_once_from_its_real_state(world: SimpleNamespace) -> None:
    async with _database() as sessions:
        world.org, case_id = await _seed(sessions)
        call = _caller()
        await asyncio.gather(
            call(case_id=str(case_id), gate={"key": "ctem.mobilize", "outcome": "waited", "by": "the lead"}),
            call(case_id=str(case_id), gate={"key": "ctem.analyst_review", "outcome": "waited", "by": "an analyst"}),
        )

        case, events = await _history(sessions, case_id)
        assert sorted(str(e.from_value) for e in events if e.kind == "gate") == ["ctem.analyst_review", "ctem.mobilize"]
        states = [(e.from_value, e.to_value) for e in events if e.kind == "state"]
        # Never a second move written from the stale "analyzing" the other call had already left.
        assert states == [("analyzing", "waiting_decision")]
        assert case.state == "waiting_decision"
