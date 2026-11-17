"""Execution-time enforcement of ``tool_deny_list``.

The deny list used to be applied only when constructing the tool list shown
to the model — the ``CompositeToolExecutor`` kept executors registered for
denied tool names and ``run_agent_loop`` dispatches tool calls without
re-checking the offered list, so a model emitting a denied tool name
(hallucination, prompt injection, provider name mismatch) could still have
it executed.

These tests pin the defense-in-depth fix:

* The exact ``apply_tool_deny_list`` → ``ScopedToolExecutor`` pipeline used
  by the chat and assignment paths blocks denied tools at execution time
  (fail closed) while allowed tools keep executing — including parity for
  multi-tool-call batches and the reordered-name recovery.
* ``HumanInputRequestedError`` (the HITL pause signal) still propagates
  through the scoped wrapper so assignment HITL is unaffected.
* Source-level pins assert that ``agent_chat.run_agent_chat`` and
  ``assignment_engine.execute_assignment_run`` hand the agent loop a
  ``ScopedToolExecutor(composite, effective_tools)`` instead of the raw
  composite (the established pattern in this suite for orchestrator
  internals buried behind live DB/LLM dependencies — see
  ``test_agent_chat_smoke.py``).

The public embed path's enforcement is covered separately in
``test_public_chat_hardening.py``.
"""

from __future__ import annotations

import inspect

import app.services.agent_chat as agent_chat_mod
import app.services.assignment_engine as assignment_engine_mod
from app.services.builtin_tools import apply_tool_deny_list
from app.tools.base import (
    CallableToolExecutor,
    CompositeToolExecutor,
    HumanInputRequestedError,
    ScopedToolExecutor,
    ToolCall,
    ToolDefinition,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_tool(name: str) -> ToolDefinition:
    return ToolDefinition(
        name=name,
        description=f"fake tool {name}",
        input_schema={"type": "object", "properties": {}},
        source="test",
    )


def _build_denied_pipeline(
    tool_names: list[str], deny_list: list[str]
) -> tuple[CompositeToolExecutor, ScopedToolExecutor]:
    """Mirror the chat/assignment wiring: register everything on the
    composite, filter the advertised list with ``apply_tool_deny_list``,
    then scope execution to the filtered surface."""
    executor = CallableToolExecutor()

    async def _ok(**kwargs):
        return "ok"

    composite = CompositeToolExecutor()
    defs = []
    for name in tool_names:
        executor.register(name, _ok)
        defs.append(_make_tool(name))
    composite.add_tools(defs, executor)

    effective_tools = apply_tool_deny_list(composite.all_tools, deny_list)
    return composite, ScopedToolExecutor(composite, effective_tools)


# ---------------------------------------------------------------------------
# Denied tool requested by name -> blocked; allowed tool -> executes
# ---------------------------------------------------------------------------


async def test_denied_tool_blocked_at_execution_time():
    composite, scoped = _build_denied_pipeline(
        ["github_list_issues", "github_delete_repo"],
        deny_list=["github_delete_repo"],
    )

    # The composite alone would happily execute the denied tool.
    raw = await composite.execute_tool(
        ToolCall(id="t0", name="github_delete_repo", arguments={})
    )
    assert raw.is_error is False

    # The scoped executor (what the agent loop actually receives) refuses it.
    denied = await scoped.execute_tool(
        ToolCall(id="t1", name="github_delete_repo", arguments={})
    )
    assert denied.is_error is True
    assert "not available" in denied.content


async def test_allowed_tool_executes_through_scoped_executor():
    _, scoped = _build_denied_pipeline(
        ["github_list_issues", "github_delete_repo"],
        deny_list=["github_delete_repo"],
    )

    allowed = await scoped.execute_tool(
        ToolCall(id="t1", name="github_list_issues", arguments={})
    )
    assert allowed.is_error is False
    assert allowed.content == "ok"


async def test_multi_tool_call_parity():
    """A batch mixing allowed and denied calls must block exactly the
    denied ones — never the whole batch, never none of it."""
    _, scoped = _build_denied_pipeline(
        ["search_docs", "send_email", "delete_everything"],
        deny_list=["delete_everything", "send_email"],
    )

    calls = [
        ToolCall(id="t1", name="search_docs", arguments={}),
        ToolCall(id="t2", name="send_email", arguments={}),
        ToolCall(id="t3", name="delete_everything", arguments={}),
    ]
    results = [await scoped.execute_tool(c) for c in calls]

    assert results[0].is_error is False
    assert results[1].is_error is True
    assert results[2].is_error is True
    assert all(
        r.tool_call_id == c.id for r, c in zip(results, calls, strict=True)
    )


async def test_reordered_name_of_denied_tool_is_blocked():
    """The token-key recovery must not become a deny-list bypass."""
    _, scoped = _build_denied_pipeline(
        ["github_list_issues", "github_delete_repo"],
        deny_list=["github_delete_repo"],
    )

    denied = await scoped.execute_tool(
        ToolCall(id="t1", name="delete_github_repo", arguments={})
    )
    assert denied.is_error is True
    assert "not available" in denied.content


async def test_reordered_name_of_allowed_tool_still_recovers():
    _, scoped = _build_denied_pipeline(
        ["github_list_issues", "github_delete_repo"],
        deny_list=["github_delete_repo"],
    )

    allowed = await scoped.execute_tool(
        ToolCall(id="t1", name="list_github_issues", arguments={})
    )
    assert allowed.is_error is False


async def test_hitl_signal_propagates_through_scoped_executor():
    """``request_human_input`` raises ``HumanInputRequestedError`` to pause
    the assignment loop — the scoped wrapper must let it propagate."""
    executor = CallableToolExecutor()

    async def _hitl(**kwargs):
        raise HumanInputRequestedError("Need approval", "tc-1", [])

    executor.register("request_human_input", _hitl)
    composite = CompositeToolExecutor()
    composite.add_tools([_make_tool("request_human_input")], executor)
    scoped = ScopedToolExecutor(composite, composite.all_tools)

    try:
        await scoped.execute_tool(
            ToolCall(id="tc-1", name="request_human_input", arguments={})
        )
        raise AssertionError("HumanInputRequestedError did not propagate")
    except HumanInputRequestedError as exc:
        assert exc.question == "Need approval"


# ---------------------------------------------------------------------------
# Source-level pins — chat and assignment paths hand the loop a scoped
# executor built from the deny-list-filtered tool surface
# ---------------------------------------------------------------------------


class TestExecutionPathsUseScopedExecutor:
    AGENT_CHAT_SOURCE = inspect.getsource(agent_chat_mod.run_agent_chat)
    ASSIGNMENT_SOURCE = inspect.getsource(
        assignment_engine_mod.execute_assignment_run
    )

    def test_agent_chat_wraps_composite_in_scoped_executor(self):
        assert (
            "ScopedToolExecutor(composite, effective_tools)"
            in self.AGENT_CHAT_SOURCE
        )

    def test_agent_chat_never_passes_raw_composite_to_loop(self):
        assert (
            "executor=composite if effective_tools else None"
            not in self.AGENT_CHAT_SOURCE
        )

    def test_assignment_engine_wraps_composite_in_scoped_executor(self):
        assert (
            "ScopedToolExecutor(composite, effective_tools)"
            in self.ASSIGNMENT_SOURCE
        )

    def test_assignment_engine_never_passes_raw_composite_to_loop(self):
        assert (
            "composite if effective_tools else None"
            not in self.ASSIGNMENT_SOURCE
        )
