"""A future ``not_before`` defers the inbox pickup, the ``due_at`` deadline
never does (#8685, #8753): the release note and the update result say so, and
an explicit null clears either date."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.services.builtin_tools.work_items import _UNSET, _clearable, _due_deferral_line
from app.services.work_item_blocked_refs import due_deferral, release_note

NOW = datetime(2026, 11, 4, 21, 55, 46, tzinfo=UTC)


def test_release_note_names_the_not_before_that_defers_the_pickup():
    later = NOW + timedelta(days=2)
    note = release_note(["xtm-one-platform/xtm-one#8509"], None, NOW) + due_deferral(later, NOW)
    assert note.endswith("; the inbox takes it from its not_before (2026-11-06T21:55:46Z)")


def test_no_suffix_without_a_not_before_or_once_it_is_reached():
    assert due_deferral(None, NOW) == ""
    assert due_deferral(NOW, NOW) == ""
    assert due_deferral(NOW - timedelta(minutes=1), NOW) == ""


def test_a_naive_not_before_is_read_as_utc():
    assert due_deferral((NOW + timedelta(hours=1)).replace(tzinfo=None), NOW) == (
        "; the inbox takes it from its not_before (2026-11-04T22:55:46Z)"
    )


def _item(**over):
    item = {
        "status": "open",
        "owner_agent_id": "9c1142f6-d1e1-434a-9099-1c63e3286156",
        "not_before": (NOW + timedelta(days=2)).isoformat(),
        "due_at": None,
    }
    item.update(over)
    return item


def test_update_result_says_an_open_owned_item_waits_for_its_not_before():
    line = _due_deferral_line(_item(), now=NOW)
    assert line.startswith("  - not picked up before 2026-11-06T21:55:46+00:00")
    assert "Set not_before to null" in line


def test_update_result_is_silent_when_nothing_defers_the_pickup():
    assert _due_deferral_line(_item(not_before=None), now=NOW) == ""
    assert _due_deferral_line(_item(not_before=(NOW - timedelta(hours=1)).isoformat()), now=NOW) == ""
    assert _due_deferral_line(_item(status="blocked"), now=NOW) == ""
    assert _due_deferral_line(_item(owner_agent_id=None), now=NOW) == ""
    assert _due_deferral_line(_item(not_before="not a date"), now=NOW) == ""


def test_a_deadline_ahead_defers_nothing():
    ahead = (NOW + timedelta(days=2)).isoformat()
    assert _due_deferral_line(_item(not_before=None, due_at=ahead), now=NOW) == ""


def test_only_an_explicit_null_clears_a_date():
    # Not given, or the empty string a model sends for an unused argument: unchanged.
    assert _clearable(_UNSET) is None
    assert _clearable("") is None
    # An explicit null clears; a value sets.
    assert _clearable(None) == ""
    assert _clearable("2026-11-07T17:00:00Z") == "2026-11-07T17:00:00Z"
