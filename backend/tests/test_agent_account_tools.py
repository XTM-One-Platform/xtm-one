"""The agent-account tools: who is offered which, and what a rule edit does.

The agent builder carries every one of them; an agent configuring the accounts
it operates gets all but creating one, and only when it operates one of the
person's; an account's own turn - the person writing is not the owner - gets
none of them, read or write; the read half is open to a read-only XTM One MCP
key. A rule edit changes one rule and keeps the others in order.
"""

from __future__ import annotations

import uuid

import pytest

from app.a2a.partner_scope import partner_task_scope
from app.api.v1.mcp_xtm import XTM_MCP_READ_TOOLS
from app.services.agent_accounts.conversation import account_brief
from app.services.agent_chat import _add_own_account_tools, _add_platform_builder_tools
from app.services.builtin_tools import build_agent_builder_tool
from app.services.builtin_tools.agent_accounts import (
    AGENT_ACCOUNT_TOOL_NAMES,
    OWN_ACCOUNT_TOOL_NAMES,
    build_agent_account_tools,
    edited_rules,
)
from app.services.tool_classification import infer_tool_type
from app.tools.base import CompositeToolExecutor


def _names(composite: CompositeToolExecutor) -> set[str]:
    return {d.name for d in composite.all_tools}


def test_the_agent_builder_carries_every_agent_account_tool():
    definitions, executor = build_agent_builder_tool(None, "")
    names = [d.name for d in definitions]
    assert set(names) >= AGENT_ACCOUNT_TOOL_NAMES
    assert set(executor.tool_names) >= AGENT_ACCOUNT_TOOL_NAMES
    assert len(names) == len(set(names)), "a tool name is declared twice"


def test_an_agent_configuring_itself_gets_every_tool_but_creating_an_account():
    definitions, handlers = build_agent_account_tools(str(uuid.uuid4()), agent_id=str(uuid.uuid4()))
    names = {d.name for d in definitions}
    assert names == set(handlers) == OWN_ACCOUNT_TOOL_NAMES
    assert "create_agent_account" not in names and "list_agent_account_options" not in names
    assert {d.source for d in definitions} == {"builtin:agent_accounts"}


def test_an_account_turn_is_offered_no_agent_account_tool():
    """Someone writes to the account: the turn runs as its owner, whose rules they must not read nor change."""
    user_id = str(uuid.uuid4())
    answering = CompositeToolExecutor()
    _add_platform_builder_tools(answering, None, user_id, ["agent_builder"], account_turn=True)
    assert not _names(answering) & AGENT_ACCOUNT_TOOL_NAMES
    assert not set(answering.tool_names) & AGENT_ACCOUNT_TOOL_NAMES
    assert "list_agents" in _names(answering)

    chatting = CompositeToolExecutor()
    _add_platform_builder_tools(chatting, None, user_id, ["agent_builder"])
    assert _names(chatting) >= AGENT_ACCOUNT_TOOL_NAMES


class _Agent:
    def __init__(self) -> None:
        self.id = uuid.uuid4()


@pytest.mark.parametrize(
    ("operates", "builtin_ids", "expected"),
    [
        (True, [], OWN_ACCOUNT_TOOL_NAMES),
        (False, [], frozenset()),
        # The builder already holds every account of the person's.
        (True, ["agent_builder"], frozenset()),
    ],
    ids=["operates-one", "operates-none", "carries-the-builder"],
)
async def test_the_own_account_tools_follow_the_accounts_the_agent_operates(monkeypatch, operates, builtin_ids, expected):
    asked: list[tuple] = []

    async def _operated_by(db, agent_id, user_id):
        asked.append((agent_id, user_id))
        return operates

    monkeypatch.setattr("app.services.agent_accounts.management.operated_by", _operated_by)
    agent, user_id = _Agent(), uuid.uuid4()
    composite = CompositeToolExecutor()
    await _add_own_account_tools(composite, None, str(user_id), agent, builtin_ids)
    assert _names(composite) == set(expected)
    if "agent_builder" not in builtin_ids:
        assert asked == [(agent.id, user_id)]


async def test_an_external_caller_gets_no_own_account_tool(monkeypatch):
    async def _operated_by(db, agent_id, user_id):
        raise AssertionError("an external caller acts as no account")

    monkeypatch.setattr("app.services.agent_accounts.management.operated_by", _operated_by)
    composite = CompositeToolExecutor()
    with partner_task_scope():
        await _add_own_account_tools(composite, None, str(uuid.uuid4()), _Agent(), [])
    assert _names(composite) == set()


def test_the_read_half_is_open_to_a_read_only_key_and_nothing_else():
    reads = {name for name in AGENT_ACCOUNT_TOOL_NAMES if infer_tool_type(name) == "read"}
    assert reads == {
        "list_agent_accounts",
        "get_agent_account",
        "list_agent_account_options",
        "preview_agent_account_decision",
        "list_agent_account_decisions",
    }
    assert reads <= XTM_MCP_READ_TOOLS
    assert not (AGENT_ACCOUNT_TOOL_NAMES - reads) & XTM_MCP_READ_TOOLS


async def test_an_anonymous_caller_manages_no_agent_account():
    _definitions, handlers = build_agent_account_tools("anonymous")
    arguments = {
        "list_agent_accounts": {},
        "get_agent_account": {"account_id": "A"},
        "list_agent_account_options": {},
        "create_agent_account": {"name": "A", "provider": "microsoft_teams", "integration_id": "i", "agent_id": "a"},
        "update_agent_account": {"account_id": "A", "enabled": False},
        "edit_agent_account_rule": {"account_id": "A", "operation": "remove", "rule_id": "direct"},
        "delete_agent_account": {"account_id": "A"},
        "preview_agent_account_decision": {"account_id": "A"},
        "list_agent_account_decisions": {"account_id": "A"},
    }
    assert set(arguments) == AGENT_ACCOUNT_TOOL_NAMES
    for name, args in arguments.items():
        assert "not available for anonymous users" in await handlers[name](**args), name


_RULES = [{"id": "direct", "name": "Direct"}, {"id": "mentions", "name": "Mentions"}, {"id": "vip", "name": "VIP"}]


def _ids(rules):
    return [r["id"] for r in rules]


def test_a_rule_is_added_where_asked_or_last():
    first, err = edited_rules(_RULES, "add", rule_id="", rule={"id": "mute", "action": "ignore"}, position=1)
    assert err is None and _ids(first) == ["mute", "direct", "mentions", "vip"]
    last, _err = edited_rules(_RULES, "add", rule_id="", rule={"name": "New"}, position=None)
    assert [r.get("name") for r in last] == ["Direct", "Mentions", "VIP", "New"]
    beyond, _err = edited_rules(_RULES, "add", rule_id="", rule={"name": "New"}, position=99)
    assert beyond[-1] == {"name": "New"}
    assert _ids(_RULES) == ["direct", "mentions", "vip"], "the stored rules are never edited in place"


def test_an_update_merges_over_the_rule_and_keeps_its_id_and_place():
    rules, err = edited_rules(
        _RULES, "update", rule_id="mentions", rule={"id": "renamed", "reply": "decide", "name": "M"}, position=None
    )
    assert err is None and _ids(rules) == ["direct", "mentions", "vip"]
    assert rules[1] == {"id": "mentions", "name": "M", "reply": "decide"}


def test_a_rule_is_moved_or_removed():
    moved, _err = edited_rules(_RULES, "move", rule_id="vip", rule=None, position=1)
    assert _ids(moved) == ["vip", "direct", "mentions"]
    removed, _err = edited_rules(_RULES, "remove", rule_id="direct", rule=None, position=None)
    assert _ids(removed) == ["mentions", "vip"]


@pytest.mark.parametrize(
    ("operation", "rule_id", "rule", "position", "says"),
    [
        ("update", "nope", {"name": "x"}, None, "No rule `nope`"),
        ("remove", "", None, None, "rule_id is required"),
        ("move", "vip", None, None, "position is required"),
        ("add", "", None, None, "rule is required"),
        ("update", "vip", None, None, "rule is required"),
    ],
    ids=["unknown-rule", "no-rule-id", "move-without-position", "add-without-rule", "update-without-fields"],
)
def test_a_rule_edit_that_cannot_apply_says_why(operation, rule_id, rule, position, says):
    rules, err = edited_rules(_RULES, operation, rule_id=rule_id, rule=rule, position=position)
    assert rules is None and says in err


def test_the_brief_says_the_rule_in_effect_and_who_labels_the_reply():
    labelled = account_brief(
        mode="on_behalf",
        platform_label="Microsoft Teams",
        account_name="Sam",
        conversation_name=None,
        instructions="",
        rule_name="Leadership groups, full access",
        label="Sent by an AI agent on behalf of Sam",
    )
    assert 'the platform adds the line "Sent by an AI agent on behalf of Sam" to your reply' in labelled
    assert "see the message as theirs" not in labelled
    assert 'You answer under the account\'s rule "Leadership groups, full access".' in labelled

    unlabelled = account_brief(
        mode="on_behalf", platform_label="Microsoft Teams", account_name="Sam", conversation_name=None, instructions=""
    )
    assert "the people you talk to see the message as theirs" in unlabelled and "rule" not in unlabelled
