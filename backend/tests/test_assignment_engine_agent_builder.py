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

import app.services.agent_chat as agent_chat
import app.services.assignment_engine as assignment_engine

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


def test_the_engine_wires_trace_suites_through_the_chat_turn_rule():
    """Every trace suite a run gets, carried or implied by ``agent_builder``,
    goes through ``_add_run_trace_tools``: the chat turn's gate
    (``agent_chat._turn_trace_tool_ids``) for the run's acting user, and the
    chat's wiring (``agent_chat._add_trace_tools``), which brings
    ``get_trace_payload`` along. A direct ``build_trace_tools`` call in the run
    body would wire them for a user who may not read the agent's chats.
    """
    source = inspect.getsource(assignment_engine.execute_assignment_run)
    assert "build_trace_tools" not in source
    assert "_add_run_trace_tools(composite, db, agent, agent_builtin_tool_ids, effective_user_id)" in source
    helper = inspect.getsource(assignment_engine._add_run_trace_tools)
    assert "_turn_trace_tool_ids" in helper and "_add_trace_tools" in helper
