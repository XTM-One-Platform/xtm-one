"""Tests for capability-not-found detection and nudge injection.

When the LLM repeatedly calls tool names that are not in the allowed set
(``ScopedToolExecutor`` rejections), the agent loop must:

1. Include helpful "Available tools" text in the first rejection so the LLM
   can self-correct immediately.
2. Aggregate rejections across *different* hallucinated names — the per-name
   ``_MAX_TOOL_FAILURES`` counter does not catch this pattern.
3. After ``_MAX_NOT_AVAILABLE_DISTINCT_NAMES`` distinct rejections, inject a
   single ``_CAPABILITY_NOT_FOUND_NUDGE`` that lists the tools that ARE
   available so the LLM can stop guessing or give up gracefully.
"""

from __future__ import annotations

import pytest

from app.llm.base import BaseLLMProvider, ChatMessage, ChatResponse
from app.tools import agent_loop as al
from app.tools.agent_loop import _MAX_NOT_AVAILABLE_DISTINCT_NAMES
from app.tools.base import (
    _UNREGISTERED_TOOL_LIST_LIMIT,
    TOOL_NOT_AVAILABLE_SENTINEL,
    CallableToolExecutor,
    CompositeToolExecutor,
    ScopedToolExecutor,
    ToolCall,
    ToolDefinition,
    build_llm_tool_names,
    format_tool_listing,
)

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _make_tool(name: str) -> ToolDefinition:
    return ToolDefinition(
        name=name,
        description=f"Tool: {name}",
        input_schema={"type": "object", "properties": {}},
        source="test",
    )


def _make_scoped_setup(
    allowed_names: list[str],
    registered_names: list[str] | None = None,
) -> tuple[ScopedToolExecutor, CompositeToolExecutor]:
    """Build a CompositeToolExecutor with *registered_names* tools, wrapped
    in a ScopedToolExecutor that only allows *allowed_names*."""
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
    """Stub LLM returning a pre-scripted sequence of responses."""

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
    """Neutralise Langfuse plumbing so the loop runs without real tracing."""
    monkeypatch.setattr(al, "_lf_start_llm_gen", lambda *a, **kw: None)
    monkeypatch.setattr(al, "_lf_end_generation", lambda *a, **kw: None)
    monkeypatch.setattr(al, "_lf_end_span", lambda *a, **kw: None)
    monkeypatch.setattr(al, "_lf_end_tool_span", lambda *a, **kw: None)
    monkeypatch.setattr(al, "_lf_start_iter_span", lambda **kw: None)
    monkeypatch.setattr(al, "_lf_start_tool_span", lambda **kw: None)


def _has_capability_nudge(messages: list[ChatMessage]) -> bool:
    return any(
        getattr(m, "role", None) == "system"
        and "capability not found" in (m.content or "")
        for m in messages
    )


def _capability_nudge_count(messages: list[ChatMessage]) -> int:
    return sum(
        1
        for m in messages
        if getattr(m, "role", None) == "system"
        and "capability not found" in (m.content or "")
    )


# ---------------------------------------------------------------------------
# Unit: ScopedToolExecutor error messages
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_scoped_executor_error_includes_available_tools():
    """Blocking a non-offered tool must list the tools that ARE available
    so the LLM can self-correct without guessing another name."""
    scoped, _ = _make_scoped_setup(
        allowed_names=["search_knowledge", "send_teams_message"],
        registered_names=["search_knowledge", "send_teams_message", "delete_all"],
    )

    result = await scoped.execute_tool(
        ToolCall(id="t1", name="send_microsoft_teams_channel_message", arguments={})
    )

    assert result.is_error is True
    assert TOOL_NOT_AVAILABLE_SENTINEL in result.content
    assert "search_knowledge" in result.content
    assert "send_teams_message" in result.content


@pytest.mark.asyncio
async def test_scoped_executor_suggests_close_match():
    """When a close name exists in the allowed set, it should appear as a
    'Did you mean' suggestion in the error message."""
    scoped, _ = _make_scoped_setup(allowed_names=["send_teams_message"])

    result = await scoped.execute_tool(
        ToolCall(id="t1", name="send_teams_msg", arguments={})
    )

    assert result.is_error is True
    assert "send_teams_message" in result.content


@pytest.mark.asyncio
async def test_scoped_executor_no_suggestions_when_no_close_match():
    """When no allowed name is close to the tried name, the error still
    lists available tools without a 'Did you mean' hint."""
    scoped, _ = _make_scoped_setup(allowed_names=["search_knowledge"])

    result = await scoped.execute_tool(
        ToolCall(id="t1", name="send_microsoft_teams_channel_message", arguments={})
    )

    assert result.is_error is True
    assert TOOL_NOT_AVAILABLE_SENTINEL in result.content
    assert "search_knowledge" in result.content
    assert "Did you mean" not in result.content


def test_build_llm_tool_names_collision_suffix_disambiguation():
    """Two originals that sanitise to the same name must be disambiguated
    with the same numeric-suffix rule ``run_agent_loop`` applies."""
    pairs = build_llm_tool_names(["teams.send", "teams/send", "teams send"])
    assert pairs == [
        ("teams.send", "teams_send"),
        ("teams/send", "teams_send_2"),
        ("teams send", "teams_send_3"),
    ]


def test_build_llm_tool_names_collision_with_already_clean_original():
    """An already-sanitised original claims its name too: a later original
    that sanitises to the same LLM-visible name must be suffixed instead of
    duplicating it (duplicate tool definitions are rejected by providers and
    would misroute ``_tool_name_map``). The rule is order-dependent in both
    directions — whichever original comes first keeps the bare name."""
    pairs = build_llm_tool_names(["teams_send", "teams.send"])
    assert pairs == [
        ("teams_send", "teams_send"),
        ("teams.send", "teams_send_2"),
    ]
    # Reversed order: the dotted original claims the bare name first.
    pairs = build_llm_tool_names(["teams.send", "teams_send"])
    assert pairs == [
        ("teams.send", "teams_send"),
        ("teams_send", "teams_send_2"),
    ]
    # All LLM-visible names must be unique regardless of composition.
    mixed = ["teams_send", "teams.send", "teams/send", "search_knowledge"]
    visible = [clean for _, clean in build_llm_tool_names(mixed)]
    assert len(visible) == len(set(visible))


def test_build_llm_tool_names_suffix_respects_64_char_limit():
    """Appending a collision suffix to a name already at the 64-char cap
    must truncate the base so the result stays within the provider limit."""
    long_a = "a" * 70  # sanitises to "a" * 64
    long_b = "a" * 64 + ".x"  # also sanitises to "a" * 64 — collision
    pairs = build_llm_tool_names([long_a, long_b])
    assert pairs[0][1] == "a" * 64
    assert pairs[1][1] == "a" * 62 + "_2"
    assert all(len(clean) <= 64 for _, clean in pairs)


def test_format_tool_listing_caps_long_lists():
    """Listings past the cap must be truncated with an '... and N more'
    suffix; short lists are joined verbatim; empty lists show '(none)'."""
    names = [f"tool_{i:03d}" for i in range(_UNREGISTERED_TOOL_LIST_LIMIT + 7)]
    capped = format_tool_listing(names)
    assert "... and 7 more" in capped
    assert names[_UNREGISTERED_TOOL_LIST_LIMIT - 1] in capped
    assert names[-1] not in capped
    assert format_tool_listing(["a", "b"]) == "a, b"
    assert format_tool_listing([]) == "(none)"


@pytest.mark.asyncio
async def test_scoped_executor_listing_disambiguates_sanitised_collisions():
    """When several allowed originals sanitise to the same LLM-visible name,
    the rejection listing must include the suffixed variants the LLM was
    actually offered (e.g. ``teams_send`` AND ``teams_send_2``) — including
    when one of the colliding originals is already sanitised."""
    allowed = ["teams_send", "teams.send", "teams/send", "search_knowledge"]
    scoped, _ = _make_scoped_setup(allowed_names=allowed)

    result = await scoped.execute_tool(
        ToolCall(id="t1", name="post_message", arguments={})
    )

    assert result.is_error is True
    assert "teams_send" in result.content
    assert "teams_send_2" in result.content
    assert "teams_send_3" in result.content


@pytest.mark.asyncio
async def test_scoped_executor_empty_allowed_set():
    """When no tools are allowed the error still contains the sentinel
    substring so the agent loop can detect it."""
    scoped, _ = _make_scoped_setup(
        allowed_names=[],
        registered_names=["some_tool"],
    )

    result = await scoped.execute_tool(
        ToolCall(id="t1", name="some_tool", arguments={})
    )

    assert result.is_error is True
    assert TOOL_NOT_AVAILABLE_SENTINEL in result.content
    assert "(none)" in result.content


# ---------------------------------------------------------------------------
# Integration: capability-not-found nudge in run_agent_loop
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_capability_not_found_nudge_fires_after_distinct_unavailable_failures(
    _quiet_langfuse,
):
    """After _MAX_NOT_AVAILABLE_DISTINCT_NAMES different tool names are
    rejected as 'not available', the loop must inject the
    capability-not-found nudge listing available tools."""
    allowed = ["search_knowledge"]
    scoped, _composite = _make_scoped_setup(
        allowed_names=allowed,
        registered_names=allowed,
    )

    # Scripted LLM tries 3 different unavailable tool names then gives up.
    hallucinated = [
        "send_microsoft_teams_channel_message",
        "list_microsoft_teams_channels",
        "send_teams_message",
    ]
    script = [
        ChatResponse(
            content="",
            model="stub",
            stop_reason="tool_calls",
            tool_calls=[ToolCall(id=f"t{i}", name=name, arguments={})],
        )
        for i, name in enumerate(hallucinated)
    ] + [ChatResponse(content="I cannot send Teams messages.", model="stub")]
    llm = _ScriptedLLM(script)
    messages: list[ChatMessage] = [ChatMessage(role="user", content="notify the team")]

    response = await al.run_agent_loop(
        llm=llm,
        messages=messages,
        tools=[_make_tool(n) for n in allowed],
        executor=scoped,
        max_iterations=10,
        progressive_tools=False,
    )

    assert response.content == "I cannot send Teams messages."
    assert _has_capability_nudge(messages), (
        f"Expected capability-not-found nudge after "
        f"{_MAX_NOT_AVAILABLE_DISTINCT_NAMES} distinct unavailable tool names"
    )
    nudge_msgs = [
        m for m in messages if getattr(m, "role", None) == "system"
        and "capability not found" in (m.content or "")
    ]
    assert len(nudge_msgs) == 1
    nudge_content = nudge_msgs[0].content or ""
    # The nudge must list what IS available
    assert "search_knowledge" in nudge_content
    # And list some of the tried names
    for tried in hallucinated:
        assert tried in nudge_content


@pytest.mark.asyncio
async def test_capability_not_found_nudge_fires_only_once(_quiet_langfuse):
    """The nudge must be injected exactly once even when many more distinct
    unavailable names are tried after the threshold is crossed."""
    allowed = ["search_knowledge"]
    scoped, _composite = _make_scoped_setup(
        allowed_names=allowed,
        registered_names=allowed,
    )

    # 5 distinct unavailable names — threshold is 3
    hallucinated = [
        "send_teams_message",
        "teams_send_channel_message",
        "microsoft_teams_post_message",
        "post_teams_channel_message",
        "create_microsoft_teams_message",
    ]
    script = [
        ChatResponse(
            content="",
            model="stub",
            stop_reason="tool_calls",
            tool_calls=[ToolCall(id=f"t{i}", name=name, arguments={})],
        )
        for i, name in enumerate(hallucinated)
    ] + [ChatResponse(content="giving up", model="stub")]
    llm = _ScriptedLLM(script)
    messages: list[ChatMessage] = [ChatMessage(role="user", content="go")]

    await al.run_agent_loop(
        llm=llm,
        messages=messages,
        tools=[_make_tool(n) for n in allowed],
        executor=scoped,
        max_iterations=15,
        progressive_tools=False,
    )

    assert _capability_nudge_count(messages) == 1, (
        "capability-not-found nudge must be injected exactly once per run"
    )


@pytest.mark.asyncio
async def test_capability_not_found_nudge_not_fired_below_threshold(_quiet_langfuse):
    """Fewer than _MAX_NOT_AVAILABLE_DISTINCT_NAMES distinct unavailable
    failures must NOT trigger the capability-not-found nudge."""
    allowed = ["search_knowledge"]
    scoped, _ = _make_scoped_setup(allowed_names=allowed, registered_names=allowed)

    # Only 2 distinct names (threshold is 3)
    hallucinated = ["send_teams_message", "teams_send_message"]
    script = [
        ChatResponse(
            content="",
            model="stub",
            stop_reason="tool_calls",
            tool_calls=[ToolCall(id=f"t{i}", name=name, arguments={})],
        )
        for i, name in enumerate(hallucinated)
    ] + [ChatResponse(content="done", model="stub")]
    llm = _ScriptedLLM(script)
    messages: list[ChatMessage] = [ChatMessage(role="user", content="go")]

    await al.run_agent_loop(
        llm=llm,
        messages=messages,
        tools=[_make_tool(n) for n in allowed],
        executor=scoped,
        max_iterations=10,
        progressive_tools=False,
    )

    assert not _has_capability_nudge(messages), (
        "capability-not-found nudge must not fire when fewer than "
        f"{_MAX_NOT_AVAILABLE_DISTINCT_NAMES} distinct unavailable names are tried"
    )


@pytest.mark.asyncio
async def test_capability_not_found_nudge_not_fired_for_regular_errors(_quiet_langfuse):
    """Tool errors that are NOT 'not available' (e.g. API failures) must
    NOT contribute to the capability-not-found counter."""
    callable_exec = CallableToolExecutor()

    async def _failing_tool(**kwargs):
        raise RuntimeError("API connection error")

    callable_exec.register("search_knowledge", _failing_tool)
    composite = CompositeToolExecutor()
    composite.add_tools([_make_tool("search_knowledge")], callable_exec)

    # No ScopedToolExecutor — failures are real errors, not "not available"
    script = [
        ChatResponse(
            content="",
            model="stub",
            stop_reason="tool_calls",
            tool_calls=[ToolCall(id=f"t{i}", name="search_knowledge", arguments={})],
        )
        for i in range(4)
    ] + [ChatResponse(content="failed", model="stub")]
    llm = _ScriptedLLM(script)
    messages: list[ChatMessage] = [ChatMessage(role="user", content="go")]

    await al.run_agent_loop(
        llm=llm,
        messages=messages,
        tools=[_make_tool("search_knowledge")],
        executor=composite,
        max_iterations=10,
        progressive_tools=False,
    )

    assert not _has_capability_nudge(messages), (
        "capability-not-found nudge must not fire for regular tool errors"
    )


@pytest.mark.asyncio
async def test_capability_not_found_nudge_caps_available_tools(_quiet_langfuse):
    """For agents with many bound tools, the nudge must list a capped
    sample with an '... and N more' suffix instead of every tool name."""
    extra = 5
    allowed = sorted(
        f"tool_{i:03d}" for i in range(_UNREGISTERED_TOOL_LIST_LIMIT + extra)
    )
    scoped, _ = _make_scoped_setup(allowed_names=allowed)

    hallucinated = [
        "send_teams_message",
        "teams_send_channel_message",
        "microsoft_teams_post_message",
    ]
    script = [
        ChatResponse(
            content="",
            model="stub",
            stop_reason="tool_calls",
            tool_calls=[ToolCall(id=f"t{i}", name=name, arguments={})],
        )
        for i, name in enumerate(hallucinated)
    ] + [ChatResponse(content="giving up", model="stub")]
    llm = _ScriptedLLM(script)
    messages: list[ChatMessage] = [ChatMessage(role="user", content="go")]

    await al.run_agent_loop(
        llm=llm,
        messages=messages,
        tools=[_make_tool(n) for n in allowed],
        executor=scoped,
        max_iterations=10,
        progressive_tools=False,
    )

    nudge_msgs = [
        m
        for m in messages
        if getattr(m, "role", None) == "system"
        and "capability not found" in (m.content or "")
    ]
    assert len(nudge_msgs) == 1
    nudge_content = nudge_msgs[0].content or ""
    # The loop offers its built-in ``wait`` primitive alongside the agent's own
    # tools, so the capped listing has one more name left to elide.
    assert f"... and {extra + 1} more" in nudge_content
    # The last (sorted) tool name falls past the cap and must NOT be listed
    assert allowed[-1] not in nudge_content
    # A name inside the cap IS listed
    assert allowed[0] in nudge_content
