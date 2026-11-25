"""CTEM case delete on PostgreSQL (#1723): cascade, items and timers cancelled, signals released, key freed, audit."""

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
from app.models.agent import Agent
from app.models.audit_log import AuditLog
from app.models.ctem_case import CtemCase, CtemCaseEvent, CtemCaseIssue
from app.models.ctem_signal import CtemSignal
from app.models.organization import Organization
from app.models.platform_timer import PlatformTimer
from app.models.user import User
from app.models.work_item import WorkItem
from app.services import ctem_case_delete as case_delete
from tests.scratch_database import ALL_TABLES, scratch_database

pytestmark = pytest.mark.integration

NOW = datetime(2026, 11, 24, 8, 0, tzinfo=UTC)
KEY = "upgrade:microsoft/windows@latest"


@asynccontextmanager
async def _database() -> AsyncIterator[async_sessionmaker]:
    async with scratch_database("ctemcasedelete", tables=ALL_TABLES, extensions=("vector",)) as db:
        yield db.sessions


@pytest.fixture(autouse=True)
def _gate(monkeypatch: pytest.MonkeyPatch) -> list[uuid.UUID]:
    """The scope gate decides the released signals after the commit; here it confirms them and records the calls.

    A gate run or a case opened inside the delete itself fails the test (#1950).
    """
    from app.services import ctem_case_loop
    from app.services import ctem_scope_gate_store as gate_store

    decided: list[uuid.UUID] = []

    async def decide_again(db: Any, rows: list[CtemSignal], **_: Any) -> dict[str, int]:
        for row in rows:
            decided.append(row.id)
            row.state = "confirmed"
            row.decision = {"outcome": "case", "state": "confirmed"}
        return {"confirmed": len(rows)}

    async def refused(*_: Any, **__: Any) -> None:
        raise AssertionError("no per-signal gate run and no case opened inside a case delete")

    monkeypatch.setattr(gate_store, "_decide_again", decide_again)
    monkeypatch.setattr(gate_store, "evaluate_one", refused)
    monkeypatch.setattr(gate_store, "read_scenarios", refused)
    monkeypatch.setattr(ctem_case_loop, "open_from_signal", refused)
    return decided


async def _world(session) -> dict[str, Any]:
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
    agent = Agent(id=uuid.uuid4(), name="Loop", user_id=admin.id)
    session.add(agent)
    await session.flush()
    legacy = CtemCase(
        organization_id=org.id,
        vertical="cyber",
        title="Upgrade Windows",
        remediation_kind="upgrade",
        keys=[KEY],
        pinned_issues=[],
        state="resolved",
        state_since=NOW,
    )
    kept = CtemCase(
        organization_id=org.id,
        vertical="cyber",
        title="Upgrade Tomcat",
        remediation_kind="upgrade",
        keys=["upgrade:apache/tomcat@9.0.98"],
        pinned_issues=[],
        state="new",
        state_since=NOW,
    )
    session.add_all([legacy, kept])
    await session.flush()
    session.add_all(
        [
            CtemCaseIssue(case_id=legacy.id, source="opencti", external_id="vuln--1", open=False),
            CtemCaseEvent(case_id=legacy.id, at=NOW, actor="system", kind="created", to_value=KEY),
            CtemCaseEvent(case_id=kept.id, at=NOW, actor="system", kind="created", to_value="x", reason="signal s1"),
        ]
    )
    signal = CtemSignal(
        organization_id=org.id,
        trigger_path="new_vulnerability",
        lens="inside",
        source_provider="opencti",
        subject_kind="software",
        subject_ref="cpe:/o:microsoft:windows",
        dedupe_key=f"d-{uuid.uuid4().hex[:8]}",
        state="case_opened",
        case_id=legacy.id,
        recorded_via="listener",
        recorded_by="system",
    )
    items = [
        WorkItem(user_id=admin.id, title="gate", correlation_key=f"ctem:review:{legacy.id}", status="blocked"),
        WorkItem(user_id=admin.id, title="fleet", correlation_key=f"ctem-loop:case:{legacy.id}", status="open"),
        WorkItem(user_id=admin.id, title="done", correlation_key=f"ctem:remediation:{legacy.id}", status="done"),
        WorkItem(user_id=admin.id, title="other", correlation_key=f"ctem:review:{kept.id}", status="open"),
    ]
    timers = [
        PlatformTimer(
            user_id=admin.id, agent_id=agent.id, correlation_key=f"ctem-loop-reminder:{legacy.id}", due_at=NOW
        ),
        PlatformTimer(user_id=admin.id, agent_id=agent.id, correlation_key=f"ctem-gate-watch:{kept.id}", due_at=NOW),
    ]
    session.add_all([signal, *items, *timers])
    await session.commit()
    return {"org": org.id, "admin": admin, "legacy": legacy.id, "kept": kept.id, "signal": signal.id}


async def test_origin_reads_the_history_as_the_cases_page():
    class E:
        def __init__(self, kind, to_value=None, reason=None):
            self.kind, self.to_value, self.reason = kind, to_value, reason

    assert case_delete.origin([E("created", "k")]) == "legacy"
    assert case_delete.origin([E("created", "k", "signal abc")]) == "signal"
    assert case_delete.origin([E("created", "manual")]) == "split"
    assert case_delete.origin([E("created", "k"), E("signal")]) == "signal"


async def test_a_dry_run_lists_what_goes_and_writes_nothing():
    async with _database() as sessions, sessions() as session:
        world = await _world(session)
        cases, unknown = await case_delete.select_cases(
            session, organization_id=world["org"], opened_by=case_delete.OPENED_BY_LEGACY
        )
        assert [c.id for c in cases] == [world["legacy"]] and unknown == []
        result = await case_delete.delete_cases(session, cases, actor=world["admin"], dry_run=True)
        await session.commit()
        assert result["dry_run"] is True and result["count"] == 1
        summary = result["cases"][0]
        assert summary["opened_by"] == "legacy" and summary["keys"] == [KEY]
        assert sorted(summary["work_items"]) == sorted(
            [f"ctem:review:{world['legacy']}", f"ctem-loop:case:{world['legacy']}"]
        )
        assert summary["timers"] == [f"ctem-loop-reminder:{world['legacy']}"]
        assert summary["signals"] == [str(world["signal"])]
        assert await session.get(CtemCase, world["legacy"]) is not None


async def test_a_delete_cascades_cancels_releases_and_audits(_gate):
    async with _database() as sessions, sessions() as session:
        world = await _world(session)
        cases, _ = await case_delete.select_cases(session, organization_id=world["org"], case_ids=[world["legacy"]])
        result = await case_delete.delete_cases(
            session, cases, actor=world["admin"], reason="legacy reset", dry_run=False, now=NOW + timedelta(minutes=1)
        )
        await session.commit()
        assert result["dry_run"] is False and result["count"] == 1
        assert result["released_signals"] == [world["signal"]] and _gate == []
        session.expire_all()
        released = await session.get(CtemSignal, world["signal"])
        assert released.case_id is None and released.state == "new" and released.decision is None
        assert await case_delete.decide_released(result["released_signals"], sessions=sessions) == 1
        session.expire_all()  # read back what the commit stored
        assert await session.get(CtemCase, world["legacy"]) is None
        assert await session.get(CtemCase, world["kept"]) is not None
        members = (await session.execute(select(CtemCaseIssue).where(CtemCaseIssue.case_id == world["legacy"]))).all()
        events = (await session.execute(select(CtemCaseEvent).where(CtemCaseEvent.case_id == world["legacy"]))).all()
        assert members == [] and events == []
        items = {i.correlation_key: i for i in (await session.execute(select(WorkItem))).scalars()}
        assert items[f"ctem:review:{world['legacy']}"].status == "cancelled"
        assert "was deleted" in (items[f"ctem:review:{world['legacy']}"].result or "")
        assert items[f"ctem-loop:case:{world['legacy']}"].status == "cancelled"
        assert items[f"ctem:remediation:{world['legacy']}"].status == "done"  # a finished item is left as it was
        assert items[f"ctem:review:{world['kept']}"].status == "open"
        timers = {t.correlation_key: t.state for t in (await session.execute(select(PlatformTimer))).scalars()}
        assert timers[f"ctem-loop-reminder:{world['legacy']}"] == "cancelled"
        assert timers[f"ctem-gate-watch:{world['kept']}"] == "armed"
        signal = await session.get(CtemSignal, world["signal"])
        # Back to the gate, decided again, waiting for the case sweep with no case.
        assert signal.case_id is None and signal.state == "confirmed" and _gate == [world["signal"]]
        audit = (await session.execute(select(AuditLog).where(AuditLog.entity_type == "ctem_case"))).scalars().all()
        assert len(audit) == 1 and audit[0].action == "delete" and audit[0].entity_id == world["legacy"]
        assert audit[0].details["case"]["keys"] == [KEY] and audit[0].details["reason"] == "legacy reset"
        # A read of the deleted id answers deleted, from that audit row (#2328).
        answer = await case_delete.deleted_case_answer(session, world["legacy"])
        assert answer is not None and "was deleted" in answer and "legacy reset" in answer
        assert await case_delete.deleted_case_answer(session, world["kept"]) is None


async def test_the_fix_key_is_free_and_the_released_signal_waits_for_the_sweep():
    async with _database() as sessions, sessions() as session:
        world = await _world(session)
        cases, _ = await case_delete.select_cases(session, organization_id=world["org"], case_ids=[world["legacy"]])
        result = await case_delete.delete_cases(session, cases, actor=world["admin"], dry_run=False, now=NOW)
        await session.commit()
        await case_delete.decide_released(result["released_signals"], sessions=sessions)
        session.expire_all()  # read back what the commit stored
        holders = [c for c in (await session.execute(select(CtemCase))).scalars() if KEY in (c.keys or [])]
        assert holders == []  # no case owns the key: the next case for it opens fresh
        # The scheduled sweep's own query: a confirmed signal with no case.
        waiting = (
            (
                await session.execute(
                    select(CtemSignal.id).where(CtemSignal.state == "confirmed", CtemSignal.case_id.is_(None))
                )
            )
            .scalars()
            .all()
        )
        assert waiting == [world["signal"]]


async def test_a_filter_and_ids_combine_and_unknown_ids_are_named():
    async with _database() as sessions, sessions() as session:
        world = await _world(session)
        stray = uuid.uuid4()
        cases, unknown = await case_delete.select_cases(
            session, organization_id=world["org"], case_ids=[world["kept"], stray], opened_by="legacy"
        )
        # kept was opened by a signal: the filter leaves it out, so it is named unknown with the stray id.
        assert cases == [] and sorted(unknown) == sorted([str(world["kept"]), str(stray)])


async def test_sixteen_cases_and_a_thousand_signals_go_in_one_quick_delete(_gate):
    """#1950: legacy, signal and split cases, any state, 1,216 signals: one delete, no gate run inside it."""
    import time

    async with _database() as sessions, sessions() as session:
        world = await _world(session)
        origins = [
            ("created", KEY, None),  # legacy
            ("created", "k", "signal s"),  # signal
            ("created", "manual", None),  # split
        ]
        case_ids: list[uuid.UUID] = []
        for n in range(16):
            case = CtemCase(
                organization_id=world["org"],
                vertical="cyber",
                title=f"Case {n}",
                remediation_kind="upgrade",
                keys=[f"upgrade:vendor/product-{n}@1"],
                pinned_issues=[],
                state=("new", "remediating", "validating", "resolved")[n % 4],
                state_since=NOW,
            )
            session.add(case)
            await session.flush()
            kind, to_value, reason = origins[n % 3]
            session.add(
                CtemCaseEvent(case_id=case.id, at=NOW, actor="system", kind=kind, to_value=to_value, reason=reason)
            )
            session.add_all(
                [
                    CtemSignal(
                        organization_id=world["org"],
                        trigger_path="new_vulnerability",
                        lens="inside",
                        source_provider="opencti",
                        subject_kind="software",
                        subject_ref=f"cpe:/a:vendor:product-{n}",
                        dedupe_key=f"d-{n}-{i}",
                        state="case_opened",
                        case_id=case.id,
                        recorded_via="listener",
                        recorded_by="system",
                    )
                    for i in range(76)
                ]
            )
            case_ids.append(case.id)
        await session.commit()
        cases, unknown = await case_delete.select_cases(session, organization_id=world["org"], case_ids=case_ids)
        assert len(cases) == 16 and unknown == []
        assert {case_delete.origin(e) for e in (await case_delete._events(session, case_ids)).values()} == {
            "legacy",
            "signal",
            "split",
        }
        started = time.monotonic()
        result = await case_delete.delete_cases(session, cases, actor=world["admin"], dry_run=False, now=NOW)
        await session.commit()
        elapsed = time.monotonic() - started
        assert result["count"] == 16 and len(result["released_signals"]) == 16 * 76
        assert elapsed < 5, f"delete took {elapsed:.1f} s"
        assert _gate == []  # nothing decided inside the delete
        session.expire_all()
        left = (await session.execute(select(CtemCase.id).where(CtemCase.id.in_(case_ids)))).scalars().all()
        assert left == []
        states = (
            await session.execute(
                select(CtemSignal.state, CtemSignal.case_id).where(CtemSignal.id.in_(result["released_signals"]))
            )
        ).all()
        assert {(s, c) for s, c in states} == {("new", None)}
        # After the commit: decided in batches, none put in a case by the delete.
        assert await case_delete.decide_released(result["released_signals"], sessions=sessions) == 16 * 76
        session.expire_all()
        opened = (
            await session.execute(
                select(CtemSignal.id).where(
                    CtemSignal.id.in_(result["released_signals"]), CtemSignal.case_id.is_not(None)
                )
            )
        ).all()
        assert opened == []
        remaining = set((await session.execute(select(CtemCase.id))).scalars().all())
        assert remaining == {world["kept"], world["legacy"]}  # no case opened by the delete


async def test_a_gate_failure_after_the_delete_leaves_the_signals_new(monkeypatch):
    from app.services import ctem_scope_gate_store as gate_store

    async def broken(*_: Any, **__: Any) -> None:
        raise RuntimeError("OpenCRQ down")

    monkeypatch.setattr(gate_store, "_decide_again", broken)
    async with _database() as sessions, sessions() as session:
        world = await _world(session)
        cases, _ = await case_delete.select_cases(session, organization_id=world["org"], case_ids=[world["legacy"]])
        result = await case_delete.delete_cases(session, cases, actor=world["admin"], dry_run=False, now=NOW)
        await session.commit()
        assert await case_delete.decide_released(result["released_signals"], sessions=sessions) == 0
        session.expire_all()
        assert await session.get(CtemCase, world["legacy"]) is None
        assert (await session.get(CtemSignal, world["signal"])).state == "new"
