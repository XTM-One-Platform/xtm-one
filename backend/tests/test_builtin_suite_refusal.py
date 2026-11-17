"""A refused call to a REAL built-in tool is told which suite it belongs to.

The incident: an agent without the ``work_items`` built-in called
``update_work_item`` and was answered with a fuzzy hint pointing at
``update_knowledge_document`` - a tool from a different capability that
happens to share a token.  The model then chased a capability the agent does
not carry.  The refusal must instead name the suite the tool comes from and
say that it is not enabled for this agent (or, when a sibling of the same
suite IS offered, that the tool is not offered on this turn; or, when the
suite is wired but withheld from the whole run by a deny list, that it is
withheld), skip the fuzzy suggestion, and carry the
``TOOL_NOT_AVAILABLE_SENTINEL`` on BOTH executors so the capability-not-found
nudge aggregates on the General Assistant's bare composite as well.  A close
misspelling of a built-in (``update_work_items``) still learns its suite when
nothing offered came close.
"""

from __future__ import annotations

import pytest

from app.llm.base import BaseLLMProvider, ChatMessage, ChatResponse
from app.services.builtin_tool_catalog import build_builtin_tool_catalog
from app.tools import agent_loop as al
from app.tools import builtin_suites
from app.tools.base import (
    TOOL_NOT_AVAILABLE_SENTINEL,
    CallableToolExecutor,
    CompositeToolExecutor,
    ScopedToolExecutor,
    ToolCall,
    ToolDefinition,
    describe_unavailable_builtin,
)
from app.tools.builtin_suites import (
    BuiltinTool,
    builtin_suite_tools,
    builtin_tool_witnesses,
    ensure_suite_index_loaded,
    resolve_builtin_tool,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_tool(name: str) -> ToolDefinition:
    return ToolDefinition(
        name=name,
        description=f"Tool: {name}",
        input_schema={"type": "object", "properties": {}},
        source="test",
    )


def _make_setup(
    allowed_names: list[str],
    registered_names: list[str] | None = None,
) -> tuple[ScopedToolExecutor, CompositeToolExecutor]:
    if registered_names is None:
        registered_names = allowed_names
    callable_exec = CallableToolExecutor()
    for name in registered_names:

        async def _fn(**kwargs):
            return "ok"

        callable_exec.register(name, _fn)
    composite = CompositeToolExecutor()
    composite.add_tools([_make_tool(n) for n in registered_names], callable_exec)
    scoped = ScopedToolExecutor(composite, [_make_tool(n) for n in allowed_names])
    return scoped, composite


class _ScriptedLLM(BaseLLMProvider):
    provider_name = "stub"
    context_window = 100_000

    def __init__(self, script: list[ChatResponse]) -> None:
        self._script = list(script)
        self._idx = 0

    async def chat(self, messages, model=None, tools=None, **kwargs):
        if self._idx < len(self._script):
            resp = self._script[self._idx]
            self._idx += 1
            return resp
        return ChatResponse(content="no more scripted responses", model="stub")

    async def chat_stream(self, messages, **kwargs):
        yield ""

    async def embed(self, text, model=None):
        return [0.0]

    async def list_models(self):
        return []


@pytest.fixture
def _quiet_langfuse(monkeypatch):
    monkeypatch.setattr(al, "_lf_start_llm_gen", lambda *a, **kw: None)
    monkeypatch.setattr(al, "_lf_end_generation", lambda *a, **kw: None)
    monkeypatch.setattr(al, "_lf_end_span", lambda *a, **kw: None)
    monkeypatch.setattr(al, "_lf_end_tool_span", lambda *a, **kw: None)
    monkeypatch.setattr(al, "_lf_start_iter_span", lambda **kw: None)
    monkeypatch.setattr(al, "_lf_start_tool_span", lambda **kw: None)


# ---------------------------------------------------------------------------
# The suite index
# ---------------------------------------------------------------------------


def test_index_agrees_with_the_builtin_tool_catalog():
    """Every tool the platform's registry lists resolves to the FIRST suite
    listing it (the catalog orders the narrow trace ids before the
    ``agent_builder`` alias) and each suite's listing is the registry's - the
    index is derived, never a second hand-maintained table that could drift."""
    catalog = build_builtin_tool_catalog(include_disabled=True)
    assert catalog, "the built-in tool catalog must not be empty"
    owner: dict[str, str] = {}
    for suite, definitions in catalog.items():
        names = tuple(str(d["name"]) for d in definitions)
        assert builtin_suite_tools(suite) == names
        for name in names:
            owner.setdefault(name, suite)
    for name, suite in owner.items():
        assert resolve_builtin_tool(name) == BuiltinTool(name, suite)


def test_the_registry_lists_every_selectable_builtin_id():
    """The catalog doubles as the registry of valid ``Agent.builtin_tool_ids``
    (skill-package validation reads its keys): every id the agent builder
    accepts must be a key, disabled suites included, so a call to any real
    built-in is classified as such."""
    from app.services.builtin_tool_aliases import alias_group
    from app.services.builtin_tools.agent_builder import _VALID_BUILTIN_TOOL_IDS

    catalog = build_builtin_tool_catalog(include_disabled=True)
    current = {tool_id for tool_id in _VALID_BUILTIN_TOOL_IDS if alias_group(tool_id) is None}
    assert current <= set(catalog), current - set(catalog)
    # A [DEPRECATED] id is accepted and resolves to a group the catalog lists.
    for tool_id in set(_VALID_BUILTIN_TOOL_IDS) - current:
        assert alias_group(tool_id) in catalog, tool_id


def test_the_traces_group_owns_its_tools_and_the_alias_merely_grants_them():
    """``observability`` owns the trace functions (the former
    ``inspect_my_traces`` / ``inspect_agent_traces`` ids are its [DEPRECATED]
    aliases); the ``platform`` group (``agent_builder``) grants them as
    well. A refusal must name the group to enable, while the group listing
    (what the UI shows) still carries everything it grants."""
    catalog = build_builtin_tool_catalog()
    names = {suite: [d["name"] for d in defs] for suite, defs in catalog.items()}
    assert names["observability"] == ["inspect_my_traces", "inspect_agent_traces", "get_trace_payload"]
    assert "inspect_my_traces" not in catalog and "inspect_agent_traces" not in catalog
    assert "agent_builder" not in catalog
    assert {"inspect_my_traces", "inspect_agent_traces", "get_trace_payload"} <= set(names["platform"]), (
        "the group still lists what it grants (the UI reads this listing)"
    )
    assert builtin_suite_tools("platform") == tuple(names["platform"])

    assert resolve_builtin_tool("inspect_my_traces") == BuiltinTool("inspect_my_traces", "observability")
    assert resolve_builtin_tool("inspect_agent_traces") == BuiltinTool("inspect_agent_traces", "observability")
    assert resolve_builtin_tool("get_trace_payload").suite == "observability"
    assert resolve_builtin_tool("create_agent") == BuiltinTool("create_agent", "platform")


def test_the_catalog_lists_the_traces_group_before_the_alias():
    """Ownership is 'the first suite listing the tool', so the ORDER of the
    catalog's keys is part of the contract, not an accident of construction:
    the traces group must come before the ``platform`` group that grants them
    too (``agent_builder``), with or without the disabled suites."""
    for catalog in (build_builtin_tool_catalog(), build_builtin_tool_catalog(include_disabled=True)):
        keys = list(catalog)
        assert keys.index("observability") < keys.index("platform"), keys


def test_agent_builder_listing_is_the_runtime_builders_own():
    """The ``agent_builder`` entry is DERIVED from ``build_agent_builder_tool``
    - never a hand-written copy.  The copy this replaced had fallen 36 tools
    behind the runtime (runs, prompts, skills, variables, MCP servers,
    integrations, the KB and custom-tool write half), and a tool the index
    cannot see is a refused call that falls back to the fuzzy hint."""
    from app.services.builtin_tools import build_agent_builder_tool

    runtime, _ = build_agent_builder_tool(None, "")  # type: ignore[arg-type]
    runtime_names = tuple(d.name for d in runtime)
    assert len(runtime_names) > 20, "the agent builder offers far more than the old hand-written twenty"
    assert len(set(runtime_names)) == len(runtime_names), "duplicate runtime tool names"

    # The ``platform`` group lists the former ``agent_builder`` builder first.
    listed = builtin_suite_tools("platform")
    assert listed[: len(runtime_names)] == runtime_names
    # ...followed by the alias-granted trace and evaluation tools, then fleets and flows.
    assert {"inspect_my_traces", "inspect_agent_traces", "get_trace_payload", "trigger_evaluation"} <= set(
        listed[len(runtime_names) :]
    )
    assert {"create_fleet", "create_flow"} <= set(listed[len(runtime_names) :])
    for name in runtime_names:
        owner = "observability" if name in {"inspect_my_traces", "inspect_agent_traces", "get_trace_payload"} else "platform"
        assert resolve_builtin_tool(name) == BuiltinTool(name, owner)


def test_witnesses_follow_the_grantors_not_the_listing():
    """``other`` vouches for ``name`` only when every suite granting ``other``
    grants ``name`` too - the test that keeps aliases and shared tools from
    proving a suite the agent may not carry."""
    # One single-suite family: every other member vouches for each member.
    # Work items, shared state and worksets are one ``coordination`` group,
    # so each of its functions vouches for the others.
    coordination = builtin_suite_tools("coordination")
    assert {"list_work_items", "get_shared_state", "workset_add_item"} <= set(coordination)
    assert set(builtin_tool_witnesses("update_work_item")) == set(coordination) - {"update_work_item"}

    # The alias grants the narrow id's tools, so an alias-only tool vouches
    # for them - but not the other way round.
    assert "create_agent" in builtin_tool_witnesses("inspect_my_traces")
    assert "inspect_my_traces" not in builtin_tool_witnesses("create_agent")
    assert "inspect_agent_traces" not in builtin_tool_witnesses("create_agent")
    assert "get_trace_payload" not in builtin_tool_witnesses("create_agent")

    # One traces group: its functions vouch for each other (a scoped
    # group withholds a function, it does not leave the group unenabled)...
    assert {"get_trace_payload", "inspect_agent_traces"} <= set(builtin_tool_witnesses("inspect_my_traces"))
    assert "inspect_my_traces" in builtin_tool_witnesses("inspect_agent_traces")
    # ...and anything from either grantor vouches for the reader.
    assert {"inspect_my_traces", "inspect_agent_traces", "create_agent"} <= set(
        builtin_tool_witnesses("get_trace_payload")
    )

    assert builtin_tool_witnesses("frobnicate_widget") == ()


def test_disabled_suites_are_still_indexed(monkeypatch):
    """A deployment without Runners does not advertise ``computer_use``, but a
    call to ``run_shell`` there is still a call to a real built-in that is
    unavailable - not a typo of some offered tool.  The index therefore reads
    the registry with its disabled suites; the advertised catalog does not."""
    from app.core.config import settings

    monkeypatch.setattr(settings, "runners_enabled", False)
    assert "computer_use" not in build_builtin_tool_catalog()
    assert "computer_use" in build_builtin_tool_catalog(include_disabled=True)

    builtin_suites._index.cache_clear()
    try:
        assert resolve_builtin_tool("run_shell") == BuiltinTool("run_shell", "computer_use")
        assert "computer_screenshot" in builtin_suite_tools("computer_use")
        note = describe_unavailable_builtin("run_shell", ["search_knowledge"], ["search_knowledge"])
        assert note is not None
        assert "from the 'computer_use' suite" in note
        assert "or is disabled on this platform" in note
    finally:
        builtin_suites._index.cache_clear()


def test_incident_tool_belongs_to_the_coordination_suite():
    # ``work_items`` merged into ``coordination``: the refusal names the
    # group to enable, and the former id still lists the functions it granted.
    assert resolve_builtin_tool("update_work_item") == BuiltinTool("update_work_item", "coordination")
    assert "update_work_item" in builtin_suite_tools("coordination")
    assert "update_work_item" in builtin_suite_tools("work_items")
    assert "get_shared_state" not in builtin_suite_tools("work_items")


def test_reordered_variant_resolves_to_the_canonical_name():
    """``work_item_update`` is the same tokens as ``update_work_item`` in
    another order - the dispatcher's own reordered-name tolerance."""
    assert resolve_builtin_tool("work_item_update") == BuiltinTool("update_work_item", "coordination", "tokens")


def test_unknown_names_are_not_builtins():
    assert resolve_builtin_tool("math_calculator") is None
    assert resolve_builtin_tool("frobnicate_widget") is None
    assert builtin_suite_tools("no_such_suite") == ()


def test_close_misspelling_resolves_only_when_asked_and_only_when_close():
    """``update_work_items`` is neither an exact nor a reordered built-in
    name, so the default lookup says no; the opt-in near-miss lookup names
    the tool it was reaching for, but stays strict enough that a typo of an
    unrelated integration tool is never called a built-in."""
    assert resolve_builtin_tool("update_work_items") is None
    assert resolve_builtin_tool("update_work_items", fuzzy=True) == BuiltinTool(
        "update_work_item", "coordination", "close"
    )
    assert resolve_builtin_tool("send_teams_msg", fuzzy=True) is None
    assert resolve_builtin_tool("frobnicate_widget", fuzzy=True) is None
    assert resolve_builtin_tool("", fuzzy=True) is None


@pytest.mark.asyncio
async def test_index_warm_up_runs_off_the_loop_and_is_then_a_no_op():
    """The first build imports the settings API chain and runs every builder;
    the executors await this before their sync lookup so it never blocks the
    event loop.  Once cached, it returns immediately."""
    await ensure_suite_index_loaded()
    assert builtin_suites._index.cache_info().currsize == 1
    hits_before = builtin_suites._index.cache_info().hits
    await ensure_suite_index_loaded()
    assert builtin_suites._index.cache_info().currsize == 1
    assert builtin_suites._index.cache_info().hits == hits_before, "a warm index is not rebuilt"


# ---------------------------------------------------------------------------
# describe_unavailable_builtin
# ---------------------------------------------------------------------------


def test_describe_names_missing_suite_when_no_sibling_is_offered():
    note = describe_unavailable_builtin("update_work_item", ["update_knowledge_document"])
    assert note is not None
    assert "'coordination' suite" in note
    assert "not enabled for this agent" in note
    assert "or is disabled on this platform" in note, "a platform-wide switch-off looks the same from here"
    assert "enable the 'coordination' built-in on this agent" in note
    assert "load_tools" in note
    assert "withheld" not in note


def test_describe_says_withheld_when_the_suite_is_registered_but_not_offered():
    """An agent that carries ``work_items`` but deny-lists ``update_work_item``
    (``Agent.tool_deny_list``) keeps the tool registered on the composite while
    the scoped surface withholds it.  Telling the model the suite is 'not
    enabled' would send the caller to enable something the agent already has;
    the truthful message is that this run withholds it."""
    registered = ["update_knowledge_document", "update_work_item"]
    note = describe_unavailable_builtin("update_work_item", ["update_knowledge_document"], registered)
    assert note is not None
    assert "'coordination' suite" in note
    assert "enabled for this agent but withheld from this run" in note
    assert "not enabled" not in note
    assert "enable the 'coordination' built-in" not in note
    assert "load_tools" in note


def test_describe_says_withheld_when_only_a_sibling_is_registered():
    """The whole suite deny-listed for the run: no work-items tool is offered,
    but a sibling on the registry still proves the agent carries the suite."""
    sibling = next(n for n in builtin_suite_tools("coordination") if n != "update_work_item")
    note = describe_unavailable_builtin("update_work_item", ["search_knowledge"], ["search_knowledge", sibling])
    assert note is not None
    assert "withheld from this run" in note


def test_describe_prefers_not_offered_when_a_sibling_is_offered_whatever_the_registry_says():
    registered = ["generate_presentation", "export_presentation"]
    note = describe_unavailable_builtin("export_presentation", ["generate_presentation"], registered)
    assert note is not None
    assert "not offered on this turn" in note
    assert "withheld" not in note


def test_describe_treats_an_unregistered_tool_as_not_enabled_even_with_a_registry():
    note = describe_unavailable_builtin("update_work_item", ["search_knowledge"], ["search_knowledge"])
    assert note is not None
    assert "not enabled for this agent" in note


def test_describe_names_a_near_miss_only_when_asked():
    assert describe_unavailable_builtin("update_work_items", []) is None
    note = describe_unavailable_builtin("update_work_items", [], fuzzy=True)
    assert note is not None
    assert "'update_work_items' looks like the platform built-in tool 'update_work_item'" in note
    assert "'coordination' suite" in note


def test_describe_says_not_offered_when_a_sibling_is_offered():
    """A gated sibling (``export_presentation`` on a turn that did not ask
    for a file) belongs to a suite the agent DOES carry: the truthful
    message is 'not offered on this turn', not 'not enabled'."""
    note = describe_unavailable_builtin("export_presentation", ["generate_presentation"])
    assert note is not None
    assert "'generate_presentation' suite" in note
    assert "not offered on this turn" in note
    assert "not enabled" not in note


def test_describe_names_the_traces_group_not_the_alias():
    """An agent without any trace tool calling ``inspect_my_traces`` is sent
    to enable the ``observability`` group - the smallest grant that fixes it
    - not the whole ``agent_builder`` alias that grants it too."""
    note = describe_unavailable_builtin("inspect_my_traces", ["search_knowledge"], ["search_knowledge"])
    assert note is not None
    assert "from the 'observability' suite" in note
    assert "enable the 'observability' built-in on this agent" in note
    assert "agent_builder" not in note


def test_describe_classifies_the_agent_builder_tools_the_hand_list_had_lost():
    """``create_prompt``, ``list_mcp_servers``, ``get_run``... are real
    agent-builder tools the hand-written listing never named, so a refusal
    used to fall through to 'Did you mean'.  Derived from the runtime builder,
    every one of them is now a classified built-in of the alias."""
    for name in ("create_prompt", "list_mcp_servers", "get_run", "delete_kb_document", "update_variable"):
        note = describe_unavailable_builtin(name, ["search_knowledge"], ["search_knowledge"])
        assert note is not None, name
        assert "from the 'platform' suite" in note, name
        assert "not enabled for this agent" in note, name


def test_describe_does_not_read_an_alias_granted_tool_as_a_sibling_of_the_alias():
    """An agent carrying only ``inspect_my_traces`` is offered the trace tools,
    which ``agent_builder`` grants as well.  Those are NOT evidence that the
    agent has ``agent_builder``: a ``create_agent`` call is 'not enabled', not
    'not offered on this turn'."""
    offered = ["inspect_my_traces", "get_trace_payload", "search_knowledge"]
    note = describe_unavailable_builtin("create_agent", offered, offered)
    assert note is not None
    assert "from the 'platform' suite" in note
    assert "not enabled for this agent" in note
    assert "not offered on this turn" not in note
    assert "withheld" not in note


def test_describe_reads_a_scoped_traces_group_as_withheld_not_unenabled():
    """One traces group: an agent scoped to other agents' traces that
    calls ``inspect_my_traces`` carries the group, so the function is
    withheld by the scoping, never 'not enabled'."""
    offered = ["inspect_agent_traces", "get_trace_payload"]
    note = describe_unavailable_builtin("inspect_my_traces", offered, offered)
    assert note is not None
    assert "from the 'observability' suite" in note
    assert "not enabled for this agent" not in note

    # The holder of the group who deny-lists its inspect tool: the reader
    # offered proves the group, so the tool is not offered, never 'not enabled'.
    note = describe_unavailable_builtin(
        "inspect_agent_traces",
        ["get_trace_payload"],
        ["get_trace_payload", "inspect_agent_traces"],
    )
    assert note is not None
    assert "from the 'observability' suite" in note
    assert "not enabled" not in note

    # The alias holder who deny-lists it: an alias-only tool on the offered
    # surface proves the alias, and the alias grants the tool - so it is
    # 'not offered on this turn', never 'not enabled'.
    note = describe_unavailable_builtin(
        "inspect_agent_traces",
        ["inspect_my_traces", "get_trace_payload", "create_agent"],
        ["inspect_my_traces", "get_trace_payload", "create_agent", "inspect_agent_traces"],
    )
    assert note is not None
    assert "from the 'observability' suite" in note
    assert "not offered on this turn" in note
    assert "not enabled" not in note


def test_describe_returns_none_for_non_builtins():
    assert describe_unavailable_builtin("send_teams_msg", ["send_teams_message"]) is None


def test_describe_identifies_a_reordered_builtin_name():
    note = describe_unavailable_builtin("work_item_update", [])
    assert note is not None
    assert "'work_item_update' is the platform built-in tool 'update_work_item'" in note


# ---------------------------------------------------------------------------
# Executor refusals
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_composite_refusal_names_the_suite_instead_of_a_fuzzy_hint():
    """The incident, on the bare composite: the refusal names the suite,
    carries the sentinel, still lists what IS registered, and never points
    at ``update_knowledge_document``."""
    _, composite = _make_setup(allowed_names=["update_knowledge_document"])

    result = await composite.execute_tool(ToolCall(id="t1", name="update_work_item", arguments={}))

    assert result.is_error is True
    assert "No executor registered for tool: update_work_item" in result.content
    assert TOOL_NOT_AVAILABLE_SENTINEL in result.content
    assert "'coordination' suite" in result.content
    assert "not enabled for this agent" in result.content
    assert "Did you mean" not in result.content
    assert "update_knowledge_document" in result.content  # the listing survives


@pytest.mark.asyncio
async def test_scoped_refusal_names_the_suite_as_not_enabled_when_unregistered():
    """The incident on an agent surface: the suite is not wired at all."""
    scoped, _ = _make_setup(allowed_names=["update_knowledge_document"])

    result = await scoped.execute_tool(ToolCall(id="t1", name="update_work_item", arguments={}))

    assert result.is_error is True
    assert TOOL_NOT_AVAILABLE_SENTINEL in result.content
    assert "'coordination' suite" in result.content
    assert "not enabled for this agent" in result.content
    assert "Did you mean" not in result.content


@pytest.mark.asyncio
async def test_scoped_refusal_says_withheld_when_the_tool_is_registered_but_denied():
    """``tool_deny_list`` keeps the tool on the composite and drops it from the
    scoped surface: the refusal reads the inner registry and says so instead
    of claiming the suite is not enabled."""
    scoped, _ = _make_setup(
        allowed_names=["update_knowledge_document"],
        registered_names=["update_knowledge_document", "update_work_item"],
    )

    result = await scoped.execute_tool(ToolCall(id="t1", name="update_work_item", arguments={}))

    assert result.is_error is True
    assert TOOL_NOT_AVAILABLE_SENTINEL in result.content
    assert "'coordination' suite" in result.content
    assert "enabled for this agent but withheld from this run" in result.content
    assert "not enabled" not in result.content
    assert "Did you mean" not in result.content
    assert "update_work_item" not in result.content.split("Available tools:")[1], (
        "a withheld tool must not appear in the offered listing"
    )


@pytest.mark.asyncio
async def test_scoped_refusal_of_a_near_miss_names_the_suite_when_nothing_offered_is_close():
    scoped, _ = _make_setup(allowed_names=["update_knowledge_document"])

    result = await scoped.execute_tool(ToolCall(id="t1", name="update_work_items", arguments={}))

    assert result.is_error is True
    assert "Did you mean" not in result.content
    assert "looks like the platform built-in tool 'update_work_item'" in result.content
    assert "'coordination' suite" in result.content


@pytest.mark.asyncio
async def test_offered_close_match_beats_the_built_in_near_miss():
    """A custom tool the agent DOES have and that is close to the name wins:
    the near-miss hint only fires when the offered surface has nothing."""
    scoped, _ = _make_setup(allowed_names=["update_work_itemx"])

    result = await scoped.execute_tool(ToolCall(id="t1", name="update_work_items", arguments={}))

    assert result.is_error is True
    assert "Did you mean: update_work_itemx?" in result.content
    assert "built-in" not in result.content


@pytest.mark.asyncio
async def test_scoped_refusal_of_gated_sibling_says_not_offered_on_this_turn():
    scoped, _ = _make_setup(
        allowed_names=["generate_presentation"],
        registered_names=["generate_presentation", "export_presentation"],
    )

    result = await scoped.execute_tool(ToolCall(id="t1", name="export_presentation", arguments={}))

    assert result.is_error is True
    assert TOOL_NOT_AVAILABLE_SENTINEL in result.content
    assert "not offered on this turn" in result.content
    assert "Did you mean" not in result.content


@pytest.mark.asyncio
async def test_scoped_refusal_of_a_formerly_unlisted_agent_builder_tool_names_the_suite():
    """An agent without ``agent_builder`` calling ``list_mcp_servers``: the
    hand-written catalog did not know the name, so this refusal used to carry
    the fuzzy hint.  It is a real built-in of the alias and is told so."""
    scoped, _ = _make_setup(allowed_names=["search_knowledge", "list_mcp_server_tools"])

    result = await scoped.execute_tool(ToolCall(id="t1", name="list_mcp_servers", arguments={}))

    assert result.is_error is True
    assert TOOL_NOT_AVAILABLE_SENTINEL in result.content
    assert "'platform' suite" in result.content
    assert "not enabled for this agent" in result.content
    assert "Did you mean" not in result.content


@pytest.mark.asyncio
async def test_non_builtin_typo_keeps_the_fuzzy_suggestion():
    """The close-match hint is still the right answer for a misspelt
    integration tool - only real built-ins skip it."""
    scoped, _ = _make_setup(allowed_names=["send_teams_message"])

    result = await scoped.execute_tool(ToolCall(id="t1", name="send_teams_msg", arguments={}))

    assert result.is_error is True
    assert "Did you mean" in result.content
    assert "send_teams_message" in result.content
    assert "built-in" not in result.content


# ---------------------------------------------------------------------------
# The loop on the bare composite (General Assistant path)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_composite_not_found_counts_toward_the_capability_nudge(_quiet_langfuse):
    """Without a ``ScopedToolExecutor`` the not-found errors used to carry no
    sentinel, so a model cycling through variants on the General Assistant
    never got the nudge.  Three distinct unknown names on the bare composite
    must now fire it exactly once, and the first refusal must already name
    the missing suite."""
    _, composite = _make_setup(allowed_names=["update_knowledge_document"])

    tried = ["update_work_item", "work_item_update", "modify_work_item"]
    script = [
        ChatResponse(
            content="",
            model="stub",
            stop_reason="tool_calls",
            tool_calls=[ToolCall(id=f"t{i}", name=name, arguments={})],
        )
        for i, name in enumerate(tried)
    ] + [ChatResponse(content="This agent cannot manage work items.", model="stub")]
    messages: list[ChatMessage] = [ChatMessage(role="user", content="update the work item")]

    response = await al.run_agent_loop(
        llm=_ScriptedLLM(script),
        messages=messages,
        tools=[_make_tool("update_knowledge_document")],
        executor=composite,
        max_iterations=10,
        progressive_tools=False,
    )

    assert response.content == "This agent cannot manage work items."
    nudges = [
        m for m in messages if getattr(m, "role", None) == "system" and "capability not found" in (m.content or "")
    ]
    assert len(nudges) == 1
    tool_results = [m.content or "" for m in messages if getattr(m, "role", None) == "tool"]
    assert tool_results, "the refusals must be in the transcript as tool results"
    assert "'coordination' suite" in tool_results[0]
    assert all("update_knowledge_document?" not in r for r in tool_results)
