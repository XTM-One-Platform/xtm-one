"""Tool usage tracking counts the calls that ran, not the calls the budget cut.

``track_tool_usage`` bumps ``call_count`` / ``last_called_at`` on the entity
behind every tool call of a turn. A call the per-run tool-call budget cut
is answered in place and never reaches the entity, so it is not a use.
The loop tags that answer (``ChatMessage.not_run``); the text is never read.
"""

from __future__ import annotations

import contextlib
import uuid
from collections import Counter

import pytest

from app.llm.base import ChatMessage, ChatResponse
from app.services import tool_tracking
from app.services.builtin_tools import serialize_messages
from app.tools import agent_loop as al
from app.tools.base import CallableToolExecutor, CompositeToolExecutor, ToolCall, ToolDefinition
from app.tools.run_budget import RunBudget
from tests.test_agent_loop_run_budget import (
    _PADDING,
    _quiet_langfuse,  # noqa: F401 - pytest fixture
    _ScriptedLLM,
)

_CRM = str(uuid.uuid4())


def _composite() -> CompositeToolExecutor:
    executor = CallableToolExecutor()

    async def _lookup(**kwargs):
        return f"record {kwargs.get('q')}"

    executor.register("lookup", _lookup)
    composite = CompositeToolExecutor()
    composite.add_tools(
        [ToolDefinition(name="lookup", description="lookup", source="integration:crm", entity_id=_CRM)],
        executor,
    )
    return composite


class _Session:
    async def commit(self) -> None:
        pass

    async def rollback(self) -> None:
        pass


@pytest.fixture
def bumped(monkeypatch) -> Counter:
    """What ``track_tool_usage`` would add to each integration's ``call_count``."""
    counts: Counter = Counter()

    async def _bump(db, integration_counts, now):
        counts.update(integration_counts)

    @contextlib.asynccontextmanager
    async def _session():
        yield _Session()

    monkeypatch.setattr(tool_tracking, "_bump_integrations", _bump)
    monkeypatch.setattr("app.core.database.async_session", _session)
    return counts


def _call(call_id: str) -> ToolCall:
    return ToolCall(id=call_id, name="lookup", arguments={"q": call_id})


def _result(call_id: str, content: str, *, not_run: str | None = None) -> ChatMessage:
    return ChatMessage(role="tool", content=content, tool_call_id=call_id, name="lookup", not_run=not_run)


@pytest.mark.asyncio
async def test_a_call_the_budget_cut_is_not_a_use(bumped):
    notice = al._format_tool_budget_refusal(RunBudget(max_tool_calls=2))
    messages = [
        ChatMessage(role="user", content="go"),
        ChatMessage(role="assistant", content="", tool_calls=[_call("c1"), _call("c2"), _call("c3")]),
        _result("c3", notice, not_run="budget"),
        _result("c1", "record 1"),
        _result("c2", "record 2"),
    ]

    await tool_tracking.track_tool_usage(None, messages, _composite())

    assert bumped == Counter({_CRM: 2})


@pytest.mark.asyncio
async def test_a_cut_is_read_per_turn(bumped):
    """Call ids are unique within a turn only: an id the budget cut in one
    turn does not hide a call that ran under the same id in another."""
    notice = al._format_tool_budget_refusal(RunBudget(max_tool_calls=1))
    messages = [
        ChatMessage(role="assistant", content="", tool_calls=[_call("call_lookup_0")]),
        _result("call_lookup_0", "record 0"),
        ChatMessage(role="assistant", content="", tool_calls=[_call("call_lookup_0")]),
        _result("call_lookup_0", notice, not_run="budget"),
    ]

    await tool_tracking.track_tool_usage(None, messages, _composite())

    assert bumped == Counter({_CRM: 1})


@pytest.mark.asyncio
async def test_a_turn_without_a_cut_counts_every_call(bumped):
    messages = [
        ChatMessage(role="assistant", content="", tool_calls=[_call("c1"), _call("c2")]),
        _result("c1", "record 1"),
        _result("c2", "Error: lookup failed"),
    ]

    await tool_tracking.track_tool_usage(None, messages, _composite())

    assert bumped == Counter({_CRM: 2})


@pytest.mark.asyncio
async def test_a_tool_that_returns_the_notice_verbatim_is_a_use(bumped):
    """The cut is read off the loop's tag, never the text: a tool that ran and
    returned the notice word for word is counted like any other."""
    notice = al._format_tool_budget_refusal(RunBudget(max_tool_calls=2))
    messages = [
        ChatMessage(role="assistant", content="", tool_calls=[_call("c1")]),
        _result("c1", notice),
    ]

    await tool_tracking.track_tool_usage(None, messages, _composite())

    assert bumped == Counter({_CRM: 1})


@pytest.mark.asyncio
@pytest.mark.usefixtures("_quiet_langfuse")
async def test_the_calls_the_loop_cut_are_not_uses(bumped):
    """Through the real loop: the answers the cut writes are what tracking
    skips, and their tag survives ``serialize_messages``, the form a paused
    assignment run is resumed from."""
    composite = _composite()
    llm = _ScriptedLLM(
        [
            ChatResponse(
                content="",
                model="stub",
                stop_reason="tool_calls",
                tool_calls=[_call(f"c{i}") for i in range(1, 6)],
                usage={},
            ),
            ChatResponse(content="Two lookups ran." + _PADDING, model="stub", usage={}),
        ]
    )
    messages = [ChatMessage(role="user", content="go")]

    await al.run_agent_loop(
        llm=llm,
        messages=messages,
        tools=composite.all_tools,
        executor=composite,
        max_iterations=10,
        budget=RunBudget(max_tool_calls=2, strict_tool_calls=True),
    )
    await tool_tracking.track_tool_usage(None, messages, composite)

    assert bumped == Counter({_CRM: 2})
    persisted = serialize_messages(messages)
    assert [m["tool_call_id"] for m in persisted if m.get("not_run") == "budget"] == ["c3", "c4", "c5"]


def test_a_call_the_budget_cut_is_not_an_assignment_action():
    """``AssignmentRun.actions_taken`` lists what the run did: a call the budget
    cut in strict mode never ran, read per turn since a call id is only unique
    within its turn."""
    from app.services.assignment_engine import _actions_taken

    notice = al._format_tool_budget_refusal(RunBudget(max_tool_calls=1))
    messages = [
        ChatMessage(role="user", content="go"),
        ChatMessage(role="assistant", content="", tool_calls=[_call("call_lookup_0"), _call("call_lookup_1")]),
        _result("call_lookup_1", notice, not_run="budget"),
        _result("call_lookup_0", "record 0"),
        ChatMessage(role="assistant", content="", tool_calls=[_call("call_lookup_1")]),
        _result("call_lookup_1", "record 1"),
    ]

    actions = _actions_taken(messages, _composite())

    assert [(a["tool"], a["arguments"]["q"], a["source"]) for a in actions] == [
        ("lookup", "call_lookup_0", "integration:crm"),
        ("lookup", "call_lookup_1", "integration:crm"),
    ]
