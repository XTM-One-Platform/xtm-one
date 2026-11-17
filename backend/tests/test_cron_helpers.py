"""Unit tests for the shared cron helpers in ``core.utils``.

``cron_is_valid()`` and ``next_cron_run()`` are the single source of truth for
"is this schedule parseable?" and "when does it next fire?", depended on by both
the autonomous scheduler (``autonomous._process_scheduled_assignments``) and the
read-only schedule endpoint (``GET /assignments/schedule``). Pin their behaviour
- invalid / non-string inputs, timezone-aware bases, the ``after`` vs ``now``
precedence, and the ``after=None`` fallback - so a future refactor cannot
reintroduce next-run drift or a cron edge case.
"""

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

from app.core.utils import (
    cron_is_valid,
    describe_schedule_next_run,
    next_cron_run,
    schedule_timezone,
)

PARIS = ZoneInfo("Europe/Paris")


def _utc(*args: int) -> datetime:
    return datetime(*args, tzinfo=UTC)


def test_cron_is_valid_accepts_standard_and_six_field():
    assert cron_is_valid("0 9 * * *") is True
    assert cron_is_valid("0 9 * * MON-FRI") is True
    # croniter also accepts the 6-field (leading seconds) form.
    assert cron_is_valid("30 0 9 * * 1") is True
    # surrounding whitespace is tolerated (stripped before parsing).
    assert cron_is_valid("  0 9 * * *  ") is True


def test_cron_is_valid_rejects_bad_and_non_string():
    assert cron_is_valid("nope") is False
    assert cron_is_valid("0 9") is False  # too few fields
    assert cron_is_valid("") is False
    assert cron_is_valid("   ") is False
    assert cron_is_valid(None) is False
    assert cron_is_valid(123) is False
    assert cron_is_valid(["0 9 * * *"]) is False


def test_next_cron_run_returns_none_for_unparseable():
    assert next_cron_run("nope", None) is None
    assert next_cron_run("0 9", None) is None
    assert next_cron_run("", None) is None
    assert next_cron_run("   ", None) is None
    # non-string inputs must not raise - read-only callers treat them as "no run".
    assert next_cron_run(None, None) is None
    assert next_cron_run(123, None) is None
    assert next_cron_run(["0 * * * *"], None) is None


def test_next_cron_run_advances_from_after_base():
    base = datetime(2026, 1, 1, 9, 0, tzinfo=UTC)
    nxt = next_cron_run("0 * * * *", base)
    assert nxt == datetime(2026, 1, 1, 10, 0, tzinfo=UTC)
    # tz-awareness of the base is preserved on the result.
    assert nxt.tzinfo is not None


def test_next_cron_run_uses_now_when_after_is_none():
    now = datetime(2026, 6, 1, 12, 30, tzinfo=UTC)
    # after=None (never triggered) -> base falls back to ``now``, so the next
    # daily 09:00 fire is the following morning.
    nxt = next_cron_run("0 9 * * *", None, now=now)
    assert nxt == datetime(2026, 6, 2, 9, 0, tzinfo=UTC)


def test_next_cron_run_prefers_after_over_now():
    after = datetime(2026, 1, 1, 9, 0, tzinfo=UTC)
    now = datetime(2026, 12, 31, 23, 0, tzinfo=UTC)
    # ``after`` (last_triggered_at) is the authoritative base; ``now`` is only a
    # fallback when ``after`` is None.
    nxt = next_cron_run("0 * * * *", after, now=now)
    assert nxt == datetime(2026, 1, 1, 10, 0, tzinfo=UTC)


def test_next_cron_run_defaults_to_current_time_when_no_base():
    # after=None and now=None -> base is datetime.now(UTC); just assert it yields
    # a tz-aware future datetime rather than raising.
    nxt = next_cron_run("* * * * *", None)
    assert nxt is not None
    assert nxt.tzinfo is not None


# --- trigger_config.timezone ------------------------------------------------
# A schedule's cron is read on the wall clock of ``trigger_config.timezone``
# (IANA, daylight saving included), UTC when absent or unknown.


def _next(cron: str, config_tz: object, base: datetime) -> datetime | None:
    config = {"cron": cron}
    if config_tz is not None:
        config["timezone"] = config_tz
    zone, _ = schedule_timezone(config)
    return next_cron_run(cron, base, tz=zone)


@pytest.mark.parametrize(
    ("config_tz", "base", "expected"),
    [
        # Winter time (UTC+1): 07:00 Paris is 06:00Z.
        ("Europe/Paris", _utc(2026, 10, 31, 0, 0), _utc(2026, 10, 31, 6, 0)),
        # Summer time (UTC+2): 07:00 Paris is 05:00Z.
        ("Europe/Paris", _utc(2026, 7, 15, 0, 0), _utc(2026, 7, 15, 5, 0)),
        # New York is still on summer time on 31 Oct (UTC-4).
        ("America/New_York", _utc(2026, 10, 31, 0, 0), _utc(2026, 10, 31, 11, 0)),
        # No timezone: unchanged, the cron reads as UTC.
        (None, _utc(2026, 10, 31, 0, 0), _utc(2026, 10, 31, 7, 0)),
        ("", _utc(2026, 10, 31, 0, 0), _utc(2026, 10, 31, 7, 0)),
        ("UTC", _utc(2026, 10, 31, 0, 0), _utc(2026, 10, 31, 7, 0)),
        # Invalid timezone: UTC (and a warning, below).
        ("Mars/Olympus", _utc(2026, 10, 31, 0, 0), _utc(2026, 10, 31, 7, 0)),
    ],
)
def test_daily_cron_fires_at_the_local_hour(config_tz, base, expected):
    assert _next("0 7 * * *", config_tz, base) == expected


def test_invalid_timezone_falls_back_to_utc_with_a_warning():
    zone, warning = schedule_timezone({"cron": "0 7 * * *", "timezone": "Mars/Olympus"})
    assert zone is UTC
    assert warning == "timezone 'Mars/Olympus' not recognised, UTC used"
    _, non_string = schedule_timezone({"timezone": 2})
    assert non_string is not None and "not recognised, UTC used" in non_string
    assert schedule_timezone({"timezone": "Europe/Paris"}) == (PARIS, None)
    assert schedule_timezone(None) == (UTC, None)


def test_result_is_utc_and_strictly_after_the_base():
    # Fired at 06:00Z (07:00 Paris): the next is tomorrow, never the same fire.
    nxt = _next("0 7 * * *", "Europe/Paris", _utc(2026, 10, 31, 6, 0))
    assert nxt == _utc(2026, 11, 1, 6, 0)
    assert nxt.utcoffset().total_seconds() == 0


def test_weekday_range_cron_in_paris():
    # "0 10,15 * * 1-5" on Friday 30 Oct 2026: 10:00 and 15:00 Paris.
    assert _next("0 10,15 * * 1-5", "Europe/Paris", _utc(2026, 10, 30, 9, 30)) == _utc(
        2026, 10, 30, 14, 0
    )
    # After 15:00 Paris on Friday: Monday 10:00 Paris.
    assert _next("0 10,15 * * 1-5", "Europe/Paris", _utc(2026, 10, 30, 14, 0)) == _utc(
        2026, 11, 2, 9, 0
    )


def test_last_fire_of_the_broken_build_does_not_fire_again_the_same_day():
    # Deploy day: the broken build fired "0 7" at 07:00Z (08:00 Paris). The new
    # computation from that base is tomorrow 07:00 Paris, not a second fire today.
    assert _next("0 7 * * *", "Europe/Paris", _utc(2026, 10, 31, 7, 0, 36)) == _utc(
        2026, 11, 1, 6, 0
    )
    # Deploy before the local hour, last fire yesterday on the broken build:
    # today's 07:00 Paris (06:00Z) is due once; the base then moves past it.
    assert _next("0 7 * * *", "Europe/Paris", _utc(2026, 10, 30, 7, 0, 36)) == _utc(
        2026, 10, 31, 6, 0
    )


# --- daylight saving transitions (Europe/Paris, 2026) ------------------------


def test_spring_forward_skipped_hour_fires_once_at_the_next_valid_instant():
    # 29 Mar 2026: 02:00 Paris jumps to 03:00 (01:00Z). "30 2" does not exist
    # that day: it fires once at the transition, 03:00 Paris = 01:00Z.
    nxt = _next("30 2 * * *", "Europe/Paris", _utc(2026, 3, 28, 12, 0))
    assert nxt == _utc(2026, 3, 29, 1, 0)
    # And once only: the next is 30 Mar 02:30 Paris (summer time, 00:30Z).
    assert _next("30 2 * * *", "Europe/Paris", nxt) == _utc(2026, 3, 30, 0, 30)


def test_spring_forward_hours_around_the_gap():
    # 01:30 Paris (winter, 00:30Z) and 03:30 Paris (summer, 01:30Z) on 29 Mar.
    assert _next("30 1 * * *", "Europe/Paris", _utc(2026, 3, 28, 12, 0)) == _utc(
        2026, 3, 29, 0, 30
    )
    assert _next("30 3 * * *", "Europe/Paris", _utc(2026, 3, 28, 12, 0)) == _utc(
        2026, 3, 29, 1, 30
    )


def test_spring_forward_hourly_cron_never_fires_twice_at_the_transition():
    base = _utc(2026, 3, 28, 23, 30)  # 00:30 Paris
    fires = []
    for _ in range(4):
        base = _next("0 * * * *", "Europe/Paris", base)
        fires.append(base)
    # 01:00, (02:00 skipped) 03:00, 04:00, 05:00 Paris.
    assert fires == [
        _utc(2026, 3, 29, 0, 0),
        _utc(2026, 3, 29, 1, 0),
        _utc(2026, 3, 29, 2, 0),
        _utc(2026, 3, 29, 3, 0),
    ]


def test_fall_back_repeated_hour_fires_once():
    # 25 Oct 2026: 03:00 Paris goes back to 02:00 (01:00Z); 02:30 Paris happens
    # at 00:30Z and again at 01:30Z. It fires once, at the first.
    nxt = _next("30 2 * * *", "Europe/Paris", _utc(2026, 10, 24, 12, 0))
    assert nxt == _utc(2026, 10, 25, 0, 30)
    # From that fire, the next is 26 Oct 02:30 Paris (winter, 01:30Z), not the
    # second 02:30 of the 25th.
    assert _next("30 2 * * *", "Europe/Paris", nxt) == _utc(2026, 10, 26, 1, 30)


def test_fall_back_daily_cron_around_the_change():
    # 07:00 Paris is 05:00Z on 24 Oct (summer) and 06:00Z on 25 Oct (winter).
    assert _next("0 7 * * *", "Europe/Paris", _utc(2026, 10, 24, 0, 0)) == _utc(
        2026, 10, 24, 5, 0
    )
    assert _next("0 7 * * *", "Europe/Paris", _utc(2026, 10, 24, 5, 0)) == _utc(
        2026, 10, 25, 6, 0
    )


def test_describe_schedule_next_run_shows_both_zones_and_the_warning():
    paris = describe_schedule_next_run(
        {"cron": "0 7 * * *", "timezone": "Europe/Paris"}, _utc(2026, 10, 31, 0, 0)
    )
    assert paris == {
        "next_run_at": "2026-10-31T06:00:00+00:00",
        "timezone": "Europe/Paris",
        "next_run_local": "2026-10-31T07:00:00+01:00",
        "timezone_warning": None,
    }
    bad = describe_schedule_next_run(
        {"cron": "0 7 * * *", "timezone": "Mars/Olympus"}, _utc(2026, 10, 31, 0, 0)
    )
    assert bad["next_run_at"] == "2026-10-31T07:00:00+00:00"
    assert bad["timezone"] == "UTC"
    assert bad["timezone_warning"] == "timezone 'Mars/Olympus' not recognised, UTC used"
    none = describe_schedule_next_run({"cron": "nope"}, None)
    assert none["next_run_at"] is None and none["next_run_local"] is None
