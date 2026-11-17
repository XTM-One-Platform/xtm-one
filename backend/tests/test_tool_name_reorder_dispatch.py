"""CompositeToolExecutor recovers from *reordered* tool names.

LLMs frequently call a tool by a token-reordered variant of its real name —
e.g. ``jira_search_issues`` for the registered ``search_jira_issues``. The
dispatcher now matches on an order-independent token key and auto-dispatches
when exactly one registered tool matches, so the call succeeds instead of
returning ``No executor registered for tool`` and looping. The match is
provider-agnostic and never routes ambiguously.
"""

import asyncio

from app.tools.base import (
    CallableToolExecutor,
    CompositeToolExecutor,
    ToolCall,
    ToolDefinition,
    ToolNameResolver,
    build_llm_tool_names,
    declared_aliases,
    normalize_tool_key,
    visible_aliases,
)

#: The alias ``search_github_issues`` declares (``ToolDefinition.aliases``).
_DECLARED = {"list_github_issues": "search_github_issues"}


def test_tool_name_resolver_is_the_single_definition_of_accepted_call_forms():
    """Exact, unambiguous reordered tokens, known alias — in that order; an
    ambiguous token key resolves to nothing.  Shared by the executors and the
    progressive-disclosure gate so they can never disagree on a call."""
    resolver = ToolNameResolver(["search_jira_issues"])
    resolver.add("search_github_issues", ("list_github_issues",))
    assert resolver.resolve("search_jira_issues") == ("search_jira_issues", "exact")
    assert resolver.resolve("jira_search_issues") == ("search_jira_issues", "tokens")
    assert resolver.resolve("ISSUES_JIRA_SEARCH") == ("search_jira_issues", "tokens")
    assert resolver.resolve("list_github_issues") == ("search_github_issues", "alias")
    assert resolver.resolve("nope") is None
    assert resolver.canonical("jira_search_issues") == "search_jira_issues"
    assert resolver.canonical("nope") is None
    assert "search_jira_issues" in resolver and "jira_search_issues" not in resolver
    assert len(resolver) == 2

    # Two registered names sharing a token key: neither is dispatched.
    resolver.add("x_y_z")
    resolver.add("z_y_x")
    assert resolver.token_matches("y_x_z") == ["x_y_z", "z_y_x"]
    assert resolver.resolve("y_x_z") is None
    assert resolver.resolve("x_y_z") == ("x_y_z", "exact")

    # A real tool registered under an alias's name wins over the alias.
    resolver.add("list_github_issues")
    assert resolver.resolve("list_github_issues") == ("list_github_issues", "exact")

    # Re-adding a name never double-indexes it into a false ambiguity.
    resolver.add("search_jira_issues")
    assert resolver.token_matches("search_jira_issues") == ["search_jira_issues"]
    assert resolver.resolve("") is None


def test_visible_aliases_follow_the_canonical_original_through_a_collision():
    """The alias table is keyed by ORIGINAL names; a resolver over the
    SANITISED names must be given the table translated through the
    original -> visible assignment, or a collision routes the alias to the
    wrong tool.  Originals ``search.github.issues`` and ``search_github_issues``
    become ``search_github_issues`` / ``search_github_issues_2``: the alias
    ``list_github_issues`` means the SECOND."""
    pairs = dict(build_llm_tool_names(["search.github.issues", "search_github_issues"]))
    assert pairs == {
        "search.github.issues": "search_github_issues",
        "search_github_issues": "search_github_issues_2",
    }
    aliases = visible_aliases(pairs, _DECLARED)
    assert aliases == {"list_github_issues": "search_github_issues_2"}

    # Applying the original-name table verbatim to the visible names is the
    # bug: it lands on the first tool's visible name.
    naive = ToolNameResolver(pairs.values(), aliases=_DECLARED)
    assert naive.canonical("list_github_issues") == "search_github_issues"
    translated = ToolNameResolver(pairs.values(), aliases=aliases)
    assert translated.canonical("list_github_issues") == "search_github_issues_2"

    # The canonical original is absent from the surface: the alias is dropped
    # rather than left dangling onto the unrelated ``search_github_issues``
    # visible name that ``search.github.issues`` got.
    only_dotted = dict(build_llm_tool_names(["search.github.issues"]))
    assert only_dotted == {"search.github.issues": "search_github_issues"}
    assert visible_aliases(only_dotted, _DECLARED) == {}
    assert (
        ToolNameResolver(only_dotted.values(), aliases=visible_aliases(only_dotted, _DECLARED)).canonical(
            "list_github_issues"
        )
        is None
    )

    # An alias shadowed by a real tool of that name could never fire (exact
    # match wins), so it is not carried over.
    shadowed = dict(build_llm_tool_names(["list_github_issues", "search_github_issues"]))
    assert visible_aliases(shadowed, _DECLARED) == {}
    # No collision: the alias maps onto the unchanged visible name, as before.
    plain = dict(build_llm_tool_names(["search_github_issues"]))
    assert visible_aliases(plain, _DECLARED) == {"list_github_issues": "search_github_issues"}
    assert visible_aliases({}, _DECLARED) == {}


def test_the_alias_table_is_what_the_definitions_declare():
    """No platform list: each tool's owner declares its aliases, and an alias
    two definitions declare keeps its first canonical."""
    definitions = [
        ToolDefinition(name="search_github_issues", description="d", aliases=("list_github_issues",)),
        ToolDefinition(name="other", description="d", aliases=("list_github_issues", "old_other")),
        ToolDefinition(name="plain", description="d"),
    ]
    assert declared_aliases(definitions) == {
        "list_github_issues": "search_github_issues",
        "old_other": "other",
    }
    # A resolver knows no alias it was not given.
    assert ToolNameResolver(["search_github_issues"]).resolve("list_github_issues") is None


def _composite(handlers: dict) -> CompositeToolExecutor:
    composite = CompositeToolExecutor()
    inner = CallableToolExecutor()
    defs = []
    for name, fn in handlers.items():
        inner.register(name, fn)
        defs.append(
            ToolDefinition(
                name=name,
                description=name,
                input_schema={"type": "object", "properties": {}},
            )
        )
    composite.add_tools(defs, inner)
    return composite


def test_normalize_tool_key_is_order_and_case_independent():
    assert normalize_tool_key("jira_search_issues") == normalize_tool_key(
        "search_jira_issues"
    )
    assert normalize_tool_key("Search_Web") == ("search", "web")
    # Dotted / slashed names tokenize the same way.
    assert normalize_tool_key("github.repo.get") == normalize_tool_key("get_github_repo")


def test_reordered_tool_name_dispatches_to_canonical():
    """The reordered-name case: ``jira_search_issues`` resolves to the registered
    ``search_jira_issues`` and executes instead of erroring."""

    async def _search(**_kw):
        return "jira results"

    composite = _composite({"search_jira_issues": _search})
    result = asyncio.run(
        composite.execute_tool(
            ToolCall(id="t1", name="jira_search_issues", arguments={"jql": "x"})
        )
    )
    assert not result.is_error
    assert result.content == "jira results"


def test_reordered_dispatch_preserves_arguments():
    captured: dict = {}

    async def _search(**kw):
        captured.update(kw)
        return "ok"

    composite = _composite({"search_jira_issues": _search})
    asyncio.run(
        composite.execute_tool(
            ToolCall(
                id="t2",
                name="jira_search_issues",
                arguments={"jql": "project = ABC"},
            )
        )
    )
    assert captured.get("jql") == "project = ABC"


def test_ambiguous_token_match_does_not_autodispatch():
    """If two registered tools share a token set, a reordered call must NOT be
    silently routed — it falls through to the helpful error."""

    async def _a(**_kw):
        return "A"

    async def _b(**_kw):
        return "B"

    composite = _composite(
        {"search_jira_issues": _a, "issues_jira_search": _b}
    )
    result = asyncio.run(
        composite.execute_tool(
            ToolCall(id="t3", name="jira_search_issues", arguments={})
        )
    )
    assert result.is_error
    assert "No executor registered" in result.content


def test_one_char_typo_is_not_token_equivalent_and_still_errors():
    """A typo (not a pure reorder) has a different token set, so it must NOT
    auto-dispatch — the existing 'Did you mean…' path handles it."""

    async def _noop(**_kw):
        return "ok"

    composite = _composite({"create_calendar_event": _noop})
    result = asyncio.run(
        composite.execute_tool(
            ToolCall(id="t4", name="create_calender_event", arguments={})
        )
    )
    assert result.is_error
    assert "No executor registered" in result.content


def _register(composite: CompositeToolExecutor, name: str, fn) -> None:
    """Register a single tool (its own inner executor) into ``composite``."""
    inner = CallableToolExecutor()
    inner.register(name, fn)
    composite.add_tools(
        [
            ToolDefinition(
                name=name,
                description=name,
                input_schema={"type": "object", "properties": {}},
            )
        ],
        inner,
    )


def test_reordered_dispatch_works_across_multiple_add_tools_batches():
    """The token-key index is built incrementally, so a reordered name still
    resolves when tools are registered across separate ``add_tools`` calls."""

    async def _search(**_kw):
        return "jira results"

    async def _other(**_kw):
        return "other"

    composite = CompositeToolExecutor()
    _register(composite, "get_notion_page", _other)
    _register(composite, "search_jira_issues", _search)

    result = asyncio.run(
        composite.execute_tool(
            ToolCall(id="t5", name="jira_search_issues", arguments={})
        )
    )
    assert not result.is_error
    assert result.content == "jira results"


def test_reordered_dispatch_still_unique_after_tool_replacement():
    """Re-registering a tool (last-write-wins) must NOT double-index its name —
    otherwise the unique-match guard would see two entries and refuse to
    recover the reordered name. The replacement handler must be the one that
    runs."""

    async def _old(**_kw):
        return "old"

    async def _new(**_kw):
        return "new"

    composite = CompositeToolExecutor()
    _register(composite, "search_jira_issues", _old)
    _register(composite, "search_jira_issues", _new)  # replace, same name

    # Index has exactly one entry for this token key (no duplicate).
    assert composite.name_resolver.token_matches("search_jira_issues") == [
        "search_jira_issues"
    ]

    result = asyncio.run(
        composite.execute_tool(
            ToolCall(id="t6", name="jira_search_issues", arguments={})
        )
    )
    assert not result.is_error
    assert result.content == "new"
