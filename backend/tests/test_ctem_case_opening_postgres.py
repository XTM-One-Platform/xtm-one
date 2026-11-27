"""CTEM case opening switch on PostgreSQL (#1951): off on each path, the drain on resume, one case per fix, audit."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

import app.models  # noqa: F401 - registers every mapper on Base.metadata
from app.models.audit_log import AuditLog
from app.models.ctem_case import CtemCase
from app.models.ctem_signal import CtemSignal
from app.models.organization import Organization
from app.models.platform_settings import PlatformSettings
from app.models.user import User
from app.services import ctem_case_loop as loop
from app.services import ctem_case_opening as case_opening
from tests.scratch_database import ALL_TABLES, scratch_database

pytestmark = pytest.mark.integration

#: tests/conftest.py keeps case opening on in every other module; this one reads the real row.
READS_CASE_OPENING_SETTING = True

NOW = datetime(2026, 11, 24, 18, 0, tzinfo=UTC)


@asynccontextmanager
async def _database() -> AsyncIterator[async_sessionmaker]:
    async with scratch_database("ctemcaseopening", tables=ALL_TABLES, extensions=("vector",)) as db:
        yield db.sessions


@pytest.fixture(autouse=True)
def _quiet(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, dict[str, Any]]]:
    """No owner lookup, no inventory read, no live frame or event bus: the switch is under test."""
    from app.services import ctem_case_owner, ctem_loop_stream, platform_events

    async def no_owner(_db: Any, _signal: Any, _case: Any = None) -> tuple[None, str, tuple[()]]:
        return None, "none", ()

    async def not_resolved(*_a: Any, **_k: Any) -> bool:
        return False

    async def no_frames(_frames: Any) -> None:
        return None

    published: list[tuple[str, dict[str, Any]]] = []

    async def publish(name: str, data: Any, *, event_id: str | None = None) -> str:
        published.append((name, dict(data)))
        return event_id or name

    async def no_owners(_items: Any) -> None:
        return None

    from app.services import ctem_case_routing, ctem_cases_view

    async def forget(_vertical: str) -> None:
        return None

    async def no_backfill(_db: Any, **_k: Any) -> int:
        return 0

    monkeypatch.setattr(ctem_case_owner, "owner_for_signal", no_owner)
    monkeypatch.setattr(ctem_case_owner, "backfill_owners", no_backfill)
    monkeypatch.setattr(loop, "_resolved_on_inventory", not_resolved)
    monkeypatch.setattr(ctem_loop_stream, "publish_case_updates", no_frames)
    monkeypatch.setattr(platform_events, "publish", publish)
    monkeypatch.setattr(ctem_case_routing, "stamp_owners", no_owners)
    monkeypatch.setattr(ctem_cases_view, "forget_all_views", forget)
    return published


async def _world(session, *, signals: int, subjects: int | None = None) -> dict[str, Any]:
    """An organization, an administrator and *signals* confirmed signals on *subjects* distinct fixes."""
    org = Organization(id=uuid.uuid4(), name="Org", role="primary")
    admin = User(
        id=uuid.uuid4(),
        email=f"admin-{uuid.uuid4().hex[:8]}@test.local",
        full_name="Admin",
        hashed_password="x",
        role="admin",
    )
    session.add_all([org, admin])
    await session.flush()
    subjects = subjects or signals
    for n in range(signals):
        session.add(_signal(org.id, n % subjects))
    await session.commit()
    return {"org": org.id, "admin": admin}


def _signal(org_id: uuid.UUID, subject: int) -> CtemSignal:
    return CtemSignal(
        organization_id=org_id,
        trigger_path="new_vulnerability",
        lens="inside",
        source_provider="opencti",
        subject_kind="vulnerability",
        subject_ref=f"CVE-2026-{1000 + subject}",
        dedupe_key=f"d-{uuid.uuid4().hex}",
        state="confirmed",
        decision={"outcome": "case", "state": "confirmed"},
        recorded_via="listener",
        recorded_by="system",
    )


async def _cases(session) -> int:
    return int(await session.scalar(select(func.count()).select_from(CtemCase)) or 0)


async def _pause(sessions: async_sessionmaker, admin: User) -> None:
    async with sessions() as session:
        assert await case_opening.set_opening(session, vertical="cyber", on=False, actor=admin) is True
        await session.commit()


async def test_off_the_sweep_opens_no_case_and_keeps_its_other_passes():
    async with _database() as sessions:
        async with sessions() as session:
            world = await _world(session, signals=3)
        await _pause(sessions, world["admin"])
        async with sessions() as session:
            counts = await loop.sweep(session, events=loop.PendingEvents(), now=NOW)
            await session.commit()
            assert counts["opened"] == counts["joined"] == 0
            assert "sla_breached" in counts and "stalled" in counts
            assert await _cases(session) == 0
            assert await case_opening.state(session, "cyber") == {"vertical": "cyber", "on": False, "waiting": 3}


async def test_off_the_gate_confirmation_opens_no_case(monkeypatch: pytest.MonkeyPatch):
    from app.services import ctem_scope_gate_store as gate_store

    async def confirm(_db: Any, row: CtemSignal, **_: Any) -> None:
        row.state = "confirmed"
        row.decision = {"outcome": "case", "state": "confirmed"}

    monkeypatch.setattr(gate_store, "evaluate", confirm)
    async with _database() as sessions:
        async with sessions() as session:
            world = await _world(session, signals=0)
        await _pause(sessions, world["admin"])
        async with sessions() as session:
            row = _signal(world["org"], 1)
            row.state = "new"
            session.add(row)
            await session.flush()
            await gate_store.evaluate_after_record(session, row, created=True)
            await session.commit()
            assert row.state == "confirmed" and row.case_id is None
            assert await _cases(session) == 0


async def test_off_a_source_read_opens_no_case():
    from app.services import ctem_cases_store as store

    async with _database() as sessions:
        async with sessions() as session:
            world = await _world(session, signals=1)
        await _pause(sessions, world["admin"])
        async with sessions() as session:
            signal = (await session.execute(select(CtemSignal))).scalar_one()
            case = await store._case_of_signal(session, signal, vertical="cyber", events=None, now=NOW)
            assert case is None
            assert await _cases(session) == 0


async def test_on_again_the_drain_opens_every_waiting_case_one_per_fix(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(loop, "SWEEP_BATCH", 50)
    async with _database() as sessions:
        async with sessions() as session:
            world = await _world(session, signals=240, subjects=120)
        await _pause(sessions, world["admin"])
        async with sessions() as session:
            assert await case_opening.set_opening(session, vertical="cyber", on=True, actor=world["admin"]) is True
            await session.commit()
        totals = await case_opening.drain("cyber", session_factory=sessions)
        assert totals["opened"] == 120 and totals["joined"] == 120 and totals["skipped"] == 0
        # 240 signals in batches of 50: five passes that open, one that finds none waiting.
        assert totals["passes"] == 6
        async with sessions() as session:
            assert await _cases(session) == 120
            assert await case_opening.waiting_count(session) == 0


async def test_two_concurrent_drains_never_open_a_fix_twice(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(loop, "SWEEP_BATCH", 25)
    async with _database() as sessions:
        async with sessions() as session:
            await _world(session, signals=100, subjects=40)
        first, second = await asyncio.gather(
            case_opening.drain("cyber", session_factory=sessions),
            case_opening.drain("cyber", session_factory=sessions),
        )
        assert first["opened"] + second["opened"] == 40
        assert first["skipped"] + second["skipped"] == 0
        async with sessions() as session:
            assert await _cases(session) == 40
            keys = [k for row in (await session.execute(select(CtemCase.keys))).scalars() for k in row]
            assert len(keys) == len(set(keys))
            assert await case_opening.waiting_count(session) == 0


async def test_a_paused_drain_stops_at_its_next_pass():
    async with _database() as sessions:
        async with sessions() as session:
            world = await _world(session, signals=5)
        await _pause(sessions, world["admin"])
        totals = await case_opening.drain("cyber", session_factory=sessions)
        assert totals == {"passes": 1, "opened": 0, "joined": 0, "skipped": 0}


async def test_off_open_from_signal_itself_opens_and_joins_nothing() -> None:
    """#2536: the choke point refuses on its own, whatever its caller checked."""
    async with _database() as sessions:
        async with sessions() as session:
            world = await _world(session, signals=2, subjects=1)
        async with sessions() as session:
            first, _second = (await session.execute(select(CtemSignal))).scalars().all()
            case = await loop.open_from_signal(session, first, events=loop.PendingEvents(), now=NOW)
            await session.commit()
            assert case is not None
        await _pause(sessions, world["admin"])
        async with sessions() as session:
            second = (await session.execute(select(CtemSignal).where(CtemSignal.case_id.is_(None)))).scalar_one()
            held = (await session.execute(select(CtemCase))).scalar_one()
            assert await loop.open_from_signal(session, second, events=loop.PendingEvents(), now=NOW) is None
            assert await loop.join_case(session, second, held, now=NOW) is False
            await session.commit()
            assert second.case_id is None and second.state == "confirmed"
            assert await _cases(session) == 1


async def test_off_mid_pass_stops_a_drain_at_the_next_signal(monkeypatch: pytest.MonkeyPatch) -> None:
    """#2536: an administrator pauses while a drain pass runs: nothing opens after the pause commits."""
    async with _database() as sessions:
        async with sessions() as session:
            world = await _world(session, signals=10)
        real = loop.open_from_signal
        calls: list[int] = []

        async def open_then_pause(db: Any, signal: CtemSignal, **kwargs: Any) -> Any:
            case = await real(db, signal, **kwargs)
            calls.append(1)
            if len(calls) == 2:
                await _pause(sessions, world["admin"])
            return case

        monkeypatch.setattr(loop, "open_from_signal", open_then_pause)
        totals = await case_opening.drain("cyber", session_factory=sessions)
        # Pass 1 stops after its second signal; pass 2 reads the switch off and opens nothing.
        assert totals["opened"] == 2 and totals["passes"] == 2
        async with sessions() as session:
            assert await _cases(session) == 2
            assert await case_opening.waiting_count(session) == 8


async def test_off_the_sweep_after_a_pause_mid_transaction_reads_the_committed_switch() -> None:
    """#2536: a session that read the switch on before the pause reads it off after, no stale copy."""
    async with _database() as sessions:
        async with sessions() as session:
            world = await _world(session, signals=3)
        async with sessions() as session:
            assert await case_opening.is_on(session, "cyber") is True
            await _pause(sessions, world["admin"])
            assert await case_opening.is_on(session, "cyber") is False
            counts = {"opened": 0, "joined": 0, "skipped": 0}
            await loop.open_waiting(session, vertical="cyber", events=loop.PendingEvents(), now=NOW, counts=counts)
            await session.commit()
            assert counts == {"opened": 0, "joined": 0, "skipped": 0}
            assert await _cases(session) == 0


async def test_every_change_writes_one_audit_row_and_the_same_value_none():
    async with _database() as sessions:
        async with sessions() as session:
            world = await _world(session, signals=0)
        admin = world["admin"]
        async with sessions() as session:
            assert await case_opening.set_opening(session, vertical="cyber", on=True, actor=admin) is False
            assert await case_opening.set_opening(session, vertical="cyber", on=False, actor=admin) is True
            assert await case_opening.set_opening(session, vertical="cyber", on=False, actor=admin) is False
            assert await case_opening.set_opening(session, vertical="cyber", on=True, actor=admin) is True
            await session.commit()
            rows = (await session.execute(select(AuditLog).order_by(AuditLog.created_at))).scalars().all()
            assert [r.details["on"] for r in rows] == [False, True]
            assert {r.entity_type for r in rows} == {"ctem_case_opening"}
            assert {r.user_id for r in rows} == {admin.id}
            stored = (await session.execute(select(PlatformSettings))).scalar_one()
            assert stored.ctem_case_opening is None
