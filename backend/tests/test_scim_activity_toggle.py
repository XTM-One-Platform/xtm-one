"""The latest SCIM changes name the direction of a deactivation (#401).

``_write_user`` audits an ``active`` change as action ``toggle`` with the new
state in the details; ``list_scim_activity`` exposes it as ``active`` so the
Provisioning screen writes "deactivated" or "reactivated" in the app language
instead of the backend's English summary.
"""

from app.api.v1.scim import _toggled_state


def test_a_toggle_row_answers_the_state_it_recorded() -> None:
    assert _toggled_state("toggle", {"changed": ["active"], "active": False}) is False
    assert _toggled_state("toggle", {"changed": ["active"], "active": True}) is True


def test_any_other_row_answers_none() -> None:
    assert _toggled_state("update", {"changed": ["name"], "active": True}) is None
    assert _toggled_state("toggle", None) is None
    assert _toggled_state("toggle", {"changed": ["active"]}) is None
    assert _toggled_state("toggle", {"active": "false"}) is None
