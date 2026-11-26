"""One-off watch on a platform event.

``trigger_config.once = true``: the first matching event starts one run and
the subscription ends; the mark is written with the run, under a row lock.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import Any

import pytest

from app.services import platform_events as pe


def _sub(**extra) -> pe.Subscription:
    base = {
        "assignment_id": uuid.uuid4(),
        "agent_id": uuid.uuid4(),
        "events": ("work_item.completed",),
        "filter": {},
        "scope_all": False,
        "once": True,
    }
    base.update(extra)
    return pe.Subscription(**base)


def test_once_must_be_a_boolean():
    assert pe.trigger_config_error("platform_event", {"event": "run.failed", "once": True}) is None
    error = pe.trigger_config_error("platform_event", {"event": "run.failed", "once": "yes"})
    assert error is not None and "'once'" in error


def test_a_watch_is_fired_only_once_marked():
    assert not pe.watch_fired({"event": "run.failed", "once": True})
    assert pe.watch_fired({"event": "run.failed", "once": True, "fired_event_id": "e1"})
    # A standing subscription is never "fired", whatever it carries.
    assert not pe.watch_fired({"event": "run.failed", "fired_event_id": "e1"})
    assert not pe.watch_fired(None)


def test_the_run_of_a_watch_says_so():
    assert pe.trigger_context("run.failed", {"event_id": "e1"}, uuid.uuid4(), once=True)["once"] is True
    assert "once" not in pe.trigger_context("run.failed", {"event_id": "e1"}, uuid.uuid4())


class _Result:
    def __init__(self, row):
        self._row = row

    def scalar_one_or_none(self):
        return self._row


class _Db:
    def __init__(self, row):
        self.row = row
        self.rolled_back = False

    async def execute(self, _stmt):
        return _Result(self.row)

    async def rollback(self):
        self.rolled_back = True


@pytest.mark.asyncio
async def test_consume_marks_the_watch_without_committing():
    row = SimpleNamespace(enabled=True, trigger_type="platform_event", trigger_config={"event": "run.failed", "once": True})
    db = _Db(row)
    assert await pe._lock_current(db, uuid.uuid4(), "run.failed", {"event_id": "e7"}) == (row, True)
    assert row.trigger_config["fired_event_id"] == "e7" and row.trigger_config["fired_at"]
    assert row.trigger_config["event"] == "run.failed" and not db.rolled_back


@pytest.mark.asyncio
async def test_a_standing_row_is_locked_and_never_marked():
    row = SimpleNamespace(enabled=True, trigger_type="platform_event", trigger_config={"event": "run.failed"})
    db = _Db(row)
    assert await pe._lock_current(db, uuid.uuid4(), "run.failed", {"event_id": "e7"}) == (row, False)
    assert row.trigger_config == {"event": "run.failed"} and not db.rolled_back


class _RecordingDb:
    """A session that records, per read, whether it locked the row (``FOR UPDATE``)."""

    def __init__(self, row: Any) -> None:
        self.row = row
        self.rolled_back = False
        self.locked: list[bool] = []

    async def execute(self, stmt: Any) -> Any:
        self.locked.append(stmt._for_update_arg is not None)
        return SimpleNamespace(scalar_one_or_none=lambda: self.row)

    async def rollback(self) -> None:
        self.rolled_back = True


@pytest.mark.asyncio
async def test_a_standing_subscription_is_read_without_a_row_lock() -> None:
    # A burst of one event (ctem_case.opened per case of a drain) never queues on one assignments row.
    row = SimpleNamespace(enabled=True, trigger_type="platform_event", trigger_config={"event": "run.failed"})
    db = _RecordingDb(row)
    assert await pe._lock_current(db, uuid.uuid4(), "run.failed", {"event_id": "e7"}, lock=False) == (row, False)
    assert db.locked == [False]


@pytest.mark.asyncio
async def test_a_row_that_became_a_watch_is_read_again_locked() -> None:
    row = SimpleNamespace(
        enabled=True, trigger_type="platform_event", trigger_config={"event": "run.failed", "once": True}
    )
    db = _RecordingDb(row)
    assert await pe._lock_current(db, uuid.uuid4(), "run.failed", {"event_id": "e7"}, lock=False) == (row, True)
    assert db.locked == [False, True]
    assert row.trigger_config["fired_event_id"] == "e7"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "row",
    [
        None,
        SimpleNamespace(enabled=False, trigger_type="platform_event", trigger_config={"event": "run.failed", "once": True}),
        SimpleNamespace(
            enabled=True,
            trigger_type="platform_event",
            trigger_config={"event": "run.failed", "once": True, "fired_event_id": "e1"},
        ),
        # Edited after the subscriptions were read: the locked row wins.
        SimpleNamespace(enabled=True, trigger_type="platform_event", trigger_config={"event": "run.completed", "once": True}),
        SimpleNamespace(enabled=True, trigger_type="schedule", trigger_config={"event": "run.failed", "once": True}),
        SimpleNamespace(
            enabled=True,
            trigger_type="platform_event",
            trigger_config={"event": "run.failed", "once": True, "filter": {"reason": "rollout"}},
        ),
    ],
    ids=["gone", "disabled", "fired", "event-edited", "type-edited", "filter-edited"],
)
async def test_a_fired_gone_disabled_or_edited_watch_is_not_consumed(row):
    db = _Db(row)
    assert await pe._lock_current(db, uuid.uuid4(), "run.failed", {"event_id": "e2", "reason": "crash"}) is None
    assert db.rolled_back


@pytest.fixture
def watch_world(monkeypatch):
    import app.core.database as database
    from app.services import assignment_engine

    # The assignment row as the database holds it at delivery time; a test edits
    # it between the subscription snapshot and the delivery.
    state = SimpleNamespace(
        row=SimpleNamespace(
            enabled=True, trigger_type="platform_event", trigger_config={"event": "work_item.completed", "once": True}
        ),
        triggered=[],
        live=None,
    )

    class _Session(_Db):
        def __init__(self):
            super().__init__(state.row)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    async def note_live(db, assignment_id, name, payload, *, pending=False):
        state.note_pending = pending
        return state.live

    async def duplicate(db, agent_id, key, *, window_seconds):
        return None

    async def trigger(assignment, agent, payload=None, *, db, trigger_context=None, overflow=None, queue=True):
        state.triggered.append((assignment, trigger_context))
        return SimpleNamespace(status="ok", run_id="r1")

    monkeypatch.setattr(database, "async_session", _Session)
    monkeypatch.setattr(pe, "_note_live_holder", note_live)
    monkeypatch.setattr(assignment_engine, "duplicate_event_run", duplicate)
    monkeypatch.setattr(assignment_engine, "trigger_or_queue_assignment", trigger)
    return state


@pytest.mark.asyncio
async def test_a_watch_starts_one_run_then_ends(watch_world):
    sub = _sub()
    armed = watch_world.row
    first = await pe._deliver("work_item.completed", {"event_id": "e1"}, sub, object(), object())
    second = await pe._deliver("work_item.completed", {"event_id": "e2"}, sub, object(), object())
    assert first == "ok run r1" and second == "watch already fired or changed"
    assert len(watch_world.triggered) == 1
    # The locked, marked row is the one triggered, so the mark commits with the run.
    assignment, context = watch_world.triggered[0]
    assert assignment is armed and context["once"] is True


@pytest.mark.asyncio
async def test_the_trigger_picker_reads_the_builder_catalog(monkeypatch: pytest.MonkeyPatch):
    from app.api.v1.assignments import list_platform_events
    from app.services import ctem_access

    async def _wires(_user_id: object) -> bool:
        return True

    monkeypatch.setattr(ctem_access, "wires_ctem", _wires)
    served = await list_platform_events(user=SimpleNamespace(id=uuid.uuid4()))
    assert served == pe.catalog()
    assert {e["event"] for e in served} == set(pe.PLATFORM_EVENTS)


@pytest.mark.asyncio
async def test_the_trigger_picker_hides_the_ctem_events_without_a_right(monkeypatch: pytest.MonkeyPatch):
    """#2441: a person with no CTEM right is offered no ctem_signal.* or case event."""
    from app.api.v1.assignments import list_platform_events
    from app.services import ctem_access

    async def _wires(_user_id: object) -> bool:
        return False

    monkeypatch.setattr(ctem_access, "wires_ctem", _wires)
    served = await list_platform_events(user=SimpleNamespace(id=uuid.uuid4()))
    assert served == pe.catalog(ctem=False)
    assert not any(pe.is_ctem_event(e["event"]) for e in served)


@pytest.mark.asyncio
async def test_the_first_event_consumes_the_watch_even_for_a_live_holder(watch_world):
    # The first matching event is the one: told to the live run holding the item,
    # the watch is spent with that note, and a later event does not start a run.
    watch_world.live = "run-live"
    watch_world.row.trigger_config = {"event": "work_item.note_added", "once": True}
    sub = _sub(events=("work_item.note_added",))
    first = await pe._deliver("work_item.note_added", {"event_id": "e1"}, sub, object(), object())
    watch_world.live = None
    second = await pe._deliver("work_item.note_added", {"event_id": "e2"}, sub, object(), object())
    assert first == "noted for live run run-live" and watch_world.note_pending is True
    assert second == "watch already fired or changed" and watch_world.triggered == []
    assert watch_world.row.trigger_config["fired_event_id"] == "e1"


def test_a_duplicated_assignment_starts_with_its_watch_armed():
    from app.models.agent_copy import strip_trigger_config_internal_ids

    fired = {"event": "run.failed", "once": True, "scope": "own", "fired_event_id": "e1", "fired_at": "t"}
    assert strip_trigger_config_internal_ids(fired) == {"event": "run.failed", "once": True, "scope": "own"}
    # The agent duplicate route keeps every other key and re-arms the watch.
    assert pe.rearm_watch(fired) == {"event": "run.failed", "once": True, "scope": "own"}
    assert pe.rearm_watch(None) is None


@pytest.mark.asyncio
async def test_a_standing_subscription_is_never_consumed(watch_world):
    watch_world.row.trigger_config = {"event": "work_item.completed"}
    sub = _sub(once=False)
    for event_id in ("e1", "e2"):
        await pe._deliver("work_item.completed", {"event_id": event_id}, sub, object(), object())
    assert len(watch_world.triggered) == 2
    assert "fired_event_id" not in watch_world.row.trigger_config
    assert all("once" not in context for _, context in watch_world.triggered)


@pytest.mark.asyncio
async def test_a_standing_snapshot_edited_into_a_watch_is_consumed_by_the_locked_config(watch_world):
    # Snapshot standing, row edited to once before delivery: the locked config
    # decides, so the run is one-off and the watch is marked with it.
    sub = _sub(once=False)
    first = await pe._deliver("work_item.completed", {"event_id": "e1"}, sub, object(), object())
    second = await pe._deliver("work_item.completed", {"event_id": "e2"}, sub, object(), object())
    assert first == "ok run r1" and second == "watch already fired or changed"
    assert watch_world.triggered[0][1]["once"] is True
    assert watch_world.row.trigger_config["fired_event_id"] == "e1"


def test_a_deadline_must_be_an_aware_date_time():
    ok = {"event": "run.failed", "once": True, "expires_at": "2026-11-02T09:00:00Z"}
    assert pe.trigger_config_error("platform_event", ok) is None
    assert pe.trigger_config_error("platform_event", {"event": "run.failed", "expires_at": None}) is None
    for bad in ("tomorrow", "2026-11-02T09:00:00", 3600, ""):
        error = pe.trigger_config_error("platform_event", {"event": "run.failed", "expires_at": bad})
        assert error is not None and "'expires_at'" in error


def test_a_subscription_ends_at_its_deadline():
    from datetime import UTC, datetime

    now = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)
    assert not pe.expired({"event": "run.failed"}, now)
    assert not pe.expired({"event": "run.failed", "expires_at": "2026-11-01T12:00:01Z"}, now)
    assert pe.expired({"event": "run.failed", "expires_at": "2026-11-01T12:00:00Z"}, now)
    # Another offset, the same instant.
    assert pe.expired({"event": "run.failed", "expires_at": "2026-11-01T13:00:00+01:00"}, now)
    # A hand-edited deadline that does not parse is not delivered.
    assert pe.expired({"event": "run.failed", "expires_at": "soon"}, now)


def test_a_deadline_travels_with_a_copy():
    from app.models.agent_copy import strip_trigger_config_internal_ids

    cfg = {"event": "run.failed", "once": True, "expires_at": "2026-11-02T09:00:00Z"}
    assert strip_trigger_config_internal_ids(cfg) == cfg


@pytest.mark.asyncio
async def test_an_expired_watch_never_fires(watch_world):
    watch_world.row.trigger_config = {
        "event": "work_item.completed",
        "once": True,
        "expires_at": "2000-01-01T00:00:00Z",
    }
    out = await pe._deliver("work_item.completed", {"event_id": "e1"}, _sub(), object(), object())
    assert out == "watch already fired or changed" and watch_world.triggered == []
    # Not consumed: saving a later deadline brings the same watch back armed.
    assert "fired_event_id" not in watch_world.row.trigger_config


@pytest.mark.asyncio
async def test_a_watch_before_its_deadline_fires_once(watch_world):
    watch_world.row.trigger_config = {
        "event": "work_item.completed",
        "once": True,
        "expires_at": "2999-01-01T00:00:00Z",
    }
    first = await pe._deliver("work_item.completed", {"event_id": "e1"}, _sub(), object(), object())
    second = await pe._deliver("work_item.completed", {"event_id": "e2"}, _sub(), object(), object())
    assert first == "ok run r1" and second == "watch already fired or changed"
    assert len(watch_world.triggered) == 1


@pytest.mark.asyncio
async def test_an_expired_standing_subscription_is_not_read(monkeypatch):
    rows = [
        (
            SimpleNamespace(
                id=uuid.uuid4(),
                user_id=None,
                trigger_config={"event": "run.failed", "expires_at": "2000-01-01T00:00:00Z"},
            ),
            SimpleNamespace(id=uuid.uuid4()),
            "user",
        ),
        (
            SimpleNamespace(
                id=uuid.uuid4(),
                user_id=None,
                trigger_config={"event": "run.failed", "expires_at": "2999-01-01T00:00:00Z"},
            ),
            SimpleNamespace(id=uuid.uuid4()),
            "user",
        ),
    ]

    class _Rows:
        def all(self):
            return rows

    class _SubsDb:
        async def execute(self, _stmt):
            return _Rows()

    subs = await pe._subscriptions(_SubsDb(), "run.failed")
    assert [s.assignment_id for s, _, _ in subs] == [rows[1][0].id]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "config",
    [{"event": "work_item.note_added"}, {"event": "work_item.completed", "filter": {"status": "blocked"}}],
    ids=["event-edited", "filter-edited"],
)
async def test_a_standing_snapshot_edited_away_starts_no_run(watch_world, config):
    watch_world.row.trigger_config = config
    out = await pe._deliver("work_item.completed", {"event_id": "e1", "status": "done"}, _sub(once=False), object(), object())
    assert out == "watch already fired or changed" and watch_world.triggered == []
