"""The role of a platform event trigger (#2054).

``trigger_config.role = "observer"`` declares an assignment that watches the
objects of its events without working them (a supervisor of the CTEM case
lifecycle); the CTEM case page leaves observers out of who works the case.
"""

from __future__ import annotations

import pytest

from app.api.v1.schemas.assignment import CreateAssignmentRequest
from app.services import platform_events as pe

CASE_EVENTS = ["ctem_case.opened", "ctem_case.reopened"]


@pytest.mark.parametrize("role", ["observer", "worker"])
def test_a_known_role_is_accepted(role: str) -> None:
    assert pe.trigger_config_error("platform_event", {"event": CASE_EVENTS, "role": role}) is None


def test_no_role_is_accepted() -> None:
    assert pe.trigger_config_error("platform_event", {"event": CASE_EVENTS}) is None


@pytest.mark.parametrize("bad", ["supervisor", "", 1, True, ["observer"]])
def test_an_unknown_role_is_refused(bad: object) -> None:
    error = pe.trigger_config_error("platform_event", {"event": CASE_EVENTS, "role": bad})
    assert error is not None and "'role'" in error


def test_the_create_request_keeps_the_role_as_sent() -> None:
    req = CreateAssignmentRequest(
        agent_id="00000000-0000-0000-0000-000000000001",
        name="Supervise the case lifecycle",
        prompt="Read the case lifecycle.",
        trigger_type="platform_event",
        trigger_config={"event": CASE_EVENTS, "filter": {"case_id": {"exists": True}}, "role": "observer"},
    )
    assert req.trigger_config["role"] == "observer"


def test_the_create_request_refuses_an_unknown_role() -> None:
    with pytest.raises(ValueError, match="'role'"):
        CreateAssignmentRequest(
            agent_id="00000000-0000-0000-0000-000000000001",
            name="Supervise",
            prompt="Read the case lifecycle.",
            trigger_type="platform_event",
            trigger_config={"event": CASE_EVENTS, "role": "boss"},
        )
