"""The approval class of each Letsignit tool.

The Letsignit API key is company-wide: it reaches every employee, and a user
write changes what every email that person sends afterwards says. The three
writes declare ``ToolDefinition.always_ask``, so for an agent that asks each
call waits for a person whatever the whitelist, standing approvals or the
user's allow-all say; an agent nobody set to ask never pauses (approval is
opt-in); the two reads stay exempt like the other directory reads. Exercised with the real Letsignit definitions, on the gate
every surface installs (``build_approval_gate``) and through a real composite
executor. Nothing here reaches the network: ``safe_request`` is replaced.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.integrations.providers import letsignit
from app.services import hitl_whitelist
from app.services.hitl_whitelist_derived import provider_read_tools
from app.services.hitl_whitelist_platform import HAND_GATED
from app.services.integration_tools import _SafeIntegrationExecutor
from app.services.scheduled_work_item_runner import approval_refusal
from app.services.tool_classification import infer_tool_type
from app.tools.approval import (
    OUTCOME_APPROVALS_DISABLED,
    OUTCOME_INSTALL_DEFAULT_OFF,
    OUTCOME_PAUSED_TOOL_CHANGED,
    OUTCOME_STANDING_EXCEPTION,
    OUTCOME_USER_ALLOWS_ALL,
    OUTCOME_WHITELISTED,
    REACH_ANY,
    definition_always_asks,
    fingerprint,
    no_approval_channel_message,
    resolve_log_outcome,
)
from app.tools.base import CompositeToolExecutor, HumanInputRequestedError, ToolCall, ToolDefinition

READS = ("get_letsignit_signature", "get_letsignit_user")
WRITES = ("create_letsignit_user", "update_letsignit_user_variables", "delete_letsignit_user")

ARGUMENTS = {
    "get_letsignit_signature": {"email": "sam@example.com"},
    "get_letsignit_user": {"email": "sam@example.com"},
    "create_letsignit_user": {"email": "sam@example.com", "variables": {"job_title": "CEO"}},
    "update_letsignit_user_variables": {"email": "sam@example.com", "variables": {"phone_work": "+33 1 00"}},
    "delete_letsignit_user": {"email": "sam@example.com"},
}


def _composite() -> tuple[CompositeToolExecutor, dict[str, ToolDefinition]]:
    executor = _SafeIntegrationExecutor()
    definitions = letsignit.build_tools("lsk_test", executor)
    composite = CompositeToolExecutor()
    composite.add_tools(definitions, executor)
    return composite, {d.name: d for d in definitions}


def _call(name: str, call_id: str | None = None) -> ToolCall:
    return ToolCall(id=call_id or f"call-{name}", name=name, arguments=ARGUMENTS[name])


def _record_requests(monkeypatch) -> list[dict]:
    calls: list[dict] = []

    class _Resp:
        status_code = 200

        def json(self):
            return {"id": "u1", "email": "sam@example.com"}

    async def _fake(client, method, url, provider, action, **kwargs):
        calls.append({"method": method, "url": url})
        return _Resp(), None

    monkeypatch.setattr(letsignit, "safe_request", _fake)
    return calls


# ---------------------------------------------------------------------------
# The class of each tool, pinned
# ---------------------------------------------------------------------------


def test_every_letsignit_tool_has_its_approval_class():
    _, definitions = _composite()

    assert set(definitions) == set(READS) | set(WRITES)
    assert {name: d.always_ask for name, d in definitions.items()} == {
        "get_letsignit_signature": False,
        "get_letsignit_user": False,
        "create_letsignit_user": True,
        "update_letsignit_user_variables": True,
        "delete_letsignit_user": True,
    }
    # The whole-tool flag, not a per-call argument the agent could leave out.
    assert {d.always_ask_argument for d in definitions.values()} == {None}
    assert {name: infer_tool_type(name) for name in definitions} == {
        "get_letsignit_signature": "read",
        "get_letsignit_user": "read",
        "create_letsignit_user": "write",
        "update_letsignit_user_variables": "write",
        "delete_letsignit_user": "write",
    }


def test_the_reads_stay_exempt_like_the_other_directory_reads():
    """``get_letsignit_user`` is a directory read, exempt with its family (``HAND_GATED`` docstring)."""
    reads = provider_read_tools()

    assert reads["letsignit"] == frozenset(READS)
    assert {"get_entra_user", "list_entra_users"} <= reads["entra"]
    assert not (set(READS) | set(WRITES)) & set(HAND_GATED)


@pytest.mark.parametrize("name", WRITES)
def test_a_write_asks_whatever_its_arguments(name):
    _, definitions = _composite()

    assert definition_always_asks(definitions[name], _call(name)) is True
    assert definition_always_asks(definitions[name], ToolCall(id="c", name=name, arguments={})) is True


@pytest.mark.parametrize("name", READS)
def test_a_read_never_forces_a_question(name):
    _, definitions = _composite()

    assert definition_always_asks(definitions[name], _call(name)) is False


def test_an_unknown_tool_forces_no_question():
    assert definition_always_asks(None, _call("delete_letsignit_user")) is False


# ---------------------------------------------------------------------------
# The gate every surface installs
# ---------------------------------------------------------------------------


class _PlatformDb:
    """``platform_settings.tool_approvals_enabled`` as stored: ``True``, or ``None`` for no row."""

    def __init__(self, enabled):
        self.enabled = enabled

    async def scalar(self, _stmt):
        return self.enabled


@pytest.mark.parametrize("enabled", [True, None], ids=["default-row", "no-row"])
@pytest.mark.asyncio
async def test_an_agent_that_does_not_ask_runs_every_write_unattended(monkeypatch, enabled):
    """Approval is opt-in: the platform switch on, an agent not set to ask never pauses."""
    requests = _record_requests(monkeypatch)
    composite, _ = _composite()
    sink: dict[str, str] = {}
    composite.set_approval_gate(
        await hitl_whitelist.build_approval_gate(
            _PlatformDb(enabled), composite, user_id="u1", hitl_enabled=False, outcome_sink=sink
        )
    )

    assert composite.screen_for_approval([_call(name) for name in (*READS, *WRITES)]) == []
    for name in WRITES:
        result = await composite.execute_tool(_call(name))
        assert result.is_error is False
    assert len(requests) == len(WRITES)
    # The gate stamps nothing, so the trail names the agent's own switch.
    assert sink == {}
    assert (
        resolve_log_outcome(
            "update_letsignit_user_variables",
            "call-update_letsignit_user_variables",
            outcomes=sink,
            hitl_enabled=False,
            hitl_off_outcome=OUTCOME_INSTALL_DEFAULT_OFF,
        )
        == OUTCOME_INSTALL_DEFAULT_OFF
    )


@pytest.mark.asyncio
async def test_for_an_agent_that_asks_only_the_writes_pause_and_their_proposals_say_so(monkeypatch):
    composite, definitions = _composite()
    _clear_by_policy(monkeypatch, list(READS), standing=None)
    gate = await hitl_whitelist.build_approval_gate(object(), composite, user_id="u1", hitl_enabled=True)
    composite.set_approval_gate(gate)

    proposals = composite.screen_for_approval([_call(name) for name in (*READS, *WRITES)])

    assert [p["tool_call_id"] for p in proposals] == [f"call-{name}" for name in WRITES]
    assert [p.get("always_asks") for p in proposals] == [True, True, True]
    assert proposals[2]["arguments"] == {"email": "sam@example.com"}
    assert proposals[2]["input_schema"] == definitions["delete_letsignit_user"].input_schema


def _clear_by_policy(monkeypatch, names, *, standing: dict[str, str] | None):
    """*names* added to the whitelist by an administrator, and/or covered by a standing approval."""

    async def _whitelist(_db, *, reach=None):
        for name in names:
            reach.setdefault(name, set()).add(REACH_ANY)
        return frozenset(names)

    async def _nothing(*_a, **_k):
        return frozenset()

    async def _exceptions(_db, _uid, *, sources=None, entity_ids=None):
        for name in standing or {}:
            sources[name] = letsignit._SOURCE
        return dict(standing or {})

    async def _not_suppressed(_db, _uid, **_k):
        return None

    monkeypatch.setattr(hitl_whitelist, "resolve_gate_suppression", _not_suppressed)
    monkeypatch.setattr(hitl_whitelist, "resolve_whitelist", _whitelist if names else _nothing)
    monkeypatch.setattr(hitl_whitelist, "resolve_vetoed", _nothing)
    monkeypatch.setattr(hitl_whitelist, "resolve_exempt_sources", _nothing)
    monkeypatch.setattr("app.services.tool_approval_service.list_active_exceptions", _exceptions)


@pytest.mark.parametrize("name", WRITES)
@pytest.mark.asyncio
async def test_a_write_asks_although_an_administrator_whitelisted_it(monkeypatch, name):
    composite, _ = _composite()
    _clear_by_policy(monkeypatch, [*READS, name], standing=None)
    sink: dict[str, str] = {}
    gate = await hitl_whitelist.build_approval_gate(
        object(), composite, user_id="u1", hitl_enabled=True, outcome_sink=sink
    )

    assert gate(_call("get_letsignit_user")) is None
    with pytest.raises(HumanInputRequestedError):
        gate(_call(name))
    assert sink == {"call-get_letsignit_user": OUTCOME_WHITELISTED}


@pytest.mark.parametrize("name", WRITES)
@pytest.mark.asyncio
async def test_a_write_asks_although_the_user_always_allows_it(monkeypatch, name):
    composite, definitions = _composite()
    _clear_by_policy(
        monkeypatch,
        [],
        standing={n: fingerprint(definitions[n].input_schema) for n in (name, "get_letsignit_user")},
    )
    sink: dict[str, str] = {}
    gate = await hitl_whitelist.build_approval_gate(
        object(), composite, user_id="u1", hitl_enabled=True, outcome_sink=sink
    )

    # The same kind of standing approval clears a tool that does not always ask.
    assert gate(_call("get_letsignit_user")) is None
    with pytest.raises(HumanInputRequestedError):
        gate(_call(name))
    assert sink == {"call-get_letsignit_user": OUTCOME_STANDING_EXCEPTION}


@pytest.mark.asyncio
async def test_a_write_asks_while_the_user_allows_every_tool(monkeypatch):
    composite, _ = _composite()

    async def _allows_all(_db, _uid, **_k):
        return OUTCOME_USER_ALLOWS_ALL

    monkeypatch.setattr(hitl_whitelist, "resolve_gate_suppression", _allows_all)
    sink: dict[str, str] = {}
    gate = await hitl_whitelist.build_approval_gate(
        object(), composite, user_id="u1", hitl_enabled=True, outcome_sink=sink
    )

    assert gate(_call("get_letsignit_signature")) is None
    for name in WRITES:
        with pytest.raises(HumanInputRequestedError):
            gate(_call(name))
    assert sink == {"call-get_letsignit_signature": OUTCOME_USER_ALLOWS_ALL}


@pytest.mark.asyncio
async def test_the_write_a_person_approved_runs_while_the_tool_is_the_one_they_were_shown(monkeypatch):
    composite, definitions = _composite()
    _clear_by_policy(monkeypatch, [], standing=None)
    shape = fingerprint(definitions["delete_letsignit_user"].input_schema)
    gate = await hitl_whitelist.build_approval_gate(
        object(),
        composite,
        user_id="u1",
        hitl_enabled=True,
        approved_call_ids={"c1"},
        approved_shapes={"c1": shape},
    )

    assert gate(_call("delete_letsignit_user", "c1")) is None
    # Approving one call does not clear the next one.
    with pytest.raises(HumanInputRequestedError):
        gate(_call("delete_letsignit_user", "c2"))


@pytest.mark.asyncio
async def test_an_approved_write_asks_again_once_the_tool_changed(monkeypatch):
    composite, _ = _composite()
    _clear_by_policy(monkeypatch, [], standing=None)
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
        gate(_call("update_letsignit_user_variables", "c1"))
    assert sink == {"c1": OUTCOME_PAUSED_TOOL_CHANGED}


@pytest.mark.asyncio
async def test_a_paused_write_never_reaches_letsignit(monkeypatch):
    requests = _record_requests(monkeypatch)
    composite, _ = _composite()
    _clear_by_policy(monkeypatch, list(READS), standing=None)
    composite.set_approval_gate(
        await hitl_whitelist.build_approval_gate(object(), composite, user_id="u1", hitl_enabled=True)
    )

    for name in WRITES:
        with pytest.raises(HumanInputRequestedError):
            await composite.execute_tool(_call(name))
    assert requests == []

    result = await composite.execute_tool(_call("get_letsignit_user"))
    assert result.is_error is False
    assert requests == [{"method": "get", "url": f"{letsignit.LETSIGNIT_API}/users/sam@example.com"}]


# ---------------------------------------------------------------------------
# What a surface with nobody to approve says
# ---------------------------------------------------------------------------


def test_a_chat_that_cannot_ask_does_not_offer_a_lever_that_does_nothing():
    always = HumanInputRequestedError(question="q", tool_call_id="c1", conversation=[])
    always.proposals = [{"tool_name": "delete_letsignit_user", "always_asks": True}]
    mixed = HumanInputRequestedError(question="q", tool_call_id="c1", conversation=[])
    mixed.proposals = [
        {"tool_name": "delete_letsignit_user", "always_asks": True},
        {"tool_name": "send_slack_message"},
    ]

    text = no_approval_channel_message(always)
    assert text.startswith("I need approval before running: delete_letsignit_user.\n\n")
    assert "A person must approve every such call" in text
    assert "Tools page" not in text
    # A pause with a call some rule could clear keeps naming that rule.
    assert "Tools page" in no_approval_channel_message(mixed)


@pytest.mark.asyncio
async def test_a_scheduled_exact_write_is_refused_without_naming_a_setting(monkeypatch):
    composite, _ = _composite()
    _clear_by_policy(monkeypatch, list(READS), standing=None)

    async def _agent_asks(_db, _agent):
        return True

    monkeypatch.setattr(hitl_whitelist, "resolve_approve_tool_calls", _agent_asks)
    agent = SimpleNamespace(name="Signatures")
    toolset = SimpleNamespace(composite=composite)

    refusal = await approval_refusal(object(), agent, "u1", toolset, _call("update_letsignit_user_variables"))

    assert refusal == (
        "A person must approve every 'update_letsignit_user_variables' call before it runs, "
        "and a scheduled exact action has nobody to ask: nothing was sent. Ask for it in a "
        "conversation instead."
    )
    assert await approval_refusal(object(), agent, "u1", toolset, _call("get_letsignit_user")) is None


@pytest.mark.asyncio
async def test_a_scheduled_exact_write_of_an_agent_that_does_not_ask_is_not_refused(monkeypatch):
    composite, _ = _composite()

    async def _agent_does_not_ask(_db, _agent):
        return False

    monkeypatch.setattr(hitl_whitelist, "resolve_approve_tool_calls", _agent_does_not_ask)
    agent = SimpleNamespace(name="Signatures")
    toolset = SimpleNamespace(composite=composite)

    for name in WRITES:
        assert await approval_refusal(_PlatformDb(True), agent, "u1", toolset, _call(name)) is None


# ---------------------------------------------------------------------------
# The platform kill switch is absolute
# ---------------------------------------------------------------------------


def _platform_approvals_off(monkeypatch) -> None:
    """An administrator turned ``platform_settings.tool_approvals_enabled`` off."""

    async def _off(_db):
        return OUTCOME_APPROVALS_DISABLED

    async def _suppressed(_db, _uid, **_k):
        return OUTCOME_APPROVALS_DISABLED

    monkeypatch.setattr(hitl_whitelist, "resolve_platform_kill_switch", _off)
    monkeypatch.setattr(hitl_whitelist, "resolve_gate_suppression", _suppressed)


@pytest.mark.parametrize("hitl_enabled", [True, False], ids=["agent-gates", "agent-does-not-gate"])
@pytest.mark.asyncio
async def test_with_the_platform_switch_off_every_write_runs_unattended(monkeypatch, hitl_enabled):
    requests = _record_requests(monkeypatch)
    _platform_approvals_off(monkeypatch)
    composite, _ = _composite()
    sink: dict[str, str] = {}
    composite.set_approval_gate(
        await hitl_whitelist.build_approval_gate(
            object(), composite, user_id="u1", hitl_enabled=hitl_enabled, outcome_sink=sink
        )
    )

    assert composite.screen_for_approval([_call(name) for name in WRITES]) == []
    for name in WRITES:
        result = await composite.execute_tool(_call(name))
        assert result.is_error is False
    assert len(requests) == len(WRITES)
    assert sink == {f"call-{name}": OUTCOME_APPROVALS_DISABLED for name in WRITES}


@pytest.mark.asyncio
async def test_with_the_platform_switch_off_a_scheduled_exact_write_is_not_refused(monkeypatch):
    _platform_approvals_off(monkeypatch)
    composite, _ = _composite()

    async def _agent_gates(_db, _agent):
        return True

    monkeypatch.setattr(hitl_whitelist, "resolve_approve_tool_calls", _agent_gates)
    agent = SimpleNamespace(name="Signatures")
    toolset = SimpleNamespace(composite=composite)

    for name in WRITES:
        assert await approval_refusal(object(), agent, "u1", toolset, _call(name)) is None
