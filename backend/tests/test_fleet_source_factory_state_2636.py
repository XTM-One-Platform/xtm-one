"""Fleet-owned sources at factory state read not provisioned, never orphaned or Error (#2636).

A fleet automation switched off holding nothing in OpenCTI drops its leftover provisioning
state instead of reading orphaned with the no run-as reason, and a factory reset removes what
the automations provisioned while their run-as can still reach it, then drops the state.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, patch

import pytest

from app.services import fleet_reset
from app.services import opencti_trigger_provisioning as prov
from app.services.fleet_run_as import NO_RUN_AS_REASON

NOW = datetime(2026, 11, 27, 16, 0, tzinfo=UTC)


NO_DB = cast(Any, None)


def _assignment(state: dict[str, Any] | None, *, enabled: bool = False) -> SimpleNamespace:
    config: dict[str, Any] = {
        "provider": "opencti",
        "provision": {"fleet": "CTEM loop", "purpose": "New relevant threats", "entity_types": ["Campaign"]},
    }
    if state is not None:
        config[prov.STATE_KEY] = state
    return SimpleNamespace(
        id="a1", name="New relevant threat", enabled=enabled, run_as_user_id=None, trigger_config=config
    )


def test_held_reads_objects_only() -> None:
    assert not prov.held(None)
    assert not prov.held({"status": "error", "error": NO_RUN_AS_REASON})
    assert prov.held({"trigger_id": "t1"})
    assert prov.held({"notifier_id": "n1"})
    assert prov.held({"playbook_id": "p1"})
    assert prov.held({"trigger_ids": {"x": "t1"}})


@pytest.mark.asyncio
async def test_an_off_source_holding_nothing_drops_its_leftover_error(monkeypatch: pytest.MonkeyPatch) -> None:
    connection = AsyncMock(return_value=NO_RUN_AS_REASON)
    monkeypatch.setattr(prov, "_connection", connection)
    assignment = _assignment({"status": "error", "error": NO_RUN_AS_REASON, "checked_at": "x"})
    assert await prov.provision_one(None, assignment, SimpleNamespace(), now=NOW) is None
    assert prov.STATE_KEY not in assignment.trigger_config
    # No connection is needed to forget a state that names nothing in OpenCTI.
    connection.assert_not_awaited()
    # The provision block stays: fleet setup provisions it as the chosen run-as.
    assert assignment.trigger_config["provision"]["purpose"] == "New relevant threats"


@pytest.mark.asyncio
async def test_an_off_source_still_holding_objects_unreachable_stays_orphaned(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(prov, "_connection", AsyncMock(return_value=NO_RUN_AS_REASON))
    assignment = _assignment({"status": "provisioned", "trigger_id": "t1", "notifier_id": "n1"})
    state = await prov.provision_one(None, assignment, SimpleNamespace(), now=NOW)
    assert state is not None and state["status"] == "orphaned"


@pytest.mark.asyncio
async def test_an_enabled_source_without_run_as_still_reads_its_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(prov, "_connection", AsyncMock(return_value=NO_RUN_AS_REASON))
    assignment = _assignment(None, enabled=True)
    state = await prov.provision_one(None, assignment, SimpleNamespace(), now=NOW)
    assert state is not None and state["status"] == "error" and state["error"] == NO_RUN_AS_REASON


@pytest.mark.asyncio
async def test_reset_removes_the_provisioned_objects_then_drops_the_state() -> None:
    assignment = _assignment({"status": "provisioned", "trigger_id": "t1", "notifier_id": "n1"})
    seen: list[dict[str, Any]] = []

    async def _deprovision(_db: object, a: SimpleNamespace) -> None:
        # Called before the state is dropped, so the provisioner still knows what to remove.
        seen.append(dict(a.trigger_config[prov.STATE_KEY]))

    with patch("app.services.trigger_provisioning.deprovision_assignment", _deprovision):
        await fleet_reset._remove_provisioned(NO_DB, assignment)
    assert seen == [{"status": "provisioned", "trigger_id": "t1", "notifier_id": "n1"}]
    assert prov.STATE_KEY not in assignment.trigger_config
    assert assignment.trigger_config["provision"]["purpose"] == "New relevant threats"


@pytest.mark.asyncio
async def test_reset_leaves_an_assignment_without_provisioning_untouched() -> None:
    deprovision = AsyncMock()
    assignment = SimpleNamespace(trigger_config={"cron": "0 8 * * *"})
    with patch("app.services.trigger_provisioning.deprovision_assignment", deprovision):
        await fleet_reset._remove_provisioned(NO_DB, assignment)
        await fleet_reset._remove_provisioned(NO_DB, SimpleNamespace())
    deprovision.assert_not_awaited()
    assert assignment.trigger_config == {"cron": "0 8 * * *"}
