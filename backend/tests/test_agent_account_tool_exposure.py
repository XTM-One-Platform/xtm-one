"""The agent-account functions an agent carries reach every run that may use them (#9551).

The allow-list says yes, so the runtime does too: a chat, an assignment run
and the owner writing to their own agent account all carry the functions;
someone else writing to the account does not, and is told by name why,
never "unknown tool".
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

from app.services.agent_accounts import pipeline
from app.services.agent_accounts.pipeline import SenderDirectory, sender_is_owner
from app.services.agent_accounts.providers.microsoft_outlook import MicrosoftOutlookProvider
from app.services.agent_accounts.providers.microsoft_teams import MicrosoftTeamsProvider
from app.services.builtin_tools.agent_accounts import AGENT_ACCOUNT_TOOL_NAMES
from app.tools.base import (
    TOOL_NOT_AVAILABLE_SENTINEL,
    CallableToolExecutor,
    CompositeToolExecutor,
    ScopedToolExecutor,
    ToolCall,
    ToolDefinition,
)
from app.tools.tool_gating import build_tool_gate

_REASON = "this turn answers someone who is not the account's owner"


def _builder_tools() -> list[ToolDefinition]:
    names = [*sorted(AGENT_ACCOUNT_TOOL_NAMES), *(f"platform_tool_{i}" for i in range(20))]
    return [ToolDefinition(name=n, description=f"{n} desc", source="integration:platform") for n in names]


def _wire(entry_point: str) -> CompositeToolExecutor:
    """What ``agent_chat._add_platform_builder_tools`` wires for one entry point."""
    from app.services import agent_chat

    composite = CompositeToolExecutor()
    defs = _builder_tools()
    executor = CallableToolExecutor()
    for d in defs:
        executor.register(d.name, lambda **_: "ok")

    def _builder(*_args, **_kwargs):
        return list(defs), executor

    import app.services.builtin_tools as builtin_tools

    original = builtin_tools.build_agent_builder_tool, builtin_tools.build_evaluation_tools
    builtin_tools.build_agent_builder_tool = _builder
    builtin_tools.build_evaluation_tools = lambda *_a, **_k: ([], CallableToolExecutor())
    try:
        agent_chat._add_platform_builder_tools(
            composite,
            None,  # type: ignore[arg-type]
            str(uuid.uuid4()),
            ["agent_builder"],
            account_turn=entry_point == "account_turn_by_someone_else",
            caller_is_admin=True,
        )
    finally:
        builtin_tools.build_agent_builder_tool, builtin_tools.build_evaluation_tools = original
    return composite


@pytest.mark.parametrize("entry_point", ["chat", "assignment", "account_turn_by_owner"])
def test_every_allowed_agent_account_function_is_exposed(entry_point):
    composite = _wire(entry_point)
    assert set(composite.tool_names).issuperset(AGENT_ACCOUNT_TOOL_NAMES)
    assert composite.withheld == {}


def test_someone_else_writing_to_the_account_gets_them_withheld_by_name():
    composite = _wire("account_turn_by_someone_else")
    assert not AGENT_ACCOUNT_TOOL_NAMES & set(composite.tool_names)
    assert set(composite.withheld) == AGENT_ACCOUNT_TOOL_NAMES
    assert all("not its owner" in reason for reason in composite.withheld.values())


def _withholding_composite() -> CompositeToolExecutor:
    composite = CompositeToolExecutor()
    defs = _builder_tools()
    kept = [d for d in defs if d.name not in AGENT_ACCOUNT_TOOL_NAMES]
    executor = CallableToolExecutor()
    for d in kept:
        executor.register(d.name, lambda **_: "ok")
    composite.withhold(sorted(AGENT_ACCOUNT_TOOL_NAMES), _REASON)
    composite.add_tools(kept, executor)
    return composite


def test_load_tools_names_a_withheld_function_with_its_reason_never_unknown():
    composite = _withholding_composite()
    gate = build_tool_gate(composite.all_tools, enabled=True, min_tools=1, withheld=composite.withheld)
    assert gate is not None

    search = gate.describe_search(gate.search("agent_account"))
    assert "get_agent_account" in search and _REASON in search and "Withheld" in search

    reveal = gate.reveal([], ["get_agent_account", "no_such_tool"])
    assert reveal.withheld_tools == {"get_agent_account": _REASON}
    assert reveal.unknown_tools == ["no_such_tool"]
    text = gate.describe_reveal(reveal)
    assert "Unknown tool name(s) ignored: no_such_tool." in text
    assert "get_agent_account" in text and _REASON in text


@pytest.mark.parametrize("scoped", [False, True])
async def test_a_call_to_a_withheld_function_says_why(scoped):
    composite = _withholding_composite()
    executor = ScopedToolExecutor(composite, composite.all_tools) if scoped else composite
    result = await executor.execute_tool(ToolCall(id="c1", name="get_agent_account", arguments={}))
    assert result.is_error
    assert TOOL_NOT_AVAILABLE_SENTINEL in result.content
    assert "withheld from this turn" in result.content and _REASON in result.content


def test_a_name_registered_after_being_withheld_is_no_longer_withheld():
    composite = CompositeToolExecutor()
    composite.withhold(["get_agent_account"], _REASON)
    executor = CallableToolExecutor()
    executor.register("get_agent_account", lambda **_: "ok")
    composite.add_tools([ToolDefinition(name="get_agent_account", description="d", source="builtin:x")], executor)
    assert composite.withheld == {}


# ---------------------------------------------------------------------------
# Who is the owner: the provider's directory, on a provider that authenticates
# ---------------------------------------------------------------------------


def _event(**kwargs):
    base = {
        "from_account": False,
        "from_application": False,
        "from_agent_account": False,
        "sender_addresses": ("owner@example.com",),
    }
    return SimpleNamespace(**{**base, **kwargs})


@pytest.fixture
def directory(monkeypatch):
    found: dict[str, str] = {}

    async def _directory(_db, addresses):
        user = next((found[a] for a in addresses if a in found), None)
        return SenderDirectory(tuple(addresses), user is not None, frozenset(), user)

    monkeypatch.setattr(pipeline, "sender_directory", _directory)
    return found


async def test_the_owner_writing_on_teams_is_the_owner(directory):
    owner = uuid.uuid4()
    directory["owner@example.com"] = str(owner)
    account = SimpleNamespace(user_id=owner)
    assert await sender_is_owner(None, account, _event(), verified_sender=True)  # type: ignore[arg-type]


async def test_someone_else_or_an_unauthenticated_sender_is_never_the_owner(directory):
    owner = uuid.uuid4()
    directory["owner@example.com"] = str(owner)
    directory["alice@example.com"] = str(uuid.uuid4())
    account = SimpleNamespace(user_id=owner)
    assert not await sender_is_owner(
        None, account, _event(sender_addresses=("alice@example.com",)), verified_sender=True  # type: ignore[arg-type]
    )
    assert not await sender_is_owner(
        None, account, _event(sender_addresses=("nobody@example.com",)), verified_sender=True  # type: ignore[arg-type]
    )
    # An email's From is written by whoever sends it: a mailbox never vouches.
    assert not await sender_is_owner(None, account, _event(), verified_sender=False)  # type: ignore[arg-type]
    # The account itself or an application writing is never the owner.
    for flag in ("from_account", "from_application", "from_agent_account"):
        assert not await sender_is_owner(
            None, account, _event(**{flag: True}), verified_sender=True  # type: ignore[arg-type]
        )


def test_only_a_provider_that_authenticates_its_sender_vouches_for_the_owner():
    assert MicrosoftTeamsProvider.capabilities.verified_sender
    assert not MicrosoftOutlookProvider.capabilities.verified_sender
