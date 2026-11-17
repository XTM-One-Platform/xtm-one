"""The opt-in state of a fleet's automations (#9566): ``fleet_views.automation_counts``.

The command center reads it to say Not set up / Partially on / On / Paused
instead of a run health that reads "Healthy" for a fleet nobody switched on.
"""

from __future__ import annotations

from app.agents.builtin_agents import FLEET_OPT_IN_ASSIGNMENTS
from app.agents.fleet_opt_in import ENABLED_BY_DEFAULT_ALLOWLIST
from app.services.fleet_views import AutomationCounts, automation_counts

_FLEET = sorted(FLEET_OPT_IN_ASSIGNMENTS)
_DASHBOARD = sorted(ENABLED_BY_DEFAULT_ALLOWLIST)


def test_a_seeded_fleet_left_as_shipped_is_not_set_up():
    rows = [(slug, True, name, False, "created_disabled") for slug, name in _FLEET[:3]]
    assert automation_counts(rows, builtin_fleet=True) == AutomationCounts(total=3, enabled=0, paused=0)


def test_quota_exempt_dashboard_automations_never_make_a_builtin_fleet_on():
    rows = [(slug, True, name, True, None) for slug, name in _DASHBOARD[:2]]
    rows.append((_FLEET[0][0], True, _FLEET[0][1], False, "created_disabled"))
    assert automation_counts(rows, builtin_fleet=True) == AutomationCounts(total=1, enabled=0, paused=0)


def test_one_switched_on_is_partially_on_and_a_user_pause_counts_as_paused():
    (a_slug, a_name), (b_slug, b_name), (c_slug, c_name) = _FLEET[:3]
    rows = [
        (a_slug, True, a_name, True, None),
        (b_slug, True, b_name, False, "user_toggle"),
        (c_slug, True, c_name, False, "system_seed"),
    ]
    assert automation_counts(rows, builtin_fleet=True) == AutomationCounts(total=3, enabled=1, paused=1)


def test_a_custom_fleet_counts_every_assignment_of_its_members():
    rows = [
        ("my-agent", False, "Daily triage", True, None),
        (None, False, "Weekly report", False, "user_toggle"),
        (None, False, "Draft", False, "created_disabled"),
    ]
    assert automation_counts(rows, builtin_fleet=False) == AutomationCounts(total=3, enabled=1, paused=1)


def test_off_names_lists_every_counted_automation_that_is_not_on_in_order():
    """The home Fleets widget's tooltip (#11996): seeded off and paused alike, sorted."""
    rows = [
        (None, False, "Weekly report", False, "user_toggle"),
        (None, False, "Daily triage", True, None),
        (None, False, "Draft", False, "created_disabled"),
    ]
    assert automation_counts(rows, builtin_fleet=False).off_names == ("Draft", "Weekly report")


def test_an_assignment_a_user_added_to_a_builtin_member_is_not_a_fleet_automation():
    slug, _name = _FLEET[0]
    rows = [(slug, True, "My own schedule", True, None)]
    assert automation_counts(rows, builtin_fleet=True) == AutomationCounts()
