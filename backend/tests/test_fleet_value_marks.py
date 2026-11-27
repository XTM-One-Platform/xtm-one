"""The fleet value mark rules (#2580): which facts a reset fleet still counts."""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime

from app.services import fleet_value_marks

FLEET = uuid.uuid4()
MARK = datetime(2026, 11, 27, 9, 30, tzinfo=UTC)
MARKS = {FLEET: MARK}


def test_a_fact_of_no_fleet_or_of_a_fleet_never_reset_counts() -> None:
    assert fleet_value_marks.counts(None, datetime(2026, 1, 1, tzinfo=UTC), MARKS)
    assert fleet_value_marks.counts(uuid.uuid4(), datetime(2026, 1, 1, tzinfo=UTC), MARKS)


def test_a_fact_of_a_reset_fleet_counts_from_the_mark() -> None:
    assert not fleet_value_marks.counts(FLEET, datetime(2026, 11, 27, 9, 29, tzinfo=UTC), MARKS)
    assert fleet_value_marks.counts(FLEET, MARK, MARKS)
    # A naive time reads as UTC.
    assert fleet_value_marks.counts(FLEET, datetime(2026, 11, 27, 10, 0), MARKS)


def test_a_daily_cost_fact_counts_from_the_day_after_the_mark() -> None:
    # The mark's own day is read again from the traces at the minute (roi_ledger._clip_cost_rows).
    assert not fleet_value_marks.counts(FLEET, date(2026, 11, 26), MARKS)
    assert not fleet_value_marks.counts(FLEET, date(2026, 11, 27), MARKS)
    assert fleet_value_marks.counts(FLEET, date(2026, 11, 28), MARKS)


def test_a_mark_is_in_the_window_from_its_first_day() -> None:
    assert fleet_value_marks.in_window(MARK, None)
    assert fleet_value_marks.in_window(MARK, date(2026, 11, 1))
    assert not fleet_value_marks.in_window(MARK, date(2026, 11, 28))
    assert not fleet_value_marks.in_window(None, date(2026, 11, 1))
