# SPDX-License-Identifier: Apache-2.0

"""Why and since when an integration is not usable, and the gate checks that depend on it (#1665)."""

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, cast

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.integration import Integration
from app.services import integration_status
from app.services.assignment_activation_check import ActivationCheckOutcome
from app.services.integration_status import (
    check_integration_ids,
    current_reason,
    describe_unusable,
    gate_check_integration_warning,
    record_status_reason,
)


class _Rows:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def all(self) -> list[Any]:
        return self._rows


class _FakeDb:
    """Answers every select with *rows* and counts the reads."""

    def __init__(self, rows: list[Any]) -> None:
        self.rows = rows
        self.reads = 0

    async def execute(self, _query: object) -> _Rows:
        self.reads += 1
        return _Rows(self.rows)


def _db(rows: list[Any]) -> tuple[_FakeDb, AsyncSession]:
    fake = _FakeDb(rows)
    return fake, cast(AsyncSession, fake)


def test_the_reason_is_recorded_with_its_time_and_read_only_while_unusable() -> None:
    integration = Integration(provider="github", name="GitHub", status="connected")
    assert current_reason(integration) == (None, None)

    record_status_reason(integration, "error", "token refresh failed: HTTP 503 from the token endpoint")
    assert integration.status == "error"
    reason, at = current_reason(integration)
    assert reason == "token refresh failed: HTTP 503 from the token endpoint"
    assert at is not None and at.tzinfo is not None

    # A reconnect (the OAuth callback, a working token) writes the status alone: the reason is history.
    integration.status = "connected"
    assert current_reason(integration) == (None, None)


def test_the_reason_is_cut_to_the_column_and_a_holder_without_the_column_keeps_its_status_alone() -> None:
    integration = Integration(provider="github", name="GitHub", status="connected")
    record_status_reason(integration, "reauthorize", "x" * 1000)
    assert len(integration.status_reason or "") == integration_status.REASON_MAX

    credential = SimpleNamespace(status="connected")
    record_status_reason(credential, "error", "token refresh failed")
    assert credential.status == "error"
    assert not hasattr(credential, "status_reason")


def test_the_description_names_status_time_and_reason() -> None:
    at = datetime(2026, 11, 24, 5, 0, tzinfo=UTC)
    assert describe_unusable("error", "token refresh failed", at) == (
        "status error since 2026-11-24 05:00 UTC: token refresh failed"
    )
    assert describe_unusable("reauthorize", None, None) == "status reauthorize: no reason recorded"


def test_the_integration_ids_of_a_chained_and_any_of_gate_are_all_read() -> None:
    first, second = str(uuid.uuid4()), str(uuid.uuid4())
    check = {
        "tool": "list_calendar_events",
        "integration_id": first,
        "also": [{"tool": "get_shared_state", "arguments": {}}],
        "or": [{"tool": "list_mail", "integration_id": second}, {"tool": "x", "integration_id": first}],
    }
    assert sorted(check_integration_ids(check)) == sorted([first, second])
    assert check_integration_ids(None) == []


@pytest.mark.asyncio
async def test_a_gate_on_an_integration_in_error_is_named_with_its_reason() -> None:
    integration_id = uuid.uuid4()
    at = datetime(2026, 11, 24, 5, 0, tzinfo=UTC)
    _fake, db = _db(
        [SimpleNamespace(id=integration_id, status="error", status_reason="token refresh failed", status_reason_at=at)]
    )
    warning = await gate_check_integration_warning(
        db, {"tool": "list_calendar_events", "integration_id": str(integration_id)}
    )
    assert warning is not None
    assert str(integration_id) in warning
    assert "status error since 2026-11-24 05:00 UTC: token refresh failed" in warning


@pytest.mark.asyncio
async def test_a_gate_on_no_integration_reads_nothing_and_warns_nothing() -> None:
    fake, db = _db([])
    assert await gate_check_integration_warning(db, {"tool": "list_work_items"}) is None
    assert fake.reads == 0
    assert await gate_check_integration_warning(db, {"tool": "x", "integration_id": "not-a-uuid"}) is None
    assert fake.reads == 0


@pytest.mark.asyncio
async def test_a_failing_read_warns_nothing() -> None:
    class _Broken:
        async def execute(self, _query: object) -> None:
            raise RuntimeError("database down")

    broken = cast(AsyncSession, _Broken())
    assert await gate_check_integration_warning(broken, {"tool": "x", "integration_id": str(uuid.uuid4())}) is None


@pytest.mark.asyncio
async def test_the_run_record_names_the_integration_state_on_a_gate_check_error() -> None:
    from app.services.assignment_engine import _with_integration_state

    integration_id = uuid.uuid4()
    _fake, db = _db(
        [SimpleNamespace(id=integration_id, status="reauthorize", status_reason="grant revoked", status_reason_at=None)]
    )
    outcome = ActivationCheckOutcome(True, "Gate check x not evaluated", error="x failed: 401")
    enriched = await _with_integration_state(db, {"tool": "x", "integration_id": str(integration_id)}, outcome)
    assert enriched.error == f"integration {integration_id} status reauthorize: grant revoked; x failed: 401"
    assert enriched.passed is True

    _healthy, healthy = _db([])
    same = await _with_integration_state(healthy, {"tool": "x", "integration_id": str(integration_id)}, outcome)
    assert same is outcome
