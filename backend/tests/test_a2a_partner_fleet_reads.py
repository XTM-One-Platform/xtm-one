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

"""#2202: a partner task reads its exposed agent's home fleet, behind the exposure's opt-in."""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from typing import Any

import pytest

from app.a2a import partner_fleet, partner_owner_reads
from app.a2a.partner_scope import PartnerPrincipal, partner_task_scope
from app.tools.base import CallableToolExecutor, ToolDefinition

HOME = uuid.UUID(int=42)
OTHER = uuid.UUID(int=43)
PARTNER = PartnerPrincipal(exposure_id=uuid.UUID(int=7), client_id="ctem-orchestrator")

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
def opted_in(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    state: dict[str, Any] = {"on": True, "home": HOME}

    async def _opted(_p: PartnerPrincipal) -> bool:
        return bool(state["on"])

    async def _home(_p: PartnerPrincipal) -> uuid.UUID | None:
        return state["home"] if state["on"] else None

    async def _no_owner_reads(_p: PartnerPrincipal) -> uuid.UUID | None:
        return None  # the #2638 opt-in stays off here: the #2202 scope alone

    monkeypatch.setattr(partner_fleet, "partner_opted_in", _opted)
    monkeypatch.setattr(partner_fleet, "partner_home_fleet_id", _home)
    monkeypatch.setattr(partner_owner_reads, "partner_owner_id", _no_owner_reads)
    return state


def test_only_the_three_reads_survive_and_no_write_is_callable() -> None:
    defs, executor, _ = _suite()
    defs, executor = partner_fleet.scope_fleet_tools_to_partner(defs, executor)
    assert sorted(d.name for d in defs) == sorted(partner_fleet.PARTNER_FLEET_TOOL_NAMES)
    assert sorted(executor.tool_names) == sorted(partner_fleet.PARTNER_FLEET_TOOL_NAMES)
    assert executor.handler("create_fleet") is None
    assert executor.handler("apply_fleet_spec") is None


@pytest.mark.asyncio
async def test_off_by_default_every_read_refuses(opted_in: dict[str, Any]) -> None:
    opted_in["on"] = False
    defs, executor, calls = _suite()
    _, executor = partner_fleet.scope_fleet_tools_to_partner(defs, executor)
    with partner_task_scope(principal=PARTNER):
        assert await _call(executor, "list_fleets") == partner_fleet.OPT_IN_OFF
        assert await _call(executor, "get_fleet", fleet_id=str(HOME)) == partner_fleet.OPT_IN_OFF
        assert await _call(executor, "get_fleet_health", fleet_id=str(HOME)) == partner_fleet.OPT_IN_OFF
    assert calls == []


@pytest.mark.asyncio
async def test_outside_a_partner_task_nothing_is_lent(opted_in: dict[str, Any]) -> None:
    defs, executor, calls = _suite()
    _, executor = partner_fleet.scope_fleet_tools_to_partner(defs, executor)
    assert await _call(executor, "get_fleet", fleet_id=str(HOME)) == partner_fleet.OPT_IN_OFF
    assert calls == []


@pytest.mark.asyncio
async def test_opted_in_reads_the_home_fleet_and_the_build(opted_in: dict[str, Any]) -> None:
    defs, executor, calls = _suite()
    _, executor = partner_fleet.scope_fleet_tools_to_partner(defs, executor)
    with partner_task_scope(principal=PARTNER):
        listing = await _call(executor, "list_fleets")
        one = await _call(executor, "get_fleet", fleet_id=str(HOME))
        await _call(executor, "get_fleet_health", fleet_id=str(HOME), apply_safe_fixes=True)
    assert "1 fleet readable from this task" in listing
    assert "Running build:" in listing
    assert str(HOME) in one
    # The health read never applies a fix, whatever the partner asks.
    assert ("get_fleet_health", {"fleet_id": str(HOME), "apply_safe_fixes": False}) in calls


@pytest.mark.asyncio
async def test_another_fleet_answers_like_an_unknown_one(opted_in: dict[str, Any]) -> None:
    defs, executor, calls = _suite()
    _, executor = partner_fleet.scope_fleet_tools_to_partner(defs, executor)
    with partner_task_scope(principal=PARTNER):
        answer = await _call(executor, "get_fleet", fleet_id=str(OTHER))
        health = await _call(executor, "get_fleet_health", fleet_id="not-a-uuid")
    assert answer == f"Fleet `{OTHER}` not found or you don't have access."
    assert "not found" in health
    assert calls == []


@pytest.mark.asyncio
async def test_an_agent_in_no_fleet_has_nothing_to_read(opted_in: dict[str, Any]) -> None:
    opted_in["home"] = None
    defs, executor, calls = _suite()
    _, executor = partner_fleet.scope_fleet_tools_to_partner(defs, executor)
    with partner_task_scope(principal=PARTNER):
        assert await _call(executor, "list_fleets") == partner_fleet.NO_HOME_FLEET
    assert calls == []
