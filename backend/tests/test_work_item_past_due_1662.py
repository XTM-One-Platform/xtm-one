"""#1662: ``work_item.past_due`` raised once when an open work item crosses its due date.

``get_fleet_health`` reported ``past_due`` but nothing reacted before the weekly
check. The scheduler tick now marks and raises the event once per item and due
date, and every fleet orchestrator with the seeded health check gets an
"Overdue work items" listener on it.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from app.services import platform_events, work_item_past_due
from app.services.work_item_past_due import (
    PAST_DUE_EVENT,
    PAST_DUE_MARK_KEY,
    carry_past_due_mark,
    clear_past_due_mark,
    past_due_data,
    past_due_event_id,
)

_NOW = datetime(2026, 11, 24, 6, 0, tzinfo=UTC)


def _item(**overrides):
    values = {
        "id": uuid.uuid4(),
        "user_id": uuid.uuid4(),
        "owner_agent_id": uuid.uuid4(),
        "created_by_agent_id": uuid.uuid4(),
        "claimed_by_agent_id": None,
        "claimed_by_run_id": None,
        "correlation_key": "ctem:f1:case:42",
        "title": "Re-test the fix",
        "status": "open",
        "priority": "high",
        "due_at": _NOW - timedelta(hours=2),
        "payload": {"case": "42"},
        "is_company_managed": False,
        "workspace_id": None,
        "blocked_reason": None,
        "blocked_on": None,
        "blocked_on_refs": None,
        "needs_human": False,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


# -- The event -------------------------------------------------------------------


def test_past_due_is_in_the_catalog_with_the_owner_key_and_priority_filters():
    spec = platform_events.PLATFORM_EVENTS[PAST_DUE_EVENT]
    for key in ("owner_agent_id", "correlation_key", "priority", "due_at", "overdue_seconds"):
        assert key in spec["filter_keys"], key
    assert PAST_DUE_EVENT in [e["event"] for e in platform_events.catalog()]


def test_the_payload_names_the_item_its_due_date_and_how_late_it_is():
    item = _item()
    data = past_due_data(item, now=_NOW)
    assert data["work_item_id"] == str(item.id)
    assert data["owner_agent_id"] == str(item.owner_agent_id)
    assert data["correlation_key"] == "ctem:f1:case:42" and data["priority"] == "high"
    assert data["due_at"] == item.due_at.isoformat()
    assert data["overdue_seconds"] == 7200
    assert str(item.owner_agent_id) in data["participant_agent_ids"]


def test_the_event_id_is_stable_per_item_and_due_date():
    item = _item()
    assert past_due_event_id(item) == past_due_event_id(item)
    moved = _item(id=item.id, due_at=item.due_at + timedelta(days=1))
    assert past_due_event_id(moved) != past_due_event_id(item)


# -- The mark --------------------------------------------------------------------


def test_a_writer_rewriting_the_payload_keeps_the_platform_mark():
    stored = {"case": "42", PAST_DUE_MARK_KEY: "2026-11-24T04:00:00+00:00"}
    assert carry_past_due_mark({"case": "43"}, stored) == {"case": "43", PAST_DUE_MARK_KEY: stored[PAST_DUE_MARK_KEY]}
    assert carry_past_due_mark({"case": "43"}, {"case": "42"}) == {"case": "43"}
    assert carry_past_due_mark(None, stored) is None


def test_a_new_due_date_clears_the_mark():
    item = _item(payload={"case": "42", PAST_DUE_MARK_KEY: "x"})
    clear_past_due_mark(item)
    assert item.payload == {"case": "42"}
    clear_past_due_mark(item)
    assert item.payload == {"case": "42"}


# -- Raising ---------------------------------------------------------------------


class _Rows:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return self

    def all(self):
        return list(self._rows)

    def scalar_one_or_none(self):
        return self._rows[0] if self._rows else None


class _Session:
    """First execute: the due ids; then one locked re-read per id."""

    def __init__(self, items, rereads=None):
        rereads = items if rereads is None else rereads
        self._results = [_Rows([i.id for i in items]), *(_Rows([r] if r else []) for r in rereads)]
        self.commits = 0
        self.rollbacks = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False

    async def execute(self, _statement):
        return self._results.pop(0)

    async def commit(self):
        self.commits += 1

    async def rollback(self):
        self.rollbacks += 1


def _patch(monkeypatch, session, published):
    from app.core import database

    async def _publish(name, data, *, event_id=None):
        published.append((name, data, event_id, session.commits))
        return event_id

    monkeypatch.setattr(database, "async_session", lambda: session)
    monkeypatch.setattr(platform_events, "publish", _publish)


@pytest.mark.asyncio
async def test_an_item_past_due_raises_once_after_its_mark_is_committed(monkeypatch):
    item = _item()
    session = _Session([item])
    published = []
    _patch(monkeypatch, session, published)

    assert await work_item_past_due.raise_past_due_items(now=_NOW) == 1

    (name, data, event_id, commits_before) = published[0]
    assert name == PAST_DUE_EVENT and commits_before == 1
    assert event_id == past_due_event_id(item)
    assert item.payload[PAST_DUE_MARK_KEY] == item.due_at.isoformat()
    assert item.payload["case"] == "42"
    assert data["work_item_id"] == str(item.id)


@pytest.mark.asyncio
async def test_an_item_already_marked_or_taken_by_another_tick_raises_nothing(monkeypatch):
    marked = _item(payload={PAST_DUE_MARK_KEY: "2026-11-24T04:00:00+00:00"})
    taken = _item()
    session = _Session([marked, taken], rereads=[marked, None])
    published = []
    _patch(monkeypatch, session, published)

    assert await work_item_past_due.raise_past_due_items(now=_NOW) == 0
    assert published == [] and session.rollbacks == 2


@pytest.mark.asyncio
async def test_a_failed_mark_skips_the_item_and_goes_on(monkeypatch):
    bad = _item(due_at=None)  # past_due_data cannot read it: logged, rolled back, skipped
    good = _item()
    session = _Session([bad, good])
    published = []
    _patch(monkeypatch, session, published)

    assert await work_item_past_due.raise_past_due_items(now=_NOW) == 1
    assert [d["work_item_id"] for _, d, _, _ in published] == [str(good.id)]


# -- The fleet listener ------------------------------------------------------------


class _AssignmentDb:
    def __init__(self, rows=None):
        self.rows = list(rows or [])

    async def execute(self, _stmt):
        rows = list(self.rows)
        return SimpleNamespace(
            scalars=lambda: SimpleNamespace(first=lambda: rows[0] if rows else None, all=lambda: rows)
        )

    def add(self, row):
        self.rows.append(row)

    async def delete(self, row):
        self.rows.remove(row)


def _fleet():
    return SimpleNamespace(id=uuid.uuid4(), name="Desk")


def _orchestrator():
    return SimpleNamespace(id=uuid.uuid4(), user_id=uuid.uuid4(), is_company_managed=False)


@pytest.mark.asyncio
async def test_an_orchestrator_with_the_health_check_hears_its_members_overdue_items():
    from app.services.assignment_pause import CREATED_DISABLED_REASON
    from app.services.fleets import (
        FLEET_HEALTH_ASSIGNMENT_NAMES,
        FLEET_PAST_DUE_NAME,
        ensure_fleet_health_check,
        ensure_fleet_past_due_listener,
        is_fleet_past_due_listener,
    )

    db, fleet, agent = _AssignmentDb(), _fleet(), _orchestrator()
    # No seeded check (opted out): no listener either.
    assert not await ensure_fleet_past_due_listener(db, fleet, agent)
    await ensure_fleet_health_check(db, fleet, agent)
    (check,) = db.rows
    assert await ensure_fleet_past_due_listener(db, fleet, agent, check)
    listener = db.rows[1]
    assert is_fleet_past_due_listener(listener)
    assert listener.trigger_config == {"event": PAST_DUE_EVENT, "scope": "fleet"}
    assert "get_work_item" in listener.prompt and str(fleet.id) in listener.prompt
    assert FLEET_PAST_DUE_NAME in FLEET_HEALTH_ASSIGNMENT_NAMES
    # Born off with the check (not set up yet), idempotent.
    assert listener.enabled is False and listener.paused_reason == CREATED_DISABLED_REASON
    assert not await ensure_fleet_past_due_listener(db, fleet, agent, check)
    assert len(db.rows) == 2
    # The trigger is a valid platform_event subscription.
    assert platform_events.trigger_config_error("platform_event", listener.trigger_config) is None


@pytest.mark.asyncio
async def test_the_listener_of_a_running_check_is_born_on_as_its_run_as():
    from app.services.fleets import ensure_fleet_health_check, ensure_fleet_past_due_listener

    db, fleet, agent = _AssignmentDb(), _fleet(), _orchestrator()
    await ensure_fleet_health_check(db, fleet, agent)
    (check,) = db.rows
    check.enabled, check.paused_reason, check.run_as_user_id = True, None, uuid.uuid4()
    assert await ensure_fleet_past_due_listener(db, fleet, agent, check)
    listener = db.rows[1]
    assert listener.enabled is True and listener.run_as_user_id == check.run_as_user_id
