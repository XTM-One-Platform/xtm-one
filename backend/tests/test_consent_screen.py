"""Consent screens on the composite executor (spec 032 FR-022).

A consent no setting waives: a screened call pauses with the gate off, its
proposal says so, an approval is spent by the call it was given for, a tool
learns whether the person approved the call it runs, "always" saves no
standing approval, and a surface that cannot ask says how to do it instead.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from app.services.builtin_tools.workspace_filing import _needs_consent
from app.tools.approval import no_approval_channel_message
from app.tools.base import (
    CallableToolExecutor,
    CompositeToolExecutor,
    HumanInputRequestedError,
    ToolCall,
    ToolDefinition,
    consent_approved,
    consent_screened,
)


def _composite(seen: list) -> CompositeToolExecutor:
    async def _act(target: str = "") -> str:
        seen.append((target, consent_screened(), consent_approved()))
        return "done"

    executor = CallableToolExecutor()
    executor.register("act", _act)
    composite = CompositeToolExecutor()
    composite.add_tools([ToolDefinition(name="act", description="", input_schema={"type": "object"})], executor)
    composite.require_consent({"act"}, lambda call: call.arguments.get("target") == "shared")
    return composite


def _call(target: str, call_id: str = "c1") -> ToolCall:
    return ToolCall(id=call_id, name="act", arguments={"target": target})


@pytest.mark.asyncio
async def test_a_screened_call_pauses_with_no_gate_and_says_it_needs_consent():
    seen: list = []
    composite = _composite(seen)
    (proposal,) = composite.screen_for_approval([_call("shared"), _call("mine", "c2")])
    assert (proposal["tool_call_id"], proposal["requires_consent"]) == ("c1", True)
    with pytest.raises(HumanInputRequestedError):
        await composite.execute_tool(_call("shared"))
    assert (await composite.execute_tool(_call("mine", "c2"))).content == "done"
    assert seen == [("mine", True, False)]


@pytest.mark.asyncio
async def test_an_approval_runs_once_and_says_so_to_the_tool():
    seen: list = []
    composite = _composite(seen)
    composite.bypass_approval_for({"c1"})
    assert (await composite.execute_tool(_call("shared"))).content == "done"
    assert seen == [("shared", True, True)]
    assert [p["tool_call_id"] for p in composite.screen_for_approval([_call("shared")])] == ["c1"]
    with pytest.raises(HumanInputRequestedError):
        await composite.execute_tool(_call("shared"))


@pytest.mark.asyncio
async def test_an_unscreened_executor_reports_no_consent():
    seen: list = []

    async def _act(target: str = "") -> str:
        seen.append((consent_screened(), consent_approved()))
        return "done"

    executor = CallableToolExecutor()
    executor.register("act", _act)
    composite = CompositeToolExecutor()
    composite.add_tools([ToolDefinition(name="act", description="", input_schema={"type": "object"})], executor)
    await composite.execute_tool(_call("shared"))
    assert seen == [(False, False)]


@pytest.mark.asyncio
async def test_always_on_a_consent_saves_no_standing_approval():
    from app.services.assignment_engine import _apply_approval_decisions

    seen: list = []
    composite = _composite(seen)
    composite.bypass_approval_for({"c1"})
    always = AsyncMock()
    proposal = {"tool_call_id": "c1", "tool_name": "act", "arguments": {"target": "shared"}, "requires_consent": True}
    await _apply_approval_decisions(
        executor=composite,
        messages=[],
        proposals=[proposal],
        decisions={"c1": {"decision": "approve_always"}},
        on_approve_always=always,
    )
    always.assert_not_awaited()
    assert seen == [("shared", True, True)]


def test_a_surface_that_cannot_ask_points_to_the_person_doing_it():
    exc = HumanInputRequestedError(
        question="Approval required to run act",
        tool_call_id="c1",
        conversation=[],
        proposals=[{"tool_call_id": "c1", "tool_name": "act", "requires_consent": True}],
    )
    message = no_approval_channel_message(exc)
    assert "Tools page" not in message
    assert message.startswith("I need your own approval for this")


@pytest.mark.parametrize(
    ("target", "asks"),
    [("", False), ("   ", False), ("mine", False), ("Team", True), ("00000000-0000-0000-0000-000000000001", True)],
)
def test_the_filing_screen_asks_for_any_named_target_but_a_private_one(target, asks):
    call = ToolCall(id="c", name="file_this_conversation", arguments={"workspace_id": target})
    assert _needs_consent(frozenset({"mine"}), call) is asks
