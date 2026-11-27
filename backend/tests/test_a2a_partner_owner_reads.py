# Copyright (c) 2025-2026 Filigran SAS
#
# This file is part of the XTM One Enterprise Edition ("EE") and is
# licensed under the XTM One Enterprise Edition License (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://github.com/XTM-One-Platform/xtm-one/blob/main/LICENSE
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.

"""#2638: a partner task reads fleets and runs with the owner's read scope, behind an opt-in."""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from typing import Any

import pytest

from app.a2a import partner_fleet, partner_owner_reads
from app.a2a.partner_scope import (
    PARTNER_PRINCIPAL_PAYLOAD_KEY,
    PARTNER_TASK_PAYLOAD_KEY,
    PartnerPrincipal,
    partner_task_scope,
    queued_partner_principal,
)
from app.tools.base import CallableToolExecutor, ToolDefinition

OWNER = uuid.UUID(int=99)
HOME = uuid.UUID(int=42)
OTHER = uuid.UUID(int=43)
TASK = uuid.UUID(int=5)
PARTNER = PartnerPrincipal(exposure_id=uuid.UUID(int=7), client_id="ctem-orchestrator", task_id=TASK)

Calls = list[tuple[str, dict[str, Any]]]


def _suite() -> tuple[list[ToolDefinition], CallableToolExecutor, Calls]:
    calls: Calls = []
    executor = CallableToolExecutor()
    names = ["list_fleets", "get_fleet", "get_fleet_health", "create_fleet", "update_fleet", "apply_fleet_spec"]

    def _handler(name: str) -> Callable[..., Awaitable[str]]:
        async def _h(**kwargs: Any) -> str:
            calls.append((name, kwargs))
            return f"{name} {kwargs}"

        return _h

    for name in names:
        executor.register(name, _handler(name))
    definitions = [ToolDefinition(name=n, description=n, input_schema={"type": "object"}) for n in names]
    return definitions, executor, calls


async def _call(executor: CallableToolExecutor, name: str, **kwargs: Any) -> str:
    handler = executor.handler(name)
    assert handler is not None
    return str(await handler(**kwargs))


@pytest.fixture
def opt_ins(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    state: dict[str, Any] = {"owner": True, "fleet": False, "audited": []}

    async def _owner(_p: PartnerPrincipal) -> uuid.UUID | None:
        return OWNER if state["owner"] else None

    async def _audit(p: PartnerPrincipal, owner: uuid.UUID, tool: str, target: str = "") -> None:
        state["audited"].append((p.task_id, owner, tool, target))

    async def _fleet_opted(_p: PartnerPrincipal) -> bool:
        return bool(state["fleet"])

    async def _home(_p: PartnerPrincipal) -> uuid.UUID | None:
        return HOME if state["fleet"] else None

    monkeypatch.setattr(partner_owner_reads, "partner_owner_id", _owner)
    monkeypatch.setattr(partner_owner_reads, "audit_partner_read", _audit)
    monkeypatch.setattr(partner_owner_reads, "running_build_line", lambda: "Running build: test.")
    monkeypatch.setattr(partner_fleet, "partner_opted_in", _fleet_opted)
    monkeypatch.setattr(partner_fleet, "partner_home_fleet_id", _home)
    return state


@pytest.mark.asyncio
async def test_off_keeps_the_2202_behaviour(opt_ins: dict[str, Any]) -> None:
    opt_ins["owner"] = False
    defs, executor, calls = _suite()
    _, executor = partner_fleet.scope_fleet_tools_to_partner(defs, executor)
    with partner_task_scope(principal=PARTNER):
        assert await _call(executor, "get_fleet", fleet_id=str(OTHER)) == partner_fleet.OPT_IN_OFF
        assert await _call(executor, "list_fleets") == partner_fleet.OPT_IN_OFF
    assert calls == []
    assert opt_ins["audited"] == []


@pytest.mark.asyncio
async def test_on_reads_any_fleet_the_owner_reads_and_audits_each(opt_ins: dict[str, Any]) -> None:
    defs, executor, calls = _suite()
    _, executor = partner_fleet.scope_fleet_tools_to_partner(defs, executor)
    with partner_task_scope(principal=PARTNER):
        listing = await _call(executor, "list_fleets")
        await _call(executor, "get_fleet", fleet_id=str(OTHER))
        await _call(executor, "get_fleet_health", fleet_id=str(OTHER), apply_safe_fixes=True)
    assert "Running build: test." in listing
    assert ("get_fleet", {"fleet_id": str(OTHER)}) in calls
    # A read: no fix is ever applied, whatever the partner asks.
    assert ("get_fleet_health", {"fleet_id": str(OTHER), "apply_safe_fixes": False}) in calls
    assert [a[2] for a in opt_ins["audited"]] == ["list_fleets", "get_fleet", "get_fleet_health"]
    assert all(a[0] == TASK and a[1] == OWNER for a in opt_ins["audited"])


def test_on_no_write_is_reachable(opt_ins: dict[str, Any]) -> None:
    defs, executor, _ = _suite()
    defs, executor = partner_fleet.scope_fleet_tools_to_partner(defs, executor)
    assert sorted(executor.tool_names) == sorted(partner_fleet.PARTNER_FLEET_TOOL_NAMES)
    for write in ("create_fleet", "update_fleet", "apply_fleet_spec"):
        assert executor.handler(write) is None


@pytest.mark.asyncio
async def test_outside_a_partner_task_nothing_is_lent(opt_ins: dict[str, Any]) -> None:
    defs, executor, calls = _suite()
    _, executor = partner_fleet.scope_fleet_tools_to_partner(defs, executor)
    assert await _call(executor, "get_fleet", fleet_id=str(OTHER)) == partner_fleet.OPT_IN_OFF
    assert calls == []
    assert opt_ins["audited"] == []


def test_the_partner_task_rides_in_the_queued_payload() -> None:
    payload = PARTNER.as_payload()
    assert payload["task_id"] == str(TASK)
    back = queued_partner_principal({PARTNER_PRINCIPAL_PAYLOAD_KEY: payload, PARTNER_TASK_PAYLOAD_KEY: True})
    assert back == PARTNER
    legacy = PartnerPrincipal(exposure_id=PARTNER.exposure_id, client_id=PARTNER.client_id).as_payload()
    assert "task_id" not in legacy
