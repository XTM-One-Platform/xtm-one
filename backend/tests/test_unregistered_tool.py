"""Test that CompositeToolExecutor returns a helpful error when the LLM
calls a tool that is not registered, listing available tools so the LLM
can self-correct (fixes "No executor registered for tool: evaluate" bug)."""

import asyncio
import re

from app.tools.base import (
    CallableToolExecutor,
    CompositeToolExecutor,
    ScopedToolExecutor,
    ToolCall,
    ToolDefinition,
    _format_tool_not_found,
)

#: The aliases the tools of these tests declare, as their owners do.
_DECLARED_ALIASES = {"search_github_issues": ("list_github_issues",)}


def _make_executor(*tool_names: str) -> CompositeToolExecutor:
    """Build a CompositeToolExecutor with trivial no-op tools."""

    async def _noop(**_kwargs: object) -> str:
        return "ok"

    composite = CompositeToolExecutor()
    inner = CallableToolExecutor()
    definitions = []
    for name in tool_names:
        inner.register(name, _noop)
        definitions.append(
            ToolDefinition(
                name=name,
                description=f"A tool named {name}",
                input_schema={"type": "object", "properties": {}},
                aliases=_DECLARED_ALIASES.get(name, ()),
            )
        )
    composite.add_tools(definitions, inner)
    return composite


def test_unregistered_tool_error_includes_available_tools():
    composite = _make_executor("search_web", "send_email")
    call = ToolCall(id="toolu_01", name="evaluate", arguments={})
    result = asyncio.run(composite.execute_tool(call))
    assert result.is_error
    assert "evaluate" in result.content
    assert "Available tools" in result.content


def test_unregistered_tool_lists_available_tools():
    composite = _make_executor("search_web", "send_email")
    call = ToolCall(id="toolu_02", name="evaluate", arguments={})
    result = asyncio.run(composite.execute_tool(call))
    assert "search_web" in result.content
    assert "send_email" in result.content


def test_unregistered_tool_empty_executor():
    """When no tools are registered at all, the error still mentions '(none)'."""
    composite = CompositeToolExecutor()
    call = ToolCall(id="toolu_03", name="evaluate", arguments={})
    result = asyncio.run(composite.execute_tool(call))
    assert result.is_error
    assert "(none)" in result.content


def test_registered_tool_still_works():
    composite = _make_executor("search_web")
    call = ToolCall(id="toolu_04", name="search_web", arguments={})
    result = asyncio.run(composite.execute_tool(call))
    assert not result.is_error
    assert result.content == "ok"


def test_unregistered_tool_lists_sanitised_names():
    """The listing must mirror the sanitised names the LLM actually sees.

    Composite stores executors under the original tool name, but
    ``agent_loop`` sanitises names before passing them to the LLM
    (dots/slashes/etc. stripped). The error message must show the
    sanitised form so the LLM can self-correct using a callable name.
    """
    composite = _make_executor("github.repo.get", "jira/search-issues")
    call = ToolCall(id="toolu_06", name="evaluate", arguments={})
    result = asyncio.run(composite.execute_tool(call))
    assert result.is_error
    # Sanitised forms (dots/slashes → underscore) must appear in the
    # listing, and the raw originals must NOT.
    assert "github_repo_get" in result.content
    assert "jira_search-issues" in result.content
    assert "github.repo.get" not in result.content
    assert "jira/search-issues" not in result.content


def test_unregistered_tool_truncates_long_tool_lists():
    """Many bound tools should not bloat the error string sent back to the LLM.

    The test stays decoupled from the precise internal cap value: it
    registers more tools than any reasonable cap and asserts the response
    contains the truncation marker (``... and N more``) plus the
    alphabetically-first registered name, without importing the private
    constant from the implementation.
    """
    total_tools = 200
    tool_names = [f"tool_{i:03d}" for i in range(total_tools)]
    composite = _make_executor(*tool_names)
    call = ToolCall(id="toolu_05", name="evaluate", arguments={})
    result = asyncio.run(composite.execute_tool(call))

    assert result.is_error
    match = re.search(r"\.\.\. and (\d+) more", result.content)
    assert match is not None, (
        f"Expected truncation marker in error content: {result.content!r}"
    )
    truncated_count = int(match.group(1))
    assert 0 < truncated_count < total_tools
    assert sorted(tool_names)[0] in result.content
    assert sorted(tool_names)[-1] not in result.content


def test_unregistered_tool_suggests_close_match():
    """A typo'd tool name should produce a "Did you mean…" hint so the LLM
    can self-correct immediately instead of having to grep the full list."""
    composite = _make_executor("send_email", "search_web", "create_calendar_event")
    # ``creat_calendar_event`` is one char away from ``create_calendar_event``.
    call = ToolCall(id="toolu_07", name="creat_calendar_event", arguments={})
    result = asyncio.run(composite.execute_tool(call))

    assert result.is_error
    assert "Did you mean:" in result.content
    assert "create_calendar_event" in result.content
    # The closest match must come before the "Available tools" listing.
    did_you_mean_idx = result.content.find("Did you mean:")
    available_idx = result.content.find("Available tools:")
    assert 0 <= did_you_mean_idx < available_idx


def test_unregistered_tool_no_suggestion_when_no_close_match():
    """Totally unrelated names should NOT produce a noisy "Did you mean…"
    block — the LLM is better served by the plain available-tools list
    than by a confident-but-wrong suggestion."""
    composite = _make_executor("send_email", "search_web")
    # ``zzzzzzzz`` shares no substring with either tool — cutoff 0.65 must
    # reject any candidate so the hint is suppressed.
    call = ToolCall(id="toolu_08", name="zzzzzzzz", arguments={})
    result = asyncio.run(composite.execute_tool(call))

    assert result.is_error
    assert "Did you mean:" not in result.content
    assert "Available tools:" in result.content


def test_unregistered_tool_suggestion_uses_sanitised_names():
    """When the original tool name is dotted (``a.b.c``), the LLM sees the
    sanitised form (``a_b_c``). The "Did you mean…" suggestion must also
    use the sanitised name so the LLM can copy it verbatim into the next
    tool call."""
    composite = _make_executor("github.repo.get", "github.repo.list", "send_email")
    call = ToolCall(id="toolu_09", name="github_repo_gat", arguments={})
    result = asyncio.run(composite.execute_tool(call))

    assert result.is_error
    assert "Did you mean:" in result.content
    # Sanitised form must appear in the hint, not the dotted original.
    assert "github_repo_get" in result.content
    assert "github.repo.get" not in result.content


# ---------------------------------------------------------------------------
# Known alias resolution (list_github_issues → search_github_issues)
# ---------------------------------------------------------------------------

# The canonical tool declares the alias (``ToolDefinition.aliases``).
_ALIAS_NAME, _ALIAS_CANONICAL = "list_github_issues", "search_github_issues"


def _alias_canonical_definition() -> ToolDefinition:
    return ToolDefinition(
        name=_ALIAS_CANONICAL, description="d", input_schema={}, aliases=_DECLARED_ALIASES[_ALIAS_CANONICAL]
    )


def test_composite_resolves_known_alias_to_canonical():
    """CompositeToolExecutor must silently dispatch a known alias to the
    canonical tool so the call succeeds instead of returning an error."""
    composite = _make_executor(_ALIAS_CANONICAL, "another_tool")
    call = ToolCall(id="toolu_10", name=_ALIAS_NAME, arguments={})
    result = asyncio.run(composite.execute_tool(call))

    assert not result.is_error
    assert result.content == "ok"


def test_composite_known_alias_missing_canonical_falls_back_to_error():
    """If the canonical for a known alias is not registered, the composite
    must fall through to the normal error path instead of crashing."""
    # Register a completely different tool — the canonical is absent.
    composite = _make_executor("other_tool")
    call = ToolCall(id="toolu_11", name=_ALIAS_NAME, arguments={})
    result = asyncio.run(composite.execute_tool(call))

    assert result.is_error
    assert "Available tools:" in result.content


def test_scoped_executor_allows_known_alias_when_canonical_offered():
    """ScopedToolExecutor must pass a call for a known alias when the
    canonical tool is in the allowed set."""

    async def _noop(**_kwargs: object) -> str:
        return "ok"

    callable_exec = CallableToolExecutor()
    callable_exec.register(_ALIAS_CANONICAL, _noop)
    composite = CompositeToolExecutor()
    composite.add_tools(
        [_alias_canonical_definition()],
        callable_exec,
    )
    scoped = ScopedToolExecutor(composite, [_alias_canonical_definition()])

    call = ToolCall(id="toolu_13", name=_ALIAS_NAME, arguments={})
    result = asyncio.run(scoped.execute_tool(call))

    assert not result.is_error
    assert result.content == "ok"


def test_scoped_executor_blocks_known_alias_when_canonical_not_offered():
    """ScopedToolExecutor must still block a known alias call when the
    canonical tool is not in the allowed set — the alias should not be used
    to bypass the security boundary."""

    async def _noop(**_kwargs: object) -> str:
        return "ok"

    callable_exec = CallableToolExecutor()
    callable_exec.register(_ALIAS_CANONICAL, _noop)
    composite = CompositeToolExecutor()
    composite.add_tools([_alias_canonical_definition()], callable_exec)
    # The allowed set does NOT include the canonical → alias must also be blocked.
    scoped = ScopedToolExecutor(composite, [])

    call = ToolCall(id="toolu_14", name=_ALIAS_NAME, arguments={})
    result = asyncio.run(scoped.execute_tool(call))

    assert result.is_error
    assert "not available" in result.content


def test_scoped_executor_prefers_real_tool_over_alias_mapping():
    """A genuinely offered tool whose name collides with a registered alias
    must dispatch to ITSELF — the alias mapping only kicks in when the
    requested name is not an allowed tool in its own right."""

    async def _real(**_kwargs: object) -> str:
        return "real"

    async def _canonical(**_kwargs: object) -> str:
        return "canonical"

    callable_exec = CallableToolExecutor()
    callable_exec.register(_ALIAS_NAME, _real)
    callable_exec.register(_ALIAS_CANONICAL, _canonical)
    composite = CompositeToolExecutor()
    definitions = [
        ToolDefinition(name=_ALIAS_NAME, description="d", input_schema={}),
        ToolDefinition(name=_ALIAS_CANONICAL, description="d", input_schema={}),
    ]
    composite.add_tools(definitions, callable_exec)
    scoped = ScopedToolExecutor(composite, definitions)

    call = ToolCall(id="toolu_15", name=_ALIAS_NAME, arguments={})
    result = asyncio.run(scoped.execute_tool(call))

    assert not result.is_error
    assert result.content == "real"


def test_format_tool_not_found_prepends_canonical_for_known_alias():
    """When the hallucinated name is a known alias and the canonical is in
    the listing, the canonical must appear FIRST in the 'Did you mean' hint
    — even when difflib would rank string-closer candidates ahead of it."""
    # These names out-rank the canonical on pure edit distance (difflib
    # would suggest e.g. ``list_github_sub_issues`` first), reproducing the
    # production failure where the correct tool never appeared.
    names = sorted(
        [
            "list_github_sub_issues",
            "get_github_issue",
            "list_github_teams",
            _ALIAS_CANONICAL,
        ]
    )
    message = _format_tool_not_found("Nope.", _ALIAS_NAME, names, {_ALIAS_NAME: _ALIAS_CANONICAL})

    assert f"Did you mean: {_ALIAS_CANONICAL}" in message


def test_format_tool_not_found_promotes_canonical_already_in_suggestions():
    """When difflib already returns the canonical at a lower rank, it must
    be PROMOTED to the front of the hint (not left buried, not duplicated)."""
    # ``list_github_sub_issues`` scores higher than the canonical here, so
    # difflib yields the canonical at rank 2 — the promotion must move it to
    # rank 1 without duplicating it.
    names = sorted(["list_github_sub_issues", "get_github_issue", _ALIAS_CANONICAL])
    message = _format_tool_not_found("Nope.", _ALIAS_NAME, names, {_ALIAS_NAME: _ALIAS_CANONICAL})

    assert f"Did you mean: {_ALIAS_CANONICAL}" in message
    # No duplicate inside the suggestion block itself (the trailing
    # "Available tools" listing legitimately repeats every name).
    did_you_mean_block = message.split("Available tools:")[0]
    assert did_you_mean_block.count(_ALIAS_CANONICAL) == 1
