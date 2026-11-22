"""The bell shows the work items waiting for the person (#9437, slice 5).

One entry per item, on by default, off with ``notify_waiting_work_items``;
unread until answered; answering the item (it leaves the waiting population)
clears its entry.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.services import notification_feed as feed

ITEM_ID = "8d2c3f1e-1111-4a5b-9c7d-2e3f4a5b6c7d"


def _waiting(**overrides):
    row = {"id": ITEM_ID, "title": "Pick a vendor", "question": "A or B?", "since": "2026-11-07T08:00:00+00:00"}
    row.update(overrides)
    return row


def test_a_waiting_item_is_an_urgent_bell_entry_that_opens_the_item():
    item = feed.present_waiting_work_item(_waiting())
    assert item is not None
    assert (item.kind, item.source) == ("decision", "work_item")
    assert item.id == f"work_item:{ITEM_ID}"
    assert item.route == f"/tasks/work-items/{ITEM_ID}"
    assert item.title == "A or B?"
    assert item.subtitle is None
    assert item.summary is None
    assert {"decision", "work_item_waiting"} <= feed.URGENT_KINDS
    assert {"decision", "work_item_waiting"} <= set(feed.NOTIFICATION_KINDS)


def test_the_bell_says_the_question_and_the_deadline_only():
    """#776: the entry carries what the card says; never the agents' blocked_reason."""
    item = feed.present_waiting_work_item(
        _waiting(deadline="2026-11-21T10:00:00+01:00", blocked_reason="CARD SENT 2026-11-20T11:43Z msg 1795")
    )
    assert item is not None
    assert item.title == "A or B?"
    assert item.summary == "Answer by Sat 21 Nov, 09:00 UTC"
    assert "CARD SENT" not in str(item.as_dict())


def test_a_legacy_item_without_a_question_reads_decision_needed_and_its_title():
    item = feed.present_waiting_work_item(_waiting(question=None))
    assert item is not None
    assert item.title == "Decision needed: Pick a vendor"
    assert item.summary is None


@pytest.mark.parametrize("deadline", [None, "", "next week", 12])
def test_an_unreadable_deadline_is_left_out(deadline):
    assert feed.waiting_deadline_text(deadline) is None


def test_a_naive_deadline_reads_as_utc():
    assert feed.waiting_deadline_text("2026-11-20T08:05:00") == "Answer by Fri 20 Nov, 08:05 UTC"


def test_a_row_without_a_valid_id_is_not_shown():
    assert feed.present_waiting_work_item(_waiting(id="nope")) is None


def test_a_waiting_item_stays_unread_until_answered() -> None:
    item = feed.present_waiting_work_item(_waiting())
    assert item is not None
    from datetime import UTC, datetime

    # "Mark all read" never reads a decision still waiting.
    assert feed.is_unread(item, read_at=datetime(2030, 1, 1, tzinfo=UTC), dismissed=set())
    # #991: opening or dismissing it does not read it either; only the answer
    # (the item leaves the waiting population) clears the entry.
    assert feed.is_unread(item, read_at=datetime(2030, 1, 1, tzinfo=UTC), dismissed={item.id})


def _flow_entry(run_id: str, created: str = "2026-11-07T08:00:00+00:00"):
    activity = SimpleNamespace(
        id="6c2f3e1d-2222-4a5b-9c7d-2e3f4a5b6c7d",
        activity_type="notification",
        agent_name=None,
        title="Approve the export",
        summary="Send the report?",
        extra_data={
            "notification_source": "flow_hitl",
            "flow_id": "7d3e4f2a-3333-4a5b-9c7d-2e3f4a5b6c7d",
            "flow_run_id": run_id,
        },
        created_at=created,
    )
    return feed.present_activity(activity, flows_enabled=True)


@pytest.mark.parametrize(
    ("status", "waiting", "expected"),
    [
        ("awaiting", {"kind": "human", "released": False}, True),
        ("awaiting", {"kind": "human", "released": True}, False),
        ("awaiting", {"kind": "breakpoint", "released": False}, False),
        ("completed", None, False),
        ("cancelled", {"kind": "human", "released": False}, False),
    ],
)
def test_a_flow_ask_waits_only_while_parked_on_an_unanswered_question(status, waiting, expected):
    run = SimpleNamespace(status=status, state={"waiting": waiting} if waiting is not None else {})
    assert feed.flow_ask_waits(run) is expected


@pytest.mark.asyncio
async def test_a_flow_ask_stays_unread_until_answered_then_reads_like_any_entry():
    from datetime import UTC, datetime

    waiting_run = "9e4f5a3b-4444-4a5b-9c7d-2e3f4a5b6c7d"
    answered_run = "0f5a6b4c-5555-4a5b-9c7d-2e3f4a5b6c7d"
    entries = [_flow_entry(waiting_run), _flow_entry(answered_run)]
    assert all(
        e is not None and (e.kind, e.source) == ("decision", "flow_run") and not e.awaiting_answer for e in entries
    )

    rows = [
        SimpleNamespace(id=waiting_run, status="awaiting", state={"waiting": {"kind": "human"}}),
        SimpleNamespace(id=answered_run, status="completed", state={}),
    ]

    class _Result:
        def scalars(self):
            return self

        def all(self):
            return rows

    class _Db:
        async def execute(self, _statement):
            return _Result()

    marked = await feed.mark_waiting_flow_asks(_Db(), entries)
    later = datetime(2030, 1, 1, tzinfo=UTC)
    assert [m.awaiting_answer for m in marked] == [True, False]
    assert feed.is_unread(marked[0], read_at=later, dismissed={marked[0].id})
    assert not feed.is_unread(marked[1], read_at=None, dismissed={marked[1].id})


def test_other_kinds_keep_the_901_read_on_open():
    from datetime import UTC, datetime

    item = feed.NotificationItem(id="r", kind="reminder", created_at="2026-11-07T08:00:00+00:00", route="/", title="t")
    assert feed.is_unread(item, read_at=None, dismissed=set())
    assert not feed.is_unread(item, read_at=None, dismissed={"r"})
    # A flag on a kind that is not a decision never pins it.
    flagged = feed.NotificationItem(
        id="x", kind="run", created_at="2026-11-07T08:00:00+00:00", route="/", title="t", awaiting_answer=True
    )
    assert not feed.is_unread(flagged, read_at=datetime(2030, 1, 1, tzinfo=UTC), dismissed=set())


def _patch_sources(monkeypatch, waiting_rows, calls):
    from app.api.v1 import activity, assignments
    from app.core import feature_flags
    from app.services import work_item

    async def _runs(**_kwargs):
        return []

    async def _activity(**_kwargs):
        return []

    async def _waiting_list(*, user_id, limit):
        calls.append((user_id, limit))
        return waiting_rows

    async def _read_state(_db, _user_id):
        return None, []

    monkeypatch.setattr(assignments, "list_all_runs", _runs)
    monkeypatch.setattr(activity, "list_activity", _activity)
    monkeypatch.setattr(feature_flags, "is_feature_enabled", lambda _flag: False)
    monkeypatch.setattr(work_item, "list_work_items_waiting_for_person", _waiting_list)
    monkeypatch.setattr(feed, "load_read_state", _read_state)


@pytest.mark.asyncio
async def test_the_feed_carries_one_entry_per_waiting_item_by_default(monkeypatch):
    calls: list = []
    second = "9e3d4f2a-2222-4b6c-8d7e-3f4a5b6c7d8e"
    _patch_sources(monkeypatch, [_waiting(), _waiting(id=second, title="Second")], calls)

    built = await feed.build_feed(None, SimpleNamespace(id="u"))

    assert calls == [("u", feed.FEED_WINDOW)]
    ids = [i["id"] for i in built["items"]]
    assert ids == [f"work_item:{ITEM_ID}", f"work_item:{second}"]
    assert built["unread_count"] == 2


@pytest.mark.asyncio
async def test_an_answered_item_leaves_the_bell(monkeypatch):
    _patch_sources(monkeypatch, [], [])
    built = await feed.build_feed(None, SimpleNamespace(id="u"))
    assert built["items"] == []
    assert built["unread_count"] == 0


@pytest.mark.asyncio
async def test_opting_out_stops_the_entries(monkeypatch):
    calls: list = []
    _patch_sources(monkeypatch, [_waiting()], calls)
    built = await feed.build_feed(None, SimpleNamespace(id="u", notify_waiting_work_items=False))
    assert calls == [], "an opted-out person's waiting items are not even read"
    assert built["items"] == []
