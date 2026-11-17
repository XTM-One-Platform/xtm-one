"""The emergency stop's re-enable gates, against a real PostgreSQL.

An assignment the emergency stop paused comes back only by lifting the stop.
The ordinary power toggle (``PUT /assignments/{id}``) answers 409 and the
agent-builder ``update_assignment`` tool refuses, and neither saves any part of
the call. The resume side (one user lifting their own stop while another
user's stop covers a shared assignment) is in ``test_panic_stop_postgres.py``.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from fastapi import HTTPException

from app.services.assignment_pause import PANIC_STOP_REASON
from tests.test_agent_builder_schedule_postgres import _update
from tests.test_assignment_update_explicit_null_postgres import _put, _seed, _stored
from tests.test_provisioning_postgres import _temp_database

pytestmark = pytest.mark.integration

_STOPPED = {
    "enabled": False,
    "paused_reason": PANIC_STOP_REASON,
    "paused_at": datetime(2026, 10, 1, 9, 0, tzinfo=UTC),
    "paused_details": "Emergency stop pressed by Panic Tester (own activity).",
}


@pytest.mark.asyncio
async def test_the_power_toggle_refuses_an_emergency_stopped_assignment():
    async with _temp_database("stop_gate") as sessionmaker:
        user, assignment_id = await _seed(sessionmaker, **_STOPPED)

        with pytest.raises(HTTPException) as refused:
            await _put(
                sessionmaker, user, assignment_id, {"enabled": True, "name": "Renamed"}
            )

        assert refused.value.status_code == 409
        assert refused.value.detail["code"] == "emergency_stop"
        stored = await _stored(sessionmaker, assignment_id)
        assert (stored.enabled, stored.paused_reason, stored.name) == (
            False,
            PANIC_STOP_REASON,
            "Digest",
        )


@pytest.mark.asyncio
async def test_the_agent_builder_refuses_to_re_enable_an_emergency_stopped_assignment():
    async with _temp_database("stop_gate") as sessionmaker:
        user, assignment_id = await _seed(sessionmaker, **_STOPPED)

        content = await _update(
            user.id,
            {"assignment_id": str(assignment_id), "name": "Renamed", "enabled": True},
        )

        assert "paused by an emergency stop" in content
        stored = await _stored(sessionmaker, assignment_id)
        assert (stored.enabled, stored.paused_reason, stored.name) == (
            False,
            PANIC_STOP_REASON,
            "Digest",
        )


@pytest.mark.asyncio
async def test_other_edits_of_an_emergency_stopped_assignment_still_save():
    """The gate is about re-enabling: a rename leaves the stop's pause alone."""
    async with _temp_database("stop_gate") as sessionmaker:
        user, assignment_id = await _seed(sessionmaker, **_STOPPED)

        await _put(sessionmaker, user, assignment_id, {"name": "Renamed"})

        stored = await _stored(sessionmaker, assignment_id)
        assert (stored.enabled, stored.paused_reason, stored.name) == (
            False,
            PANIC_STOP_REASON,
            "Renamed",
        )
