"""A call that sets its tool's ``always_ask_argument`` asks a person.

``act_on_page`` with ``submit`` skips the decision model's check before a
click that saves, creates or deletes, and the agent sets ``submit`` itself: no
whitelist entry, standing approval or user switch may clear such a call in
place of the person, for an agent that asks. Approval stays opt-in: an agent
nobody set to ask never pauses, and where an administrator turned approvals
off for the platform, nothing pauses. Exercised on the gate the chat surfaces
install and through a real composite executor, with the real ``act_on_page`` definition.
"""

from __future__ import annotations

import inspect

import pytest

from app.services import hitl_whitelist
from app.services.builtin_tools import page_actions as pa
from app.tools.approval import (
    OUTCOME_APPROVALS_DISABLED,
    OUTCOME_PAUSED_TOOL_CHANGED,
    OUTCOME_STANDING_EXCEPTION,
    OUTCOME_USER_ALLOWS_ALL,
    OUTCOME_WHITELISTED,
    REACH_PRODUCT,
    call_always_asks,
    fingerprint,
)
from app.tools.base import CompositeToolExecutor, HumanInputRequestedError, ToolCall


@pytest.mark.parametrize(
    ("arguments", "asks"),
    [
        ({"submit": True}, True),
        ({"submit": "true"}, True),
        ({"submit": 1}, True),
        ({"submit": False}, False),
        ({"submit": None}, False),
        ({"submit": ""}, False),
        ({}, False),
        (None, True),
        ("submit", True),
    ],
    ids=["true", "true-string", "one", "false", "null", "empty", "absent", "no-arguments", "not-a-mapping"],
)
def test_a_call_asks_when_the_tool_would_act_on_its_flag(arguments, asks):
    assert call_always_asks("submit", arguments) is asks


def test_a_tool_that_declares_no_argument_never_forces_a_question():
    assert call_always_asks(None, {"submit": True}) is False


def _composite() -> tuple[CompositeToolExecutor, object]:
    defs, executor = pa.build_page_action_tools(
        None, user_id="u1", conversation_id="c1", model="clef-flash", provider_id=None
    )
    composite = CompositeToolExecutor()
    composite.add_tools(defs, executor)
    return composite, defs[0]


def _submit(call_id="c1", submit=True) -> ToolCall:
    return ToolCall(
        id=call_id,
        name=pa.ACT_ON_PAGE_TOOL,
        arguments={"goal": "Delete the APT28 report", "submit": submit},
    )


def _clear_by_policy(monkeypatch, *, whitelisted: bool, standing_fp: str | None):
    """The tool whitelisted and/or covered by the user's standing approval."""

    async def _whitelist(_db, *, reach=None):
        if not whitelisted:
            return frozenset()
        reach.setdefault(pa.ACT_ON_PAGE_TOOL, set()).add(REACH_PRODUCT)
        return frozenset({pa.ACT_ON_PAGE_TOOL})

    async def _nothing(*_a, **_k):
        return frozenset()

    async def _exceptions(_db, _uid, *, sources=None, entity_ids=None):
        if standing_fp is None:
            return {}
        sources[pa.ACT_ON_PAGE_TOOL] = "builtin"
        return {pa.ACT_ON_PAGE_TOOL: standing_fp}

    async def _not_suppressed(_db, _uid, **_k):
        return None

    monkeypatch.setattr(hitl_whitelist, "resolve_gate_suppression", _not_suppressed)
    monkeypatch.setattr(hitl_whitelist, "resolve_whitelist", _whitelist)
    monkeypatch.setattr(hitl_whitelist, "resolve_vetoed", _nothing)
    monkeypatch.setattr(hitl_whitelist, "resolve_exempt_sources", _nothing)
    monkeypatch.setattr("app.services.tool_approval_service.list_active_exceptions", _exceptions)


@pytest.mark.parametrize(
    ("whitelisted", "standing", "cleared_as"),
    [(True, False, OUTCOME_WHITELISTED), (False, True, OUTCOME_STANDING_EXCEPTION)],
    ids=["whitelisted", "standing-approval"],
)
@pytest.mark.asyncio
async def test_submit_asks_although_the_tool_is_cleared_otherwise(monkeypatch, whitelisted, standing, cleared_as):
    composite, definition = _composite()
    _clear_by_policy(
        monkeypatch, whitelisted=whitelisted, standing_fp=fingerprint(definition.input_schema) if standing else None
    )
    sink: dict[str, str] = {}
    gate = await hitl_whitelist.build_approval_gate(
        object(), composite, user_id="u1", hitl_enabled=True, outcome_sink=sink
    )

    # Without submit the tool runs unattended, as the policy says.
    assert gate(_submit("c0", submit=False)) is None
    assert sink == {"c0": cleared_as}
    with pytest.raises(HumanInputRequestedError):
        gate(_submit("c1"))
    assert sink == {"c0": cleared_as}


@pytest.mark.asyncio
async def test_submit_asks_while_the_user_allows_every_tool(monkeypatch):
    composite, _ = _composite()

    async def _allows_all(_db, _uid, **_k):
        return OUTCOME_USER_ALLOWS_ALL

    monkeypatch.setattr(hitl_whitelist, "resolve_gate_suppression", _allows_all)
    sink: dict[str, str] = {}
    gate = await hitl_whitelist.build_approval_gate(
        object(), composite, user_id="u1", hitl_enabled=True, outcome_sink=sink
    )

    assert gate(_submit("c0", submit=False)) is None
    with pytest.raises(HumanInputRequestedError):
        gate(_submit("c1"))
    assert sink == {"c0": OUTCOME_USER_ALLOWS_ALL}


class _PlatformDb:
    """``platform_settings.tool_approvals_enabled`` as stored: ``True``, or ``None`` for no row."""

    def __init__(self, enabled):
        self.enabled = enabled

    async def scalar(self, _stmt):
        return self.enabled


@pytest.mark.parametrize("enabled", [True, None], ids=["default-row", "no-row"])
@pytest.mark.asyncio
async def test_submit_runs_for_an_agent_that_does_not_ask(enabled):
    """Approval is opt-in: the platform switch on, an agent not set to ask never pauses."""
    composite, _ = _composite()
    sink: dict[str, str] = {}
    gate = await hitl_whitelist.build_approval_gate(
        _PlatformDb(enabled), composite, user_id="u1", hitl_enabled=False, outcome_sink=sink
    )
    composite.set_approval_gate(gate)

    assert composite.screen_for_approval([_submit("c1")]) == []
    assert gate(_submit("c1")) is None
    # Nothing stamped: the trail names the agent's own switch after the turn.
    assert sink == {}


@pytest.mark.asyncio
async def test_submit_asks_for_an_agent_that_asks_and_the_proposal_shows_the_call(monkeypatch):
    composite, definition = _composite()
    _clear_by_policy(monkeypatch, whitelisted=True, standing_fp=None)
    gate = await hitl_whitelist.build_approval_gate(object(), composite, user_id="u1", hitl_enabled=True)
    composite.set_approval_gate(gate)

    proposals = composite.screen_for_approval([_submit("c0", submit=False), _submit("c1")])

    assert [p["tool_call_id"] for p in proposals] == ["c1"]
    assert proposals[0]["tool_name"] == pa.ACT_ON_PAGE_TOOL
    assert proposals[0]["arguments"] == {"goal": "Delete the APT28 report", "submit": True}
    assert proposals[0]["input_schema"] == definition.input_schema


@pytest.mark.asyncio
async def test_the_call_a_person_approved_runs_while_the_tool_is_the_one_they_were_shown(monkeypatch):
    composite, definition = _composite()
    _clear_by_policy(monkeypatch, whitelisted=True, standing_fp=None)
    gate = await hitl_whitelist.build_approval_gate(
        object(),
        composite,
        user_id="u1",
        hitl_enabled=True,
        approved_call_ids={"c1"},
        approved_shapes={"c1": fingerprint(definition.input_schema)},
    )

    assert gate(_submit("c1")) is None
    # Approving one call does not clear the next one.
    with pytest.raises(HumanInputRequestedError):
        gate(_submit("c2"))


@pytest.mark.asyncio
async def test_an_approved_call_asks_again_once_the_tool_changed(monkeypatch):
    composite, _ = _composite()
    _clear_by_policy(monkeypatch, whitelisted=True, standing_fp=None)
    sink: dict[str, str] = {}
    gate = await hitl_whitelist.build_approval_gate(
        object(),
        composite,
        user_id="u1",
        hitl_enabled=True,
        approved_call_ids={"c1"},
        approved_shapes={"c1": "another-shape"},
        outcome_sink=sink,
    )

    with pytest.raises(HumanInputRequestedError):
        gate(_submit("c1"))
    assert sink == {"c1": OUTCOME_PAUSED_TOOL_CHANGED}


def test_every_gate_a_run_installs_for_an_agent_that_asks_goes_through_the_always_ask_wrapper():
    """The assignment engine builds its own gate inline; it must not drift from chat's."""
    from app.services import assignment_engine

    source = inspect.getsource(assignment_engine.execute_assignment_run)
    assert source.count("composite.set_approval_gate(") == 5
    # The fifth wraps whichever gate was installed with the fleet's autonomy
    # gates (#9769 S1b): it adds pauses, it never replaces the inner gate.
    assert source.count("make_fleet_autonomy_gate(\n                                composite.approval_gate,") == 1
    assert source.count("_always_ask(make_recording_gate(") == 1
    assert source.count("_always_ask(_approval_gate)") == 1
    # An agent that does not ask is never wrapped: approval is opt-in.
    assert source.count("_always_ask(None)") == 0
    assert source.count("composite.set_approval_gate(make_ungated_gate())") == 1


def test_a_run_with_the_platform_switch_off_installs_the_recording_gate_alone():
    """The kill switch is read whether the agent gates or not, and wins over always-ask."""
    from app.services import assignment_engine

    source = inspect.getsource(assignment_engine.execute_assignment_run)
    assert "_suppressed_outcome = await resolve_platform_kill_switch(db)" in source
    killed = source.index("if _suppressed_outcome == OUTCOME_APPROVALS_DISABLED:")
    wrapped = source.index("_always_ask(make_recording_gate(")
    alone = source.index("make_recording_gate(_suppressed_outcome, _gate_outcomes)", killed)
    assert killed < alone < wrapped


@pytest.mark.parametrize("hitl_enabled", [True, False], ids=["agent-gates", "agent-does-not-gate"])
@pytest.mark.asyncio
async def test_submit_runs_unattended_while_the_platform_switch_is_off(monkeypatch, hitl_enabled):
    composite, _ = _composite()

    async def _off(_db):
        return OUTCOME_APPROVALS_DISABLED

    async def _suppressed(_db, _uid, **_k):
        return OUTCOME_APPROVALS_DISABLED

    monkeypatch.setattr(hitl_whitelist, "resolve_platform_kill_switch", _off)
    monkeypatch.setattr(hitl_whitelist, "resolve_gate_suppression", _suppressed)
    sink: dict[str, str] = {}
    gate = await hitl_whitelist.build_approval_gate(
        object(), composite, user_id="u1", hitl_enabled=hitl_enabled, outcome_sink=sink
    )
    composite.set_approval_gate(gate)

    assert composite.screen_for_approval([_submit("c1")]) == []
    assert gate(_submit("c1")) is None
    assert sink == {"c1": OUTCOME_APPROVALS_DISABLED}


@pytest.mark.asyncio
async def test_the_platform_switch_reads_platform_settings():
    class _Db:
        def __init__(self, value):
            self.value = value

        async def scalar(self, _query):
            return self.value

    assert await hitl_whitelist.resolve_platform_kill_switch(_Db(False)) == OUTCOME_APPROVALS_DISABLED
    assert await hitl_whitelist.resolve_platform_kill_switch(_Db(True)) is None
    # No row yet means the default: approvals are on.
    assert await hitl_whitelist.resolve_platform_kill_switch(_Db(None)) is None
    # Unreadable fails closed: gate normally.
    assert await hitl_whitelist.resolve_platform_kill_switch(object()) is None
