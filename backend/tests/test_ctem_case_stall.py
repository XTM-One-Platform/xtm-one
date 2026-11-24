"""An open CTEM case never waits on nobody (#1736).

Rules of ``services/ctem_case_stall.py`` with a fake session: a case waiting on
a gate item or an armed reminder is left alone; a case idle past the window
that waits on neither gets one ``stalled`` event and one ``ctem_case.stalled``
platform event; the event is registered and heard by both case listeners.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast

import pytest

from app.models.ctem_case import CtemCase, CtemCaseEvent
from app.services import ctem_case_loop as loop
from app.services import ctem_case_stall as stall

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

NOW = datetime(2026, 11, 24, 8, 0, tzinfo=UTC)


class _Rows(list[Any]):
    def all(self) -> list[Any]:
        return list(self)


class StallSession:
    """Answers the reads of ``sweep_stalled`` in order: candidates, gate keys, timer keys."""

    def __init__(self, *reads: list[Any]) -> None:
        self.reads = [_Rows(r) for r in reads]
        self.added: list[object] = []
        self.statements: list[object] = []

    def add(self, row: object) -> None:
        self.added.append(row)

    async def execute(self, statement: object) -> SimpleNamespace:
        self.statements.append(statement)
        rows = self.reads.pop(0) if self.reads else _Rows()
        return SimpleNamespace(scalars=lambda: rows)


def _session(*reads: list[Any]) -> tuple[StallSession, AsyncSession]:
    db = StallSession(*reads)
    return db, cast("AsyncSession", db)


def _case(**kw: Any) -> CtemCase:
    base = dict(
        id=uuid.uuid4(),
        organization_id=uuid.uuid4(),
        vertical="cyber",
        title="Patch a product",
        remediation_kind="patch",
        keys=["patch:software/a"],
        pinned_issues=[],
        state="analyzing",
        state_since=NOW - timedelta(days=1),
        updated_at=NOW - timedelta(hours=10),
        trigger_path="relevant_threat",
        risk_scenario_ids=[],
        severity="P1",
        sla_due_at=NOW - timedelta(hours=2),
        sla_breached_at=NOW - timedelta(hours=2),
        tickets=[],
        validations=[],
        validation_plan=False,
        resolution_kind=None,
    )
    base.update(kw)
    return CtemCase(**base)


def test_waiting_on_reads_a_gate_item_then_a_reminder() -> None:
    cid = str(uuid.uuid4())
    assert stall.waiting_on(cid, [f"ctem:remediation:{cid}"], []) == stall.PERSON
    assert stall.waiting_on(cid, [f"ctem:f1:launch:{cid}"], [f"ctem-loop-reminder:{cid}"]) == stall.PERSON
    assert stall.waiting_on(cid, [], [f"ctem-loop-reminder:{cid}"]) == stall.TIME
    assert stall.waiting_on(cid, [], [f"ctem-gate-watch:{cid}"]) == stall.TIME
    # Another case's gate or reminder, or a key outside the loop's prefixes, keeps nobody waiting.
    other = str(uuid.uuid4())
    assert stall.waiting_on(cid, [f"ctem:remediation:{other}"], [f"ctem-loop-reminder:{other}"]) == stall.NOBODY
    assert stall.waiting_on(cid, [f"dev:x:{cid}"], [f"schedule:{cid}"]) == stall.NOBODY


def test_the_stall_window_reads_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CTEM_CASE_STALL_HOURS", raising=False)
    assert stall.stall_hours() == stall.DEFAULT_STALL_HOURS
    monkeypatch.setenv("CTEM_CASE_STALL_HOURS", "24")
    assert stall.stall_hours() == 24
    monkeypatch.setenv("CTEM_CASE_STALL_HOURS", "0")
    assert stall.stall_hours() == 1
    monkeypatch.setenv("CTEM_CASE_STALL_HOURS", "soon")
    assert stall.stall_hours() == stall.DEFAULT_STALL_HOURS


@pytest.mark.asyncio
async def test_a_case_waiting_on_nobody_is_raised_once_and_the_others_are_left_alone() -> None:
    nobody, gated, reminded = _case(), _case(), _case()
    db, session = _session(
        [nobody, gated, reminded],
        [f"ctem:remediation:{gated.id}"],
        [f"ctem-loop-reminder:{reminded.id}"],
    )
    events = loop.PendingEvents()
    raised = await stall.sweep_stalled(session, events=events, now=NOW)
    assert raised == 1
    written = [r for r in db.added if isinstance(r, CtemCaseEvent)]
    assert [(e.case_id, e.kind, e.to_value) for e in written] == [(nobody.id, stall.STALLED, stall.NOBODY)]
    assert "no open gate, no armed reminder" in (written[0].reason or "")
    assert [(name, data["case_id"]) for name, data, _ in events.items] == [(stall.CASE_STALLED, str(nobody.id))]
    assert [f["case_id"] for f in events.frames] == [str(nobody.id)]


@pytest.mark.asyncio
async def test_no_candidate_reads_nothing_more() -> None:
    db, session = _session([])
    assert await stall.sweep_stalled(session, events=loop.PendingEvents(), now=NOW) == 0
    assert len(db.statements) == 1 and db.added == []


@pytest.mark.asyncio
async def test_a_second_stall_window_raises_a_new_event_id() -> None:
    case = _case()
    first, second = loop.PendingEvents(), loop.PendingEvents()
    await stall.sweep_stalled(_session([case], [], [])[1], events=first, now=NOW)
    await stall.sweep_stalled(_session([case], [], [])[1], events=second, now=NOW + timedelta(hours=6))
    assert first.items[0][2] != second.items[0][2]


def test_the_stalled_event_is_registered_viewer_checked_and_heard_by_both_case_listeners() -> None:
    from app.agents.builtin_agents import CTEM_F1_CASE_ASSIGNMENT, CTEM_F2_CASE_ASSIGNMENT
    from app.services.platform_events import PLATFORM_EVENTS, VIEWER_CHECKED_EVENTS

    assert stall.CASE_STALLED == loop.CASE_STALLED
    assert stall.CASE_STALLED in loop.CASE_EVENTS
    assert stall.CASE_STALLED in PLATFORM_EVENTS and stall.CASE_STALLED in VIEWER_CHECKED_EVENTS
    for assignment in (CTEM_F1_CASE_ASSIGNMENT, CTEM_F2_CASE_ASSIGNMENT):
        assert stall.CASE_STALLED in assignment["trigger_config"]["event"]
        assert "ctem_case.stalled" in assignment["prompt"]


def test_the_prefixes_match_the_loop_catalog() -> None:
    from app.agents.builtin_agents import CTEM_KEY_PREFIX, CTEM_LOOP_LEGACY_TIMER_PREFIXES, CTEM_LOOP_REMINDER_PREFIX

    catalog = (CTEM_LOOP_REMINDER_PREFIX, *CTEM_LOOP_LEGACY_TIMER_PREFIXES)
    assert CTEM_KEY_PREFIX == stall.GATE_KEY_PREFIX
    assert catalog == stall.REMINDER_PREFIXES
