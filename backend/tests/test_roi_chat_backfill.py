"""One-off pass over past chat deliverables (#10544, slice S3): ``services/roi_chat_backfill.py``.

The pure parts (signals, revisions, tally, the insert statement); the round
trip against PostgreSQL is ``tests/test_roi_chat_backfill_postgres.py``.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from sqlalchemy.dialects import postgresql

import app.models  # noqa: F401 - registers every table on Base.metadata
from app.services import roi_chat
from app.services import roi_chat_backfill as bf

_NOW = datetime(2026, 11, 10, 9, 0, tzinfo=UTC)


def _deck(*, days_ago: float = 30, size: float = 30, signals: tuple[str, ...] = ()) -> bf.PastDeliverable:
    at = _NOW - timedelta(days=days_ago)
    return bf.PastDeliverable(
        kind=roi_chat.KIND_PRESENTATION,
        deliverable_id=str(uuid.uuid4()),
        conversation_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        title="All Hands",
        size=size,
        created_at=at,
        last_edit_at=at,
        signals=signals,
    )


def test_past_signals_are_the_ones_the_database_still_shows():
    assert bf.past_signals(granted=False, public_link=False) == ()
    assert bf.past_signals(granted=True, public_link=True) == (roi_chat.SIGNAL_SHARED, roi_chat.SIGNAL_PUBLIC_LINK)


def test_a_grant_to_a_person_a_group_or_the_company_is_a_share():
    assert not bf._granted(SimpleNamespace(is_company_managed=False, shared_user_ids=None, shared_group_ids=[]))
    assert bf._granted(SimpleNamespace(is_company_managed=True, shared_user_ids=None, shared_group_ids=None))
    assert bf._granted(SimpleNamespace(is_company_managed=False, shared_user_ids=["u"], shared_group_ids=None))
    assert bf._granted(SimpleNamespace(is_company_managed=False, shared_user_ids=[], shared_group_ids=["g"]))


def test_a_document_rewritten_under_the_same_name_is_one_deliverable_on_its_latest_file():
    conv = uuid.uuid4()
    old = SimpleNamespace(id=uuid.uuid4(), conversation_id=conv, filename="brief.pdf", created_at=_NOW)
    new = SimpleNamespace(id=uuid.uuid4(), conversation_id=conv, filename="brief.pdf ", created_at=_NOW + timedelta(1))
    other = SimpleNamespace(id=uuid.uuid4(), conversation_id=conv, filename="memo.pdf", created_at=_NOW)
    elsewhere = SimpleNamespace(id=uuid.uuid4(), conversation_id=uuid.uuid4(), filename="brief.pdf", created_at=_NOW)
    kept = {r.id for r in bf.latest_document_revisions([new, old, other, elsewhere])}
    assert kept == {new.id, other.id, elsewhere.id}


def test_the_all_hands_deck_of_the_past_comes_out_counted_low():
    """30 slides, settled long ago, 3 h of measured time: the low bound minus 3.75 h."""
    result = bf.ChatBackfillResult(dry_run=True)
    deck = _deck(size=30)
    write = bf.tally(result, deck, recorded=False, user_min=180.0, now=_NOW)
    assert write
    counts = result.kinds[roi_chat.KIND_PRESENTATION]
    assert (counts.scanned, counts.to_create, counts.counted_now) == (1, 1, 1)
    expected = roi_chat.estimate(roi_chat.KIND_PRESENTATION, 30, user_min=180.0)
    assert counts.manual_min_low == expected.manual_min_low
    assert counts.counted_min == expected.counted_min
    assert counts.counted_min == max(min(expected.manual_min_low - 225.0, 24 * 60), 0.0)


def test_a_recent_deliverable_with_no_signal_is_written_but_does_not_count_yet():
    result = bf.ChatBackfillResult(dry_run=True)
    assert bf.tally(result, _deck(days_ago=2), recorded=False, user_min=0.0, now=_NOW)
    counts = result.kinds[roi_chat.KIND_PRESENTATION]
    assert (counts.to_create, counts.counted_now, counts.counted_min) == (1, 0, 0.0)


def test_a_recent_deliverable_with_a_public_link_counts_at_once():
    result = bf.ChatBackfillResult(dry_run=True)
    bf.tally(result, _deck(days_ago=1, signals=(roi_chat.SIGNAL_PUBLIC_LINK,)), recorded=False, user_min=0.0, now=_NOW)
    assert result.kinds[roi_chat.KIND_PRESENTATION].counted_now == 1


def test_a_deliverable_already_recorded_is_left_to_its_record():
    result = bf.ChatBackfillResult(dry_run=False)
    assert not bf.tally(result, _deck(), recorded=True, user_min=0.0, now=_NOW)
    counts = result.kinds[roi_chat.KIND_PRESENTATION]
    assert (counts.scanned, counts.already_recorded, counts.to_create) == (1, 1, 0)


def test_the_report_totals_every_kind():
    result = bf.ChatBackfillResult(dry_run=True)
    bf.tally(result, _deck(), recorded=False, user_min=0.0, now=_NOW)
    bf.tally(result, _deck(), recorded=True, user_min=0.0, now=_NOW)
    totals = result.as_dict()["totals"]
    assert (totals["scanned"], totals["to_create"], totals["already_recorded"], totals["counted_now"]) == (2, 1, 1, 1)
    assert set(result.as_dict()["kinds"]) == set(roi_chat.KINDS)


def test_the_insert_never_overwrites_a_record_and_dates_the_row_at_its_deliverable():
    deck = _deck(signals=(roi_chat.SIGNAL_SHARED,))
    statement = bf._insert_statement(deck, now=_NOW)
    sql = str(statement.compile(dialect=postgresql.dialect()))
    assert "INSERT INTO roi_chat_records" in sql
    assert "ON CONFLICT (record_key) DO NOTHING" in sql
    assert "FROM conversations" in sql
    params = statement.compile(dialect=postgresql.dialect()).params
    assert params["record_key"] == f"presentation:{deck.deliverable_id}"
    assert params["backfilled_at"] == _NOW
    assert params["created_at"] == deck.created_at
    assert params["last_edit_at"] == deck.last_edit_at
    assert params["first_signal_at"] == deck.last_edit_at
