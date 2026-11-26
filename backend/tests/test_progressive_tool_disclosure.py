"""Tests for progressive tool disclosure (the ``load_tools`` tool multiplexer).

Two layers are covered:

* **Unit** — the pure grouping / gate logic in ``app.tools.tool_gating``:
  partitioning, threshold activation, lenient group resolution, and the
  per-iteration visible-tool view.
* **Integration** — ``run_agent_loop`` with a stub LLM + a real
  ``CompositeToolExecutor``, asserting that a simple turn never sees the gated
  tool schemas, and that a ``load_tools`` call reveals a group's tools for the
  next iteration without routing the meta-tool through the executor.
"""

from __future__ import annotations

import dataclasses
import logging

import pytest

from app.llm.base import BaseLLMProvider, ChatMessage, ChatResponse
from app.tools import agent_loop as al
from app.tools.base import (
    CallableToolExecutor,
    CompositeToolExecutor,
    ToolCall,
    ToolDefinition,
    build_llm_tool_names,
    declared_aliases,
    visible_aliases,
)
from app.tools.tool_gating import (
    _SEARCH_LOADED_LIMIT,
    _SEARCH_RESULT_LIMIT,
    CORE_OVERFLOW_GROUP_ID,
    LOAD_TOOLS_TOOL_NAME,
    MIN_GATED_BUDGET,
    ToolGate,
    build_load_tools_definition,
    build_tool_gate,
    group_tools,
)

#: The aliases tools declare (``ToolDefinition.aliases``), as the GitHub
#: provider declares ``list_github_issues`` on ``search_github_issues``.
_DECLARED_ALIASES = {"search_github_issues": ("list_github_issues",)}


def _tool(name: str, source: str) -> ToolDefinition:
    return ToolDefinition(name=name, description=f"{name} desc", source=source, aliases=_DECLARED_ALIASES.get(name, ()))


def _integration_tools(provider: str, n: int) -> list[ToolDefinition]:
    """As the integration layer builds them: labelled with their catalog name."""
    from app.services.integration_tools import with_source_labels

    return with_source_labels([_tool(f"{provider}_tool_{i}", f"integration:{provider}") for i in range(n)])


# ─────────────────────────────────────────────────────────────────────────
# Unit: grouping
# ─────────────────────────────────────────────────────────────────────────


def test_group_tools_partitions_builtin_vs_gateable():
    tools = [
        _tool("search_knowledge", "builtin:knowledge"),
        _tool("consult_agent", "builtin:sub_agent"),
        _tool("notion_search", "integration:notion"),
        _tool("notion_get", "integration:notion"),
        _tool("gh_issue", "mcp:GitHub"),
        _tool("my_custom", "custom:abc-123"),
    ]
    always_on, groups = group_tools(tools)

    assert {t.name for t in always_on} == {"search_knowledge", "consult_agent"}
    assert set(groups.keys()) == {"integration:notion", "mcp:GitHub", "custom"}
    assert [t.name for t in groups["integration:notion"].tools] == [
        "notion_search",
        "notion_get",
    ]
    # MCP group label is humanised + tagged.
    assert groups["mcp:GitHub"].label == "GitHub (MCP)"
    # All custom tools collapse into a single group regardless of their UUIDs.
    assert groups["custom"].label == "Custom Tools"
    assert groups["custom"].count == 1


def test_group_tools_labels_a_group_with_the_name_its_tools_carry():
    """The label is the catalog name the integration layer stamps, never a list in the gate."""
    labelled = [
        ToolDefinition(name="opencti_tool", description="d", source="integration:opencti", source_label="OpenCTI")
    ]
    _, groups = group_tools(labelled + _integration_tools("some_provider", 1))
    assert groups["integration:opencti"].label == "OpenCTI"
    assert groups["integration:some_provider"].label == "Some Provider"


def test_integration_tools_carry_their_catalog_name():
    from app.services.integration_tools import with_source_labels

    tools = with_source_labels(
        [
            _tool("search_github_issues", "integration:github"),
            _tool("search_github_issues__2", "integration:github#2"),
            _tool("slack_post", "channel_bot:slack_bot:00000000-0000-4000-8000-000000000001"),
            _tool("telegram_send", "integration:telegram_bot"),
            _tool("mine", "custom:abc"),
        ]
    )
    assert [tool.source_label for tool in tools] == ["GitHub", "GitHub", "Slack", "Telegram", None]


# ─────────────────────────────────────────────────────────────────────────
# Unit: gate construction / thresholds
# ─────────────────────────────────────────────────────────────────────────


def test_build_tool_gate_disabled_returns_none():
    tools = _integration_tools("notion", 30)
    assert build_tool_gate(tools, enabled=False, min_tools=16) is None


def test_build_tool_gate_below_threshold_returns_none():
    tools = _integration_tools("notion", 10)
    assert build_tool_gate(tools, enabled=True, min_tools=16) is None


def test_build_tool_gate_no_gateable_tools_returns_none():
    tools = [_tool("search_knowledge", "builtin:knowledge") for _ in range(3)]
    assert build_tool_gate(tools, enabled=True, min_tools=1) is None


def test_build_tool_gate_disabled_on_name_collision():
    """A real tool already named ``load_tools`` disables gating (legacy
    fallback), so the loop never intercepts calls meant for that tool."""
    tools = [
        _tool(LOAD_TOOLS_TOOL_NAME, "integration:notion"),
        *_integration_tools("notion", 20),
    ]
    assert build_tool_gate(tools, enabled=True, min_tools=1) is None


def test_build_tool_gate_active_above_threshold():
    tools = [
        _tool("search_knowledge", "builtin:knowledge"),
        *_integration_tools("notion", 20),
    ]
    gate = build_tool_gate(tools, enabled=True, min_tools=16)
    assert gate is not None
    assert gate.gated_tool_count == 20
    assert gate.group_count == 1


# ─────────────────────────────────────────────────────────────────────────
# Unit: ToolGate behaviour
# ─────────────────────────────────────────────────────────────────────────


def _gate_with(*provider_counts: tuple[str, int]) -> ToolGate:
    tools: list[ToolDefinition] = [_tool("search_knowledge", "builtin:knowledge")]
    for provider, n in provider_counts:
        tools.extend(_integration_tools(provider, n))
    gate = build_tool_gate(tools, enabled=True, min_tools=1)
    assert gate is not None
    return gate


def test_visible_tools_starts_with_core_plus_meta_only():
    gate = _gate_with(("notion", 20))
    names = {t.name for t in gate.visible_tools()}
    # Always-on core + meta-tool, but NONE of the gated tools yet.
    assert "search_knowledge" in names
    assert LOAD_TOOLS_TOOL_NAME in names
    assert not any(n.startswith("notion_tool_") for n in names)


def test_reveal_exposes_group_in_visible_tools():
    gate = _gate_with(("notion", 20), ("opencti", 5))
    result = gate.reveal(["integration:notion"])
    assert result.revealed == ["integration:notion"]
    assert result.unknown == []

    names = {t.name for t in gate.visible_tools()}
    assert any(n.startswith("notion_tool_") for n in names)
    # The unrevealed group stays hidden.
    assert not any(n.startswith("opencti_tool_") for n in names)


def test_reveal_is_lenient_about_group_token_form():
    gate = _gate_with(("notion", 20))
    # Bare provider name (no "integration:" prefix) still resolves.
    result = gate.reveal(["notion"])
    assert result.revealed == ["integration:notion"]
    assert result.unknown == []


def test_reveal_reports_unknown_groups():
    gate = _gate_with(("notion", 20))
    result = gate.reveal(["nope", "integration:notion"])
    assert result.revealed == ["integration:notion"]
    assert result.unknown == ["nope"]
    # describe_reveal surfaces the valid set so the model can self-correct.
    text = gate.describe_reveal(result)
    assert "Unknown group(s) ignored: nope" in text
    assert "integration:notion" in text


def test_reveal_idempotent_no_double_reveal():
    gate = _gate_with(("notion", 20))
    gate.reveal(["integration:notion"])
    again = gate.reveal(["integration:notion"])
    assert again.revealed == [], "already-revealed group must not be reported again"
    assert again.already == ["integration:notion"]
    assert "No new tools were loaded" in gate.describe_reveal(again)


def test_load_tools_definition_enumerates_group_ids():
    _, groups = group_tools(_integration_tools("notion", 2) + _integration_tools("opencti", 2))
    definition = build_load_tools_definition(groups)
    assert definition.name == LOAD_TOOLS_TOOL_NAME
    assert definition.priority == 0
    enum = definition.input_schema["properties"]["groups"]["items"]["enum"]
    assert set(enum) == {"integration:notion", "integration:opencti"}


def test_catalog_prompt_lists_groups_and_load_instruction():
    gate = _gate_with(("notion", 20))
    prompt = gate.catalog_prompt
    assert LOAD_TOOLS_TOOL_NAME in prompt
    assert "integration:notion" in prompt
    assert "20 tools" in prompt


def test_describe_reveal_samples_tool_names_for_large_group():
    """The load_tools result must not enumerate every tool name for a large
    group — the schemas are already in the visible list, so listing them all
    would re-bloat the conversation and undercut the token savings."""
    gate = _gate_with(("notion", 40))
    result = gate.reveal(["integration:notion"])
    text = gate.describe_reveal(result)

    # Count + a sample are reported, the rest summarised as "+N more".
    assert "Loaded 40 tool(s)" in text
    assert "+34 more" in text  # 40 - 6 sample
    # A tool beyond the sample window must NOT be spelled out.
    assert "notion_tool_39" not in text


# ─────────────────────────────────────────────────────────────────────────
# Unit: the provider tool budget (``max_visible``)
# ─────────────────────────────────────────────────────────────────────────


def _capped_gate(
    max_visible: int,
    *provider_counts: tuple[str, int],
    core: int = 1,
) -> ToolGate:
    tools: list[ToolDefinition] = [
        _tool(f"core_{i}", "builtin:knowledge") for i in range(core)
    ]
    for provider, n in provider_counts:
        tools.extend(_integration_tools(provider, n))
    gate = build_tool_gate(tools, enabled=True, min_tools=1, max_visible=max_visible)
    assert gate is not None
    return gate


def _visible_names(gate: ToolGate) -> set[str]:
    return {t.name for t in gate.visible_tools()}


def test_capped_gate_catalogue_lists_every_group_and_explains_the_budget():
    """The inventory is never truncated: on a 20-tool provider the catalogue
    still lists all 8 groups (160 gated tools), and the ONLY prompt text the
    cap adds is the budget paragraph — the old "N tools were unavailable this
    run" notice is gone from the gated path."""
    providers = [(f"prov{i}", 20) for i in range(8)]
    gate = _capped_gate(20, *providers)
    assert gate.group_count == 8
    assert gate.gated_tool_count == 160
    for i in range(8):
        assert f'"integration:prov{i}"' in gate.catalog_prompt
    assert "Tool budget: this model accepts at most 20 tool schemas" in gate.catalog_prompt
    # 20 - 1 core - 1 load_tools = 18 free slots.
    assert "18 of which are free" in gate.catalog_prompt
    assert gate.demoted_always_on == []
    assert gate.min_visible_count == 2
    assert gate.required_budget == 1 + MIN_GATED_BUDGET


def test_uncapped_gate_catalogue_has_no_budget_paragraph():
    gate = _gate_with(("notion", 20))
    assert "Tool budget" not in gate.catalog_prompt


def test_budget_paragraph_omitted_when_everything_fits():
    """The cap only earns its paragraph when it can actually bind."""
    gate = _capped_gate(100, ("notion", 20))
    assert "Tool budget" not in gate.catalog_prompt


def test_reveal_that_fits_is_full_and_stays_under_the_cap():
    gate = _capped_gate(20, ("notion", 10), ("hubspot", 10))
    result = gate.reveal(["integration:notion"])
    assert result.revealed == ["integration:notion"]
    assert not result.partial and not result.evicted
    assert len(gate.visible_tools()) == 12 <= 20


def test_oversized_group_is_revealed_partially_most_important_first():
    """A group larger than the free room is loaded partially, and the tools
    an integration marked ``priority=0`` are the ones that make the cut."""
    tools = [_tool("core_0", "builtin:knowledge")]
    for i in range(30):
        tools.append(
            ToolDefinition(
                name=f"ms_tool_{i}",
                description="d",
                source="integration:microsoft",
                # Three "essential" tools buried in the middle of the group.
                priority=0 if i in (12, 17, 25) else 1,
            )
        )
    gate = build_tool_gate(tools, enabled=True, min_tools=1, max_visible=12)
    assert gate is not None

    result = gate.reveal(["integration:microsoft"])
    # Nothing is fully revealed; 10 of 30 made it (12 - core - load_tools).
    assert result.revealed == []
    assert result.partial == {"integration:microsoft": 10}
    visible = _visible_names(gate)
    assert len(gate.visible_tools()) == 12
    assert {"ms_tool_12", "ms_tool_17", "ms_tool_25"} <= visible
    # Then catalogue order among the default-priority tools.
    assert {"ms_tool_0", "ms_tool_1", "ms_tool_2"} <= visible
    assert "ms_tool_29" not in visible
    assert gate.partially_revealed_group_ids == {"integration:microsoft"}
    assert gate.revealed_group_ids == set()

    text = gate.describe_reveal(result)
    assert "Loaded 10 of 30 tool(s)" in text
    # The remainder is named so the model can reach it.
    assert "ms_tool_29" in text
    assert "`tools` argument" in text


def test_named_tool_load_reaches_the_unloaded_remainder():
    """After a partial reveal the rest of the group is reachable by name: the
    named tool displaces the least important visible tool of its own group
    when nothing else can be unloaded."""
    gate = _capped_gate(6, ("notion", 20))  # 4 free slots
    first = gate.reveal(["integration:notion"])
    assert first.partial == {"integration:notion": 4}
    assert "notion_tool_19" not in _visible_names(gate)

    pinned = gate.reveal([], ["notion_tool_19"])
    assert pinned.pinned == ["notion_tool_19"]
    assert len(pinned.displaced) == 1
    assert "notion_tool_19" in _visible_names(gate)
    assert len(gate.visible_tools()) == 6
    text = gate.describe_reveal(pinned)
    assert "Loaded tool(s) by name: notion_tool_19" in text


def test_reloading_a_partial_group_at_capacity_is_a_no_op_not_a_partial_reveal():
    """Asking again for a group that is already partially loaded, when nothing
    more can be freed for it, is a no-op — NOT another "partial reveal": nothing
    changed, so the result must not say it did, and must not re-list the
    not-loaded names (the first call did; repeating them on every retry would
    bloat the conversation)."""
    gate = _capped_gate(6, ("notion", 20))  # 4 free slots
    first = gate.reveal(["integration:notion"])
    assert first.partial == {"integration:notion": 4}
    before = _visible_names(gate)

    again = gate.reveal(["integration:notion"])
    assert not again.changed
    assert again.partial == {}
    assert again.evicted == []
    assert again.at_capacity == {"integration:notion": 4}
    assert _visible_names(gate) == before

    text = gate.describe_reveal(again)
    assert (
        'No new tools were loaded from "Notion" (integration:notion): '
        "4 of 20 tool(s) are already loaded" in text
    )
    assert "`tools` argument" in text
    # No re-listing of the remainder, and no misleading generic reason.
    assert "Not loaded:" not in text
    assert "notion_tool_19" not in text
    assert "did not match" not in text
    assert "You can now call" not in text


def test_reloading_a_partial_group_takes_room_freed_by_eviction():
    """The at-capacity no-op only applies when nothing can be freed: once a
    group loaded in the SAME call (hence protected then) is evictable, asking
    for the partial group again loads more of it."""
    gate = _capped_gate(12, ("a", 4), ("b", 20))  # 10 free slots
    first = gate.reveal(["integration:a", "integration:b"])
    assert first.revealed == ["integration:a"]
    assert first.partial == {"integration:b": 6}
    assert first.evicted == []  # a was protected within the same call

    again = gate.reveal(["integration:b"])
    assert again.changed
    assert again.evicted == ["integration:a"]
    assert again.partial == {"integration:b": 10}
    assert again.at_capacity == {}
    assert len(gate.visible_tools()) == 12


def test_named_tools_loaded_in_one_call_never_displace_each_other():
    """``load_tools(tools=[A, B])`` must not load A and then drop it to fit B
    when the only room left is inside the tools' own group."""
    gate = _capped_gate(4, ("notion", 20))  # 2 free slots
    first = gate.reveal(["integration:notion"])
    assert first.partial == {"integration:notion": 2}

    result = gate.reveal([], ["notion_tool_10", "notion_tool_11", "notion_tool_12"])
    assert result.pinned == ["notion_tool_10", "notion_tool_11"]
    assert sorted(result.displaced) == ["notion_tool_0", "notion_tool_1"]
    # The third named tool found nothing but this call's own loads to displace.
    assert result.no_room == ["notion_tool_12"]
    visible = _visible_names(gate)
    assert {"notion_tool_10", "notion_tool_11"} <= visible
    assert len(gate.visible_tools()) == 4


def test_named_tool_load_is_lenient_and_reports_unknown_names():
    gate = _capped_gate(50, ("notion", 5))
    result = gate.reveal([], ["NOTION_TOOL_2", "does_not_exist"])
    assert result.pinned == ["notion_tool_2"]
    assert result.unknown_tools == ["does_not_exist"]
    assert "Unknown tool name(s) ignored: does_not_exist" in gate.describe_reveal(result)


def test_full_reveal_evicts_least_recently_used_group_first():
    """Loading a third group that does not fit unloads the LRU one — and the
    model is told which, so it can reload it later."""
    gate = _capped_gate(22, ("a", 10), ("b", 10), ("c", 10))  # 20 free slots
    gate.reveal(["integration:a"])
    gate.reveal(["integration:b"])
    assert len(gate.visible_tools()) == 22

    result = gate.reveal(["integration:c"])
    assert result.revealed == ["integration:c"]
    assert result.evicted == ["integration:a"]
    visible = _visible_names(gate)
    assert any(n.startswith("c_tool_") for n in visible)
    assert any(n.startswith("b_tool_") for n in visible)
    assert not any(n.startswith("a_tool_") for n in visible)
    assert len(gate.visible_tools()) == 22
    text = gate.describe_reveal(result)
    assert 'Unloaded to make room (least recently used first): "A" (integration:a)' in text
    # Evicted groups stay in the catalogue and can be reloaded.
    again = gate.reveal(["integration:a"])
    assert again.revealed == ["integration:a"]
    assert again.evicted == ["integration:b"]


def test_touch_refreshes_recency_so_active_group_survives_eviction():
    gate = _capped_gate(22, ("a", 10), ("b", 10), ("c", 10))
    gate.reveal(["integration:a"])
    gate.reveal(["integration:b"])
    # The model keeps calling into group A: it is the one in active use.
    gate.touch(["a_tool_3"])

    result = gate.reveal(["integration:c"])
    assert result.evicted == ["integration:b"]
    assert any(n.startswith("a_tool_") for n in _visible_names(gate))


def test_a_called_tool_is_never_evicted_by_a_later_load():
    """A run that called a sandbox tool and a GitHub tool, then loads a third
    group that does not fit, keeps both called tools on the wire: only the
    uncalled tools of the least recently used groups make room."""
    gate = _capped_gate(22, ("sandbox", 10), ("github", 10), ("coord", 10))
    gate.reveal(["integration:sandbox"])
    gate.touch(["sandbox_tool_4"])
    gate.reveal(["integration:github"])
    gate.touch(["github_tool_7"])

    result = gate.reveal(["integration:coord"])
    visible = _visible_names(gate)
    assert {"sandbox_tool_4", "github_tool_7"} <= visible
    assert result.evicted == []
    assert result.trimmed == {"integration:sandbox": 9, "integration:github": 1}
    assert result.revealed == ["integration:coord"]
    assert len(gate.visible_tools()) == 22
    assert gate.called_tool_names == ["github_tool_7", "sandbox_tool_4"]
    text = gate.describe_reveal(result)
    assert "Unloaded the tools not called yet to make room" in text
    assert "The tools already called stay loaded." in text


def test_a_called_tool_is_never_displaced_by_a_named_load():
    gate = _capped_gate(4, ("notion", 20))  # 2 free slots
    gate.reveal(["integration:notion"])
    gate.touch(["notion_tool_0", "notion_tool_1"])

    result = gate.reveal([], ["notion_tool_10"])
    assert result.pinned == []
    assert result.no_room == ["notion_tool_10"]
    assert {"notion_tool_0", "notion_tool_1"} <= _visible_names(gate)
    text = gate.describe_reveal(result)
    # The over-budget answer names what fills the budget and the way out.
    assert '"Notion" (integration:notion): 2 loaded, 2 called' in text
    assert "Called tools are never unloaded" in text
    assert "`tools` argument" in text


def test_a_recap_drops_uncalled_tools_before_called_ones():
    gate = _capped_gate(30, ("a", 10), ("b", 10))
    gate.reveal(["integration:a"])
    gate.touch(["a_tool_2"])
    gate.reveal(["integration:b"])
    _evicted, demoted = gate.set_max_visible(8)
    assert demoted == []
    visible = _visible_names(gate)
    assert "a_tool_2" in visible
    assert len(gate.visible_tools()) <= 8


def test_touch_accepts_a_reordered_name_exactly_like_the_executor_does():
    """``tool_a_3`` dispatches to ``a_tool_3`` (token-key recovery), so
    it must refresh A's recency too — otherwise the call succeeds while a
    sibling ``load_tools`` evicts the very group being used."""
    gate = _capped_gate(22, ("a", 10), ("b", 10), ("c", 10))
    gate.reveal(["integration:a"])
    gate.reveal(["integration:b"])
    assert gate.groups_in_use(["tool_a_3"]) == ["integration:a"]
    gate.touch(["tool_a_3"])

    result = gate.reveal(["integration:c"])
    assert result.evicted == ["integration:b"]
    assert any(n.startswith("a_tool_") for n in _visible_names(gate))


def test_touch_accepts_a_known_alias_exactly_like_the_executor_does():
    """``list_github_issues`` is a known alias of ``search_github_issues``
    (declared in ``ToolDefinition.aliases``): the executor runs it, so the gate counts it."""
    tools = [
        _tool("core_0", "builtin:knowledge"),
        _tool("search_github_issues", "integration:github"),
        *_integration_tools("github", 9),
        *_integration_tools("b", 10),
        *_integration_tools("c", 10),
    ]
    gate = build_tool_gate(tools, enabled=True, min_tools=1, max_visible=22)
    assert gate is not None
    gate.reveal(["integration:github"])
    gate.reveal(["integration:b"])
    assert gate.groups_in_use(["list_github_issues"]) == ["integration:github"]
    gate.touch(["list_github_issues"])

    result = gate.reveal(["integration:c"])
    assert result.evicted == ["integration:b"]
    assert "search_github_issues" in _visible_names(gate)


def test_touch_ignores_ambiguous_and_unknown_names_like_the_executor_refuses_them():
    """A token key shared by two tools must touch NOTHING (the executor
    refuses to dispatch it); an unknown name is a no-op.  Loading by such a
    name is reported as unknown for the same reason."""
    tools = [
        _tool("core_0", "builtin:knowledge"),
        _tool("x_y_z", "integration:p"),
        *_integration_tools("p", 9),
        _tool("z_y_x", "integration:q"),
        *_integration_tools("q", 9),
        *_integration_tools("r", 10),
    ]
    gate = build_tool_gate(tools, enabled=True, min_tools=1, max_visible=22)
    assert gate is not None
    gate.reveal(["integration:p"])
    gate.reveal(["integration:q"])  # p is now the least recently used
    assert gate.groups_in_use(["y_x_z", "nope"]) == []
    gate.touch(["y_x_z", "nope"])  # ambiguous + unknown: recency unchanged

    result = gate.reveal(["integration:r"])
    assert result.evicted == ["integration:p"], "an ambiguous name touched nothing"
    by_name = gate.reveal([], ["y_x_z"])
    assert by_name.unknown_tools == ["y_x_z"]
    assert not by_name.changed


def test_gate_accepts_exactly_the_padded_names_the_executor_accepts():
    """The gate hands a model-supplied name to the shared resolver VERBATIM.
    Trimming it would accept a form dispatch refuses: ``" x_y_z "`` is not
    the exact registered name and its token key is shared with ``z_y_x``,
    so the executor refuses it — the gate must not protect or pin it either.
    A padded name whose key is unambiguous is accepted by BOTH through the
    reordered-tokens rule, so nothing legitimate is lost."""
    callable_exec = CallableToolExecutor()
    tools = [
        _tool("core_0", "builtin:knowledge"),
        _tool("x_y_z", "integration:p"),
        _tool("z_y_x", "integration:p"),
        _tool("p_only", "integration:p"),
        *_integration_tools("q", 10),
    ]
    for t in tools:
        callable_exec.register(t.name, lambda **kw: None)
    composite = CompositeToolExecutor()
    composite.add_tools(tools, callable_exec)
    gate = build_tool_gate(tools, enabled=True, min_tools=1, max_visible=40)
    assert gate is not None
    gate.reveal(["integration:p"])
    gate.reveal(["integration:q"])

    for padded in (" x_y_z ", "x_y_z\n"):
        assert composite.name_resolver.canonical(padded) is None, "dispatch refuses it"
        assert gate.groups_in_use([padded]) == [], "so the gate protects nothing"
        by_name = gate.reveal([], [padded])
        assert by_name.unknown_tools == [padded]
    assert composite.name_resolver.canonical(" p_only ") == "p_only"
    assert gate.groups_in_use([" p_only "]) == ["integration:p"]
    # Behaviour check, not just the resolver: the padded ambiguous call did
    # not refresh P, so P (older than Q) is the one evicted next.
    gate.touch([" x_y_z "])
    assert gate.set_max_visible(14)[0] == ["integration:p"]


def test_a_group_named_twice_in_one_call_is_revealed_once():
    """``groups=["notion", "integration:notion"]`` (an alias and the id) or a
    plain repeat names one group twice.  Revealing it twice recorded a
    partial reveal AND an ``at_capacity`` no-op for the same group, so the
    result said both "Loaded 10 of 300" and "No new tools were loaded".
    Resolved ids are deduplicated first; a repeated tool name is pinned once.
    """
    tools = _integration_tools("notion", 300)
    gate = build_tool_gate(tools, enabled=True, min_tools=1, max_visible=11)
    assert gate is not None
    result = gate.reveal(["notion", "integration:notion", "Notion"])
    assert result.partial == {"integration:notion": 10}
    assert result.at_capacity == {}
    assert result.unknown == []
    text = gate.describe_reveal(result)
    assert text.count("Loaded 10 of 300") == 1
    assert "No new tools were loaded" not in text
    assert "are already loaded" not in text

    gate = build_tool_gate(tools, enabled=True, min_tools=1, max_visible=12)
    assert gate is not None
    gate.reveal(["integration:notion"])
    pinned = gate.reveal([], ["notion_tool_299", "notion_tool_299"])
    assert pinned.pinned == ["notion_tool_299"]
    assert len(pinned.displaced) == 1, "one displacement for one pin"
    assert pinned.no_room == []


def test_touch_keeps_exact_name_behaviour_and_ignores_unloaded_groups():
    gate = _capped_gate(22, ("a", 10), ("b", 10), ("c", 10))
    gate.reveal(["integration:a"])
    gate.reveal(["integration:b"])
    # C is not loaded: calling one of its tools is not a recency event.
    assert gate.groups_in_use(["c_tool_0", "a_tool_0", "a_tool_1"]) == ["integration:a"]
    gate.touch(["c_tool_0", "a_tool_0"])
    assert gate.reveal(["integration:c"]).evicted == ["integration:b"]


def test_a_protection_set_carried_across_reveals_shields_the_active_group_and_earlier_loads():
    """One model response = one ``protect`` set.  Seeded with the group in
    active use (A) and extended by each reveal, it keeps A safe from the
    SECOND ``load_tools`` of the response as well — recency alone would not,
    because the first reveal makes its own group the newest.  The request
    that does not fit is reported as ``no_room``, and that paragraph is not
    followed by a contradictory "did not match anything" line."""
    gate = _capped_gate(22, ("a", 10), ("b", 10), ("c", 10), ("d", 10))
    gate.reveal(["integration:a"])
    gate.reveal(["integration:b"])
    protect = set(gate.groups_in_use(["a_tool_0"]))
    assert protect == {"integration:a"}

    first = gate.reveal(["integration:c"], protect=protect)
    assert first.revealed == ["integration:c"]
    assert first.evicted == ["integration:b"]
    assert protect == {"integration:a", "integration:c"}

    second = gate.reveal(["integration:d"], protect=protect)
    assert second.no_room == ["integration:d"]
    assert second.evicted == []
    assert not second.changed
    visible = _visible_names(gate)
    assert any(n.startswith("a_tool_") for n in visible), "the active group survived"
    assert any(n.startswith("c_tool_") for n in visible), "the earlier load survived"
    text = gate.describe_reveal(second)
    assert "Could not load integration:d" in text
    assert "did not match anything" not in text


def test_protected_active_group_survives_a_reveal_larger_than_the_idle_groups():
    """Recency is not protection: with A(10) and B(5) visible, 10 free slots
    and a mixed response calling A while loading C(20), LRU eviction would
    take B and then A too, to fit C fully.  Seeding ``protect`` with the
    active group keeps A; C gets the room B freed plus the free slots — a
    partial reveal — and its remainder stays loadable by name."""
    gate = _capped_gate(27, ("a", 10), ("b", 5), ("c", 20))  # 25 gated slots
    gate.reveal(["integration:a"])
    gate.reveal(["integration:b"])
    assert gate.visible_count == 17  # core + load_tools + 15

    protect = set(gate.groups_in_use(["a_tool_2"]))
    result = gate.reveal(["integration:c"], protect=protect)
    assert result.evicted == ["integration:b"]
    assert result.revealed == []
    assert result.partial == {"integration:c": 15}
    visible = _visible_names(gate)
    assert sum(1 for n in visible if n.startswith("a_tool_")) == 10, "A survived"
    assert sum(1 for n in visible if n.startswith("c_tool_")) == 15
    assert gate.visible_count == 27
    assert 'Loaded 15 of 20 tool(s) from "C"' in gate.describe_reveal(result)


def test_groups_loaded_in_one_call_never_evict_each_other():
    """``load_tools(groups=[A, B])`` must not load A then unload it to fit B."""
    gate = _capped_gate(22, ("a", 10), ("b", 10), ("c", 10))
    gate.reveal(["integration:c"])
    result = gate.reveal(["integration:a", "integration:b"])
    assert result.revealed == ["integration:a", "integration:b"]
    assert result.evicted == ["integration:c"]
    visible = _visible_names(gate)
    assert any(n.startswith("a_tool_") for n in visible)
    assert any(n.startswith("b_tool_") for n in visible)


def test_set_max_visible_lower_evicts_lru_groups_and_keeps_the_core():
    """A mid-run move to a tighter provider (downgrade) re-caps the
    visible list by unloading LRU groups; the always-on core is untouched."""
    gate = _capped_gate(40, ("a", 10), ("b", 10), ("c", 10))
    gate.reveal(["integration:a"])
    gate.reveal(["integration:b"])
    gate.reveal(["integration:c"])
    gate.touch(["a_tool_0"])  # a is now the most recently used
    assert len(gate.visible_tools()) == 32

    evicted, demoted_core = gate.set_max_visible(15)
    assert evicted == ["integration:b", "integration:c"]
    assert demoted_core == []
    assert gate.max_visible == 15
    assert len(gate.visible_tools()) == 12 <= 15
    assert "core_0" in _visible_names(gate)
    assert "Tool budget: this model accepts at most 15" in gate.catalog_prompt


def test_core_that_leaves_no_room_for_a_gated_tool_is_demoted_not_dropped():
    """The gated groups never cost a core tool its slot — but the budget must
    keep the meta-tool AND one gated tool beyond the core, so a core of 3 on
    a cap of 3 keeps its single most important tool always-on and DEMOTES the
    other two into the loadable ``builtin:overflow`` group (retention key
    order).  Nothing is dropped: the catalogue and the meta-tool's enum list
    the group, a gated group can be loaded (partially, into the reserved
    slot) instead of every load answering ``no_room``, and a demoted built-in
    is one ``load_tools`` away by group or by name."""
    tools = [
        ToolDefinition(name="core_hi", description="d", source="builtin:x", priority=0),
        ToolDefinition(name="core_default", description="d", source="builtin:y"),
        ToolDefinition(name="core_lo", description="d", source="builtin:z", priority=2),
        *_integration_tools("notion", 5),
    ]
    gate = build_tool_gate(tools, enabled=True, min_tools=1, max_visible=3)
    assert gate is not None
    # 3 slots: the most important core tool + load_tools + one for a gated tool.
    assert [t.name for t in gate.demoted_always_on] == ["core_default", "core_lo"]
    assert _visible_names(gate) == {"core_hi", LOAD_TOOLS_TOOL_NAME}
    assert gate.min_visible_count == 2 and gate.required_budget == 3
    assert f'"{CORE_OVERFLOW_GROUP_ID}"' in gate.catalog_prompt
    assert "core_default, core_lo" in gate.catalog_prompt
    # #2191: the demotion is told to the model, every tool named, with the call.
    assert "Moved out of your always-on tools for this run" in gate.catalog_prompt
    assert f'groups=["{CORE_OVERFLOW_GROUP_ID}"]' in gate.catalog_prompt
    enum = gate.load_tools_definition.input_schema["properties"]["groups"]["items"]["enum"]
    assert enum == [CORE_OVERFLOW_GROUP_ID, "integration:notion"]
    assert gate.gated_tool_count == 7
    # A gated group IS loadable — partially, into the reserved slot.
    result = gate.reveal(["integration:notion"])
    assert result.partial == {"integration:notion": 1} and result.no_room == []
    assert len(gate.visible_tools()) == 3
    # The demoted built-ins are reachable like any gated tool: by group...
    result = gate.reveal([CORE_OVERFLOW_GROUP_ID])
    assert result.evicted == ["integration:notion"]
    assert result.partial == {CORE_OVERFLOW_GROUP_ID: 1}
    assert "core_default" in _visible_names(gate), "most important first"
    # ...and by exact name, displacing the less important one of its group.
    result = gate.reveal([], ["core_lo"])
    assert result.pinned == ["core_lo"] and result.displaced == ["core_default"]
    assert _visible_names(gate) == {"core_hi", LOAD_TOOLS_TOOL_NAME, "core_lo"}
    # Naming an always-on tool is a no-op, not an unknown name: it is on the
    # wire already.
    result = gate.reveal([], ["core_hi"])
    assert result.unknown_tools == [] and not result.changed
    assert "Unknown tool name" not in gate.describe_reveal(result)


def test_the_budget_always_keeps_a_slot_for_a_gated_tool():
    """A core of ``max_visible - 1`` used to fill the visible list on its own:
    reveals cannot evict core entries, so every group and named load came
    back ``no_room`` while the catalogue kept advertising them.  The core is
    now trimmed to ``max_visible - 2`` (one slot for ``load_tools``, one for a
    gated tool) and the trimmed built-in stays loadable."""
    gate = _capped_gate(8, ("a", 5), ("b", 5), ("c", 5), core=7)
    assert len(gate.visible_tools()) == 7 <= 8 - 1
    assert [t.name for t in gate.demoted_always_on] == ["core_6"], "retention order: last in"
    assert gate.group_count == 4 and gate.gated_tool_count == 16
    assert f'"{CORE_OVERFLOW_GROUP_ID}"' in gate.catalog_prompt
    assert "1 of which are free" in gate.catalog_prompt
    result = gate.reveal(["integration:a"])
    assert result.partial == {"integration:a": 1} and result.no_room == []
    assert len(gate.visible_tools()) == 8
    result = gate.reveal([], ["core_6"])
    assert result.pinned == ["core_6"] and result.evicted == ["integration:a"]
    assert "core_6" in _visible_names(gate)
    assert gate.groups_in_use(["core_6"]) == [CORE_OVERFLOW_GROUP_ID], "gated now"
    assert gate.groups_in_use(["core_0"]) == [], "still always-on: not a recency event"


def test_demotion_notice_names_every_demoted_tool_up_to_the_bound() -> None:
    """#2191: the overflow group's catalogue line samples a few names; the
    demotion line names each always-on tool moved (bounded), so an agent never
    finds one of its always-on tools silently missing."""
    gate = _capped_gate(8, ("a", 5), core=12)
    demoted = [t.name for t in gate.demoted_always_on]
    assert len(demoted) == 6
    notice = next(line for line in gate.catalog_prompt.splitlines() if line.startswith("Moved out of"))
    for name in demoted:
        assert name in notice
    assert LOAD_TOOLS_TOOL_NAME in notice


def test_no_demotion_notice_when_the_core_fits() -> None:
    gate = _capped_gate(8, ("a", 5), core=6)
    assert gate.demoted_always_on == []
    assert "Moved out of your always-on tools" not in gate.catalog_prompt


def test_core_exactly_two_below_the_cap_is_untouched():
    """``max_visible - 2`` core tools fit with the meta-tool and a gated slot:
    nothing is demoted and no overflow group appears."""
    gate = _capped_gate(8, ("a", 5), core=6)
    assert gate.demoted_always_on == []
    assert CORE_OVERFLOW_GROUP_ID not in gate.catalog_prompt
    assert gate.group_count == 1
    assert len(gate.visible_tools()) == 7 and gate.required_budget == 8
    assert gate.reveal(["integration:a"]).partial == {"integration:a": 1}
    # A re-cap that leaves no gated slot demotes (defensively — the loop's
    # downgrade guard never lets a gated run reach such a cap).
    evicted, demoted = gate.set_max_visible(7)
    assert evicted == ["integration:a"] and demoted == ["core_5"]
    assert f'"{CORE_OVERFLOW_GROUP_ID}"' in gate.catalog_prompt
    assert gate.reveal([], ["core_5"]).pinned == ["core_5"]


def test_gate_and_dispatch_resolve_a_name_to_the_same_tool():
    """The gate resolves over the WHOLE offered surface with the dispatch
    map's own resolver, so a call name denotes the same tool to both: an
    exact always-on name wins over a token-equivalent gated one (core
    ``jira_search_issues`` vs gated ``search_jira_issues`` — the gate used to
    resolve the core call to the gated tool and falsely protect its group), a
    name ambiguous between a core and a gated tool is refused by both, and an
    alias follows its canonical through a sanitisation collision in both.  A
    gate built stand-alone (no shared resolver, the translated alias table
    instead) must agree too."""
    originals = [
        _tool("jira_search_issues", "builtin:jira_shortcut"),
        _tool("z_y_x", "builtin:other"),
        _tool("search_jira_issues", "integration:jira"),
        *_integration_tools("jira", 3),
        _tool("x_y_z", "integration:p"),
        _tool("search.github.issues", "integration:gh"),
        _tool("search_github_issues", "integration:gh"),
    ]
    pairs = build_llm_tool_names([t.name for t in originals])
    visible_defs = [
        dataclasses.replace(t, name=visible)
        for t, (_original, visible) in zip(originals, pairs, strict=True)
    ]
    renamed = {visible: original for original, visible in pairs if visible != original}
    aliases = visible_aliases(dict(pairs), declared_aliases(originals))
    name_map = al._ExecutorNameMap(renamed, [t.name for t in visible_defs], aliases=aliases)
    shared = build_tool_gate(
        visible_defs, enabled=True, min_tools=1, max_visible=64, resolver=name_map.resolver
    )
    standalone = build_tool_gate(
        visible_defs, enabled=True, min_tools=1, max_visible=64, aliases=aliases
    )
    assert shared is not None and standalone is not None
    # (call name, canonical visible name dispatch executes, gated group or None)
    expectations = [
        ("jira_search_issues", "jira_search_issues", None),  # exact core wins
        ("search_jira_issues", "search_jira_issues", "integration:jira"),
        ("issues_jira_search", None, None),  # ambiguous: core AND gated share the key
        ("y_x_z", None, None),  # ambiguous: core z_y_x vs gated x_y_z
        ("x_y_z", "x_y_z", "integration:p"),
        ("list_github_issues", "search_github_issues_2", "integration:gh"),  # alias
        # Reordered: only the un-suffixed visible name shares the token key
        # (the ``_2`` suffix is a token), so this is `search.github.issues`.
        ("issues_github_search", "search_github_issues", "integration:gh"),
        ("nope", None, None),
    ]
    for gate in (shared, standalone):
        gate.reveal(["integration:jira", "integration:p", "integration:gh"])
        for name, canonical, group in expectations:
            assert name_map.canonical_visible(name) == canonical, name
            assert name_map.get(name, name) == (
                renamed.get(canonical, canonical) if canonical else name
            ), name
            assert gate.groups_in_use([name]) == ([group] if group else []), name
            result = gate.reveal([], [name])
            assert result.unknown_tools == ([name] if canonical is None else []), name
            assert not result.changed, name


def test_partial_reveal_lists_every_not_loaded_name():
    """The not-loaded names are the model's ONLY handle on the rest of an
    oversized group (``tools=[...]`` needs the exact name, and re-requesting
    the group is a no-op once at capacity), so the result spells out every
    one of them — no ``and N more`` summary, whatever the group size."""
    tools = _integration_tools("notion", 300)
    gate = build_tool_gate(tools, enabled=True, min_tools=1, max_visible=11)
    assert gate is not None
    result = gate.reveal(["integration:notion"])
    assert result.partial == {"integration:notion": 10}
    text = gate.describe_reveal(result)
    loaded = {t.name for t in gate.visible_tools()}
    missing = [t.name for t in tools if t.name not in loaded]
    assert len(missing) == 290
    assert all(name in text for name in missing)
    assert " more" not in text


def test_search_finds_not_loaded_names_and_tells_loaded_ones_apart():
    """``search`` is the durable way back to a remainder: it returns the exact
    not-loaded names (most important first, with their group) and separately
    says which matches are already callable.  Read-only — nothing moves."""
    tools = _integration_tools("notion", 300)
    gate = build_tool_gate(tools, enabled=True, min_tools=1, max_visible=11)
    assert gate is not None
    gate.reveal(["integration:notion"])
    visible_before = [t.name for t in gate.visible_tools()]

    result = gate.search("tool_29")
    assert result.scope == []
    names = [name for name, _gid in result.not_loaded]
    # 29, 290..299 minus whatever the partial reveal already loaded.
    assert "notion_tool_299" in names and "notion_tool_290" in names
    assert all(gid == "integration:notion" for _n, gid in result.not_loaded)
    assert not (set(names) & set(visible_before))
    assert [t.name for t in gate.visible_tools()] == visible_before, "read-only"
    text = gate.describe_search(result)
    assert 'Not-loaded tool(s) matching "tool_29"' in text
    assert "notion_tool_299 (integration:notion)" in text
    assert "`tools` argument" in text

    # A tool that IS loaded is reported as callable, not offered for loading.
    loaded_name = next(n for n in visible_before if n.startswith("notion_tool_"))
    result = gate.search(loaded_name)
    assert result.loaded == [loaded_name]
    assert result.not_loaded == []
    text = gate.describe_search(result)
    assert "No not-loaded tool matches" in text
    assert f"Already loaded (call directly): {loaded_name}" in text


def test_search_is_bounded_and_scoped_and_matches_every_word():
    """A wide match is capped (the model narrows it, the context is never
    re-inflated), ``groups`` scopes the search, every query word must match
    (name OR description, case-insensitive), and an empty query is a no-op."""
    tools = [
        *_integration_tools("notion", 300),
        ToolDefinition(
            name="github_create_issue",
            description="Create a GitHub issue in a repository",
            source="integration:github",
        ),
        *_integration_tools("github", 5),
    ]
    gate = build_tool_gate(tools, enabled=True, min_tools=1, max_visible=11)
    assert gate is not None

    wide = gate.search("notion")
    assert wide.not_loaded_total == 300
    assert len(wide.not_loaded) == _SEARCH_RESULT_LIMIT
    text = gate.describe_search(wide)
    assert f"({_SEARCH_RESULT_LIMIT} of 300)" in text
    assert "Narrow the search" in text
    assert len(text) < al._STALE_TOOL_RESULT_MIN_CHARS * 6, "bounded output"

    scoped = gate.search("tool_1", ["integration:github"])
    assert scoped.scope == ["integration:github"]
    assert [n for n, _g in scoped.not_loaded] == ["github_tool_1"]
    assert '"GitHub" (integration:github)' in gate.describe_search(scoped)

    # Words match name OR description, all of them, case-insensitively.
    by_description = gate.search("Repository CREATE")
    assert [n for n, _g in by_description.not_loaded] == ["github_create_issue"]
    assert gate.search("create nonexistent").not_loaded == []

    assert gate.search("   ").not_loaded == []
    assert gate.describe_search(gate.search("   ")) == "Empty `search` ignored."
    # Punctuation in the query is a separator, never a miss.
    assert [n for n, _g in gate.search("Repository, CREATE.").not_loaded] == [
        "github_create_issue"
    ]
    assert gate.search(", . ;").not_loaded == []
    assert gate.describe_search(gate.search(", . ;")).startswith(
        'No not-loaded tool matches ", . ;"'
    )


def test_search_scoped_to_unknown_groups_searches_nothing():
    """``groups`` present means scoped, literally: when none of the tokens
    resolves the search covers NOTHING and reports the unknown tokens -
    falling back to the whole catalogue would answer a misspelled group with
    unrelated tools.  No ``groups`` still searches everything, and a mix of
    valid and unknown tokens searches only the valid ones."""
    tools = [*_integration_tools("notion", 20), *_integration_tools("github", 5)]
    gate = build_tool_gate(tools, enabled=True, min_tools=1, max_visible=11)
    assert gate is not None

    nothing = gate.search("tool_1", ["nope"])
    assert nothing.scope == [] and nothing.unknown == ["nope"]
    assert nothing.not_loaded == [] and nothing.not_loaded_total == 0
    assert nothing.loaded == []
    text = gate.describe_search(nothing)
    assert 'Unknown group(s) for `search`: "nope"' in text
    assert "Nothing was searched" in text and "omit `groups`" in text
    assert "notion_tool_1" not in text and "github_tool_1" not in text

    everywhere = gate.search("tool_1")
    assert everywhere.scope == [] and everywhere.unknown == []
    assert {n for n, _g in everywhere.not_loaded} >= {"notion_tool_1", "github_tool_1"}
    # ``[]`` from the loop (no ``groups`` argument) means the same as None.
    assert gate.search("tool_1", []).not_loaded == everywhere.not_loaded

    partial = gate.search("tool_1", ["integration:github", "nope"])
    assert partial.scope == ["integration:github"] and partial.unknown == ["nope"]
    assert [n for n, _g in partial.not_loaded] == ["github_tool_1"]
    text = gate.describe_search(partial)
    assert 'Unknown group(s) for `search`: "nope"' in text
    assert "Nothing was searched" not in text
    assert "github_tool_1 (integration:github)" in text


def test_gating_needs_a_budget_for_the_meta_tool_plus_one_real_tool():
    """A gate always sends ``load_tools``, so on ``max_tools == 0`` it would
    put one schema on a wire that allows none, and on ``max_tools == 1`` it
    could never reveal anything.  Below ``MIN_GATED_BUDGET`` no gate is
    built (the legacy path truncates to the cap) and a live gate refuses to
    be re-capped there."""
    tools = _integration_tools("notion", 30)
    assert MIN_GATED_BUDGET == 2
    assert build_tool_gate(tools, enabled=True, min_tools=1, max_visible=0) is None
    assert build_tool_gate(tools, enabled=True, min_tools=1, max_visible=1) is None
    gate = build_tool_gate(tools, enabled=True, min_tools=1, max_visible=2)
    assert gate is not None
    assert [t.name for t in gate.visible_tools()] == [LOAD_TOOLS_TOOL_NAME]
    assert gate.reveal(["integration:notion"]).partial == {"integration:notion": 1}
    assert len(gate.visible_tools()) == 2
    with pytest.raises(ValueError, match="minimum 2"):
        gate.set_max_visible(1)
    assert gate.max_visible == 2, "a refused re-cap changes nothing"
    assert gate.set_max_visible(None) == ([], [])


def test_search_ranks_every_match_before_cutting_to_the_limit():
    """The cap is "least important first", never "later group first": a
    ``priority=0`` tool in the LAST group searched must not be hidden behind
    forty default-priority matches from the first.  Every match across the
    searched groups is ranked by the shared retention key before the slice,
    and the cap is still honoured afterwards."""
    tools = [
        # 60 default-priority matches in the first group, declared first.
        *_integration_tools("notion", 60),
        # One top-priority match in a LATER group, plus filler behind it.
        ToolDefinition(
            name="github_tool_hi",
            description="d",
            source="integration:github",
            priority=0,
        ),
        *_integration_tools("github", 5),
    ]
    gate = build_tool_gate(tools, enabled=True, min_tools=1, max_visible=11)
    assert gate is not None

    result = gate.search("tool")
    assert result.not_loaded_total == 66
    assert len(result.not_loaded) == _SEARCH_RESULT_LIMIT, "cap still honoured"
    assert result.not_loaded[0] == ("github_tool_hi", "integration:github")
    # Same rank -> catalogue order (group order, then declaration), exactly as
    # ``ToolGroup.ranked`` orders a partial reveal.
    assert [n for n, _g in result.not_loaded[1:]] == [
        f"notion_tool_{i}" for i in range(_SEARCH_RESULT_LIMIT - 1)
    ]
    text = gate.describe_search(result)
    assert text.index("github_tool_hi") < text.index("notion_tool_0")
    assert f"({_SEARCH_RESULT_LIMIT} of 66)" in text

    # Scoping to the later group alone still finds it first.
    scoped = gate.search("tool", ["integration:github"])
    assert [n for n, _g in scoped.not_loaded][:2] == ["github_tool_hi", "github_tool_0"]


def test_search_caps_the_already_loaded_matches_too():
    """Loaded matches are a reminder, not a handle (their schemas are already
    on the wire), so a broad search after a large reveal must not re-list
    hundreds of visible names: the list is cut at ``_SEARCH_LOADED_LIMIT``
    and the cut is announced."""
    tools = _integration_tools("notion", 100)
    gate = build_tool_gate(tools, enabled=True, min_tools=1, max_visible=200)
    assert gate is not None
    gate.reveal(["integration:notion"])
    assert len(gate.visible_tools()) == 101

    result = gate.search("notion")
    assert result.not_loaded == [] and result.not_loaded_total == 0
    assert result.loaded_total == 100
    assert len(result.loaded) == _SEARCH_LOADED_LIMIT
    assert result.loaded == [f"notion_tool_{i}" for i in range(_SEARCH_LOADED_LIMIT)]
    text = gate.describe_search(result)
    assert f"+{100 - _SEARCH_LOADED_LIMIT} more loaded match(es)" in text
    assert "notion_tool_99" not in text
    assert text.count("notion_tool_") == _SEARCH_LOADED_LIMIT
    assert len(text) < al._STALE_TOOL_RESULT_MIN_CHARS, "a reminder, not a listing"

    # Under the cap there is no marker to mislead the model about a cut.
    few = gate.search("tool_9")
    assert few.loaded_total == len(few.loaded) == 11  # 9, 90..99
    assert "more loaded match" not in gate.describe_search(few)


def test_search_names_the_always_on_tools_it_matches():
    """An unscoped search covers the always-on core too, and reports its
    matches as callable: answering only "No not-loaded tool matches" for a
    core tool made a model conclude the tool did not exist while its schema
    was in the request.  A scoped search stays literal and skips the core."""
    tools = [
        ToolDefinition(
            name="workset_add_item",
            description="Put one document into the working corpus.",
            source="builtin:workset",
        ),
        ToolDefinition(
            name="workset_seal",
            description="Declare that you have finished ADDING items.",
            source="builtin:workset",
        ),
        ToolDefinition(name="explain_opencti_filters", description="d", source="builtin:help"),
        _tool("search_memory", "builtin:memory"),
        *_integration_tools("opencti", 20),
    ]
    gate = build_tool_gate(tools, enabled=True, min_tools=1, max_visible=50)
    assert gate is not None
    visible = [t.name for t in gate.visible_tools()]
    assert "workset_add_item" in visible and "workset_seal" in visible

    result = gate.search("workset")
    assert result.loaded == ["workset_add_item", "workset_seal"]
    assert result.loaded_total == 2
    assert result.not_loaded == [] and result.not_loaded_total == 0
    assert gate.describe_search(result) == (
        'No not-loaded tool matches "workset". '
        "Already loaded (call directly): workset_add_item, workset_seal."
    )
    assert [t.name for t in gate.visible_tools()] == visible, "read-only"

    # A query matching the core and a gated group reports each where it is.
    both = gate.search("opencti")
    assert both.loaded == ["explain_opencti_filters"]
    assert both.not_loaded_total == 20

    # ``groups`` given: only those groups, never the core.
    scoped = gate.search("workset", ["integration:opencti"])
    assert scoped.loaded == [] and scoped.loaded_total == 0
    assert scoped.not_loaded == []
    assert "workset_add_item" not in gate.describe_search(scoped)


def test_search_lists_the_core_after_the_revealed_groups_under_the_cap():
    """Core tools whose description merely mentions the keyword come after the
    revealed groups' matches, so they never push those out of the cap."""
    tools = [
        *(
            ToolDefinition(name=f"helper_{i}", description="Works with OpenCTI.", source="builtin:help")
            for i in range(_SEARCH_LOADED_LIMIT)
        ),
        *_integration_tools("opencti", 5),
    ]
    gate = build_tool_gate(tools, enabled=True, min_tools=1, max_visible=100)
    assert gate is not None
    gate.reveal(["integration:opencti"])

    result = gate.search("opencti")
    assert result.loaded_total == 5 + _SEARCH_LOADED_LIMIT
    assert result.loaded == [
        *(f"opencti_tool_{i}" for i in range(5)),
        *(f"helper_{i}" for i in range(_SEARCH_LOADED_LIMIT - 5)),
    ]
    assert "+5 more loaded match(es)" in gate.describe_search(result)


def test_at_capacity_retry_and_unknown_tool_point_to_search():
    """The at-capacity retry deliberately omits the remainder names; since the
    first listing is not durable in the context, the retry must say how to
    get a name back — and so must an unknown-name refusal."""
    tools = _integration_tools("notion", 300)
    gate = build_tool_gate(tools, enabled=True, min_tools=1, max_visible=11)
    assert gate is not None
    gate.reveal(["integration:notion"])
    retry = gate.reveal(["integration:notion"])
    assert retry.at_capacity == {"integration:notion": 10}
    text = gate.describe_reveal(retry)
    assert "notion_tool_299" not in text, "the retry stays terse"
    assert "`search` argument" in text

    unknown = gate.reveal([], ["notion_tool_two_ninety_nine"])
    assert unknown.unknown_tools == ["notion_tool_two_ninety_nine"]
    assert "`search` argument" in gate.describe_reveal(unknown)


def test_load_tools_schema_exposes_search_without_requiring_anything():
    tools = _integration_tools("notion", 20)
    _always_on, groups = group_tools(tools)
    definition = build_load_tools_definition(groups)
    props = definition.input_schema["properties"]
    assert set(props) == {"groups", "tools", "search"}
    assert props["search"]["type"] == "string"
    assert "required" not in definition.input_schema
    assert "'search'" in definition.description


# ─────────────────────────────────────────────────────────────────────────
# Integration: run_agent_loop with gating
# ─────────────────────────────────────────────────────────────────────────


class _ScriptedLLM(BaseLLMProvider):
    """Stub LLM that returns a pre-scripted sequence of responses and records
    the tool names it was offered on each ``chat`` call."""

    provider_name = "stub"
    context_window = 100_000

    def __init__(self, script: list[ChatResponse]) -> None:
        self._script = script
        self._idx = 0
        self.offered_tools: list[list[str]] = []

    async def chat(self, messages, model=None, tools=None, **kwargs):
        self.offered_tools.append([t.name for t in (tools or [])])
        response = self._script[self._idx]
        self._idx += 1
        return response

    async def chat_stream(self, messages, **kwargs):
        yield ""

    async def embed(self, text, model=None):
        return [0.0]

    async def list_models(self):
        return []


@pytest.fixture
def _quiet_langfuse(monkeypatch):
    """Neutralise Langfuse plumbing so the loop runs without real tracing."""
    monkeypatch.setattr(al, "_lf_start_llm_gen", lambda *a, **kw: None)
    monkeypatch.setattr(al, "_lf_end_generation", lambda *a, **kw: None)
    monkeypatch.setattr(al, "_lf_end_span", lambda *a, **kw: None)
    monkeypatch.setattr(al, "_lf_end_tool_span", lambda *a, **kw: None)
    monkeypatch.setattr(al, "_lf_start_iter_span", lambda **kw: None)
    monkeypatch.setattr(al, "_lf_start_tool_span", lambda **kw: None)


def _executor_with_n_notion(n: int) -> tuple[CompositeToolExecutor, list[str]]:
    """Composite executor with *n* notion tools. Returns the executor and a
    list that records every tool name actually executed."""
    executed: list[str] = []
    callable_exec = CallableToolExecutor()
    tools = _integration_tools("notion", n)

    def _make_handler(tool_name: str):
        async def _handler(**kwargs):
            executed.append(tool_name)
            return f"ran {tool_name}"

        return _handler

    for t in tools:
        callable_exec.register(t.name, _make_handler(t.name))

    composite = CompositeToolExecutor()
    composite.add_tools(tools, callable_exec)
    return composite, executed


def _executor_with_notion() -> tuple[CompositeToolExecutor, list[str]]:
    """Composite executor with 18 notion tools (>= threshold)."""
    return _executor_with_n_notion(18)


@pytest.mark.asyncio
async def test_simple_turn_does_not_offer_gated_tools(_quiet_langfuse):
    """A "hello"-style turn (no tool calls) must be answered while only the
    always-on core + ``load_tools`` meta-tool are offered — the 18 gated
    integration schemas are NOT sent, which is the whole cost win."""
    composite, executed = _executor_with_notion()
    llm = _ScriptedLLM([ChatResponse(content="Hi there!", model="stub")])

    response = await al.run_agent_loop(
        llm=llm,
        messages=[ChatMessage(role="user", content="hello")],
        tools=composite.all_tools,
        executor=composite,
        max_iterations=5,
        progressive_tools=True,
    )

    assert response.content == "Hi there!"
    assert executed == [], "no tool should run for a greeting"
    assert len(llm.offered_tools) == 1
    offered = llm.offered_tools[0]
    assert LOAD_TOOLS_TOOL_NAME in offered
    assert not any(name.startswith("notion_tool_") for name in offered), (
        "gated integration tools must NOT be sent on a simple turn"
    )


@pytest.mark.asyncio
async def test_load_tools_reveals_group_then_tool_executes(_quiet_langfuse):
    """The model loads the notion group, then on the next iteration it can see
    and call a notion tool. ``load_tools`` itself never routes to the executor."""
    composite, executed = _executor_with_notion()
    llm = _ScriptedLLM(
        [
            # Iteration 1: ask to load the notion group.
            ChatResponse(
                content="",
                model="stub",
                stop_reason="tool_calls",
                tool_calls=[
                    ToolCall(
                        id="c1",
                        name=LOAD_TOOLS_TOOL_NAME,
                        arguments={"groups": ["integration:notion"]},
                    )
                ],
            ),
            # Iteration 2: now call a real (revealed) notion tool.
            ChatResponse(
                content="",
                model="stub",
                stop_reason="tool_calls",
                tool_calls=[
                    ToolCall(id="c2", name="notion_tool_3", arguments={"q": "x"})
                ],
            ),
            # Iteration 3: final answer.
            ChatResponse(content="Found it.", model="stub"),
        ]
    )

    messages: list[ChatMessage] = [
        ChatMessage(role="user", content="search my notion")
    ]
    response = await al.run_agent_loop(
        llm=llm,
        messages=messages,
        tools=composite.all_tools,
        executor=composite,
        max_iterations=6,
        progressive_tools=True,
    )

    assert response.content == "Found it."
    # The real tool ran exactly once; the meta-tool never reached the executor.
    assert executed == ["notion_tool_3"]

    # Iteration 1 offered the meta-tool but not the gated tools.
    assert not any(n.startswith("notion_tool_") for n in llm.offered_tools[0])
    # Iteration 2 (after load_tools) DID offer the revealed notion tools.
    assert any(n.startswith("notion_tool_") for n in llm.offered_tools[1])

    # The conversation carries a tool result for the load_tools call so the
    # provider's tool_call/result pairing stays valid.
    load_results = [
        m
        for m in messages
        if getattr(m, "role", None) == "tool"
        and getattr(m, "tool_call_id", None) == "c1"
    ]
    assert len(load_results) == 1
    assert "Loaded 18 tool(s)" in load_results[0].content


@pytest.mark.asyncio
async def test_disabled_progressive_tools_offers_everything(_quiet_langfuse):
    """With progressive disclosure off, the legacy behaviour holds: every tool
    is offered up-front (no ``load_tools`` meta-tool, all schemas sent)."""
    composite, _ = _executor_with_notion()
    llm = _ScriptedLLM([ChatResponse(content="hi", model="stub")])

    await al.run_agent_loop(
        llm=llm,
        messages=[ChatMessage(role="user", content="hello")],
        tools=composite.all_tools,
        executor=composite,
        max_iterations=3,
        progressive_tools=False,
    )

    offered = llm.offered_tools[0]
    assert LOAD_TOOLS_TOOL_NAME not in offered
    assert sum(1 for n in offered if n.startswith("notion_tool_")) == 18


@pytest.mark.asyncio
async def test_visible_tools_never_exceed_provider_max_tools(_quiet_langfuse):
    """With gating active, the on-the-wire tool list (always-on core +
    ``load_tools`` + revealed groups) must never exceed the provider's
    ``max_tools`` — even after a group larger than the budget is revealed.
    The gate enforces the cap on the VISIBLE list: the group is
    revealed partially and the inventory is left whole."""
    # 30 gateable tools, provider cap 20: 19 fit next to the meta-tool, never
    # 21 — and the 11 that did not fit are named to the model.
    composite, _ = _executor_with_n_notion(30)
    llm = _ScriptedLLM(
        [
            ChatResponse(
                content="",
                model="stub",
                stop_reason="tool_calls",
                tool_calls=[
                    ToolCall(
                        id="c1",
                        name=LOAD_TOOLS_TOOL_NAME,
                        arguments={"groups": ["integration:notion"]},
                    )
                ],
            ),
            ChatResponse(content="done", model="stub"),
        ]
    )
    llm.max_tools = 20

    messages: list[ChatMessage] = [ChatMessage(role="user", content="use notion")]
    await al.run_agent_loop(
        llm=llm,
        messages=messages,
        tools=composite.all_tools,
        executor=composite,
        max_iterations=5,
        progressive_tools=True,
    )

    assert llm.offered_tools, "expected at least one chat call"
    offered_counts = [len(o) for o in llm.offered_tools]
    assert all(c <= 20 for c in offered_counts), (
        f"visible tool list exceeded max_tools (20): {offered_counts}"
    )
    # After loading the group the cap is fully used (19 tools + meta = 20).
    assert max(offered_counts) == 20
    load_result = next(
        m for m in messages if getattr(m, "tool_call_id", None) == "c1"
    )
    assert "Loaded 19 of 30 tool(s)" in load_result.content
    # No tool was truncated away from the run: the legacy notice is gone.
    assert not any(
        "unavailable this run" in (m.content or "")
        for m in messages
        if getattr(m, "role", None) == "system"
    )


def _executor_with_groups(
    *provider_counts: tuple[str, int],
) -> tuple[CompositeToolExecutor, list[str]]:
    """Composite executor with several integration groups; records executions."""
    executed: list[str] = []
    callable_exec = CallableToolExecutor()
    tools: list[ToolDefinition] = []
    for provider, n in provider_counts:
        tools.extend(_integration_tools(provider, n))

    def _make_handler(tool_name: str):
        async def _handler(**kwargs):
            executed.append(tool_name)
            return f"ran {tool_name}"

        return _handler

    for t in tools:
        callable_exec.register(t.name, _make_handler(t.name))
    composite = CompositeToolExecutor()
    composite.add_tools(tools, callable_exec)
    return composite, executed


def _load(call_id: str, **arguments) -> ChatResponse:
    return ChatResponse(
        content="",
        model="stub",
        stop_reason="tool_calls",
        tool_calls=[ToolCall(id=call_id, name=LOAD_TOOLS_TOOL_NAME, arguments=arguments)],
    )


def _call(call_id: str, name: str) -> ChatResponse:
    return ChatResponse(
        content="",
        model="stub",
        stop_reason="tool_calls",
        tool_calls=[ToolCall(id=call_id, name=name, arguments={})],
    )


@pytest.mark.asyncio
async def test_provider_cap_never_truncates_the_inventory_when_gating_is_active(
    _quiet_langfuse,
):
    """The regression, scaled down: 8 integration groups (120 tools) on
    a 20-tool provider.  The old inventory truncation kept the 19 lowest
    retention keys — all from the first group — and deleted the other seven
    from the catalogue, so ``load_tools`` could not name them and a system
    notice told the model they were "unavailable this run".  Now every group
    is in the catalogue, the LAST one loads and its tool runs, loading another
    unloads the least recently used group, and the wire never carries more
    than 20 schemas."""
    providers = [(f"prov{i}", 15) for i in range(8)]
    composite, executed = _executor_with_groups(*providers)
    llm = _ScriptedLLM(
        [
            _load("c1", groups=["integration:prov7"]),
            _call("c2", "prov7_tool_3"),
            _load("c3", groups=["integration:prov2"]),
            ChatResponse(content="done", model="stub"),
        ]
    )
    llm.max_tools = 20

    messages: list[ChatMessage] = [
        ChatMessage(role="user", content="what is on my calendar next week?")
    ]
    response = await al.run_agent_loop(
        llm=llm,
        messages=messages,
        tools=composite.all_tools,
        executor=composite,
        max_iterations=8,
        progressive_tools=True,
    )

    assert response.content == "done"
    assert executed == ["prov7_tool_3"]

    system_texts = [
        m.content or "" for m in messages if getattr(m, "role", None) == "system"
    ]
    catalogue = next(t for t in system_texts if "Available tool groups" in t)
    for i in range(8):
        assert f'"integration:prov{i}"' in catalogue, "every group stays in the catalogue"
    assert "Tool budget: this model accepts at most 20 tool schemas" in catalogue
    assert not any("unavailable this run" in t for t in system_texts)

    offered_counts = [len(o) for o in llm.offered_tools]
    assert all(c <= 20 for c in offered_counts), offered_counts
    # Turn 1: only the meta-tool (+ the built-in timer) — no group schemas.
    assert not any(n.startswith("prov") for n in llm.offered_tools[0])
    # Turn 2: prov7 is on the wire in full.
    assert sum(1 for n in llm.offered_tools[1] if n.startswith("prov7_")) == 15
    # Turn 4: prov2 is loaded in full; prov7 (LRU) gave up only the uncalled
    # tools the load needed, and the tool the run called stays on the wire.
    assert sum(1 for n in llm.offered_tools[3] if n.startswith("prov2_")) == 15
    assert "prov7_tool_3" in llm.offered_tools[3]
    assert len(llm.offered_tools[3]) == 20

    second_load = next(m for m in messages if getattr(m, "tool_call_id", None) == "c3")
    assert "Loaded 15 tool(s)" in second_load.content
    assert 'Unloaded the tools not called yet to make room: "Prov7"' in second_load.content


@pytest.mark.asyncio
async def test_oversized_group_stays_loadable_and_completable_by_name(_quiet_langfuse):
    """A group larger than the whole budget used to be cut to the budget AND
    disable gating when the cut pushed it under the threshold (the reserved
    ``load_tools`` slot was then reclaimed).  Gating now stays active whatever
    the cap: the group is revealed partially, the remainder is named, and a
    named load reaches it — so all 20 tools are reachable on a 16-tool
    provider, not 16 of them forever."""
    composite, executed = _executor_with_n_notion(20)
    llm = _ScriptedLLM(
        [
            _load("c1", groups=["integration:notion"]),
            _load("c2", tools=["notion_tool_19"]),
            _call("c3", "notion_tool_19"),
            ChatResponse(content="done", model="stub"),
        ]
    )
    llm.max_tools = 16

    messages: list[ChatMessage] = [ChatMessage(role="user", content="use notion")]
    await al.run_agent_loop(
        llm=llm,
        messages=messages,
        tools=composite.all_tools,
        executor=composite,
        max_iterations=8,
        progressive_tools=True,
    )

    assert executed == ["notion_tool_19"]
    offered_counts = [len(o) for o in llm.offered_tools]
    assert all(c <= 16 for c in offered_counts), offered_counts
    # The meta-tool stays offered on every turn — gating was never disabled.
    assert all(LOAD_TOOLS_TOOL_NAME in o for o in llm.offered_tools)
    first = next(m for m in messages if getattr(m, "tool_call_id", None) == "c1")
    assert "Loaded 15 of 20 tool(s)" in first.content
    assert "notion_tool_19" in first.content
    second = next(m for m in messages if getattr(m, "tool_call_id", None) == "c2")
    assert "Loaded tool(s) by name: notion_tool_19" in second.content
    assert "notion_tool_19" in llm.offered_tools[2]


@pytest.mark.asyncio
async def test_group_called_alongside_load_tools_in_one_response_is_not_the_one_evicted(
    _quiet_langfuse,
):
    """A mixed response calls into group A and loads group C in the same
    batch.  A is the least recently used group at that point (B was loaded
    after it), so a reveal processed before the sibling calls are touched
    would evict the very group the model is using right now — and an
    unloaded group cannot be touched back.  The loop refreshes the sibling
    calls' groups BEFORE the reveal, so B goes and A survives."""
    composite, executed = _executor_with_groups(("a", 15), ("b", 15), ("c", 15))
    mixed = ChatResponse(
        content="",
        model="stub",
        stop_reason="tool_calls",
        tool_calls=[
            ToolCall(id="c3a", name="a_tool_0", arguments={}),
            ToolCall(
                id="c3b", name=LOAD_TOOLS_TOOL_NAME, arguments={"groups": ["integration:c"]}
            ),
        ],
    )
    llm = _ScriptedLLM(
        [
            _load("c1", groups=["integration:a"]),
            _load("c2", groups=["integration:b"]),
            mixed,
            ChatResponse(content="done", model="stub"),
        ]
    )
    # Two full groups + load_tools + the timer fit; a third group does not.
    llm.max_tools = 35

    messages: list[ChatMessage] = [ChatMessage(role="user", content="go")]
    response = await al.run_agent_loop(
        llm=llm,
        messages=messages,
        tools=composite.all_tools,
        executor=composite,
        max_iterations=8,
        progressive_tools=True,
    )

    assert response.content == "done"
    assert executed == ["a_tool_0"]
    offered_counts = [len(o) for o in llm.offered_tools]
    assert all(c <= 35 for c in offered_counts), offered_counts
    last = llm.offered_tools[3]
    assert sum(1 for n in last if n.startswith("a_tool_")) == 15, "the group in use survived"
    assert sum(1 for n in last if n.startswith("c_tool_")) == 15
    assert not any(n.startswith("b_tool_") for n in last), "the idle group was evicted"
    reveal = next(m for m in messages if getattr(m, "tool_call_id", None) == "c3b")
    assert 'Unloaded to make room (least recently used first): "B"' in reveal.content


@pytest.mark.asyncio
async def test_reordered_name_call_alongside_load_tools_keeps_its_group_loaded(
    _quiet_langfuse,
):
    """Same mixed response, but the model calls ``tool_a_0`` — the reordered
    form the executor recovers to ``a_tool_0``.  The call runs, so its
    group must count as in use: B is evicted, not A."""
    composite, executed = _executor_with_groups(("a", 15), ("b", 15), ("c", 15))
    mixed = ChatResponse(
        content="",
        model="stub",
        stop_reason="tool_calls",
        tool_calls=[
            ToolCall(id="c3a", name="tool_a_0", arguments={}),
            ToolCall(
                id="c3b", name=LOAD_TOOLS_TOOL_NAME, arguments={"groups": ["integration:c"]}
            ),
        ],
    )
    llm = _ScriptedLLM(
        [
            _load("c1", groups=["integration:a"]),
            _load("c2", groups=["integration:b"]),
            mixed,
            ChatResponse(content="done", model="stub"),
        ]
    )
    llm.max_tools = 35

    messages: list[ChatMessage] = [ChatMessage(role="user", content="go")]
    response = await al.run_agent_loop(
        llm=llm,
        messages=messages,
        tools=composite.all_tools,
        executor=composite,
        max_iterations=8,
        progressive_tools=True,
    )

    assert response.content == "done"
    assert executed == ["a_tool_0"], "the reordered name dispatched"
    last = llm.offered_tools[3]
    assert sum(1 for n in last if n.startswith("a_tool_")) == 15, "the group in use survived"
    assert sum(1 for n in last if n.startswith("c_tool_")) == 15
    assert not any(n.startswith("b_tool_") for n in last)


@pytest.mark.asyncio
async def test_two_load_tools_in_one_response_never_evict_the_group_in_use(
    _quiet_langfuse,
):
    """A response calling into A with TWO ``load_tools`` (C, then D) under a
    cap that holds two groups.  Recency alone protects A only from the first
    reveal (C then becomes the newest); the batch-wide protection set keeps A
    — and the freshly loaded C — safe from the second one, which only gets the
    leftover room (a partial reveal) instead of evicting either."""
    composite, executed = _executor_with_groups(
        ("a", 15), ("b", 15), ("c", 15), ("d", 15)
    )
    mixed = ChatResponse(
        content="",
        model="stub",
        stop_reason="tool_calls",
        tool_calls=[
            ToolCall(id="c3a", name="a_tool_0", arguments={}),
            ToolCall(
                id="c3c", name=LOAD_TOOLS_TOOL_NAME, arguments={"groups": ["integration:c"]}
            ),
            ToolCall(
                id="c3d", name=LOAD_TOOLS_TOOL_NAME, arguments={"groups": ["integration:d"]}
            ),
        ],
    )
    llm = _ScriptedLLM(
        [
            _load("c1", groups=["integration:a"]),
            _load("c2", groups=["integration:b"]),
            mixed,
            ChatResponse(content="done", model="stub"),
        ]
    )
    llm.max_tools = 35

    messages: list[ChatMessage] = [ChatMessage(role="user", content="go")]
    response = await al.run_agent_loop(
        llm=llm,
        messages=messages,
        tools=composite.all_tools,
        executor=composite,
        max_iterations=8,
        progressive_tools=True,
    )

    assert response.content == "done"
    assert executed == ["a_tool_0"]
    assert all(len(o) <= 35 for o in llm.offered_tools)
    last = llm.offered_tools[3]
    assert sum(1 for n in last if n.startswith("a_tool_")) == 15, "the group in use survived"
    assert sum(1 for n in last if n.startswith("c_tool_")) == 15, "the first load survived"
    assert not any(n.startswith("b_tool_") for n in last), "the idle group was evicted"
    d_count = sum(1 for n in last if n.startswith("d_tool_"))
    assert 0 < d_count < 15, "D only got the leftover room"
    assert len(last) == 35, "the leftover room was used to the cap"
    second = next(m for m in messages if getattr(m, "tool_call_id", None) == "c3d")
    assert f'Loaded {d_count} of 15 tool(s) from "D" (integration:d)' in second.content
    assert "Unloaded" not in second.content, "nothing was evicted for D"


def _executor_with_named_groups(
    *groups: tuple[str, list[str]],
) -> tuple[CompositeToolExecutor, list[str]]:
    """Composite executor whose groups carry explicit ORIGINAL tool names —
    for name-space tests, where what matters is how the loop sanitises them."""
    executed: list[str] = []
    callable_exec = CallableToolExecutor()
    tools: list[ToolDefinition] = []
    for provider, names in groups:
        tools.extend(_tool(name, f"integration:{provider}") for name in names)

    def _make_handler(tool_name: str):
        async def _handler(**kwargs):
            executed.append(tool_name)
            return f"ran {tool_name}"

        return _handler

    for t in tools:
        callable_exec.register(t.name, _make_handler(t.name))
    composite = CompositeToolExecutor()
    composite.add_tools(tools, callable_exec)
    return composite, executed


def _fillers(provider: str, n: int) -> list[str]:
    return [f"{provider}_tool_{i}" for i in range(n)]


@pytest.mark.asyncio
async def test_collision_suffixed_call_touches_and_protects_its_own_group(
    _quiet_langfuse,
):
    """Name spaces: the gate is built from the SANITISED
    definitions, so the model's call names must reach it verbatim.  Originals
    ``a.b`` (group X) and ``a/b`` (group Y) are exposed as ``a_b`` / ``a_b_2``.
    A mixed response calling ``a_b_2`` and loading Z must keep Y — the group
    actually in use — and evict X.  Reverse-mapping the call to ``a/b`` first
    would token-match ``a_b`` in X instead, leaving Y unprotected."""
    composite, executed = _executor_with_named_groups(
        ("x", ["a.b", *_fillers("x", 14)]),
        ("y", ["a/b", *_fillers("y", 14)]),
        ("z", _fillers("z", 15)),
    )
    mixed = ChatResponse(
        content="",
        model="stub",
        stop_reason="tool_calls",
        tool_calls=[
            ToolCall(id="c3a", name="a_b_2", arguments={}),
            ToolCall(
                id="c3b", name=LOAD_TOOLS_TOOL_NAME, arguments={"groups": ["integration:z"]}
            ),
        ],
    )
    llm = _ScriptedLLM(
        [
            _load("c1", groups=["integration:x"]),
            _load("c2", groups=["integration:y"]),
            mixed,
            ChatResponse(content="done", model="stub"),
        ]
    )
    llm.max_tools = 35

    response = await al.run_agent_loop(
        llm=llm,
        messages=[ChatMessage(role="user", content="go")],
        tools=composite.all_tools,
        executor=composite,
        max_iterations=8,
        progressive_tools=True,
    )

    assert response.content == "done"
    assert executed == ["a/b"], "dispatch still reverse-maps to the original name"
    assert "a_b_2" in llm.offered_tools[2], "the suffixed name is what the model saw"
    last = llm.offered_tools[3]
    assert "a_b_2" in last, "the group in use survived"
    assert sum(1 for n in last if n.startswith("y_tool_")) == 14
    assert sum(1 for n in last if n.startswith("z_tool_")) == 15
    assert "a_b" not in last, "the idle group (X) was evicted, not Y"
    assert not any(n.startswith("x_tool_") for n in last)


@pytest.mark.asyncio
async def test_load_tools_by_collision_suffixed_name_pins_the_second_tool(
    _quiet_langfuse,
):
    """``load_tools(tools=["a_b_2"])`` must pin the SECOND tool (``a/b``), the
    one the model asked for by its LLM-visible name — not the first one.
    Reverse-mapping the request to ``a/b`` before the gate would let the
    resolver token-match ``a_b`` and pin ``a.b``'s schema instead."""
    composite, executed = _executor_with_named_groups(
        ("x", ["a.b", *_fillers("x", 14)]),
        ("y", ["a/b", *_fillers("y", 14)]),
    )
    llm = _ScriptedLLM(
        [
            _load("c1", tools=["a_b_2"]),
            _call("c2", "a_b_2"),
            ChatResponse(content="done", model="stub"),
        ]
    )

    messages: list[ChatMessage] = [ChatMessage(role="user", content="go")]
    response = await al.run_agent_loop(
        llm=llm,
        messages=messages,
        tools=composite.all_tools,
        executor=composite,
        max_iterations=8,
        progressive_tools=True,
    )

    assert response.content == "done"
    offered = llm.offered_tools[1]
    assert "a_b_2" in offered, "the requested (second) tool was pinned"
    assert "a_b" not in offered, "the first tool must NOT be pinned in its place"
    assert not any(n.startswith("y_tool_") for n in offered), "a pin loads one tool"
    assert executed == ["a/b"], "and the pinned tool dispatches to the right handler"
    result = next(m for m in messages if getattr(m, "tool_call_id", None) == "c1")
    assert "a_b_2" in result.content
    assert "Unknown" not in result.content


@pytest.mark.asyncio
async def test_non_colliding_sanitised_name_works_on_touch_and_pin_paths(
    _quiet_langfuse,
):
    """A sanitised name with no collision (``foo.bar`` -> ``foo_bar``) keeps
    working on BOTH gate paths: pinned by name, then called alongside a
    sibling ``load_tools`` without its group being evicted."""
    composite, executed = _executor_with_named_groups(
        ("p", ["foo.bar", *_fillers("p", 14)]),
        ("q", _fillers("q", 15)),
        ("r", _fillers("r", 15)),
    )
    mixed = ChatResponse(
        content="",
        model="stub",
        stop_reason="tool_calls",
        tool_calls=[
            ToolCall(id="c4a", name="foo_bar", arguments={}),
            ToolCall(
                id="c4b", name=LOAD_TOOLS_TOOL_NAME, arguments={"groups": ["integration:r"]}
            ),
        ],
    )
    llm = _ScriptedLLM(
        [
            _load("c1", tools=["foo_bar"]),
            _load("c2", groups=["integration:p"]),
            _load("c3", groups=["integration:q"]),
            mixed,
            ChatResponse(content="done", model="stub"),
        ]
    )
    llm.max_tools = 35

    response = await al.run_agent_loop(
        llm=llm,
        messages=[ChatMessage(role="user", content="go")],
        tools=composite.all_tools,
        executor=composite,
        max_iterations=8,
        progressive_tools=True,
    )

    assert response.content == "done"
    assert executed == ["foo.bar"]
    assert "foo_bar" in llm.offered_tools[1], "pinned by its sanitised name"
    assert not any(n.startswith("p_tool_") for n in llm.offered_tools[1])
    last = llm.offered_tools[4]
    assert "foo_bar" in last, "the group in use survived the sibling reveal"
    assert sum(1 for n in last if n.startswith("p_tool_")) == 14
    assert sum(1 for n in last if n.startswith("r_tool_")) == 15
    assert not any(n.startswith("q_tool_") for n in last), "the idle group was evicted"


def _tool_result(messages: list[ChatMessage], call_id: str) -> str:
    return next(m for m in messages if getattr(m, "tool_call_id", None) == call_id).content


@pytest.mark.asyncio
async def test_remainder_stays_recoverable_after_the_partial_result_is_stubbed(
    _quiet_langfuse,
):
    """The remainder listing of a partial reveal is an ordinary tool result,
    large enough for ``_clear_stale_tool_results`` to stub it once it is
    stale — and the at-capacity retry deliberately omits the names.  The
    model must still be able to reach a tool beyond the catalogue sample:
    the retry points to ``search``, ``search`` returns the exact name in a
    result small enough never to be a stubbing candidate itself, and the
    named load pins it."""
    composite, executed = _executor_with_n_notion(300)
    llm = _ScriptedLLM(
        [
            _load("c1", groups=["integration:notion"]),
            _load("c2", groups=["integration:notion"]),
            _load("c3", search="tool_299"),
            _load("c4", tools=["notion_tool_299"]),
            _call("c5", "notion_tool_299"),
            ChatResponse(content="done", model="stub"),
        ]
    )
    llm.max_tools = 11

    messages: list[ChatMessage] = [ChatMessage(role="user", content="use notion")]
    response = await al.run_agent_loop(
        llm=llm,
        messages=messages,
        tools=composite.all_tools,
        executor=composite,
        max_iterations=8,
        progressive_tools=True,
    )

    assert response.content == "done"
    assert executed == ["notion_tool_299"]
    assert all(len(o) <= 11 for o in llm.offered_tools)

    first = _tool_result(messages, "c1")
    assert "Loaded 10 of 300 tool(s)" in first and "notion_tool_299" in first
    assert len(first) >= al._STALE_TOOL_RESULT_MIN_CHARS, "a stubbing candidate"
    retry = _tool_result(messages, "c2")
    assert "notion_tool_299" not in retry, "the retry stays terse"
    assert "`search` argument" in retry, "and says how to get the names back"
    found = _tool_result(messages, "c3")
    assert "notion_tool_299 (integration:notion)" in found
    assert "Loaded" not in found, "a search-only call reports only the search"
    assert len(found) < al._STALE_TOOL_RESULT_MIN_CHARS, "never itself stubbed"
    assert "Loaded tool(s) by name: notion_tool_299" in _tool_result(messages, "c4")
    assert "notion_tool_299" in llm.offered_tools[4]

    # The scenario is real: the context-rot guard stubs the first listing
    # while leaving the compact search result and the retry text intact.
    al._clear_stale_tool_results(messages, live_budget_chars=200)
    assert _tool_result(messages, "c1").startswith("[Earlier load_tools result")
    assert "notion_tool_299" in _tool_result(messages, "c3")


@pytest.mark.asyncio
async def test_search_composes_with_a_group_load_in_the_same_call(_quiet_langfuse):
    """``load_tools(groups=[G], search=...)`` loads what fits AND lists the
    matching remainder, scoped to G — one round-trip instead of two."""
    composite, executed = _executor_with_n_notion(300)
    llm = _ScriptedLLM(
        [
            _load("c1", groups=["integration:notion"], search="tool_29"),
            ChatResponse(content="done", model="stub"),
        ]
    )
    llm.max_tools = 11

    messages: list[ChatMessage] = [ChatMessage(role="user", content="use notion")]
    await al.run_agent_loop(
        llm=llm,
        messages=messages,
        tools=composite.all_tools,
        executor=composite,
        max_iterations=4,
        progressive_tools=True,
    )

    text = _tool_result(messages, "c1")
    assert "Loaded 10 of 300 tool(s)" in text
    assert 'matching "tool_29" in "Notion" (integration:notion)' in text
    assert "notion_tool_299 (integration:notion)" in text
    assert executed == []


@pytest.mark.asyncio
async def test_search_for_an_always_on_tool_tells_the_model_to_call_it(_quiet_langfuse):
    """The tool result the model reads for ``load_tools(search=...)`` naming an
    always-on tool says it is already loaded, on a run where the gate is
    active and the tool is offered from the first request."""
    composite, executed = _executor_with_n_notion(30)
    core = [
        ToolDefinition(
            name="workset_add_item",
            description="Put one document into the working corpus.",
            source="builtin:workset",
        ),
        ToolDefinition(
            name="workset_seal",
            description="Declare that you have finished ADDING items.",
            source="builtin:workset",
        ),
    ]
    core_exec = CallableToolExecutor()

    async def _ok(**kwargs):
        return "ok"

    for tool in core:
        core_exec.register(tool.name, _ok)
    composite.add_tools(core, core_exec)
    llm = _ScriptedLLM(
        [
            _load("c1", search="workset"),
            ChatResponse(content="done", model="stub"),
        ]
    )

    messages: list[ChatMessage] = [ChatMessage(role="user", content="build the corpus")]
    await al.run_agent_loop(
        llm=llm,
        messages=messages,
        tools=composite.all_tools,
        executor=composite,
        max_iterations=4,
        progressive_tools=True,
    )

    assert {"workset_add_item", "workset_seal"} <= set(llm.offered_tools[0])
    assert not any(name.startswith("notion_tool_") for name in llm.offered_tools[0])
    assert _tool_result(messages, "c1") == (
        'No not-loaded tool matches "workset". '
        "Already loaded (call directly): workset_add_item, workset_seal."
    )
    assert executed == []


@pytest.mark.asyncio
async def test_reordered_collision_suffixed_call_is_accepted_by_gate_and_dispatched(
    _quiet_langfuse,
):
    """Gate and dispatch must agree on every name.  ``b_a_2``
    is the reordered form of the LLM-visible ``a_b_2`` (original ``a/b``):
    the gate resolves it to Y's tool and protects Y — so dispatch must run
    ``a/b`` too.  Forwarding ``b_a_2`` untouched to the original-name executor
    used to fail: no original carries the synthetic ``2`` token, so the call
    the gate had just accepted was refused."""
    composite, executed = _executor_with_named_groups(
        ("x", ["a.b", *_fillers("x", 14)]),
        ("y", ["a/b", *_fillers("y", 14)]),
        ("z", _fillers("z", 15)),
    )
    mixed = ChatResponse(
        content="",
        model="stub",
        stop_reason="tool_calls",
        tool_calls=[
            ToolCall(id="c3a", name="b_a_2", arguments={}),
            ToolCall(
                id="c3b", name=LOAD_TOOLS_TOOL_NAME, arguments={"groups": ["integration:z"]}
            ),
        ],
    )
    llm = _ScriptedLLM(
        [
            _load("c1", groups=["integration:x"]),
            _load("c2", groups=["integration:y"]),
            mixed,
            ChatResponse(content="done", model="stub"),
        ]
    )
    llm.max_tools = 35

    messages: list[ChatMessage] = [ChatMessage(role="user", content="go")]
    response = await al.run_agent_loop(
        llm=llm,
        messages=messages,
        tools=composite.all_tools,
        executor=composite,
        max_iterations=8,
        progressive_tools=True,
    )

    assert response.content == "done"
    assert executed == ["a/b"], "the reordered suffixed name dispatched to a/b"
    assert "ran a/b" in _tool_result(messages, "c3a")
    last = llm.offered_tools[3]
    assert "a_b_2" in last, "the gate protected the same group dispatch ran"
    assert "a_b" not in last and not any(n.startswith("x_tool_") for n in last)


@pytest.mark.asyncio
async def test_alias_after_a_sanitisation_collision_reaches_the_tool_it_stands_for(
    _quiet_langfuse,
):
    """The known alias ``list_github_issues`` means the ORIGINAL
    ``search_github_issues``.  With ``search.github.issues`` (group X) and
    ``search_github_issues`` (group Y) in one surface the loop exposes them
    as ``search_github_issues`` / ``search_github_issues_2``, so applying the
    original-name alias table to the visible names routed the alias to X's
    tool.  Gate and dispatch now share ONE table translated through the
    original -> visible assignment: the alias protects Y and dispatches
    ``search_github_issues``."""
    composite, executed = _executor_with_named_groups(
        ("x", ["search.github.issues", *_fillers("x", 14)]),
        ("y", ["search_github_issues", *_fillers("y", 14)]),
        ("z", _fillers("z", 15)),
    )
    mixed = ChatResponse(
        content="",
        model="stub",
        stop_reason="tool_calls",
        tool_calls=[
            ToolCall(id="c3a", name="list_github_issues", arguments={}),
            ToolCall(
                id="c3b", name=LOAD_TOOLS_TOOL_NAME, arguments={"groups": ["integration:z"]}
            ),
        ],
    )
    llm = _ScriptedLLM(
        [
            _load("c1", groups=["integration:x"]),
            _load("c2", groups=["integration:y"]),
            mixed,
            ChatResponse(content="done", model="stub"),
        ]
    )
    llm.max_tools = 35

    messages: list[ChatMessage] = [ChatMessage(role="user", content="go")]
    response = await al.run_agent_loop(
        llm=llm,
        messages=messages,
        tools=composite.all_tools,
        executor=composite,
        max_iterations=8,
        progressive_tools=True,
    )

    assert response.content == "done"
    assert executed == ["search_github_issues"], "the alias's own canonical, not X's"
    assert "ran search_github_issues" in _tool_result(messages, "c3a")
    assert "search_github_issues_2" in llm.offered_tools[2]
    last = llm.offered_tools[3]
    assert "search_github_issues_2" in last, "the gate protected Y - the group dispatch ran"
    assert "search_github_issues" not in last and not any(
        n.startswith("x_tool_") for n in last
    ), "X, idle, is the one evicted"


@pytest.mark.asyncio
async def test_alias_whose_canonical_is_absent_is_refused_by_gate_and_dispatch_alike(
    _quiet_langfuse,
):
    """Only ``search.github.issues`` is bound - exposed as
    ``search_github_issues``, the very string the alias's canonical happens
    to be.  The alias must NOT be let dangle onto it: the executor refuses
    ``list_github_issues`` (no original ``search_github_issues`` is
    registered) and the gate, given the same translated table, resolves
    nothing either - so it neither pins nor protects X."""
    composite, executed = _executor_with_named_groups(
        ("x", ["search.github.issues", *_fillers("x", 14)]),
        ("z", _fillers("z", 15)),
    )
    mixed = ChatResponse(
        content="",
        model="stub",
        stop_reason="tool_calls",
        tool_calls=[
            ToolCall(id="c2a", name="list_github_issues", arguments={}),
            ToolCall(
                id="c2b", name=LOAD_TOOLS_TOOL_NAME, arguments={"groups": ["integration:z"]}
            ),
        ],
    )
    llm = _ScriptedLLM(
        [
            _load("c1", groups=["integration:x"]),
            mixed,
            ChatResponse(content="done", model="stub"),
        ]
    )
    llm.max_tools = 20  # X (15) + load_tools + wait fit; Z needs X evicted

    messages: list[ChatMessage] = [ChatMessage(role="user", content="go")]
    response = await al.run_agent_loop(
        llm=llm,
        messages=messages,
        tools=composite.all_tools,
        executor=composite,
        max_iterations=8,
        progressive_tools=True,
    )

    assert response.content == "done"
    assert executed == [], "dispatch refused the dangling alias"
    refused = _tool_result(messages, "c2a")
    assert "ran " not in refused
    last = llm.offered_tools[2]
    assert "search_github_issues" not in last, "X was not protected by a name it does not own"
    assert any(n.startswith("z_tool_") for n in last)
    # And the gate's own by-name path agrees: the alias is unknown to it.
    pairs = build_llm_tool_names([t.name for t in composite.all_tools])
    visible_defs = [
        dataclasses.replace(t, name=visible)
        for t, (_original, visible) in zip(composite.all_tools, pairs, strict=True)
    ]
    gate = build_tool_gate(
        visible_defs,
        enabled=True,
        min_tools=1,
        max_visible=20,
        aliases=visible_aliases(dict(pairs), declared_aliases(composite.all_tools)),
    )
    assert gate is not None
    assert gate.reveal([], ["list_github_issues"]).unknown_tools == ["list_github_issues"]
    assert gate.groups_in_use(["list_github_issues"]) == []


@pytest.mark.asyncio
async def test_reordered_call_to_the_unsuffixed_collision_tool_dispatches_to_it(
    _quiet_langfuse,
):
    """``b_a`` denotes ``a_b`` (original ``a.b``) in the LLM-visible space,
    where the token key is unique because the sibling carries the ``2``.  In
    the ORIGINAL space ``a.b`` and ``a/b`` share the key, so canonicalising
    there — which is what forwarding the raw name to the executor amounts to
    — refused the call as ambiguous."""
    composite, executed = _executor_with_named_groups(
        ("x", ["a.b", *_fillers("x", 14)]),
        ("y", ["a/b", *_fillers("y", 14)]),
    )
    llm = _ScriptedLLM(
        [
            _load("c1", groups=["integration:x"]),
            _call("c2", "b_a"),
            ChatResponse(content="done", model="stub"),
        ]
    )

    messages: list[ChatMessage] = [ChatMessage(role="user", content="go")]
    await al.run_agent_loop(
        llm=llm,
        messages=messages,
        tools=composite.all_tools,
        executor=composite,
        max_iterations=8,
        progressive_tools=True,
    )

    assert executed == ["a.b"]
    assert "ran a.b" in _tool_result(messages, "c2")


@pytest.mark.asyncio
async def test_reordered_non_colliding_sanitised_name_dispatches(_quiet_langfuse):
    """``bar_foo`` for ``foo.bar`` (visible ``foo_bar``): the two stages
    compose — canonical visible ``foo_bar``, then the original ``foo.bar``."""
    composite, executed = _executor_with_named_groups(
        ("p", ["foo.bar", *_fillers("p", 14)]),
    )
    llm = _ScriptedLLM(
        [
            _load("c1", groups=["integration:p"]),
            _call("c2", "bar_foo"),
            ChatResponse(content="done", model="stub"),
        ]
    )

    messages: list[ChatMessage] = [ChatMessage(role="user", content="go")]
    await al.run_agent_loop(
        llm=llm,
        messages=messages,
        tools=composite.all_tools,
        executor=composite,
        max_iterations=8,
        progressive_tools=True,
    )

    assert executed == ["foo.bar"]
    assert "ran foo.bar" in _tool_result(messages, "c2")


@pytest.mark.asyncio
async def test_ambiguous_reordered_name_is_refused_by_gate_and_dispatch_alike(
    _quiet_langfuse,
):
    """``y_x_z`` shares its token key with BOTH ``x_y_z`` and ``z_y_x``.  The
    gate touches nothing for it (P is the LRU and is evicted by the sibling
    reveal) and dispatch forwards it untouched, so the executor refuses it
    too — consistently, never "protected but not run" or the reverse."""
    composite, executed = _executor_with_named_groups(
        ("p", ["x_y_z", "z_y_x", *_fillers("p", 13)]),
        ("q", _fillers("q", 15)),
        ("r", _fillers("r", 15)),
    )
    mixed = ChatResponse(
        content="",
        model="stub",
        stop_reason="tool_calls",
        tool_calls=[
            ToolCall(id="c3a", name="y_x_z", arguments={}),
            ToolCall(
                id="c3b", name=LOAD_TOOLS_TOOL_NAME, arguments={"groups": ["integration:r"]}
            ),
        ],
    )
    llm = _ScriptedLLM(
        [
            _load("c1", groups=["integration:p"]),
            _load("c2", groups=["integration:q"]),
            mixed,
            ChatResponse(content="done", model="stub"),
        ]
    )
    llm.max_tools = 35

    messages: list[ChatMessage] = [ChatMessage(role="user", content="go")]
    await al.run_agent_loop(
        llm=llm,
        messages=messages,
        tools=composite.all_tools,
        executor=composite,
        max_iterations=8,
        progressive_tools=True,
    )

    assert executed == [], "the ambiguous name ran nothing"
    result = _tool_result(messages, "c3a")
    assert "ran " not in result
    assert "y_x_z" in result, "the refusal names the call so the model can correct it"
    last = llm.offered_tools[3]
    assert not any(n in last for n in ("x_y_z", "z_y_x")), "P was not protected"
    assert sum(1 for n in last if n.startswith("q_tool_")) == 15
    assert sum(1 for n in last if n.startswith("r_tool_")) == 15


def _executor_with_core_and_groups(
    core: list[ToolDefinition], *groups: tuple[str, list[str]]
) -> tuple[CompositeToolExecutor, list[str]]:
    """Composite executor with explicit always-on (builtin) tools plus named
    integration groups; records executions."""
    executed: list[str] = []
    callable_exec = CallableToolExecutor()
    tools: list[ToolDefinition] = list(core)
    for provider, names in groups:
        tools.extend(_tool(name, f"integration:{provider}") for name in names)

    def _make_handler(tool_name: str):
        async def _handler(**kwargs):
            executed.append(tool_name)
            return f"ran {tool_name}"

        return _handler

    for t in tools:
        callable_exec.register(t.name, _make_handler(t.name))
    composite = CompositeToolExecutor()
    composite.add_tools(tools, callable_exec)
    return composite, executed


@pytest.mark.parametrize(
    ("called", "survivor", "evicted"),
    [
        # The exact always-on name: dispatch runs the core tool and the gate
        # must NOT protect the token-equivalent gated group - JIRA is the LRU
        # and goes; Q (touched later) stays.
        ("jira_search_issues", "q", "jira"),
        # The exact gated name still touches and protects its own group.
        ("search_jira_issues", "jira", "q"),
    ],
)
@pytest.mark.asyncio
async def test_exact_core_call_is_not_a_recency_event_for_a_token_equivalent_gated_tool(
    _quiet_langfuse, called: str, survivor: str, evicted: str
):
    """Core ``jira_search_issues`` and gated ``search_jira_issues`` share a
    token key.  The gate used to resolve over the gated tools ALONE, so the
    core call token-matched the gated tool and protected JIRA while dispatch
    ran the core tool - a sibling reveal then evicted the wrong group.  Over
    the full offered surface the exact core name wins, exactly as at
    dispatch, and a core call protects nothing (the core is never evicted).
    """
    composite, executed = _executor_with_core_and_groups(
        [_tool("jira_search_issues", "builtin:jira_shortcut")],
        ("jira", ["search_jira_issues", *_fillers("jira", 14)]),
        ("q", _fillers("q", 15)),
        ("r", _fillers("r", 15)),
    )
    mixed = ChatResponse(
        content="",
        model="stub",
        stop_reason="tool_calls",
        tool_calls=[
            ToolCall(id="c3a", name=called, arguments={}),
            ToolCall(
                id="c3b", name=LOAD_TOOLS_TOOL_NAME, arguments={"groups": ["integration:r"]}
            ),
        ],
    )
    llm = _ScriptedLLM(
        [
            _load("c1", groups=["integration:jira"]),
            _load("c2", groups=["integration:q"]),
            mixed,
            ChatResponse(content="done", model="stub"),
        ]
    )
    llm.max_tools = 36  # core 1 + load_tools + JIRA 15 + Q 15 = 32; R (15) needs one out

    messages: list[ChatMessage] = [ChatMessage(role="user", content="go")]
    response = await al.run_agent_loop(
        llm=llm,
        messages=messages,
        tools=composite.all_tools,
        executor=composite,
        max_iterations=8,
        progressive_tools=True,
    )

    assert response.content == "done"
    assert executed == [called], "dispatch ran the exact tool named"
    assert f"ran {called}" in _tool_result(messages, "c3a")
    last = llm.offered_tools[3]
    assert "jira_search_issues" in last, "always-on, whatever happens"
    assert sum(1 for n in last if n.startswith("r_tool_")) == 15
    assert ("search_jira_issues" in last) == (survivor == "jira")
    assert any(n.startswith(f"{survivor}_tool_") for n in last), f"{survivor} survived"
    assert not any(n.startswith(f"{evicted}_tool_") for n in last), f"{evicted} was evicted"


@pytest.mark.asyncio
async def test_name_ambiguous_between_a_core_and_a_gated_tool_is_refused_by_both(
    _quiet_langfuse,
):
    """``issues_jira_search`` shares its token key with core
    ``jira_search_issues`` AND gated ``search_jira_issues``: ambiguity is
    judged over the same candidate set at the gate and at dispatch, so
    neither runs nor protects anything - JIRA, the LRU, is evicted."""
    composite, executed = _executor_with_core_and_groups(
        [_tool("jira_search_issues", "builtin:jira_shortcut")],
        ("jira", ["search_jira_issues", *_fillers("jira", 14)]),
        ("q", _fillers("q", 15)),
        ("r", _fillers("r", 15)),
    )
    mixed = ChatResponse(
        content="",
        model="stub",
        stop_reason="tool_calls",
        tool_calls=[
            ToolCall(id="c3a", name="issues_jira_search", arguments={}),
            ToolCall(
                id="c3b", name=LOAD_TOOLS_TOOL_NAME, arguments={"groups": ["integration:r"]}
            ),
        ],
    )
    llm = _ScriptedLLM(
        [
            _load("c1", groups=["integration:jira"]),
            _load("c2", groups=["integration:q"]),
            mixed,
            ChatResponse(content="done", model="stub"),
        ]
    )
    llm.max_tools = 36

    messages: list[ChatMessage] = [ChatMessage(role="user", content="go")]
    await al.run_agent_loop(
        llm=llm,
        messages=messages,
        tools=composite.all_tools,
        executor=composite,
        max_iterations=8,
        progressive_tools=True,
    )

    assert executed == [], "the ambiguous name ran nothing"
    refused = _tool_result(messages, "c3a")
    assert "ran " not in refused and "issues_jira_search" in refused
    last = llm.offered_tools[3]
    assert "search_jira_issues" not in last, "JIRA was not protected"
    assert not any(n.startswith("jira_tool_") for n in last)
    assert sum(1 for n in last if n.startswith("q_tool_")) == 15
    assert sum(1 for n in last if n.startswith("r_tool_")) == 15


@pytest.mark.asyncio
async def test_a_64_tool_provider_with_a_large_builtin_suite_keeps_every_tool_reachable(
    _quiet_langfuse,
):
    """The realistic shape of the bug: Ollama caps at 64 and an agent with the
    large builtin suites enabled carries ~70 always-on tools.  The core used
    to be trimmed to 63, the visible list was full from the first request,
    and every ``load_tools`` answered ``no_room`` while the catalogue kept
    advertising Notion and GitHub.  Now 62 built-ins stay always-on, the 8
    least important are demoted into ``builtin:overflow``, a group load
    reveals its most important tool into the reserved slot, and a demoted
    built-in is loadable by name - every request stays within 64."""
    core = [
        *(_tool(f"builtin_{i}", f"builtin:suite_{i % 5}") for i in range(62)),
        *(
            ToolDefinition(
                name=f"builtin_lo_{i}", description="d", source="builtin:extras", priority=2
            )
            for i in range(8)
        ),
    ]
    composite, executed = _executor_with_core_and_groups(
        core,
        ("notion", [*_fillers("notion", 20)]),
        ("github", [*_fillers("github", 20)]),
    )
    llm = _ScriptedLLM(
        [
            _load("c1", groups=["integration:notion"]),
            _call("c2", "notion_tool_0"),
            _load("c3", tools=["builtin_lo_3"]),
            _call("c4", "builtin_lo_3"),
            ChatResponse(content="done", model="stub"),
        ]
    )
    llm.max_tools = 64

    messages: list[ChatMessage] = [ChatMessage(role="user", content="go")]
    response = await al.run_agent_loop(
        llm=llm,
        messages=messages,
        tools=composite.all_tools,
        executor=composite,
        max_iterations=8,
        progressive_tools=True,
    )

    assert response.content == "done"
    assert executed == ["notion_tool_0", "builtin_lo_3"]
    assert all(len(offered) <= 64 for offered in llm.offered_tools)
    first = llm.offered_tools[0]
    assert sum(1 for n in first if n.startswith("builtin_")) == 62
    assert not any(n.startswith("builtin_lo_") for n in first), "demoted, not always-on"
    assert LOAD_TOOLS_TOOL_NAME in first
    catalogue = next(
        m.content for m in messages if m.role == "system" and "Available tool groups" in m.content
    )
    assert f'"{CORE_OVERFLOW_GROUP_ID}"' in catalogue and "builtin_lo_0" in catalogue
    assert "1 of which are free" in catalogue
    assert "Loaded 1 of 20" in _tool_result(messages, "c1")
    assert "notion_tool_0" in llm.offered_tools[1]
    pinned = _tool_result(messages, "c3")
    assert "builtin_lo_3" in pinned and "Could not load" not in pinned
    assert "builtin_lo_3" in llm.offered_tools[3]
    assert "notion_tool_0" not in llm.offered_tools[3], "the idle partial group made room"
    assert not any("unavailable this run" in (m.content or "") for m in messages if m.role == "system")


@pytest.mark.asyncio
async def test_iteration_span_reports_visible_tools_not_full_catalogue(monkeypatch):
    """Trace truthfulness: the per-iteration span's
    ``available_tools`` must list only the tools the model can actually see
    that turn — the always-on core + ``load_tools`` up-front, growing as
    groups are revealed — NOT the full gated catalogue (which would falsely
    claim every gated tool was available before it had been loaded).  Also
    asserts the gating-split metadata is enriched onto the trace exactly once.
    """
    captured_iter: list[list[str] | None] = []
    captured_meta: list[dict] = []

    def _capture_iter(**kw):
        captured_iter.append(kw.get("available_tools"))
        return None

    def _capture_meta(_ctx, meta):
        captured_meta.append(meta)

    monkeypatch.setattr(al, "_lf_start_llm_gen", lambda *a, **kw: None)
    monkeypatch.setattr(al, "_lf_end_generation", lambda *a, **kw: None)
    monkeypatch.setattr(al, "_lf_end_span", lambda *a, **kw: None)
    monkeypatch.setattr(al, "_lf_end_tool_span", lambda *a, **kw: None)
    monkeypatch.setattr(al, "_lf_start_tool_span", lambda **kw: None)
    monkeypatch.setattr(al, "_lf_start_iter_span", _capture_iter)
    monkeypatch.setattr(al, "_lf_enrich_trace_metadata", _capture_meta)

    composite, _ = _executor_with_notion()  # 18 notion tools (>= threshold)
    llm = _ScriptedLLM(
        [
            # Iteration 1: reveal the notion group.
            ChatResponse(
                content="",
                model="stub",
                stop_reason="tool_calls",
                tool_calls=[
                    ToolCall(
                        id="c1",
                        name=LOAD_TOOLS_TOOL_NAME,
                        arguments={"groups": ["integration:notion"]},
                    )
                ],
            ),
            # Iteration 2: final answer.
            ChatResponse(content="done", model="stub"),
        ]
    )

    await al.run_agent_loop(
        llm=llm,
        messages=[ChatMessage(role="user", content="search notion")],
        tools=composite.all_tools,
        executor=composite,
        max_iterations=5,
        progressive_tools=True,
    )

    assert len(captured_iter) >= 2, "expected at least two iteration spans"
    # Iteration 1: only the meta-tool is visible, none of the gated tools.
    it1 = set(captured_iter[0] or [])
    assert LOAD_TOOLS_TOOL_NAME in it1
    assert not any(n.startswith("notion_tool_") for n in it1), (
        "iteration span must not list gated tools before they are loaded"
    )
    # Iteration 2: the revealed notion tools now appear (visible set grows).
    it2 = set(captured_iter[1] or [])
    assert any(n.startswith("notion_tool_") for n in it2)

    # Clarity metadata enriched onto the trace once, with an accurate split.
    assert len(captured_meta) == 1
    meta = captured_meta[0]
    assert meta["progressive_tools_active"] is True
    assert meta["tools_gated"] == 18
    assert meta["tool_groups"] == 1
    # ``tools_total`` mirrors the root card's ``tool_count`` (the catalogue
    # count): it is the original tool list, NOT ``effective_tools`` — so it
    # excludes the synthetic ``load_tools`` meta-tool and is unaffected by the
    # provider max-tools truncation.
    assert meta["tools_total"] == len(composite.all_tools) == 18, (
        "tools_total must equal the catalogue count (no synthetic meta-tool)"
    )
    assert meta["tools_visible_upfront"] < meta["tools_total"], (
        "up-front visible count must be far smaller than the full catalogue"
    )


@pytest.mark.asyncio
async def test_iteration_span_reports_none_when_tools_stripped(monkeypatch):
    """Trace truthfulness: on the near-budget-cap iterations
    where the loop strips all tools to force a text answer (``iter_tools =
    None``), the per-iteration span's ``available_tools`` must be ``None`` —
    matching what is actually sent to the LLM — not the still-"visible" tool
    set, which would mislead for the very reason this change addresses.
    """
    captured_iter: list[list[str] | None] = []

    def _capture_iter(**kw):
        captured_iter.append(kw.get("available_tools"))
        return None

    monkeypatch.setattr(al, "_lf_start_llm_gen", lambda *a, **kw: None)
    monkeypatch.setattr(al, "_lf_end_generation", lambda *a, **kw: None)
    monkeypatch.setattr(al, "_lf_end_span", lambda *a, **kw: None)
    monkeypatch.setattr(al, "_lf_end_tool_span", lambda *a, **kw: None)
    monkeypatch.setattr(al, "_lf_start_tool_span", lambda **kw: None)
    monkeypatch.setattr(al, "_lf_start_iter_span", _capture_iter)

    # Gating left OFF: ``effective_tools`` is the plain catalogue, so the
    # non-stripped iterations list real tool names and the assertion isolates
    # the strip behaviour from the progressive-disclosure visibility logic.
    composite, _ = _executor_with_notion()  # 18 notion tools
    llm = _ScriptedLLM(
        [
            # Iterations 0 + 1: keep calling a tool so the loop runs to the cap.
            ChatResponse(
                content="",
                model="stub",
                stop_reason="tool_calls",
                tool_calls=[ToolCall(id="c0", name="notion_tool_0", arguments={})],
            ),
            ChatResponse(
                content="",
                model="stub",
                stop_reason="tool_calls",
                tool_calls=[ToolCall(id="c1", name="notion_tool_1", arguments={})],
            ),
            # Iteration 2 == strip_tools_at (max_iterations - 1): tools stripped.
            ChatResponse(content="done", model="stub"),
        ]
    )

    await al.run_agent_loop(
        llm=llm,
        messages=[ChatMessage(role="user", content="go")],
        tools=composite.all_tools,
        executor=composite,
        max_iterations=3,  # strip_tools_at = max_iterations - 1 = 2
        progressive_tools=False,
    )

    assert len(captured_iter) == 3, "expected three iteration spans"
    # Iterations 0 and 1: tools were sent, so the span lists them.
    assert captured_iter[0] and any(
        n.startswith("notion_tool_") for n in captured_iter[0]
    )
    assert captured_iter[1] and any(
        n.startswith("notion_tool_") for n in captured_iter[1]
    )
    # Iteration 2: tools stripped to force a text answer → span must be None,
    # and the LLM is indeed called with no tools on that iteration.
    assert captured_iter[2] is None, (
        "stripped iteration must report no available tools"
    )
    assert llm.offered_tools[2] == []


def test_tools_not_in_cooldown_filter():
    """The shared cooldown filter (used by both the iteration span's
    ``available_tools`` and the on-the-wire ``iter_tools``) drops a tool only
    while its recorded cooldown iteration is still in the future, and offers it
    again once that iteration is reached."""
    tools = [_tool("a", "integration:x"), _tool("b", "integration:x")]

    # No cooldown dict → unchanged.
    assert al._tools_not_in_cooldown(tools, {}, 3) is tools
    # ``None`` tools → ``None`` (stripped iteration).
    assert al._tools_not_in_cooldown(None, {"a": 5}, 3) is None

    # ``b`` is cooling until iteration 5; at iteration 3 it is filtered out.
    kept = al._tools_not_in_cooldown(tools, {"b": 5}, 3)
    assert [t.name for t in kept] == ["a"]

    # At iteration 5 the cooldown has elapsed (``<= iteration``) → ``b`` returns.
    kept = al._tools_not_in_cooldown(tools, {"b": 5}, 5)
    assert {t.name for t in kept} == {"a", "b"}

    # Every tool cooling → empty list (callers collapse ``[]`` to ``None``).
    assert al._tools_not_in_cooldown(tools, {"a": 9, "b": 9}, 3) == []


@pytest.mark.asyncio
async def test_rate_limited_tool_suppressed_for_full_cooldown(_quiet_langfuse):
    """A tool that fails with a rate-limit error must be withheld from the LLM
    for ``_RATE_LIMIT_COOLDOWN_ITERS`` FULL subsequent iterations.

    ``_tools_not_in_cooldown`` re-offers a tool once ``cooldown[name] <=
    iteration``, so the recorded end must be ``current + N + 1`` — a bare
    ``current + N`` would bring the tool back one iteration early.
    """
    executed: list[str] = []
    callable_exec = CallableToolExecutor()

    async def _limited(**kwargs):
        executed.append("limited")
        raise Exception("GitHub API error: 429 Too Many Requests")

    async def _other(**kwargs):
        executed.append("other")
        return "ok"

    callable_exec.register("limited", _limited)
    callable_exec.register("other", _other)
    tools = [_tool("limited", "integration:x"), _tool("other", "integration:x")]
    composite = CompositeToolExecutor()
    composite.add_tools(tools, callable_exec)

    def _call_other(i: int) -> ChatResponse:
        # Distinct arguments per call so the dedup cache never kicks in.
        return ChatResponse(
            content="",
            model="stub",
            stop_reason="tool_calls",
            tool_calls=[ToolCall(id=f"o{i}", name="other", arguments={"i": i})],
        )

    llm = _ScriptedLLM(
        [
            # Iteration 0: the rate-limited tool fails → cooldown recorded.
            ChatResponse(
                content="",
                model="stub",
                stop_reason="tool_calls",
                tool_calls=[ToolCall(id="c1", name="limited", arguments={})],
            ),
            # Iterations 1-4: keep the loop alive with the other tool.
            *[_call_other(i) for i in range(1, 5)],
            ChatResponse(content="done", model="stub"),
        ]
    )

    await al.run_agent_loop(
        llm=llm,
        messages=[ChatMessage(role="user", content="go")],
        tools=composite.all_tools,
        executor=composite,
        max_iterations=8,
        progressive_tools=False,
    )

    assert executed[0] == "limited"
    # Failure at iteration 0 with _RATE_LIMIT_COOLDOWN_ITERS = 3 → suppressed
    # on iterations 1, 2 and 3 (three full iterations), offered again on 4.
    for it in (1, 2, 3):
        assert "limited" not in llm.offered_tools[it], (
            f"iteration {it} must not offer the rate-limited tool"
        )
    assert "limited" in llm.offered_tools[4], (
        "cooldown must expire after _RATE_LIMIT_COOLDOWN_ITERS full iterations"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "auth_error",
    [
        "GitHub API error: 401 Unauthorized",
        (
            "Validation Failed (422): The listed users and repositories cannot "
            "be searched either because the resources do not exist or you do "
            "not have permission to view them."
        ),
    ],
    ids=["bare-401", "422-permission-shape"],
)
async def test_auth_failed_tool_suppressed_for_full_cooldown(
    _quiet_langfuse, auth_error
):
    """A tool that fails with an auth/permission error must be withheld from
    the LLM for ``_AUTH_FAILURE_COOLDOWN_ITERS`` FULL subsequent iterations —
    the longer window that stops a broken credential from being
    hammered into a provider-level secondary rate limit.

    Same ``current + N + 1`` arithmetic as the rate-limit path, exercised
    end-to-end for both classifier shapes: a bare 401 and a GitHub 422
    permission-shaped validation failure.
    """
    executed: list[str] = []
    callable_exec = CallableToolExecutor()

    async def _broken(**kwargs):
        executed.append("broken")
        raise Exception(auth_error)

    async def _other(**kwargs):
        executed.append("other")
        return "ok"

    callable_exec.register("broken", _broken)
    callable_exec.register("other", _other)
    tools = [_tool("broken", "integration:x"), _tool("other", "integration:x")]
    composite = CompositeToolExecutor()
    composite.add_tools(tools, callable_exec)

    cooldown = al._AUTH_FAILURE_COOLDOWN_ITERS

    def _call_other(i: int) -> ChatResponse:
        # Distinct arguments per call so the dedup cache never kicks in.
        return ChatResponse(
            content="",
            model="stub",
            stop_reason="tool_calls",
            tool_calls=[ToolCall(id=f"o{i}", name="other", arguments={"i": i})],
        )

    llm = _ScriptedLLM(
        [
            # Iteration 0: the auth-broken tool fails → cooldown recorded.
            ChatResponse(
                content="",
                model="stub",
                stop_reason="tool_calls",
                tool_calls=[ToolCall(id="c1", name="broken", arguments={})],
            ),
            # Iterations 1..cooldown+1: keep the loop alive with the other tool.
            *[_call_other(i) for i in range(1, cooldown + 2)],
            ChatResponse(content="done", model="stub"),
        ]
    )

    await al.run_agent_loop(
        llm=llm,
        messages=[ChatMessage(role="user", content="go")],
        tools=composite.all_tools,
        executor=composite,
        max_iterations=cooldown + 5,
        progressive_tools=False,
    )

    assert executed[0] == "broken"
    assert executed.count("broken") == 1, "the broken tool must not be re-run"
    # Failure at iteration 0 with _AUTH_FAILURE_COOLDOWN_ITERS = N → suppressed
    # on iterations 1..N (N full iterations), offered again on N+1.
    for it in range(1, cooldown + 1):
        assert "broken" not in llm.offered_tools[it], (
            f"iteration {it} must not offer the auth-broken tool"
        )
    assert "broken" in llm.offered_tools[cooldown + 1], (
        "cooldown must expire after _AUTH_FAILURE_COOLDOWN_ITERS full iterations"
    )


@pytest.mark.asyncio
async def test_root_trace_tools_track_offered_set_not_full_catalogue(monkeypatch):
    """The root trace card's ``tools`` must reflect what the model was actually
    offered across the run: up-front it is the always-on core
    + ``load_tools``; after a ``load_tools`` reveal it expands to include the
    revealed group. The final value is the union of tools available across all
    iterations — NOT the full pre-gating catalogue."""
    captured: list[list[str]] = []

    def _capture_update(_ctx, tools):
        captured.append([t.name for t in (tools or [])])

    monkeypatch.setattr(al, "_lf_start_llm_gen", lambda *a, **kw: None)
    monkeypatch.setattr(al, "_lf_end_generation", lambda *a, **kw: None)
    monkeypatch.setattr(al, "_lf_end_span", lambda *a, **kw: None)
    monkeypatch.setattr(al, "_lf_end_tool_span", lambda *a, **kw: None)
    monkeypatch.setattr(al, "_lf_start_iter_span", lambda **kw: None)
    monkeypatch.setattr(al, "_lf_start_tool_span", lambda **kw: None)
    monkeypatch.setattr(al, "_lf_update_trace_tools", _capture_update)
    # A truthy ctx so the loop actually attempts the trace-tools update.
    monkeypatch.setattr(al, "_lf_get_ctx", lambda: {"trace": object(), "input_data": {}})

    composite, _ = _executor_with_notion()  # 18 notion tools (>= threshold)
    llm = _ScriptedLLM(
        [
            ChatResponse(
                content="",
                model="stub",
                stop_reason="tool_calls",
                tool_calls=[
                    ToolCall(
                        id="c1",
                        name=LOAD_TOOLS_TOOL_NAME,
                        arguments={"groups": ["integration:notion"]},
                    )
                ],
            ),
            ChatResponse(content="done", model="stub"),
        ]
    )

    await al.run_agent_loop(
        llm=llm,
        messages=[ChatMessage(role="user", content="search notion")],
        tools=composite.all_tools,
        executor=composite,
        max_iterations=5,
        progressive_tools=True,
    )

    assert len(captured) >= 2, "expected an up-front sync + a post-reveal sync"
    # Up-front: only the meta-tool is offered (no gated notion tools).
    first = set(captured[0])
    assert LOAD_TOOLS_TOOL_NAME in first
    assert not any(n.startswith("notion_tool_") for n in first), (
        "root card must not list gated tools before they are loaded"
    )
    # After the reveal: the notion group is now part of the card's tools.
    last = set(captured[-1])
    assert LOAD_TOOLS_TOOL_NAME in last
    assert any(n.startswith("notion_tool_") for n in last)


@pytest.mark.asyncio
async def test_legacy_path_still_truncates_the_whole_list_to_the_cap(_quiet_langfuse):
    """With progressive disclosure OFF the whole list goes on the wire, so the
    whole list is what the provider cap trims — by retention key, with the
    notice naming what was dropped.  This is the one path where a tool
    is genuinely unavailable for the run; the gated path never is."""
    composite, _ = _executor_with_n_notion(20)
    llm = _ScriptedLLM([ChatResponse(content="hi", model="stub")])
    llm.max_tools = 16

    messages: list[ChatMessage] = [ChatMessage(role="user", content="hello")]
    await al.run_agent_loop(
        llm=llm,
        messages=messages,
        tools=composite.all_tools,
        executor=composite,
        max_iterations=3,
        progressive_tools=False,
    )

    assert llm.offered_tools, "expected at least one chat call"
    offered = llm.offered_tools[0]
    assert LOAD_TOOLS_TOOL_NAME not in offered
    # The full provider budget is used, not one slot less.
    assert len(offered) == 16, f"expected full provider budget (16), got {len(offered)}"
    assert sum(1 for n in offered if n.startswith("notion_tool_")) == 16
    notice = [
        m.content
        for m in messages
        if getattr(m, "role", None) == "system" and "unavailable this run" in (m.content or "")
    ]
    assert len(notice) == 1
    assert "4 tool(s) were unavailable this run" in notice[0]


@pytest.mark.asyncio
@pytest.mark.parametrize("cap", [0, 1])
async def test_a_provider_below_the_gated_minimum_gets_no_gate_and_a_capped_wire(
    _quiet_langfuse, cap: int
):
    """Gating ON, but the provider cannot hold ``load_tools`` plus one real
    tool: no gate is built, so the request never carries the meta-tool -
    zero schemas on a zero-tool provider, exactly one on a one-tool provider
    - and the legacy truncation notice names what was dropped.  Before, the
    gate's ``visible_tools()`` sent ``load_tools`` regardless, breaking a
    ``max_tools == 0`` cap with a single schema."""
    composite, _ = _executor_with_n_notion(20)
    llm = _ScriptedLLM([ChatResponse(content="hi", model="stub")])
    llm.max_tools = cap

    messages: list[ChatMessage] = [ChatMessage(role="user", content="hello")]
    await al.run_agent_loop(
        llm=llm,
        messages=messages,
        tools=composite.all_tools,
        executor=composite,
        max_iterations=3,
        progressive_tools=True,
    )

    offered = llm.offered_tools[0]
    assert len(offered) == cap, f"the wire must carry exactly {cap} schema(s)"
    assert LOAD_TOOLS_TOOL_NAME not in offered and al.WAIT_TOOL_NAME not in offered
    system = [m.content or "" for m in messages if getattr(m, "role", None) == "system"]
    assert not any(LOAD_TOOLS_TOOL_NAME in s for s in system), "no catalogue"
    assert sum(1 for s in system if "unavailable this run" in s) == 1
    assert any(f"{20 - cap} tool(s) were unavailable this run" in s for s in system)


@pytest.mark.asyncio
async def test_cap_is_the_model_cap_and_a_trim_is_warned_once(_quiet_langfuse, caplog):
    """The gate takes the run model's cap (``max_tools_for``), not the provider's
    fixed ``max_tools``, and the first reveal the cap cuts down is a warning, once per run (#11639)."""

    class _PerModelLLM(_ScriptedLLM):
        async def max_tools_for(self, model):
            return 20 if model == "small" else self.max_tools

    composite, _ = _executor_with_n_notion(30)
    llm = _PerModelLLM(
        [
            _load("c1", groups=["integration:notion"]),
            _load("c2", groups=["integration:notion"], tools=["notion_tool_29"]),
            ChatResponse(content="done", model="stub"),
        ]
    )

    with caplog.at_level(logging.INFO, logger=al.logger.name):
        await al.run_agent_loop(
            llm=llm,
            messages=[ChatMessage(role="user", content="use notion")],
            tools=composite.all_tools,
            executor=composite,
            model="small",
            max_iterations=5,
            progressive_tools=True,
        )

    assert max(len(o) for o in llm.offered_tools) == 20
    reveals = [r for r in caplog.records if r.getMessage().startswith("load_tools revealed")]
    warned = [r.getMessage() for r in reveals if r.levelno == logging.WARNING]
    assert len(reveals) == 2 and len(warned) == 1
    assert "partial: {'integration:notion': 19}" in warned[0]
    assert "model small: 20 tools per request" in warned[0]


@pytest.mark.asyncio
async def test_a_sticky_decline_route_re_caps_the_gate(_quiet_langfuse):
    """A decline route to a smaller-cap model re-caps the gate from the next request on (#11639)."""

    class _DecliningLLM(_ScriptedLLM):
        cap = 64

        async def max_tools_for(self, model):
            return self.cap

        async def chat(self, messages, model=None, tools=None, **kwargs):
            response = await super().chat(messages, model=model, tools=tools, **kwargs)
            if self._idx == 2:
                # The second call was declined and routed: the wrapper's route sticks.
                self.cap = 20
            return response

    composite, _ = _executor_with_n_notion(30)
    llm = _DecliningLLM(
        [
            _load("c1", groups=["integration:notion"]),
            _call("c2", "notion_tool_0"),
            ChatResponse(content="done", model="stub"),
        ]
    )
    messages = [ChatMessage(role="user", content="use notion")]
    await al.run_agent_loop(
        llm=llm,
        messages=messages,
        tools=composite.all_tools,
        executor=composite,
        model="claude",
        max_iterations=5,
        progressive_tools=True,
    )

    assert len(llm.offered_tools[1]) > 20
    assert len(llm.offered_tools[2]) <= 20
    assert any(
        m.role == "system" and "accepts at most 20 tool schemas" in (m.content or "") for m in messages
    )


@pytest.mark.asyncio
async def test_a_rising_cap_restores_the_tools_an_ungated_run_dropped(_quiet_langfuse):
    """An ungated run cut to a low cap gets its dropped tools back once the cap rises (#11639)."""

    class _RisingLLM(_ScriptedLLM):
        cap = 10

        async def max_tools_for(self, model):
            return self.cap

        async def chat(self, messages, model=None, tools=None, **kwargs):
            response = await super().chat(messages, model=model, tools=tools, **kwargs)
            # The gateway listing failed at run start (128 fallback) and succeeds now.
            self.cap = 1024
            return response

    composite, _ = _executor_with_n_notion(30)
    llm = _RisingLLM([_call("c1", "notion_tool_0"), ChatResponse(content="done", model="stub")])
    messages = [ChatMessage(role="user", content="use notion")]
    await al.run_agent_loop(
        llm=llm,
        messages=messages,
        tools=composite.all_tools,
        executor=composite,
        model="claude",
        max_iterations=5,
        progressive_tools=False,
    )

    assert len(llm.offered_tools[0]) <= 10 + 1  # + the loop's own wait primitive
    assert {t.name for t in composite.all_tools} <= set(llm.offered_tools[1])
    assert any(m.role == "system" and "available again" in (m.content or "") for m in messages)
