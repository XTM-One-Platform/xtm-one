"""CTEM ticketing sync on PostgreSQL (#917): one ticket per case, status mapping kept, organizations apart.

The sync layer against the real schema: the scheduled pass never opens a
ticket (only the Mobilization role's call gated by ``ctem.mobilize`` does); a
case gets exactly one ticket however often that call and the pass replay, a
call that lost its case write finds the ticket again by its correlation
instead of opening a second one, the ticket's sync record survives a reload of
the row, a ticket moved to done asks the re-test once and never resolves the
case, and a case of another organization is never touched.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

import app.models  # noqa: F401 - registers every mapper on Base.metadata
from app.models.ctem_case import CtemCase, CtemCaseEvent
from app.models.organization import Organization
from app.models.user import User
from app.services import ctem_case_loop as case_loop
from app.services import ctem_cases as rules
from app.services import ctem_cases_store as store
from app.services import ctem_ticketing as tk
from app.services import organization_context as oc
from tests.scratch_database import ALL_TABLES, scratch_database
from tests.test_ctem_ticketing import FakeAdapter

pytestmark = pytest.mark.integration

NOW = datetime(2026, 11, 21, 18, 0, tzinfo=UTC)


@asynccontextmanager
async def _database() -> AsyncIterator[async_sessionmaker]:

    async with scratch_database("ctemticketing", tables=ALL_TABLES, extensions=("vector",)) as db:
        yield db.sessions


@pytest.fixture(autouse=True)
def _gate_opens_a_case(monkeypatch: pytest.MonkeyPatch) -> None:
    """A source read's signal opens its case (#1529): the scope gate decides ``case`` here."""
    from app.services import ctem_scope_gate_store as gate_store

    async def evaluate(db: object, row: Any, **_: object) -> None:
        row.state = "confirmed"
        row.decision = {"outcome": "case", "state": "confirmed"}

    monkeypatch.setattr(gate_store, "evaluate", evaluate)


def _issue(ext: str, cve: str) -> rules.SourceIssue:
    return rules.SourceIssue(
        source="opencti",
        external_id=ext,
        name=cve,
        cve=cve,
        product="nginx",
        vendor="F5",
        fixed_version="1.30.7",
        cvss=9.2,
    )


async def _mobilized_case(session, *, severity: str = "P1", primary: bool = True) -> tuple[uuid.UUID, uuid.UUID]:
    """One organization with one case in progress (it passed Mobilize); answers the organization and case ids."""
    org = Organization(
        id=uuid.uuid4(),
        name=f"Org {uuid.uuid4().hex[:6]}",
        role="primary" if primary else "tracked",
        relationship=None if primary else "customer",
    )
    user = User(
        id=uuid.uuid4(), email=f"ctem-{uuid.uuid4().hex[:8]}@test.local", full_name="Owner", hashed_password="x"
    )
    session.add_all([org, user])
    await session.flush()
    await store.apply_recompute(
        session, organization_id=org.id, vertical="cyber", issues=[_issue("v1", "CVE-1")], now=NOW
    )
    case = (await session.execute(select(CtemCase).where(CtemCase.organization_id == org.id))).scalar_one()
    await store.update_case(
        session, case, actor=str(user.id), actor_kind="user", state="remediating", owner_user_id=user.id, now=NOW
    )
    case.severity = severity
    await session.commit()
    return org.id, case.id


def _declare(monkeypatch, organization_id: uuid.UUID, adapter: FakeAdapter) -> None:
    """The organization context declares a ticketing target answered by *adapter*."""
    row = type(
        "V",
        (),
        {"organization_id": organization_id, "profile": {"ticketing": {"integration": "jira-int", "project": "ITS"}}},
    )()

    async def _current(db, organization_id=None):
        return row

    async def _adapter(db, profile):
        return adapter

    monkeypatch.setattr(oc, "current_version", _current)
    monkeypatch.setattr(tk, "adapter_for", _adapter)


async def _mobilize(sessions, case_id: uuid.UUID, now: datetime = NOW) -> dict:
    """The Mobilization role's call once the case passed Mobilize (the gated create)."""
    async with sessions() as session:
        case = await session.get(CtemCase, case_id)
        answer = await tk.sync_one(session, case, create=True, now=now)
        await session.commit()
    return answer


async def _ticket_events(session, case_id: uuid.UUID) -> list[CtemCaseEvent]:
    rows = await session.execute(
        select(CtemCaseEvent).where(CtemCaseEvent.case_id == case_id, CtemCaseEvent.kind == "ticket")
    )
    return list(rows.scalars())


async def test_the_pass_opens_nothing_and_replays_and_a_lost_case_write_keep_one_ticket(monkeypatch):
    adapter = FakeAdapter()
    async with _database() as sessions:
        async with sessions() as session:
            org_id, case_id = await _mobilized_case(session)
        _declare(monkeypatch, org_id, adapter)

        async with sessions() as session:
            counts = await tk.sync_cases(session, now=NOW)
            await session.commit()
        assert counts["created"] == 0 and adapter.created == 0

        for _ in range(3):
            await _mobilize(sessions, case_id)
            async with sessions() as session:
                await tk.sync_cases(session, now=NOW)
                await session.commit()
        assert adapter.created == 1

        async with sessions() as session:
            case = await session.get(CtemCase, case_id)
            assert [t["key"] for t in case.tickets] == ["ITS-1"]
            sync = case.tickets[0]["sync"]
            assert (sync["state"], sync["correlation"]) == ("in_sync", tk.correlation_of(case))
            assert len(await _ticket_events(session, case_id)) == 1
            # A run that created the ticket then lost its case write: the row has no ticket.
            case.tickets = []
            await session.commit()

        answer = await _mobilize(sessions, case_id, NOW + timedelta(minutes=5))
        assert answer["action"] == "created" and not answer["created"] and adapter.created == 1

        async with sessions() as session:
            case = await session.get(CtemCase, case_id)
            assert [t["key"] for t in case.tickets] == ["ITS-1"]


async def test_ticket_done_asks_the_retest_once_and_the_case_stays_open(monkeypatch):
    adapter = FakeAdapter()
    async with _database() as sessions:
        async with sessions() as session:
            org_id, case_id = await _mobilized_case(session)
        _declare(monkeypatch, org_id, adapter)
        await _mobilize(sessions, case_id)

        adapter.tickets["ITS-1"] = tk.RemoteTicket(key="ITS-1", url="u", status="done", external_status="Done")
        events = case_loop.PendingEvents()
        for minutes in (5, 10):
            async with sessions() as session:
                await tk.sync_cases(session, events=events, now=NOW + timedelta(minutes=minutes))
                await session.commit()
        assert [name for name, _data, _id in events.items] == [tk.CASE_RETEST_REQUESTED]

        async with sessions() as session:
            case = await session.get(CtemCase, case_id)
            assert case.state == "remediating"
            assert (case.tickets[0]["status"], case.tickets[0]["sync"]["external_status"]) == ("done", "Done")
            # The case resolves on its re-test: the next pass leaves the done ticket as it is.
            case.state = "resolved"
            await session.commit()
        async with sessions() as session:
            counts = await tk.sync_cases(session, events=events, now=NOW + timedelta(minutes=15))
            await session.commit()
        assert counts["pushed"] == 0 and len(events.items) == 1


async def test_the_sync_touches_the_declared_organization_only(monkeypatch):
    adapter = FakeAdapter()
    async with _database() as sessions:
        async with sessions() as session:
            org_id, case_id = await _mobilized_case(session)
            _other_org, other_case = await _mobilized_case(session, primary=False)
        _declare(monkeypatch, org_id, adapter)
        await _mobilize(sessions, case_id)
        # The tool refuses a case of another organization than the declared one.
        assert (await _mobilize(sessions, other_case))["connected"] is False
        async with sessions() as session:
            await tk.sync_cases(session, now=NOW)
            await session.commit()
        assert adapter.created == 1
        async with sessions() as session:
            assert (await session.get(CtemCase, case_id)).tickets
            assert (await session.get(CtemCase, other_case)).tickets in ([], None)
