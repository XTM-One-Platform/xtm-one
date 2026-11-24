"""What a work item means to the person reading it, without a database.

The pure halves of ``services/work_item_context.py`` (the pickup state, the
retention dates, how a page's lookup words each item) and the agent tool's
grounding: the origin it records and what it says will pick a new item up.
The SQL halves run against PostgreSQL in ``test_work_item_context_postgres.py``.
"""

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

import app.services.work_item as wi_service
from app.services import work_item_context as ctx
from app.services.builtin_tools import build_work_item_tools
from app.services.company_managed_writes import agent_writer
from app.services.work_item import WorkItemInbox, WorkItemOrigin
from app.tools.base import ToolCall

NOW = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)
OWNER = str(uuid.uuid4())


def _item(**overrides) -> dict:
    base = {
        "id": str(uuid.uuid4()),
        "user_id": None,
        "is_company_managed": False,
        "owner_agent_id": OWNER,
        "created_by_agent_id": None,
        "claimed_by_agent_id": None,
        "status": "open",
        "priority": "medium",
        "due_at": None,
        "source": "agent",
        "payload": {},
        "scheduled": None,
        "workspace_id": None,
        "conversation_id": None,
        "origin_run_id": None,
        "origin_flow_run_id": None,
        "last_activity_at": (NOW - timedelta(days=1)).isoformat(),
        "created_at": (NOW - timedelta(days=1)).isoformat(),
        "updated_at": (NOW - timedelta(days=1)).isoformat(),
    }
    base.update(overrides)
    return base


def _inbox(kind="assignment") -> WorkItemInbox:
    return WorkItemInbox(
        kind=kind,
        id=uuid.uuid4(),
        name="Inbox",
        owner_agent_id=OWNER,
        user_id=None,
        writer=agent_writer(is_company_managed=False, agent_id=uuid.uuid4()),
    )


# ── Pickup state ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("overrides", "inboxes", "state"),
    [
        ({"scheduled": {"state": "scheduled"}}, [], "scheduled"),
        ({"status": "done"}, [], "finished"),
        ({"status": "cancelled"}, [], "finished"),
        ({"status": "claimed"}, [], "in_progress"),
        ({"status": "in_progress"}, [], "in_progress"),
        ({"status": "blocked"}, [True], "blocked"),
        ({"owner_agent_id": None}, [], "unassigned"),
        ({}, [True], "automatic"),
        ({}, [False, True], "automatic"),
        ({"not_before": (NOW + timedelta(hours=3)).isoformat()}, [True], "waiting_until_due"),
        ({"not_before": (NOW - timedelta(hours=3)).isoformat()}, [True], "automatic"),
        # The deadline defers nothing (#8753).
        ({"due_at": (NOW + timedelta(hours=3)).isoformat()}, [True], "automatic"),
        ({}, [False], "filtered_out"),
        ({}, [], "manual"),
    ],
    ids=[
        "scheduled",
        "done",
        "cancelled",
        "claimed",
        "in-progress",
        "blocked",
        "unassigned",
        "taken",
        "one-of-two-takes-it",
        "not-before-later",
        "not-before-reached",
        "deadline-ahead",
        "filtered-out",
        "no-inbox",
    ],
)
def test_pickup_state(overrides, inboxes, state):
    entries = [(_inbox(), accepts) for accepts in inboxes]
    assert ctx.pickup_state(_item(**overrides), entries, NOW) == state
    assert state in ctx.PICKUP_STATES


def test_an_item_held_by_a_run_that_ended_is_not_being_worked_on():
    """Released on the next tick: no view says an agent works on it."""
    ended = {"id": str(uuid.uuid4()), "kind": "run", "status": "failed", "alive": False}
    alive = {**ended, "status": "running", "alive": True}
    held = {"status": "in_progress", "claimed_by_agent_id": OWNER}
    assert ctx.pickup_state(_item(**held, claimed_by_run=ended), [(_inbox(), True)], NOW) == "automatic"
    assert ctx.pickup_state(_item(**held, claimed_by_run=ended), [], NOW) == "manual"
    assert ctx.pickup_state(_item(**held, claimed_by_run=alive), [], NOW) == "in_progress"
    # A claim whose holder was not read (no run recorded) still reads as held.
    assert ctx.pickup_state(_item(**held, claimed_by_run=None), [], NOW) == "in_progress"

    lookup = _lookup(uuid.uuid4())
    owner = uuid.UUID(OWNER)
    lookup.agents = {owner: ("Developer", "bot")}
    lookup.visible_agents = {owner}
    assert lookup.context(_item(**held, claimed_by_run=ended), NOW)["claimed_by_agent"] is None
    assert lookup.context(_item(**held, claimed_by_run=alive), NOW)["claimed_by_agent"]["name"] == "Developer"


def test_an_open_item_says_when_the_abandonment_sweep_cancels_it(monkeypatch):
    monkeypatch.setattr(ctx.settings, "work_item_abandoned_after_days", 14)
    anchor = NOW - timedelta(days=1)
    item = _item(last_activity_at=anchor.isoformat())
    assert ctx._retention(item, "manual") == ((anchor + timedelta(days=14)).isoformat(), None)

    # The sweep never cancels an item before its deadline or its pickup time.
    due = NOW + timedelta(days=30)
    item = _item(last_activity_at=anchor.isoformat(), due_at=due.isoformat())
    assert ctx._retention(item, "automatic") == (due.isoformat(), None)
    later = NOW + timedelta(days=40)
    deferred = _item(last_activity_at=anchor.isoformat(), due_at=due.isoformat(), not_before=later.isoformat())
    assert ctx._retention(deferred, "waiting_until_due") == (later.isoformat(), None)

    monkeypatch.setattr(ctx.settings, "work_item_abandoned_after_days", 0)
    assert ctx._retention(item, "manual") == (None, None)


def test_a_finished_item_says_when_the_retention_sweep_deletes_it(monkeypatch):
    monkeypatch.setattr(ctx.settings, "work_item_resolved_retention_days", 30)
    updated = NOW - timedelta(days=2)
    item = _item(status="done", updated_at=updated.isoformat())
    assert ctx._retention(item, "finished") == (None, (updated + timedelta(days=30)).isoformat())
    # Held by an agent: no sweep applies.
    assert ctx._retention(item, "in_progress") == (None, None)


# ── One page's lookup, as one reader sees it ──────────────────────────────────


def _lookup(viewer_id: uuid.UUID) -> ctx._Lookup:
    return ctx._Lookup(SimpleNamespace(id=viewer_id, is_admin=False))


def test_an_owner_agent_the_reader_cannot_open_keeps_its_name_never_its_id():
    # #858: the owner of an item the reader sees is named read-only (no id, no
    # link); any other agent the reader cannot open stays unnamed.
    viewer = uuid.uuid4()
    mine, theirs, hidden = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    lookup = _lookup(viewer)
    lookup.agents = {mine: ("Mine", "bot"), theirs: ("Theirs", "bot"), hidden: ("Hidden", "bot")}
    lookup.visible_agents = {mine}
    context = lookup.context(
        _item(owner_agent_id=str(theirs), created_by_agent_id=str(mine), user_id=str(viewer)), NOW
    )
    assert context["owner_agent"] == {"id": None, "name": "Theirs", "icon": "bot", "accessible": False}
    assert lookup.agent(hidden) == {"id": None, "name": None, "icon": None, "accessible": False}
    assert context["created_by_agent"] == {
        "id": str(mine),
        "name": "Mine",
        "icon": "bot",
        "accessible": True,
    }
    assert context["person"]["is_you"] is True


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({}, "own"),
        ({"is_company_managed": True}, "company_managed"),
        # A filed item is read through its workspace, company-managed or not.
        ({"is_company_managed": True, "workspace_id": "WS"}, "workspace"),
    ],
    ids=["own", "company-managed", "workspace"],
)
def test_why_the_reader_sees_it(overrides, reason):
    viewer, workspace = uuid.uuid4(), uuid.uuid4()
    if overrides.get("workspace_id"):
        overrides = {**overrides, "workspace_id": str(workspace)}
    lookup = _lookup(viewer)
    lookup.workspaces = {workspace: "Marketing"}
    visibility = lookup.context(_item(user_id=str(viewer), **overrides), NOW)["visibility"]
    assert visibility["reason"] == reason
    if reason == "workspace":
        assert visibility["workspace"] == {"id": str(workspace), "name": "Marketing"}


def test_the_origin_links_only_what_the_reader_may_open():
    viewer, other = uuid.uuid4(), uuid.uuid4()
    mine, theirs = uuid.uuid4(), uuid.uuid4()
    run = uuid.uuid4()
    lookup = _lookup(viewer)
    lookup.conversations = {mine: ("Notion import", viewer), theirs: ("Private", other)}
    lookup.runs = {run: ("Nightly sync", "completed")}

    chat = lookup.context(_item(conversation_id=str(mine)), NOW)["origin"]
    assert chat["kind"] == "chat"
    assert chat["conversation"] == {"id": str(mine), "title": "Notion import"}

    hidden = lookup.context(_item(conversation_id=str(theirs), origin_run_id=str(run)), NOW)["origin"]
    assert hidden["kind"] == "run"
    assert hidden["conversation"] is None and hidden["conversation_hidden"] is True
    assert hidden["run"] == {"id": None, "name": None, "status": None, "accessible": False}

    lookup.visible_runs = {run}
    shown = lookup.context(_item(origin_run_id=str(run)), NOW)["origin"]["run"]
    assert shown == {"id": str(run), "name": "Nightly sync", "status": "completed", "accessible": True}


def test_an_action_register_item_from_before_the_origin_column_keeps_its_run():
    run = uuid.uuid4()
    item = _item(source="action_register", payload={"source_run_id": str(run)})
    assert ctx._origin_run_id(item) == run
    # Any other item's payload is the agent's, never its origin.
    assert ctx._origin_run_id(_item(payload={"source_run_id": str(run)})) is None


def test_the_pickup_block_counts_failed_dispatches(monkeypatch):
    monkeypatch.setattr(ctx.settings, "work_item_max_dispatch_attempts", 5)
    lookup = _lookup(uuid.uuid4())
    item = _item(status="blocked", payload={"_dispatch_attempts": 5})
    pickup = lookup.context(item, NOW)["pickup"]
    assert pickup["state"] == "blocked"
    assert (pickup["dispatch_attempts"], pickup["max_dispatch_attempts"]) == (5, 5)


def test_the_origin_keeps_only_uuids():
    run = uuid.uuid4()
    values = WorkItemOrigin(conversation_id="not-a-uuid", run_id=str(run), flow_run_id=None).column_values()
    assert values == {"conversation_id": None, "origin_run_id": run, "origin_flow_run_id": None}


# ── The agent tool ───────────────────────────────────────────────────────────


async def _create(monkeypatch, *, pickup_state, **build_kwargs):
    captured: dict = {}

    async def _fake_create(**kwargs):
        captured.update(kwargs)
        return {
            "id": "wi-1",
            "title": kwargs["title"],
            "status": "open",
            "priority": "medium",
            "owner_agent_id": kwargs.get("owner_agent_id"),
            "not_before": "2026-10-22T09:00:00+00:00",
        }

    async def _pickup(item):
        return pickup_state

    monkeypatch.setattr(wi_service, "create_work_item", _fake_create)
    monkeypatch.setattr(ctx, "new_item_pickup_state", _pickup)
    _, executor = build_work_item_tools(SimpleNamespace(), str(uuid.uuid4()), str(uuid.uuid4()), **build_kwargs)
    result = await executor.execute_tool(
        ToolCall(id="c1", name="create_work_item", arguments={"title": "x", "owner_agent_id": OWNER})
    )
    return result.content, captured


@pytest.mark.asyncio
async def test_the_tool_records_where_it_runs(monkeypatch):
    origin = WorkItemOrigin(conversation_id=uuid.uuid4(), run_id=uuid.uuid4())
    _, captured = await _create(monkeypatch, pickup_state="automatic", origin=origin)
    assert captured["origin"] is origin


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("state", "says", "never"),
    [
        ("automatic", "will pick it up automatically", "nothing will pick it up"),
        ("waiting_until_due", "from its not_before (2026-10-22T09:00:00+00:00)", "nothing will pick it up"),
        ("filtered_out", "do not take this item: nothing will pick it up", "inbox that will pick it up"),
        ("manual", "has no work-item inbox", "inbox that will pick it up"),
        (None, "if a work-item inbox drains its queue", "inbox that will pick it up"),
    ],
    ids=["automatic", "due-later", "filtered-out", "no-inbox", "unknown"],
)
async def test_the_tool_says_whether_anything_will_pick_the_item_up(monkeypatch, state, says, never):
    content, _ = await _create(monkeypatch, pickup_state=state)
    assert says in content
    assert never not in content
    # The old promise, true only with an inbox, is never made.
    assert "next sweep" not in content
