# SPDX-License-Identifier: Apache-2.0

"""CTEM segregation (#933): case events reach readers of the case only, and the
scope gate decides a signal with its recorder's rights."""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

from app.services import ctem_case_loop as loop
from app.services import ctem_scope_gate as gate
from app.services import ctem_scope_gate_store as store
from app.services import platform_events as pe

CASE_EVENTS = (
    "ctem_case.opened",
    "ctem_case.reopened",
    "ctem_case.resolved",
    "ctem_case.sla_breached",
    "ctem_case.validation_finished",
)


def _case(**extra) -> SimpleNamespace:
    base = {
        "id": uuid.uuid4(),
        "organization_id": uuid.uuid4(),
        "vertical": "default",
        "state": "new",
        "remediation_kind": "patch",
        "trigger_path": "new_vulnerability",
        "severity": "P1",
        "sla_due_at": None,
        "owner_user_id": uuid.uuid4(),
        "risk_scenario_ids": ["crq-1"],
    }
    base.update(extra)
    return SimpleNamespace(**base)


def _sub(user_id, *, run_as=None, scope_all=False) -> pe.Subscription:
    return pe.Subscription(
        assignment_id=uuid.uuid4(),
        agent_id=uuid.uuid4(),
        events=CASE_EVENTS,
        filter={},
        scope_all=scope_all,
        user_id=user_id,
        run_as_user_id=run_as,
    )


def test_case_events_are_no_longer_platform_wide_and_carry_routing_only():
    for name in CASE_EVENTS:
        assert name not in pe.PLATFORM_WIDE_EVENTS
        assert name in pe.VIEWER_CHECKED_EVENTS
    payload = loop.case_data(_case())
    assert set(payload) == {
        "case_id",
        "vertical",
        "state",
        "trigger_path",
        "object_type",
        "object_id",
        "participant_agent_ids",
    }
    # owner_fleet is stamped at publish (ctem_case_routing.stamp_owners, #1390), never by case_data.
    for key in pe.PLATFORM_EVENTS["ctem_case.opened"]["filter_keys"]:
        assert key in payload or key == "owner_fleet"


@pytest.fixture
def world(monkeypatch):
    import app.core.database as database

    class _Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    monkeypatch.setattr(database, "async_session", _Session)
    state = SimpleNamespace(subs=[], delivered=[], readers=set(), asked=[])

    async def subscriptions(db, name):
        return state.subs

    async def deliver(name, payload, sub, assignment, agent):
        state.delivered.append(sub.assignment_id)
        return "ok"

    async def reads(user_id, payload):
        state.asked.append(user_id)
        return user_id in state.readers

    async def no_watches(name, payload):
        return []

    from app.services import flow_triggers, platform_event_task_watches

    monkeypatch.setattr(pe, "_subscriptions", subscriptions)
    monkeypatch.setattr(pe, "_deliver", deliver)
    monkeypatch.setattr(pe, "user_reads_object", reads)
    monkeypatch.setattr(platform_event_task_watches, "dispatch_event", no_watches)
    monkeypatch.setattr(flow_triggers, "dispatch_flow_platform_event", no_watches)
    return state


def _event(name="ctem_case.opened"):
    return {**loop.case_data(_case()), "event": name, "event_id": "e1"}


@pytest.mark.asyncio
async def test_a_non_admin_assignment_is_not_woken_for_a_case_its_user_cannot_open(world):
    reader, outsider = uuid.uuid4(), uuid.uuid4()
    world.readers = {str(reader)}
    inside, outside = _sub(reader), _sub(outsider)
    row = SimpleNamespace(ignore_self_triggered_events=False)
    world.subs = [(inside, row, None), (outside, row, None)]
    await pe.dispatch_event("ctem_case.opened", _event())
    assert world.delivered == [inside.assignment_id]


@pytest.mark.asyncio
async def test_the_run_as_user_must_see_the_case_too(world):
    owner, run_as = uuid.uuid4(), uuid.uuid4()
    world.readers = {str(owner)}
    row = SimpleNamespace(ignore_self_triggered_events=False)
    world.subs = [(_sub(owner, run_as=run_as), row, None)]
    await pe.dispatch_event("ctem_case.sla_breached", _event("ctem_case.sla_breached"))
    assert world.delivered == []


@pytest.mark.asyncio
async def test_an_admin_scope_all_hears_every_case_as_itself_never_through_another_run_as(world):
    admin, outsider = uuid.uuid4(), uuid.uuid4()
    world.readers = set()
    as_itself = _sub(admin, scope_all=True)
    as_other = _sub(admin, run_as=outsider, scope_all=True)
    row = SimpleNamespace(ignore_self_triggered_events=False)
    world.subs = [(as_itself, row, None), (as_other, row, None)]
    await pe.dispatch_event("ctem_case.resolved", _event("ctem_case.resolved"))
    assert world.delivered == [as_itself.assignment_id]


@pytest.mark.asyncio
async def test_an_unreadable_view_refuses_the_delivery(monkeypatch):
    import app.core.database as database
    from app.services import ctem_cases_view

    class _Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    async def boom(*args, **kwargs):
        raise RuntimeError("source down")

    monkeypatch.setattr(database, "async_session", _Session)
    monkeypatch.setattr(ctem_cases_view, "user_sees_case", boom)
    assert await pe.user_reads_object(str(uuid.uuid4()), _event()) is False
    assert await pe.user_reads_object(str(uuid.uuid4()), {"event": "ctem_case.opened"}) is False


@pytest.mark.asyncio
async def test_other_events_are_not_checked_against_a_case(world):
    sub = _sub(uuid.uuid4())
    assert await pe.can_read_object(sub, {"event": "work_item.note_added"}, {}) is True
    assert world.asked == []


def _flow(owner):
    return SimpleNamespace(id=uuid.uuid4(), user_id=owner, graph={"nodes": []})


@pytest.mark.asyncio
async def test_a_flow_hears_a_case_only_when_its_owner_sees_it(monkeypatch):
    from app.services import flow_triggers

    owner = uuid.uuid4()
    seen: dict[str, bool] = {}

    async def reads(user_id, payload):
        return seen.get(user_id, False)

    async def role(db, owner_id):
        return "member"

    monkeypatch.setattr(pe, "user_reads_object", reads)
    monkeypatch.setattr(flow_triggers, "_owner_role", role)
    node = {"id": "t", "config": {"events": ["ctem_case.opened"]}}
    payload = _event()
    assert await flow_triggers._flow_node_takes(None, _flow(owner), node, "ctem_case.opened", payload, {}) is False
    seen[str(owner)] = True
    assert await flow_triggers._flow_node_takes(None, _flow(owner), node, "ctem_case.opened", payload, {}) is True


def _signal(recorded_by, **extra):
    base = {
        "id": uuid.uuid4(),
        "organization_id": uuid.uuid4(),
        "state": "new",
        "trigger_path": "new_exposure",
        "lens": "outside",
        "subject_kind": "asset",
        "subject_ref": "shop.example.com",
        "source_provider": "manual",
        "source_external_id": None,
        "subject_label": None,
        "evidence": {},
        "decision": None,
        "updated_at": None,
        "recorded_by": recorded_by,
    }
    base.update(extra)
    return SimpleNamespace(**base)


def test_the_scenario_reader_is_the_recorder_and_system_signals_use_the_evaluator():
    admin, user = uuid.uuid4(), uuid.uuid4()
    assert store.scenario_reader(_signal(str(user)), admin) == user
    assert store.scenario_reader(_signal("system"), admin) == admin
    assert store.scenario_reader(_signal("not-a-user"), admin) is None


@pytest.mark.asyncio
async def test_an_admin_evaluation_writes_no_admin_scenario_into_a_user_signal(monkeypatch):
    """After POST /ctem-signals/evaluate by an administrator, a user with no OpenCRQ
    access carries no risk scenario id or name in their signal."""
    admin, user = uuid.uuid4(), uuid.uuid4()
    user_signal = _signal(str(user))
    system_signal = _signal("system")
    read_for: list = []

    async def read(db, *, user_id):
        read_for.append(user_id)
        if user_id == admin:
            index = gate.ScenarioIndex(connected=True, note="")
            ref = gate.RiskScenarioRef(id="crq-secret", name="Ransomware on billing", criticality="critical")
            index.by_asset_name["shop.example.com"] = [ref]
            return index
        return gate.ScenarioIndex(note="OpenCRQ not read: no OpenCRQ account")

    async def profile(db, organization_id):
        from tests.test_ctem_scope_gate import _profile

        return _profile()

    async def no_related(db, row):
        return []

    class _Result:
        def scalars(self):
            return self

        def all(self):
            return [user_signal, system_signal]

    class _Db:
        async def execute(self, statement):
            return _Result()

    monkeypatch.setattr(store, "read_scenarios", read)
    monkeypatch.setattr(store, "_profile", profile)
    monkeypatch.setattr(store, "related_signals", no_related)
    await store.evaluate_open(_Db(), organization_id=uuid.uuid4(), user_id=admin)

    assert sorted(map(str, read_for)) == sorted([str(user), str(admin)])
    scope = user_signal.decision["scope"]
    assert scope["risk_scenarios"] == []
    assert "crq-secret" not in str(user_signal.decision)
    assert "Ransomware on billing" not in str(user_signal.decision)
    # The system signal, read by administrators only, keeps the administrator's read.
    assert "crq-secret" in str(system_signal.decision)


@pytest.mark.asyncio
async def test_related_signals_of_a_non_admin_recorder_are_their_own(monkeypatch):
    from sqlalchemy.dialects import postgresql

    user = uuid.uuid4()
    seen: list[str] = []

    class _Result:
        def scalars(self):
            return self

        def all(self):
            return []

    class _Db:
        async def get(self, model, key):
            return SimpleNamespace(is_admin=False)

        async def execute(self, statement):
            seen.append(str(statement.compile(dialect=postgresql.dialect())))
            return _Result()

    await store.related_signals(_Db(), _signal(str(user)))
    assert "ctem_signals.recorded_by = " in seen[0]

    class _AdminDb(_Db):
        async def get(self, model, key):
            return SimpleNamespace(is_admin=True)

    seen.clear()
    await store.related_signals(_AdminDb(), _signal(str(user)))
    assert "ctem_signals.recorded_by = " not in seen[0]
    seen.clear()
    await store.related_signals(_AdminDb(), _signal("system"))
    assert "ctem_signals.recorded_by = " not in seen[0]
