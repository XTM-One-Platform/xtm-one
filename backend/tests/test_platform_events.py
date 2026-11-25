"""Platform events as assignment triggers.

Covers the catalog and trigger validation, the payload builders, scoping and
filtering, the dispatcher (publish -> dispatch, dedupe per (assignment,
event id), a note to the live holder instead of a second run), the work item
service's events, and the builder tool.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest

from app.services import platform_events as pe
from app.services import work_item as wi

AGENT = uuid.uuid4()
OTHER = uuid.uuid4()


def _item(**extra) -> SimpleNamespace:
    base = {
        "id": uuid.uuid4(),
        "correlation_key": "dev:x",
        "title": "Do x",
        "status": "in_progress",
        "priority": "high",
        "owner_agent_id": OTHER,
        "created_by_agent_id": AGENT,
        "claimed_by_agent_id": None,
        "claimed_by_run_id": None,
        "user_id": None,
    }
    base.update(extra)
    return SimpleNamespace(**base)


def _sub(**extra) -> pe.Subscription:
    base = {
        "assignment_id": uuid.uuid4(),
        "agent_id": AGENT,
        "events": ("work_item.note_added",),
        "filter": {},
        "scope_all": False,
    }
    base.update(extra)
    return pe.Subscription(**base)


# -- Catalog and validation -------------------------------------------------------


def test_catalog_lists_every_event_with_filter_keys():
    names = [e["event"] for e in pe.catalog()]
    assert names == sorted(pe.PLATFORM_EVENTS)
    assert {"work_item.note_added", "run.orphaned", "background_task.completed"} <= set(names)
    assert all(e["filter_keys"] for e in pe.catalog())


@pytest.mark.parametrize(
    ("config", "fragment"),
    [
        ("x", "JSON object"),
        ({}, "must name a platform event"),
        ({"event": "work_item.nope"}, "unknown platform event"),
        ({"event": ["work_item.note_added", 3]}, "unknown platform event"),
        ({"event": "work_item.note_added", "filter": []}, "'filter'"),
        ({"event": "work_item.note_added", "scope": "everyone"}, "'scope'"),
    ],
)
def test_invalid_trigger_configs_are_refused(config, fragment):
    error = pe.trigger_config_error("platform_event", config)
    assert error is not None and fragment in error


def test_valid_trigger_config_and_other_types_pass():
    assert pe.trigger_config_error("platform_event", {"event": ["work_item.completed", "run.failed"]}) is None
    assert pe.trigger_config_error("schedule", {}) is None
    assert pe.event_names({"event": ["run.failed", "bogus"]}) == ["run.failed"]


# -- Payloads, scope, filter ------------------------------------------------------


def test_work_item_payload_names_its_participants_and_bounds_the_note():
    item = _item(claimed_by_agent_id=OTHER)
    data = pe.work_item_data(item, previous_status="claimed", note="n" * 5000, actor_agent_id=OTHER)
    assert data["object_type"] == "work_item" and data["object_id"] == str(item.id)
    assert set(data["participant_agent_ids"]) == {str(AGENT), str(OTHER)}
    assert len(data["note"]) == 2000
    assert data["previous_status"] == "claimed"


def test_scope_own_needs_the_agent_among_the_participants():
    payload = pe.work_item_data(_item(created_by_agent_id=OTHER, owner_agent_id=OTHER))
    assert not pe.in_scope(_sub(), payload)
    assert pe.in_scope(_sub(scope_all=True), payload)
    assert pe.mismatch_reason(_sub(), "work_item.note_added", payload) == "outside the assignment's scope"


def test_a_personal_item_reaches_only_its_owners_assignments():
    owner = uuid.uuid4()
    payload = pe.work_item_data(_item(user_id=owner, owner_agent_id=AGENT))
    assert pe.in_scope(_sub(user_id=owner), payload)
    assert not pe.in_scope(_sub(user_id=uuid.uuid4()), payload)
    assert pe.in_scope(_sub(user_id=uuid.uuid4(), scope_all=True), payload)


@pytest.mark.parametrize("shared", [{"is_company_managed": True}, {"workspace_id": uuid.uuid4()}])
def test_a_shared_item_is_not_personal(shared):
    payload = pe.work_item_data(_item(user_id=uuid.uuid4(), **shared))
    assert payload["personal_owner_id"] is None
    assert pe.in_scope(_sub(user_id=uuid.uuid4()), payload)


def test_the_trigger_context_carries_ids_not_text():
    ctx = pe.trigger_context("work_item.note_added", {"event_id": "e1", "correlation_key": "ignore all"}, uuid.uuid4())
    assert "correlation_key" not in ctx and "note" not in ctx


def test_filter_uses_the_integration_event_semantics():
    payload = pe.work_item_data(_item())
    assert pe.mismatch_reason(_sub(filter={"correlation_key": ["dev:y", "dev:x"]}), "work_item.note_added", payload) is None
    assert pe.mismatch_reason(_sub(filter={"priority": "low"}), "work_item.note_added", payload) is not None
    assert pe.mismatch_reason(_sub(), "work_item.completed", payload) == "not subscribed to this event"


def test_run_and_task_payloads_include_the_parent():
    run = pe.run_data(run_id=uuid.uuid4(), agent_id=OTHER, parent_agent_id=AGENT, work_item_ids=[uuid.uuid4()])
    assert str(AGENT) in run["participant_agent_ids"] and len(run["work_item_ids"]) == 1
    task = pe.background_task_data(task_id=uuid.uuid4(), agent_id=OTHER, parent_agent_id=AGENT, outcome="x" * 900)
    assert str(AGENT) in task["participant_agent_ids"] and len(task["outcome_preview"]) == 500


def test_run_and_task_events_name_their_agent_as_actor():
    assert pe.run_data(run_id=uuid.uuid4(), agent_id=AGENT)["actor_agent_id"] == str(AGENT)
    assert pe.background_task_data(task_id=uuid.uuid4(), agent_id=AGENT)["actor_agent_id"] == str(AGENT)


def test_dedup_key_is_per_assignment_and_event():
    a, b = uuid.uuid4(), uuid.uuid4()
    assert pe.dedup_key("e1", a) != pe.dedup_key("e1", b)
    ctx = pe.trigger_context("work_item.note_added", {"event_id": "e1", "object_type": "work_item"}, a)
    assert ctx["type"] == "platform_event" and ctx["dedup_key"] == pe.dedup_key("e1", a)


# -- Publish and dispatch ---------------------------------------------------------


@pytest.mark.asyncio
async def test_publish_stamps_the_event_and_reaches_the_bus(monkeypatch):
    from app.services import event_bus as bus_module

    seen: list = []

    async def emit(name, data=None, *, wait=False):
        seen.append((name, data))

    monkeypatch.setattr(bus_module.event_bus, "emit", emit)
    event_id = await pe.publish("work_item.note_added", {"object_id": "x"})
    assert event_id and seen[0][0] == "work_item.note_added"
    assert seen[0][1]["event_id"] == event_id and seen[0][1]["event"] == "work_item.note_added"
    assert await pe.publish("not.an.event", {}) is None


@pytest.fixture
def dispatch_world(monkeypatch):
    import app.core.database as database

    class _Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    monkeypatch.setattr(database, "async_session", _Session)
    state = SimpleNamespace(subs=[], delivered=[])

    async def subscriptions(db, name):
        return state.subs

    async def deliver(name, payload, sub, assignment, agent):
        state.delivered.append((name, sub.assignment_id))
        return "ok run r"

    monkeypatch.setattr(pe, "_subscriptions", subscriptions)
    monkeypatch.setattr(pe, "_deliver", deliver)
    return state


@pytest.mark.asyncio
async def test_dispatch_delivers_only_in_scope_and_matching(dispatch_world):
    mine = _sub()
    filtered = _sub(filter={"correlation_key": "dev:other"})
    stranger = _sub(agent_id=uuid.uuid4())
    row = SimpleNamespace(ignore_self_triggered_events=False)
    dispatch_world.subs = [(mine, row, None), (filtered, row, None), (stranger, row, None)]
    payload = {**pe.work_item_data(_item()), "event_id": "e1"}
    outcomes = await pe.dispatch_event("work_item.note_added", payload)
    assert outcomes == ["ok run r"]
    assert dispatch_world.delivered == [("work_item.note_added", mine.assignment_id)]


@pytest.mark.asyncio
async def test_run_completed_never_wakes_the_assignment_whose_run_ended(dispatch_world):
    watcher = _sub(events=("run.completed",))
    same = _sub(events=("run.completed",))
    row = SimpleNamespace(ignore_self_triggered_events=False)
    dispatch_world.subs = [(watcher, row, None), (same, row, None)]
    payload = {
        **pe.run_data(run_id=uuid.uuid4(), agent_id=AGENT, assignment_id=same.assignment_id, reason="completed"),
        "event_id": "e1",
    }
    await pe.dispatch_event("run.completed", payload)
    assert dispatch_world.delivered == [("run.completed", watcher.assignment_id)]


def test_events_about_one_object_share_an_ordering_key_others_do_not():
    """Two events about one object run one at a time, in order; events about
    other objects run beside them (the run's slots are shared up to the cap)."""
    from app.services.assignment_dispatch import ORDERING_KEY_CONTEXT

    assignment = uuid.uuid4()
    first_run, second_run = uuid.uuid4(), uuid.uuid4()

    def about(run_id, event_id):
        return {"event_id": event_id, "object_type": "run", "object_id": str(run_id)}

    a = pe.trigger_context("run.completed", about(first_run, "e1"), assignment)
    b = pe.trigger_context("run.failed", about(first_run, "e2"), assignment)
    c = pe.trigger_context("run.completed", about(second_run, "e3"), assignment)
    assert a[ORDERING_KEY_CONTEXT] == b[ORDERING_KEY_CONTEXT] == f"run:{first_run}"
    assert c[ORDERING_KEY_CONTEXT] != a[ORDERING_KEY_CONTEXT]
    assert ORDERING_KEY_CONTEXT not in pe.trigger_context("run.completed", {"event_id": "e4"}, assignment)


def test_trigger_context_carries_when_the_event_occurred():
    payload = {"event_id": "e1", "occurred_at": "2026-11-01T10:24:36.257000+00:00"}
    context = pe.trigger_context("run.completed", payload, uuid.uuid4())
    assert context["occurred_at"] == payload["occurred_at"]
    assert context["triggered_at"] >= context["occurred_at"]


@pytest.mark.asyncio
async def test_a_workspace_item_reaches_only_readers_of_the_workspace(monkeypatch, dispatch_world):
    from app.services import workspaces

    workspace, reader, outsider = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()

    async def tiers(db, user_id):
        return ([workspace] if user_id == str(reader) else []), []

    monkeypatch.setattr(workspaces, "workspace_scope_tiers", tiers)
    inside, outside = _sub(user_id=reader), _sub(user_id=outsider)
    row = SimpleNamespace(ignore_self_triggered_events=False)
    dispatch_world.subs = [(inside, row, None), (outside, row, None)]
    payload = {**pe.work_item_data(_item(workspace_id=workspace)), "event_id": "e1"}
    await pe.dispatch_event("work_item.note_added", payload)
    assert dispatch_world.delivered == [("work_item.note_added", inside.assignment_id)]

    # The user it runs as must read the workspace too.
    dispatch_world.delivered.clear()
    dispatch_world.subs = [(_sub(user_id=reader, run_as_user_id=outsider), row, None)]
    await pe.dispatch_event("work_item.note_added", payload)
    assert dispatch_world.delivered == []

    # A run event checks the workspaces of the items it held.
    run_sub = _sub(user_id=outsider, events=("run.orphaned",))
    dispatch_world.subs = [(run_sub, row, None)]
    orphaned = {**pe.run_data(run_id=uuid.uuid4(), agent_id=AGENT, workspace_ids=[workspace]), "event_id": "e2"}
    await pe.dispatch_event("run.orphaned", orphaned)
    assert dispatch_world.delivered == []


@pytest.mark.asyncio
async def test_scope_all_still_needs_the_run_as_user_to_read_the_workspace(monkeypatch, dispatch_world):
    from app.services import workspaces

    workspace, admin, outsider = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()

    async def tiers(db, user_id):
        return [], []  # nobody reads it: only the owner's check is skipped

    monkeypatch.setattr(workspaces, "workspace_scope_tiers", tiers)
    row = SimpleNamespace(ignore_self_triggered_events=False)
    payload = {**pe.work_item_data(_item(workspace_id=workspace)), "event_id": "e1"}
    as_itself = _sub(user_id=admin, scope_all=True)
    as_other = _sub(user_id=admin, run_as_user_id=outsider, scope_all=True)
    dispatch_world.subs = [(as_itself, row, None), (as_other, row, None)]
    await pe.dispatch_event("work_item.note_added", payload)
    assert dispatch_world.delivered == [("work_item.note_added", as_itself.assignment_id)]


@pytest.mark.asyncio
async def test_dispatch_skips_the_agent_that_raised_it_when_asked(dispatch_world):
    sub = _sub()
    dispatch_world.subs = [(sub, SimpleNamespace(ignore_self_triggered_events=True), None)]
    payload = {**pe.work_item_data(_item(), actor_agent_id=AGENT), "event_id": "e1"}
    assert await pe.dispatch_event("work_item.note_added", payload) == []


@pytest.mark.asyncio
async def test_dispatch_skips_its_own_failed_run_when_asked(dispatch_world):
    sub = _sub(events=("run.failed",))
    dispatch_world.subs = [(sub, SimpleNamespace(ignore_self_triggered_events=True), None)]
    payload = {**pe.run_data(run_id=uuid.uuid4(), agent_id=AGENT), "event_id": "e1"}
    assert await pe.dispatch_event("run.failed", payload) == []


@pytest.mark.asyncio
async def test_dispatch_never_raises(monkeypatch, dispatch_world):
    async def broken(db, name):
        raise RuntimeError("db down")

    monkeypatch.setattr(pe, "_subscriptions", broken)
    assert await pe.dispatch_event("work_item.note_added", {"event_id": "e1"}) == []
    assert await pe.dispatch_event("unknown.event", {}) == []


@pytest.fixture
def deliver_world(monkeypatch):
    import app.core.database as database
    from app.services import assignment_engine

    class _Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    state = SimpleNamespace(live=None, duplicate=None, triggered=[])

    async def note_live(db, assignment_id, name, payload, *, pending=False):
        return state.live

    async def duplicate(db, agent_id, key, *, window_seconds):
        return state.duplicate

    async def lock_current(
        db: Any, assignment_id: uuid.UUID, name: str, payload: Any, *, lock: bool = True
    ) -> tuple[Any, bool]:
        # The locked row still takes the event (edits are covered in test_platform_events_watch).
        return SimpleNamespace(id=assignment_id), False

    async def trigger(assignment, agent, payload=None, *, db, trigger_context=None, overflow=None, queue=True):
        state.triggered.append(trigger_context)
        return SimpleNamespace(status="ok", run_id="r1")

    monkeypatch.setattr(database, "async_session", _Session)
    monkeypatch.setattr(pe, "_note_live_holder", note_live)
    monkeypatch.setattr(pe, "_lock_current", lock_current)
    monkeypatch.setattr(assignment_engine, "duplicate_event_run", duplicate)
    monkeypatch.setattr(assignment_engine, "trigger_or_queue_assignment", trigger)
    return state


@pytest.mark.asyncio
async def test_deliver_starts_a_run_with_the_event_in_its_context(deliver_world):
    sub = _sub()
    payload = {**pe.work_item_data(_item()), "event_id": "e1", "note": "progress"}
    assert await pe._deliver("work_item.note_added", payload, sub, object(), object()) == "ok run r1"
    ctx = deliver_world.triggered[0]
    assert ctx["event"] == "work_item.note_added" and ctx["dedup_key"] == pe.dedup_key("e1", sub.assignment_id)


@pytest.mark.asyncio
async def test_a_live_holder_gets_a_note_not_a_second_run(deliver_world):
    deliver_world.live = "run-live"
    out = await pe._deliver("work_item.note_added", {"event_id": "e1"}, _sub(), object(), object())
    assert out == "noted for live run run-live" and deliver_world.triggered == []


@pytest.mark.asyncio
async def test_a_repeated_event_is_a_duplicate_not_a_second_run(deliver_world):
    deliver_world.duplicate = "run-first"
    out = await pe._deliver("work_item.note_added", {"event_id": "e1"}, _sub(), object(), object())
    assert out == "duplicate of run run-first" and deliver_world.triggered == []


@pytest.mark.asyncio
async def test_an_event_a_run_already_took_is_not_noted_on_a_live_holder(deliver_world):
    deliver_world.duplicate, deliver_world.live = "run-first", "run-live"
    out = await pe._deliver("work_item.note_added", {"event_id": "e1"}, _sub(), object(), object())
    assert out == "duplicate of run run-first" and deliver_world.triggered == []


def test_live_note_names_the_event_and_the_run():
    note = pe.live_note("work_item.note_added", {"note": "pushed abc"}, "r9", datetime(2026, 11, 1, tzinfo=UTC))
    assert "work_item.note_added" in note["text"] and "r9" in note["text"] and "pushed abc" in note["text"]


# -- The work item service's events -------------------------------------------------


def test_update_events_for_a_note_and_a_status_change():
    item = _item(status="done")
    writer = SimpleNamespace(agent_id=AGENT, run_id=None)
    events = wi.update_events(item, previous_status="in_progress", note="shipped", writer=writer)
    names = [n for n, _ in events]
    assert names == ["work_item.note_added", "work_item.status_changed", "work_item.completed"]
    assert events[0][1]["note"] == "shipped" and events[1][1]["previous_status"] == "in_progress"


def test_update_events_none_when_nothing_moved():
    item = _item(status="in_progress")
    assert wi.update_events(item, previous_status="in_progress", note=None) == []


def test_needs_human_raised_once_when_the_flag_turns_true():
    item = _item(status="blocked", needs_human=True, blocked_reason="waits for your go")
    events = wi.update_events(
        item, previous_status="in_progress", note=None, previous_needs_human=None, previous_blocked_reason=None
    )
    raised = [d for n, d in events if n == "work_item.needs_human"]
    assert len(raised) == 1
    assert raised[0]["needs_human"] is True and raised[0]["blocked_reason"] == "waits for your go"
    # The same write again (the flag already true, same reason): no second event.
    again = wi.update_events(
        item, previous_status="blocked", note="still", previous_needs_human=True, previous_blocked_reason="waits for your go"
    )
    assert "work_item.needs_human" not in [n for n, _ in again]


def test_needs_human_on_a_new_reason_of_a_person_owned_item_only():
    person = _item(status="blocked", owner_agent_id=None, needs_human=None, blocked_reason="sign the contract")
    events = wi.update_events(
        person, previous_status="blocked", note=None, previous_needs_human=None, previous_blocked_reason="old"
    )
    assert [n for n, _ in events] == ["work_item.needs_human"]
    agent = _item(status="blocked", needs_human=None, blocked_reason="waits for PR #1")
    assert wi.update_events(
        agent, previous_status="blocked", note=None, previous_needs_human=None, previous_blocked_reason="old"
    ) == []


def test_needs_human_never_on_a_terminal_item_nor_without_the_previous_values():
    done = _item(status="done", needs_human=True, blocked_reason="x")
    names = [n for n, _ in wi.update_events(done, previous_status="blocked", note=None, previous_needs_human=None)]
    assert "work_item.needs_human" not in names
    flagged = _item(status="blocked", needs_human=True, blocked_reason="x")
    assert "work_item.needs_human" not in [
        n for n, _ in wi.update_events(flagged, previous_status="in_progress", note=None)
    ]


def test_needs_human_at_creation():
    item = _item(status="blocked", needs_human=True, blocked_reason="decide", created_at=datetime(2026, 11, 5, tzinfo=UTC))
    events = wi.created_events(item)
    assert [n for n, _ in events] == ["work_item.needs_human"]
    assert events[0][1]["waiting_since"] == "2026-11-05T00:00:00+00:00"
    assert wi.created_events(_item(status="open")) == []


def test_needs_human_is_in_the_catalog_with_blocked_reason():
    entry = next(e for e in pe.catalog() if e["event"] == "work_item.needs_human")
    assert "blocked_reason" in entry["filter_keys"] and "status" in entry["filter_keys"]


@pytest.mark.parametrize("event", ["work_item.status_changed", "work_item.needs_human", "work_item.released"])
def test_work_item_events_list_the_blocker_filter_keys(event):
    entry = next(e for e in pe.catalog() if e["event"] == event)
    assert {"blocked_reason", "blocked_on", "blocked_on_refs"} <= set(entry["filter_keys"])
    assert len(entry["filter_keys"]) == len(set(entry["filter_keys"]))


def test_work_item_payload_carries_what_a_block_waits_for_bounded():
    dep = uuid.uuid4()
    refs = [
        {"repo": "o/r", "kind": "pull_request", "number": 1, "release_on": "merged"},
        {"repo": "o/r", "kind": "deployment", "number": 2, "state": "deployed"},
        {"repo": "o/r", "number": 3},
        "junk",
    ]
    data = pe.work_item_data(
        _item(status="blocked", blocked_reason="  " + "w" * 900, blocked_on=[dep], blocked_on_refs=refs)
    )
    assert data["blocked_reason"] == "w" * 500
    assert data["blocked_on"] == [str(dep)]
    # The satisfied deployment is left out; a ref without kind is a pull request.
    assert data["blocked_on_refs"] == ["pull_request"]


@pytest.mark.parametrize(
    "columns",
    [
        {},
        {"blocked_on": [], "blocked_on_refs": [], "blocked_reason": "  "},
        {"blocked_on": "x", "blocked_on_refs": 3, "blocked_reason": 7},
    ],
)
def test_work_item_payload_without_a_blocker_reads_absent(columns):
    data = pe.work_item_data(_item(status="blocked", **columns))
    assert data["blocked_reason"] is None and data["blocked_on"] is None and data["blocked_on_refs"] is None


def test_a_listener_filtered_on_no_release_skips_a_block_that_names_one():
    flt = {"status": "blocked", "blocked_on_refs": {"exists": False}, "blocked_on": {"exists": False}}
    sub = _sub(events=("work_item.status_changed",), filter=flt, agent_id=OTHER)
    waits_for_ci = pe.work_item_data(
        _item(status="blocked", blocked_on_refs=[{"repo": "o/r", "kind": "pull_request", "number": 9}])
    )
    waits_for_item = pe.work_item_data(_item(status="blocked", blocked_on=[uuid.uuid4()]))
    waits_for_nothing = pe.work_item_data(_item(status="blocked", blocked_reason="stuck"))
    assert pe.mismatch_reason(sub, "work_item.status_changed", waits_for_ci) is not None
    assert pe.mismatch_reason(sub, "work_item.status_changed", waits_for_item) is not None
    assert pe.mismatch_reason(sub, "work_item.status_changed", waits_for_nothing) is None
    # A filter saved before the keys existed keeps its result.
    legacy = _sub(events=("work_item.status_changed",), filter={"status": "blocked"}, agent_id=OTHER)
    assert pe.mismatch_reason(legacy, "work_item.status_changed", waits_for_ci) is None


def test_claim_events_show_the_claimed_state():
    item = _item(status="open")
    run = uuid.uuid4()
    events = wi._claim_events(item, previous_status="open", actor_agent_id=OTHER, actor_run_id=run)
    assert [n for n, _ in events] == ["work_item.claimed", "work_item.status_changed"]
    data = events[0][1]
    assert data["status"] == "claimed" and data["holder_run_id"] == str(run)
    assert str(OTHER) in data["participant_agent_ids"]


@pytest.mark.asyncio
async def test_run_failed_carries_the_held_items_workspace_scope(monkeypatch):
    """A failed run (the dead-letter path included) publishes ``run.failed`` with
    the workspace of the item it held, so dispatch checks workspace visibility."""
    from app.services import assignment_engine as engine

    workspace = uuid.uuid4()
    row = SimpleNamespace(
        workspace_id=workspace,
        created_by_agent_id=AGENT,
        owner_agent_id=OTHER,
        user_id=uuid.uuid4(),
        is_company_managed=False,
    )

    class _Result:
        def one_or_none(self):
            return row

    class _Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def execute(self, _stmt):
            return _Result()

    published: list[tuple[str, dict]] = []

    async def _publish(name, data):
        published.append((name, data))
        return "e1"

    monkeypatch.setattr(engine, "async_session", _Session)
    monkeypatch.setattr(pe, "publish", _publish)
    item_id = uuid.uuid4()
    run = SimpleNamespace(
        id=uuid.uuid4(),
        agent_id=OTHER,
        assignment_id=uuid.uuid4(),
        parent_run_id=None,
        trigger_context={"work_item_id": str(item_id)},
        user_id=uuid.uuid4(),
        is_company_managed=True,
    )
    await engine._publish_run_ended(run, "run.completed", reason="completed")
    assert published[-1][0] == "run.completed" and published[-1][1]["reason"] == "completed"
    assert published[-1][1]["workspace_ids"] == [str(workspace)]
    published.clear()
    await engine._publish_run_failed(run, "abandoned")
    assert [n for n, _ in published] == ["run.failed"]
    data = published[0][1]
    assert data["workspace_ids"] == [str(workspace)]
    assert data["work_item_ids"] == [str(item_id)]
    assert {str(AGENT), str(OTHER)} <= set(data["participant_agent_ids"])
    # A workspace item is shared: no personal owner narrows the event.
    assert data["personal_owner_ids"] == []
    # A run not visible to everyone (personal, or hidden with hide_runs) reaches
    # only its owner's assignments, as run_visible_filter scopes it elsewhere.
    published.clear()
    run.is_company_managed = False
    await engine._publish_run_ended(run, "run.completed", reason="completed")
    assert published[-1][1]["personal_owner_ids"] == [str(run.user_id)]
    # A run that ended on its own terms carries no stop_reason.
    assert published[-1][1]["stop_reason"] is None
    assert published[-1][1]["partial"] is False


@pytest.mark.asyncio
async def test_a_completed_run_cut_short_says_why_in_its_event(monkeypatch: pytest.MonkeyPatch) -> None:
    """A run stopped at its deadline or budget completes with a partial
    result: its run.completed event names the stop_reason, a failed run's
    event never carries one."""
    from app.services import assignment_engine as engine

    published: list[tuple[str, dict[str, Any]]] = []

    async def _publish(name: str, data: dict[str, Any]) -> str:
        published.append((name, data))
        return "e1"

    monkeypatch.setattr(pe, "publish", _publish)
    run: Any = SimpleNamespace(
        id=uuid.uuid4(),
        agent_id=OTHER,
        assignment_id=uuid.uuid4(),
        parent_run_id=None,
        trigger_context={},
        user_id=uuid.uuid4(),
        is_company_managed=True,
        stop_reason="deadline",
    )
    await engine._publish_run_ended(run, "run.completed", reason="completed")
    assert published[-1][0] == "run.completed"
    assert published[-1][1]["stop_reason"] == "deadline"
    assert published[-1][1]["partial"] is True
    await engine._publish_run_ended(run, "run.failed", reason="failed", error="boom")
    assert published[-1][1]["stop_reason"] is None
    assert published[-1][1]["partial"] is False
    assert "stop_reason" in pe.PLATFORM_EVENTS["run.completed"]["filter_keys"]
    assert "partial" in pe.PLATFORM_EVENTS["run.completed"]["filter_keys"]


def test_run_event_of_a_personal_item_reaches_only_its_owner():
    owner, stranger = uuid.uuid4(), uuid.uuid4()
    data = pe.run_data(
        run_id=uuid.uuid4(),
        agent_id=OTHER,
        participant_agent_ids=[AGENT],
        personal_owner_ids=[owner],
    )
    assert data["personal_owner_ids"] == [str(owner)]
    assert pe.in_scope(_sub(user_id=owner), data)
    # Another user's assignment on a participant agent does not see it.
    assert not pe.in_scope(_sub(user_id=stranger), data)


# -- Builder tool -------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_platform_events_tool():
    from app.services.builtin_tools.agent_builder import (
        ASSIGNMENT_TRIGGER_TYPES,
        build_agent_builder_tool,
        platform_event_trigger_error,
    )

    assert "platform_event" in ASSIGNMENT_TRIGGER_TYPES
    assert platform_event_trigger_error("platform_event", {"event": "x"}) is not None
    definitions, executor = build_agent_builder_tool(SimpleNamespace(), str(uuid.uuid4()))
    assert "list_platform_events" in {d.name for d in definitions}
    from app.tools.base import ToolCall

    result = await executor.execute_tool(
        ToolCall(id=str(uuid.uuid4()), name="list_platform_events", arguments={"search": "orphaned"})
    )
    assert "run.orphaned" in result.content and "work_item.note_added" not in result.content
    # The catalog tells the operator grammar of the filter.
    assert "starts_with" in result.content and "not_in" in result.content
