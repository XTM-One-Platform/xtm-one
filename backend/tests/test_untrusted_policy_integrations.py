"""The untrusted-content policy joins every prompt an integration answer can reach.

Integration providers wrap the third-party text they return, and the
declarative bundle executor wraps every answer, in ``<<UNTRUSTED-...>>``
blocks. The policy that reads those markers used to join the prompt only with
``web_fetch`` or ``document_vision``: an agent with bundle tools and neither
saw a quarantined answer with no matching rule in its system prompt.

Every prompt builder now decides through one predicate,
``content_safety.untrusted_policy_needed``. This file covers the predicate,
the assignment run's decision and its prompt, and the public widget's prompt;
the agent chat and the General Assistant are driven on PostgreSQL in
``test_untrusted_policy_integrations_postgres.py``.
"""

from __future__ import annotations

import uuid

import pytest

from app.api.v1.public_chat import _build_public_system_prompt
from app.models.agent import Agent
from app.models.assignment import Assignment
from app.services.assignment_helpers import (
    _build_system_message,
    assignment_untrusted_policy_needed,
)
from app.services.content_safety import (
    QUARANTINING_BUILTIN_TOOL_IDS,
    UNTRUSTED_CONTENT_POLICY,
    untrusted_policy_needed,
)
from app.services.prompt_guard import SAFETY_SYSTEM_PROMPT

_BUNDLE = str(uuid.UUID("00000000-0000-4000-8000-0000000b0d1e"))


@pytest.mark.parametrize(
    ("kwargs", "needed"),
    [
        ({}, False),
        ({"builtin_tool_ids": None}, False),
        ({"builtin_tool_ids": ["search_knowledge", "code_interpreter"]}, False),
        ({"builtin_tool_ids": ["web_fetch"]}, True),
        ({"builtin_tool_ids": ("document_vision",)}, True),
        ({"integration_tools": True}, True),
        ({"quarantined": True}, True),
        ({"quarantined": False, "builtin_tool_ids": [], "integration_tools": False}, False),
    ],
    ids=[
        "nothing",
        "no-builtin-list",
        "other-builtins",
        "web-fetch",
        "document-vision",
        "integration-tools",
        "quarantined-prompt",
        "all-off",
    ],
)
def test_the_predicate_is_true_exactly_when_a_block_can_reach_the_model(kwargs, needed):
    assert untrusted_policy_needed(**kwargs) is needed


def test_the_quarantining_builtins_are_the_ones_that_wrap_their_answers():
    # web_fetch wraps fetched pages, document_vision the words of an uploaded
    # HTML page, shared_state a value another writer chose.
    assert frozenset({"web_fetch", "document_vision", "shared_state"}) == QUARANTINING_BUILTIN_TOOL_IDS


def _agent(**fields) -> Agent:
    return Agent(name="Desk", persona="You are the desk agent.", **fields)


def _assignment(**fields) -> Assignment:
    return Assignment(
        agent_id=uuid.uuid4(),
        name="Nightly digest",
        trigger_type="schedule",
        trigger_config={},
        prompt="Summarise the day",
        **fields,
    )


@pytest.mark.parametrize(
    ("agent_fields", "assignment_fields", "untrusted_trigger", "needed"),
    [
        ({}, {}, False, False),
        ({"builtin_tool_ids": ["search_knowledge"]}, {}, False, False),
        ({"integration_bindings": {"bundle_acme": [_BUNDLE]}}, {}, False, True),
        ({"integration_bindings": {"bundle_acme": []}}, {}, False, False),
        # An assignment's own bindings replace the agent's: {} means none.
        ({"integration_bindings": {"bundle_acme": [_BUNDLE]}}, {"integration_bindings": {}}, False, False),
        ({}, {"integration_bindings": {"bundle_acme": [_BUNDLE]}}, False, True),
        ({"integration_template_bindings": ["opencti"]}, {"integration_bindings": {}}, False, True),
        ({"builtin_tool_ids": ["web_fetch"]}, {}, False, True),
        ({}, {}, True, True),
    ],
    ids=[
        "nothing",
        "other-builtin",
        "agent-bundle-binding",
        "agent-empty-binding",
        "assignment-clears-agent-bindings",
        "assignment-bundle-binding",
        "template-binding",
        "web-fetch",
        "untrusted-trigger",
    ],
)
def test_an_assignment_run_needs_the_policy_when_its_integration_tools_can_answer(
    agent_fields, assignment_fields, untrusted_trigger, needed
):
    agent = _agent(**agent_fields)
    assignment = _assignment(**assignment_fields)

    assert assignment_untrusted_policy_needed(agent, assignment, untrusted_trigger=untrusted_trigger) is needed


@pytest.mark.asyncio
async def test_an_assignment_agent_with_a_bundle_tool_and_no_web_fetch_gets_the_policy():
    agent = _agent(builtin_tool_ids=[], integration_bindings={"bundle_acme": [_BUNDLE]})
    assignment = _assignment()

    prompt = await _build_system_message(
        db=None,
        agent=agent,
        assignment=assignment,
        user_id=None,
        user_memory_text="",
        agent_memory_text="",
        element_bindings={},
        include_untrusted_policy=assignment_untrusted_policy_needed(agent, assignment, untrusted_trigger=False),
    )

    assert prompt.count(UNTRUSTED_CONTENT_POLICY) == 1


@pytest.mark.parametrize("integration_tools", [True, False], ids=["integration-tools", "no-tools"])
def test_the_public_widget_prompt_has_the_policy_with_integration_tools(integration_tools):
    prompt = _build_public_system_prompt(
        _agent(integration_bindings={"bundle_acme": [_BUNDLE]}),
        integration_lines=["- Acme (bundle_acme)"],
        integration_tools=integration_tools,
    )

    assert prompt.endswith(SAFETY_SYSTEM_PROMPT)
    if integration_tools:
        assert prompt.count(UNTRUSTED_CONTENT_POLICY) == 1
        assert prompt.endswith(UNTRUSTED_CONTENT_POLICY + "\n" + SAFETY_SYSTEM_PROMPT)
    else:
        assert UNTRUSTED_CONTENT_POLICY not in prompt


@pytest.mark.asyncio
@pytest.mark.parametrize("allow_tools", [True, False], ids=["tools-allowed", "tools-off"])
async def test_a_widget_turn_with_its_bundle_tools_gets_the_policy(monkeypatch, allow_tools):
    """The widget turn passes the gate its integration tools are built on."""
    from app.api.v1.public_chat import _run_public_agent_chat
    from tests.test_public_chat_hardening import _FakeDB, _make_public_agent, _patch_chat_pipeline

    recorder = _patch_chat_pipeline(monkeypatch, integration_tools=["acme_lookup"])
    agent = _make_public_agent(
        knowledge_base_ids=[],
        integration_bindings={"bundle_acme": [_BUNDLE]},
        channel_config={"web_embed": {"allow_tools": allow_tools}},
    )

    assert await _run_public_agent_chat(_FakeDB({}), agent, [], "hello") == "loop response"

    assert recorder["integration_tools_called"] is allow_tools
    prompt = recorder["system_prompt"]
    assert prompt.endswith(SAFETY_SYSTEM_PROMPT)
    if allow_tools:
        assert prompt.endswith(UNTRUSTED_CONTENT_POLICY + "\n" + SAFETY_SYSTEM_PROMPT)
    else:
        assert UNTRUSTED_CONTENT_POLICY not in prompt
