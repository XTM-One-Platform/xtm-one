"""``describe_schedule_history``: what ``get_assignment`` shows of a schedule's past.

The last fire, the last resume and the cron slot the resume skipped (the
schedule was off and enabling it never catches up, ``schedule_base``).
"""

from datetime import UTC, datetime
from types import SimpleNamespace

from app.core.utils import describe_schedule_history

T_FIRE = datetime(2026, 11, 1, 7, 30, tzinfo=UTC)
T_RESUME = datetime(2026, 11, 3, 8, 0, tzinfo=UTC)


def _assignment(**fields):
    return SimpleNamespace(trigger_config={"cron": "0 7 * * *"}, **fields)


def test_resume_after_a_missed_slot_names_the_skipped_slot():
    history = describe_schedule_history(_assignment(last_triggered_at=T_FIRE, schedule_resumed_at=T_RESUME))

    assert history == {
        "last_triggered_at": "2026-11-01T07:30:00+00:00",
        "schedule_resumed_at": "2026-11-03T08:00:00+00:00",
        "skipped_slot": "2026-11-03T07:00:00+00:00",
    }


def test_resume_before_the_last_fire_skips_nothing():
    history = describe_schedule_history(_assignment(last_triggered_at=T_RESUME, schedule_resumed_at=T_FIRE))

    assert history["skipped_slot"] is None


def test_resume_with_no_slot_since_the_last_fire_skips_nothing():
    fire = datetime(2026, 11, 3, 7, 0, tzinfo=UTC)
    history = describe_schedule_history(_assignment(last_triggered_at=fire, schedule_resumed_at=T_RESUME))

    assert history["skipped_slot"] is None


def test_never_triggered_uses_the_creation_as_the_floor():
    created = datetime(2026, 11, 3, 7, 30, tzinfo=UTC)
    history = describe_schedule_history(
        _assignment(last_triggered_at=None, schedule_resumed_at=T_RESUME, created_at=created)
    )

    assert history["last_triggered_at"] is None
    assert history["skipped_slot"] is None


def test_zoned_cron_names_the_slot_in_utc():
    assignment = SimpleNamespace(
        trigger_config={"cron": "0 7 * * *", "timezone": "Europe/Paris"},
        last_triggered_at=T_FIRE,
        schedule_resumed_at=T_RESUME,
    )

    assert describe_schedule_history(assignment)["skipped_slot"] == "2026-11-03T06:00:00+00:00"


def test_unparseable_cron_and_empty_row_answer_none():
    broken = SimpleNamespace(trigger_config={"cron": "nope"}, last_triggered_at=T_FIRE, schedule_resumed_at=T_RESUME)

    assert describe_schedule_history(broken)["skipped_slot"] is None
    assert describe_schedule_history(SimpleNamespace()) == {
        "last_triggered_at": None,
        "schedule_resumed_at": None,
        "skipped_slot": None,
    }
