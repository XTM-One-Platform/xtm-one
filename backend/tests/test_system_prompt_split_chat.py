"""The chat prompt builder writes no per-call section before the clock line.

The Anthropic provider caches the system prompt up to its clock line
(``split_system_prompt``). ``run_agent_chat`` gathers the sections that change
from one call to the next in ``_call_parts`` and the clock, identity and memory
in ``_tail_parts``, and writes both after every stable section (#11971). This
guard reads the builder's syntax tree: a per-call value appended straight to
``system_parts`` lands in the cached prefix again and moves the cut on every
call. The end-to-end check, two turns with two call contexts sharing one
prefix, is ``test_system_prompt_split_chat_postgres.py``.
"""

from __future__ import annotations

import ast
import inspect
import textwrap

from app.services import agent_chat

#: Values that change from one call or run to the next: they go to the tail.
_PER_CALL_NAMES = frozenset(
    {
        "context",
        "_context_section",
        "_account_brief",
        "prior_conversation",
        "reason",
        "_silent_lead",
        "page_context",
        "presentation_studio",
        "handover_context",
        "_files_section",
        "_identity_parts",
        "_memory_block",
        "user_memory_text",
        "agent_memory_text",
    }
)


def _builder() -> ast.AST:
    return ast.parse(textwrap.dedent(inspect.getsource(agent_chat.run_agent_chat)))


def _writes_to(tree: ast.AST, target: str) -> list[ast.Call]:
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in {"append", "extend", "insert"}
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == target
    ]


def test_no_per_call_value_is_written_to_the_stable_parts():
    offenders = []
    for call in _writes_to(_builder(), "system_parts"):
        # The security anchor reads ``bool(context)`` as a flag; its text is
        # the same for every call of one kind, and it closes the prompt.
        if "build_security_policy" in ast.unparse(call):
            continue
        names = {n.id for arg in call.args for n in ast.walk(arg) if isinstance(n, ast.Name)}
        if names & _PER_CALL_NAMES:
            offenders.append((call.lineno, sorted(names & _PER_CALL_NAMES)))
    assert not offenders, offenders


def test_the_tail_and_the_call_sections_join_once_before_the_security_anchor():
    source = inspect.getsource(agent_chat.run_agent_chat)
    tail = source.index("system_parts.extend(_tail_parts)")
    calls = source.index("system_parts.extend(_call_parts)")
    anchor = source.index("build_security_policy(\n")
    assert source.count("system_parts.extend(_tail_parts)") == 1
    assert source.count("system_parts.extend(_call_parts)") == 1
    assert tail < calls < anchor
    # Every stable section is written before the tail joins.
    for stable in (
        "build_agent_context(agent)",
        "Connected integrations",
        "## Agent Delegation",
        "_coordination_prompt",
    ):
        assert source.index(stable) < tail, stable
