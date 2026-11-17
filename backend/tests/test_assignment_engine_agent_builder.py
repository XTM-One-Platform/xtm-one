"""The assignment engine must load the ``agent_builder`` built-in tool group.

An agent with ``agent_builder`` in its ``builtin_tool_ids`` got the
platform-management tools (list_agents, get_agent, list_runs, ...) in chat
(``agent_chat.py``) but NOT in assignment execution — the engine's opt-in
builtin-tool dispatch simply never gained the block, so scheduled runs
reported "platform tools unavailable" even on an admin-owned agent.

These are source-contract tests (the repo's established pattern for the
engine's giant ``execute_assignment_run`` body): they pin the gate, the
acting identity, and — via a parity sweep — that no other chat-gated builtin
tool group silently goes missing from the assignment path again.
"""

import inspect
import re

import pytest

import app.services.agent_chat as agent_chat
import app.services.assignment_engine as assignment_engine
from app.services.builtin_tools.traces import TRACE_PAYLOAD_TOOL_NAME

_GATE_RE = re.compile(r'"([a-z_]+)" in agent_builtin_tool_ids')


def _gated_builtin_ids(source: str) -> set[str]:
    return set(_GATE_RE.findall(source))


def test_assignment_engine_gates_agent_builder_on_builtin_tool_ids():
    source = inspect.getsource(assignment_engine.execute_assignment_run)
    assert '"agent_builder" in agent_builtin_tool_ids' in source, (
        "execute_assignment_run must load the agent_builder tool group when "
        "the agent opts in via builtin_tool_ids (chat/assignment parity)"
    )
    assert "build_agent_builder_tool" in source


def test_assignment_engine_agent_builder_acts_as_effective_user():
    """The builder tools enforce per-user permissions, so the assignment path
    must pass the run's effective identity (run owner, falling back to the
    agent owner) — NOT the file-owner id, which is deliberately NULLed for
    company-managed runs and would demote every shared run to the anonymous
    read-only surface."""
    source = inspect.getsource(assignment_engine.execute_assignment_run)
    gate_body = source.split('"agent_builder" in agent_builtin_tool_ids', 1)[1]
    call = gate_body.split("composite.add_tools", 1)[0]
    assert "build_agent_builder_tool" in call
    assert "effective_user_id" in call
    assert "_run_file_owner_id" not in call


def test_assignment_engine_builtin_tool_parity_with_chat():
    """Every builtin tool group gated in agent_chat must also be gated in the
    assignment engine (this drift is exactly how agent_builder went missing).

    ``generate_image`` is the single known, deliberate chat-only group — if
    another id lands here, wire it into ``execute_assignment_run`` instead of
    growing the allowlist.
    """
    chat_ids = _gated_builtin_ids(inspect.getsource(agent_chat))
    engine_ids = _gated_builtin_ids(
        inspect.getsource(assignment_engine.execute_assignment_run)
    )
    known_chat_only = {"generate_image"}

    missing = chat_ids - known_chat_only - engine_ids
    assert not missing, (
        f"builtin tool group(s) {sorted(missing)} are gated in agent_chat but "
        "never loaded by the assignment engine — agents lose these tools in "
        "assignments"
    )


@pytest.mark.parametrize(
    "source",
    [
        pytest.param(
            inspect.getsource(assignment_engine.execute_assignment_run),
            id="assignment_engine",
        ),
    ],
)
def test_agent_builder_alias_admits_the_trace_payload_tool(source):
    """The ``agent_builder`` alias filters ``build_trace_tools``' definitions
    through a whitelist of *selectable* builtin ids. ``get_trace_payload`` is
    not one — it ships with whichever inspection tool is enabled — so a
    name whitelist drops it silently, leaving the alias with tools that report
    bounded previews and no way to fetch what was held back.

    Same drift class as the missing ``agent_builder`` block above: a filter
    that has to be told about a tool nobody selects. Any new
    non-selectable trace tool needs adding to both whitelists too. The chat
    side wires every trace suite through ``agent_chat._add_trace_tools``,
    tested on the tools it registers in ``test_general_assistant_tools``.
    """
    gate_body = source.split('"agent_builder" in agent_builtin_tool_ids', 1)[1]
    alias_block = gate_body.split("if _missing_trace_tools:", 1)[1].split(
        "composite.add_tools", 1
    )[0]
    # From the filter's own assignment onward: the ``import`` above it and the
    # comment explaining it both name the tool, and matching those would pass
    # while the comprehension quietly drops it.
    alias_filter = alias_block.split("trace_defs = [", 1)[1]
    assert "TRACE_PAYLOAD_TOOL_NAME" in alias_filter, (
        f"the agent_builder alias must let {TRACE_PAYLOAD_TOOL_NAME} past its "
        "_missing_trace_tools whitelist — it is not a selectable builtin id, so "
        "filtering by name alone strips the agent's only way to read a "
        "truncated or offloaded trace payload in full"
    )
