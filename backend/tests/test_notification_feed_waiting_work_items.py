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
    assert item.kind == "work_item_waiting"
    assert item.id == f"work_item:{ITEM_ID}"
    assert item.route == f"/tasks/work-items/{ITEM_ID}"
    assert item.title == "A or B?"
    assert item.subtitle == "Waiting for your decision"
    assert item.summary is None
    assert "work_item_waiting" in feed.URGENT_KINDS
    assert "work_item_waiting" in feed.NOTIFICATION_KINDS


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


def test_a_waiting_item_stays_unread_until_answered():
    item = feed.present_waiting_work_item(_waiting())
    assert feed.is_unread(item, read_at=None, dismissed={item.id})
    from datetime import UTC, datetime

    assert feed.is_unread(item, read_at=datetime(2030, 1, 1, tzinfo=UTC), dismissed=set())


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
