# SPDX-License-Identifier: Apache-2.0
"""``trigger_config.run_on_create: false`` skips a schedule's creation auto-run (#607).

- absent or ``true``: a never-run, never-resumed schedule is due at the first
  tick (the creation auto-run, unchanged);
- ``false``: the cron advances from ``created_at``, so the first fire is the
  first cron occurrence after creation;
- a non-boolean value is refused where the trigger is saved.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.core.utils import next_cron_run, schedule_base, validate_schedule_trigger_config
from app.services import autonomous


def _result(items: list[Any]) -> MagicMock:
    scalars = MagicMock()
    scalars.all.return_value = items
    res = MagicMock()
    res.scalars.return_value = scalars
    return res


def _assignment(config: dict[str, Any], *, created_at: datetime) -> MagicMock:
    assignment = MagicMock()
    assignment.id = uuid.uuid4()
    assignment.agent_id = uuid.uuid4()
    assignment.user_id = uuid.uuid4()
    assignment.run_as_user_id = None
    assignment.is_company_managed = False
    assignment.enabled = True
    assignment.run_as_all_users = False
    assignment.name = "Monday report"
    assignment.trigger_type = "schedule"
    assignment.trigger_config = config
    assignment.last_triggered_at = None
    assignment.schedule_resumed_at = None
    assignment.created_at = created_at
    return assignment


async def _tick(monkeypatch: pytest.MonkeyPatch, assignment: MagicMock) -> AsyncMock:
    agent = MagicMock()
    agent.id = assignment.agent_id
    agent.enabled = True
    agent.is_builtin = False
    db = MagicMock()
    db.execute = AsyncMock(side_effect=[_result([assignment]), _result([agent])])
    db.commit = AsyncMock()
    db.rollback = AsyncMock()
    monkeypatch.setattr(autonomous, "has_active_run", AsyncMock(return_value=False))
    trigger = AsyncMock()
    monkeypatch.setattr(autonomous, "trigger_assignment", trigger)
    await autonomous._process_scheduled_assignments(db)
    return trigger


def _cron_minutes_ago(minutes: int, now: datetime) -> str:
    past = now - timedelta(minutes=minutes)
    return f"{past.minute} {past.hour} * * *"


def test_the_anchor_is_the_creation_only_when_run_on_create_is_false() -> None:
    created = datetime(2026, 11, 20, 4, 0, tzinfo=UTC)
    assert schedule_base(SimpleNamespace(trigger_config={"run_on_create": False}, created_at=created)) == created
    assert schedule_base(SimpleNamespace(trigger_config={"run_on_create": True}, created_at=created)) is None
    assert schedule_base(SimpleNamespace(trigger_config={}, created_at=created)) is None
    fired = created + timedelta(days=1)
    assert (
        schedule_base(
            SimpleNamespace(trigger_config={"run_on_create": False}, created_at=created, last_triggered_at=fired)
        )
        == fired
    )


@pytest.mark.asyncio
async def test_a_new_schedule_with_run_on_create_false_waits_for_its_cron(monkeypatch: pytest.MonkeyPatch) -> None:
    now = datetime.now(UTC)
    cron = _cron_minutes_ago(10, now)
    assignment = _assignment({"cron": cron, "run_on_create": False}, created_at=now - timedelta(seconds=30))

    trigger = await _tick(monkeypatch, assignment)

    trigger.assert_not_awaited()
    nxt = next_cron_run(cron, schedule_base(assignment))
    assert nxt is not None and nxt > now


@pytest.mark.asyncio
async def test_a_new_schedule_keeps_its_creation_run_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    now = datetime.now(UTC)
    cron = _cron_minutes_ago(10, now)
    assignment = _assignment({"cron": cron}, created_at=now - timedelta(seconds=30))

    trigger = await _tick(monkeypatch, assignment)

    trigger.assert_awaited()
    assert trigger.await_args is not None
    assert trigger.await_args.kwargs["trigger_context"]["_first_run"] is True


@pytest.mark.asyncio
async def test_run_on_create_false_still_fires_a_cron_occurrence_after_creation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime.now(UTC)
    cron = _cron_minutes_ago(1, now)
    assignment = _assignment({"cron": cron, "run_on_create": False}, created_at=now - timedelta(minutes=5))

    trigger = await _tick(monkeypatch, assignment)

    trigger.assert_awaited()


def test_a_non_boolean_run_on_create_is_refused() -> None:
    validate_schedule_trigger_config("schedule", {"cron": "0 7 * * 1", "run_on_create": False})
    with pytest.raises(ValueError, match="run_on_create must be true or false"):
        validate_schedule_trigger_config("schedule", {"cron": "0 7 * * 1", "run_on_create": "no"})
