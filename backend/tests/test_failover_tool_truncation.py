"""Tests for FailoverLLMProvider tool truncation on fallback.

Regression test for the bug where the OpenAI fallback provider rejected
requests when an agent had more than 128 tools (array_above_max_length).
"""

import asyncio
from typing import Any

from app.llm.base import BaseLLMProvider, ChatResponse
from app.llm.router import FailoverLLMProvider
from app.tools.base import ToolDefinition

# ---------------------------------------------------------------------------
# Minimal stub providers
# ---------------------------------------------------------------------------


class _StubProvider(BaseLLMProvider):
    """Minimal stub LLM provider for testing."""

    provider_name = "stub"
    max_tools: int = 1024

    def __init__(self, name: str, max_tools: int, fail: bool = False):
        self.provider_name = name
        self.max_tools = max_tools
        self._fail = fail
        self.received_tools: list[Any] | None = None

    async def chat(self, messages, model=None, temperature=0.7, max_tokens=4096, tools=None, **kwargs):
        self.received_tools = tools
        if self._fail:
            raise RuntimeError(f"{self.provider_name} failed")
        return ChatResponse(content="ok", model=model or "stub-model")

    async def chat_stream(self, messages, model=None, temperature=0.7, max_tokens=4096, **kwargs):
        self.received_tools = kwargs.get("tools")
        if self._fail:
            raise RuntimeError(f"{self.provider_name} failed")
        yield "ok"

    async def chat_stream_with_tools(self, messages, tools=None, *, on_token=None, model=None, temperature=0.7, max_tokens=4096, **kwargs):
        self.received_tools = tools
        if self._fail:
            raise RuntimeError(f"{self.provider_name} failed")
        return ChatResponse(content="ok", model=model or "stub-model")

    async def embed(self, text, model=None):
        return [0.0]

    async def list_models(self):
        return []


def _make_tools(count: int) -> list[ToolDefinition]:
    """Build *count* dummy ToolDefinition objects with varying priorities."""
    tools = []
    for i in range(count):
        # Rotate priorities to make the truncation meaningful
        priority = i % 3  # 0, 1, 2
        source = ["builtin", "custom", "integration"][i % 3]
        tools.append(
            ToolDefinition(
                name=f"tool_{i}",
                description=f"Tool number {i}",
                source=source,
                priority=priority,
            )
        )
    return tools


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_truncate_tools_no_truncation_needed():
    """No truncation when tool count is within the provider limit."""
    provider = _StubProvider("openai", max_tools=128)
    tools = _make_tools(10)
    result = FailoverLLMProvider._truncate_tools(tools, provider.max_tools)
    assert result == tools


def test_truncate_tools_truncates_to_limit():
    """Tools are truncated to the provider's max_tools limit."""
    provider = _StubProvider("openai", max_tools=128)
    tools = _make_tools(134)
    result = FailoverLLMProvider._truncate_tools(tools, provider.max_tools)
    assert result is not None
    assert len(result) == 128


def test_truncate_tools_priority_ordering():
    """Higher-priority (lower number) tools are retained after truncation."""
    provider = _StubProvider("openai", max_tools=2)
    tools = [
        ToolDefinition(name="low", description="low", source="integration", priority=2),
        ToolDefinition(name="medium", description="med", source="custom:abc", priority=1),
        ToolDefinition(name="high", description="high", source="builtin", priority=0),
    ]
    result = FailoverLLMProvider._truncate_tools(tools, provider.max_tools)
    assert result is not None
    assert len(result) == 2
    names = {t.name for t in result}
    # The two highest-priority tools should be retained
    assert "high" in names
    assert "medium" in names
    assert "low" not in names


def test_fit_tools_none_passthrough():
    """A call without tools is passed through unchanged."""
    provider = _StubProvider("openai", max_tools=128)
    fp = FailoverLLMProvider(primary=provider, fallbacks=[])
    kwargs = {"tools": None}
    assert asyncio.run(fp._fit_tools(kwargs, provider, None)) is kwargs


def test_failover_truncates_tools_for_fallback():
    """_failover_call truncates tools to the fallback provider's max_tools."""
    primary = _StubProvider("anthropic", max_tools=1024, fail=True)
    fallback = _StubProvider("openai", max_tools=128, fail=False)

    fp = FailoverLLMProvider(primary=primary, fallbacks=[fallback])

    # 134 tools — exceeds OpenAI's limit of 128 but within Anthropic's 1024
    tools = _make_tools(134)

    result = asyncio.run(
        fp.chat(messages=[{"role": "user", "content": "hi"}], tools=tools)
    )
    assert result.content == "ok"
    # Fallback must have received a truncated list
    assert fallback.received_tools is not None
    assert len(fallback.received_tools) == 128


def test_failover_truncates_tools_for_stream_with_tools():
    """_failover_call truncates tools in chat_stream_with_tools fallback path."""
    primary = _StubProvider("anthropic", max_tools=1024, fail=True)
    fallback = _StubProvider("openai", max_tools=128, fail=False)

    fp = FailoverLLMProvider(primary=primary, fallbacks=[fallback])

    tools = _make_tools(134)

    result = asyncio.run(
        fp.chat_stream_with_tools(
            messages=[{"role": "user", "content": "hi"}],
            tools=tools,
        )
    )
    assert result.content == "ok"
    assert fallback.received_tools is not None
    assert len(fallback.received_tools) == 128


def test_failover_no_truncation_when_within_limit():
    """No truncation when tool count is within the fallback provider's limit."""
    primary = _StubProvider("anthropic", max_tools=1024, fail=True)
    fallback = _StubProvider("openai", max_tools=128, fail=False)

    fp = FailoverLLMProvider(primary=primary, fallbacks=[fallback])

    tools = _make_tools(50)  # well within 128

    asyncio.run(
        fp.chat(messages=[{"role": "user", "content": "hi"}], tools=tools)
    )
    # All tools should be forwarded unchanged
    assert fallback.received_tools is not None
    assert len(fallback.received_tools) == 50


def test_failover_preserves_tools_for_primary():
    """Primary provider receives the full tool list (no truncation by failover)."""
    primary = _StubProvider("anthropic", max_tools=1024, fail=False)

    fp = FailoverLLMProvider(primary=primary, fallbacks=[])

    tools = _make_tools(134)

    asyncio.run(
        fp.chat(messages=[{"role": "user", "content": "hi"}], tools=tools)
    )
    # Primary should receive all tools unchanged
    assert primary.received_tools is not None
    assert len(primary.received_tools) == 134


def test_truncate_tools_stable_sort_for_equal_priority():
    """Truncation preserves insertion order for tools with equal priority and source.

    Python's ``sorted()`` is stable, so when two tools share the same sort key
    (priority + source_category), their original relative order must be
    preserved.  This is important because it determines which tools are dropped
    when many tools share the same tier — the first N in insertion order are
    kept, the rest are dropped.  Asserting this behaviour explicitly guards
    against regressions if the sort is ever changed.
    """
    provider = _StubProvider("openai", max_tools=2)

    # Three tools with identical priority (1) and source ("integration").
    # Only the first two (by insertion order) should be retained.
    tools = [
        ToolDefinition(name="alpha", description="a", source="integration", priority=1),
        ToolDefinition(name="beta", description="b", source="integration", priority=1),
        ToolDefinition(name="gamma", description="c", source="integration", priority=1),
    ]

    result = FailoverLLMProvider._truncate_tools(tools, provider.max_tools)
    assert result is not None
    assert len(result) == 2
    # Stable sort: first two in insertion order are retained
    assert result[0].name == "alpha"
    assert result[1].name == "beta"
    # Third tool is dropped
    assert all(t.name != "gamma" for t in result)


def test_unknown_source_priority_is_derived_from_map():
    """``TOOL_UNKNOWN_SOURCE_PRIORITY`` must always equal the largest value
    in ``TOOL_SOURCE_PRIORITY`` so unknown sources are ranked at or below
    the lowest known category. Adding or shifting an entry in the map
    without re-evaluating the unknown fallback would silently break the
    failover truncation tiebreaker; this invariant guards against that
    drift."""
    from app.tools.base import TOOL_SOURCE_PRIORITY, TOOL_UNKNOWN_SOURCE_PRIORITY

    assert max(TOOL_SOURCE_PRIORITY.values()) == TOOL_UNKNOWN_SOURCE_PRIORITY


def test_truncate_tools_unknown_source_ranked_with_lowest_known():
    """An unknown ``source`` value should rank with the lowest-priority
    known category (currently ``integration``), so unknown-source tools
    are dropped before higher-priority sources when the list must be
    trimmed. This locks the tiebreaker behavior that
    ``TOOL_UNKNOWN_SOURCE_PRIORITY = max(...)`` enables."""
    provider = _StubProvider("openai", max_tools=2)

    tools = [
        ToolDefinition(name="unknown", description="x", source="something-new", priority=1),
        ToolDefinition(name="builtin-1", description="b", source="builtin", priority=1),
        ToolDefinition(name="builtin-2", description="b", source="builtin", priority=1),
    ]

    result = FailoverLLMProvider._truncate_tools(tools, provider.max_tools)
    assert result is not None
    assert {t.name for t in result} == {"builtin-1", "builtin-2"}
    assert all(t.name != "unknown" for t in result)


def test_truncate_tools_custom_source_ranked_above_integration():
    """A custom tool (real ``source="custom:<uuid>"``) must outrank an
    ``integration`` tool at equal per-tool priority, so it survives truncation.

    Custom tools are emitted with ``source="custom:<uuid>"`` (see
    ``services/builtin_tools.load_custom_tools``). The source-priority map keys
    on the category before the colon (``custom``), ranked just below ``builtin``
    and above ``mcp``/``integration``. Keying on ``custom_tool`` instead would
    mis-rank custom tools as an unknown source — dropped as aggressively as
    integration tools — which this test guards against."""
    provider = _StubProvider("openai", max_tools=1)

    tools = [
        ToolDefinition(
            name="integration-tool",
            description="i",
            source="integration:notion",
            priority=1,
        ),
        ToolDefinition(
            name="custom-tool",
            description="c",
            source="custom:11111111-1111-1111-1111-111111111111",
            priority=1,
        ),
    ]

    result = FailoverLLMProvider._truncate_tools(tools, provider.max_tools)
    assert result is not None
    assert len(result) == 1
    # The custom tool wins the source-category tiebreaker at equal priority.
    assert result[0].name == "custom-tool"


def test_run_agent_loop_truncation_builtin_survives_high_priority_integration():
    """The PRIMARY truncation path pins the same ordering contract.

    ``run_agent_loop`` carries its own copy of the truncation sort (the
    pre-loop "Tool count cap" block in ``app/tools/agent_loop.py``), separate
    from ``FailoverLLMProvider._truncate_tools``.  The failover
    tests below cannot catch a drift in the loop's copy — and the loop is the
    path that originally evicted the agent-builder builtins — so this test
    drives ``run_agent_loop`` directly: a priority-0 integration tool must
    not push default-priority builtin tools out under a tight provider cap.
    """
    from app.llm.base import ChatMessage
    from app.tools.agent_loop import run_agent_loop

    llm = _StubProvider("openai", max_tools=2)
    tools = [
        ToolDefinition(
            name="integration-p0",
            description="i",
            source="integration:opencti",
            priority=0,
        ),
        ToolDefinition(
            name="builtin-default-prio",
            description="b",
            source="builtin",
            priority=1,
        ),
        ToolDefinition(
            name="builtin-late",
            description="b",
            source="builtin",
            priority=2,
        ),
    ]

    response = asyncio.run(
        run_agent_loop(
            llm=llm,
            messages=[ChatMessage(role="user", content="go")],
            tools=tools,
            max_iterations=3,
            model="stub-model",
            progressive_tools=False,
        )
    )

    assert response.content == "ok"
    assert llm.received_tools is not None
    offered = {t.name for t in llm.received_tools}
    # Source category dominates per-tool priority: both builtins survive, the
    # priority-0 integration tool is the one dropped.
    assert {"builtin-default-prio", "builtin-late"} <= offered
    assert "integration-p0" not in offered


def test_truncate_tools_builtin_survives_high_priority_integration():
    """Source category DOMINATES per-tool priority.

    Regression: the sort key used to be ``(priority, source_category)``, so an
    integration tool declaring ``priority=0`` evicted builtin platform tools
    (``priority=1`` default) under a tight provider tool cap — on OpenAI
    (max_tools=128) with several large integrations bound, the agent-builder
    and other builtin tools silently vanished. The key is now
    ``(source_category, priority)``: a builtin can never be evicted by an
    integration tool, whatever priority the integration declares."""
    provider = _StubProvider("openai", max_tools=2)

    tools = [
        ToolDefinition(
            name="integration-p0",
            description="i",
            source="integration:opencti",
            priority=0,
        ),
        ToolDefinition(
            name="builtin-default-prio",
            description="b",
            source="builtin",
            priority=1,
        ),
        ToolDefinition(
            name="builtin-late",
            description="b",
            source="builtin",
            priority=2,
        ),
    ]

    result = FailoverLLMProvider._truncate_tools(tools, provider.max_tools)
    assert result is not None
    assert {t.name for t in result} == {"builtin-default-prio", "builtin-late"}


class _PerModelStub(_StubProvider):
    """A gateway-like provider whose cap depends on the model (#11639)."""

    def __init__(self, name: str, caps: dict[str, int], fail: bool = False):
        super().__init__(name, max_tools=128, fail=fail)
        self.caps = caps

    async def max_tools_for(self, model):
        return self.caps.get(model, self.max_tools)


def test_wrapper_cap_is_the_primary_model_cap():
    primary = _PerModelStub("gateway", {"claude": 1024})
    fp = FailoverLLMProvider(primary=primary, fallbacks=[])
    assert asyncio.run(fp.max_tools_for("claude")) == 1024
    assert asyncio.run(fp.max_tools_for("gpt")) == 128


def test_failover_truncates_to_the_fallback_model_cap():
    primary = _StubProvider("anthropic", max_tools=1024, fail=True)
    fallback = _PerModelStub("gateway", {"claude": 1024})
    fallback.default_model = "claude"
    fp = FailoverLLMProvider(primary=primary, fallbacks=[fallback])

    asyncio.run(fp.chat(messages=[{"role": "user", "content": "hi"}], tools=_make_tools(300)))
    assert len(fallback.received_tools) == 300


def test_decline_route_truncates_to_the_routed_model_cap():
    """Calls sent straight to a decline fallback fit its cap, not the requested model's."""
    primary = _PerModelStub("gateway", {"claude": 1024, "gpt": 128})
    fp = FailoverLLMProvider(primary=primary, fallbacks=[])
    fp._decline_routes = {"claude": "gpt"}

    asyncio.run(fp.chat(messages=[{"role": "user", "content": "hi"}], model="claude", tools=_make_tools(300)))
    assert len(primary.received_tools) == 128


def test_decline_route_truncates_a_stream_too():
    primary = _PerModelStub("gateway", {"claude": 1024, "gpt": 128})
    fp = FailoverLLMProvider(primary=primary, fallbacks=[])
    fp._decline_routes = {"claude": "gpt"}

    async def _drain():
        async for _ in fp.chat_stream([{"role": "user", "content": "hi"}], model="claude", tools=_make_tools(300)):
            pass

    asyncio.run(_drain())
    assert len(primary.received_tools) == 128


def test_cap_is_read_through_a_delegating_wrapper():
    """Chat hands the loop proxies that delegate attributes without subclassing the provider."""

    class _Proxy:
        def __init__(self, llm):
            self._llm = llm

        def __getattr__(self, name):
            return getattr(self._llm, name)

    from app.llm.base import provider_max_tools

    assert asyncio.run(provider_max_tools(_Proxy(_PerModelStub("gateway", {"claude": 1024})), "claude")) == 1024
