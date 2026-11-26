# SPDX-License-Identifier: Apache-2.0

"""CTEM signal platform events (#1409): ``ctem_signal.recorded`` on a first sighting,
nothing on a re-sighting, ``ctem_signal.triaged`` on a verdict change, delivered only
to subscribers whose run identity reads the signal, filtered on routing fields."""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

from app.services import ctem_case_loop as loop
from app.services import ctem_scope_gate_store as store
from app.services import ctem_signals as signals
from app.services import platform_events as pe


def _signal(**extra) -> SimpleNamespace:
    base = {
        "id": uuid.uuid4(),
        "organization_id": uuid.uuid4(),
        "state": "new",
        "trigger_path": "new_exposure",
        "lens": "outside",
        "subject_kind": "asset",
        "subject_ref": "shop.example.com",
        "source_provider": "manual",
        "case_id": None,
        "decision": None,
        "updated_at": None,
        "recorded_by": "system",
    }
    base.update(extra)
    return SimpleNamespace(**base)


class _Nested:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _Db:
    def begin_nested(self):
        return _Nested()


def _decide_to(monkeypatch, state: str) -> None:
    from app.services import ctem_scope_gate as gate

    async def profile(db, organization_id):
        return None

    async def no_related(db, row):
        return []

    def decide(profile, facts, **_):
        return SimpleNamespace(state=state, record={})

    monkeypatch.setattr(store, "_profile", profile)
    monkeypatch.setattr(store, "related_signals", no_related)
    monkeypatch.setattr(store, "facts_of", lambda row: {})
    monkeypatch.setattr(gate, "decide", decide)


def test_both_events_are_registered_viewer_checked_with_routing_filter_keys():
    for name in signals.SIGNAL_EVENTS:
        assert name in pe.PLATFORM_EVENTS
        assert name in pe.VIEWER_CHECKED_EVENTS
        assert name not in pe.PLATFORM_WIDE_EVENTS
    payload = signals.signal_event_data(_signal(state="to_validate"), triaged=True, previous_verdict="watchlist")
    for name in signals.SIGNAL_EVENTS:
        for key in pe.PLATFORM_EVENTS[name]["filter_keys"]:
            assert key in payload, (name, key)
    # Ids and routing only: no subject, evidence or decision travels in the event.
    assert "subject_ref" not in payload and "evidence" not in payload and "decision" not in payload
    assert payload["verdict"] == "to_validate" and payload["previous_verdict"] == "watchlist"


@pytest.mark.asyncio
async def test_a_first_sighting_raises_recorded_with_the_gate_verdict(monkeypatch):
    _decide_to(monkeypatch, "to_validate")

    async def no_case(db, row, *, events):
        return None

    monkeypatch.setattr(loop, "open_from_signal", no_case)
    row = _signal()
    pending = await store.evaluate_after_record(_Db(), row, created=True)
    names = [name for name, _, _ in pending.items]
    assert names == ["ctem_signal.recorded"]
    _, data, event_id = pending.items[0]
    assert data["verdict"] == "to_validate" and data["signal_id"] == str(row.id)
    assert "previous_verdict" not in data
    assert event_id == signals.signal_event_id("ctem_signal.recorded", row, "first")


@pytest.mark.asyncio
async def test_a_confirmed_first_sighting_keeps_its_verdict_ahead_of_the_case_events(monkeypatch):
    _decide_to(monkeypatch, "confirmed")

    async def open_case(db, row, *, events):
        row.state = "case_opened"
        row.case_id = uuid.uuid4()
        events.add("ctem_case.opened", {"case_id": str(row.case_id)}, "c1")

    monkeypatch.setattr(loop, "open_from_signal", open_case)
    row = _signal()
    pending = await store.evaluate_after_record(_Db(), row, created=True)
    assert [name for name, _, _ in pending.items] == ["ctem_signal.recorded", "ctem_case.opened"]
    data = pending.items[0][1]
    assert data["verdict"] == "confirmed" and data["case_id"] == str(row.case_id)


@pytest.mark.asyncio
async def test_a_re_sighting_raises_nothing():
    pending = await store.evaluate_after_record(_Db(), _signal(state="to_validate"), created=False)
    assert pending.items == []


@pytest.mark.asyncio
async def test_a_verdict_change_raises_triaged_and_an_unchanged_one_nothing(monkeypatch):
    _decide_to(monkeypatch, "confirmed")
    row = _signal(state="to_validate")
    events = loop.PendingEvents()
    await store.evaluate(_Db(), row, events=events)
    assert [name for name, _, _ in events.items] == ["ctem_signal.triaged"]
    data = events.items[0][1]
    assert data["previous_verdict"] == "to_validate" and data["verdict"] == "confirmed"

    again = loop.PendingEvents()
    await store.evaluate(_Db(), row, events=again)
    assert again.items == []


@pytest.mark.asyncio
async def test_the_first_verdict_of_a_new_signal_is_not_a_triage(monkeypatch):
    _decide_to(monkeypatch, "watchlist")
    events = loop.PendingEvents()
    await store.evaluate(_Db(), _signal(state="new"), events=events)
    assert events.items == []


@pytest.fixture
def world(monkeypatch):
    import app.core.database as database

    class _Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    monkeypatch.setattr(database, "async_session", _Session)
    state = SimpleNamespace(subs=[], delivered=[], readers=set())

    async def subscriptions(db, name):
        return state.subs

    async def deliver(name, payload, sub, assignment, agent):
        state.delivered.append(sub.assignment_id)
        return "ok"

    async def reads(db, user_id, signal_id):
        return str(user_id) in state.readers

    async def no_watches(name, payload):
        return []

    from app.services import flow_triggers, platform_event_task_watches

    monkeypatch.setattr(pe, "_subscriptions", subscriptions)
    monkeypatch.setattr(pe, "_deliver", deliver)
    monkeypatch.setattr(signals, "user_reads_signal", reads)
    monkeypatch.setattr(platform_event_task_watches, "dispatch_event", no_watches)
    monkeypatch.setattr(flow_triggers, "dispatch_flow_platform_event", no_watches)
    return state


def _sub(user_id, *, filter=None, scope_all=False) -> pe.Subscription:
    return pe.Subscription(
        assignment_id=uuid.uuid4(),
        agent_id=uuid.uuid4(),
        events=signals.SIGNAL_EVENTS,
        filter=filter or {},
        scope_all=scope_all,
        user_id=user_id,
        run_as_user_id=None,
    )


def _event(name="ctem_signal.recorded", **extra):
    return {**signals.signal_event_data(_signal(state="to_validate", **extra)), "event": name, "event_id": "e1"}


@pytest.mark.asyncio
async def test_a_non_admin_subscriber_does_not_hear_an_admin_only_signal(world):
    admin, member = uuid.uuid4(), uuid.uuid4()
    world.readers = {str(admin)}
    row = SimpleNamespace(ignore_self_triggered_events=False)
    heard, deaf = _sub(admin), _sub(member)
    world.subs = [(heard, row, None), (deaf, row, None)]
    await pe.dispatch_event("ctem_signal.recorded", _event())
    assert world.delivered == [heard.assignment_id]


@pytest.mark.asyncio
async def test_filters_match_on_routing_fields(world):
    reader = uuid.uuid4()
    world.readers = {str(reader)}
    row = SimpleNamespace(ignore_self_triggered_events=False)
    wanted = _sub(reader, filter={"trigger_path": ["new_exposure", "credential_leak"], "verdict": "to_validate"})
    other_path = _sub(reader, filter={"trigger_path": "new_vulnerability"})
    other_lens = _sub(reader, filter={"lens": ["world"]})
    world.subs = [(wanted, row, None), (other_path, row, None), (other_lens, row, None)]
    await pe.dispatch_event("ctem_signal.recorded", _event())
    assert world.delivered == [wanted.assignment_id]


@pytest.mark.asyncio
async def test_user_reads_signal_is_admin_or_a_ctem_right(monkeypatch):
    """#2441: an administrator or a CTEM view or decide right reads a signal; recording it grants nothing."""
    from app.services import ctem_access

    recorder, admin, viewer = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()

    async def _right(_db, user_id):
        return "view" if str(user_id) == str(viewer) else None

    monkeypatch.setattr(ctem_access, "group_right", _right)

    def db_for(recorded_by):
        class _Result:
            def scalar_one_or_none(self):
                return recorded_by

        class _Db:
            async def execute(self, statement):
                return _Result()

            async def get(self, model, key):
                return SimpleNamespace(is_admin=key == admin)

        return _Db()

    sid = uuid.uuid4()
    assert await signals.user_reads_signal(db_for(str(recorder)), str(recorder), sid) is False
    assert await signals.user_reads_signal(db_for(str(recorder)), str(viewer), sid) is True
    assert await signals.user_reads_signal(db_for("system"), str(viewer), sid) is True
    assert await signals.user_reads_signal(db_for("system"), str(admin), sid) is True
    assert await signals.user_reads_signal(db_for(None), str(admin), sid) is False
    assert await signals.user_reads_signal(db_for("system"), "not-a-uuid", sid) is False
