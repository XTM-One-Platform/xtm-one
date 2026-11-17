"""Context-window occupancy readout surfaced to the chat composer.

The chat context gauge (a ring plus percentage in the composer, the
affordance Cursor popularised) is fed by two pieces:

  * The agent loop hangs ``context_tokens`` / ``context_window`` /
    ``context_breakdown`` / ``context_level`` on the per-iteration
    ``thinking`` event.  It rides on an existing event on purpose — a
    ``status`` of its own would surface as an unknown phase in the web chat's
    thinking indicator.
  * ``services/context_usage.py`` reads those figures back, on the platform
    SSE path and in the native web chat alike, which persist them on
    the assistant message so reopening the conversation restores the gauge.

What the figures mean matters as much as their presence.  Like the loop's own
budgeting, they are char-derived estimates rather than billed token counts, so
the gauge predicts the summarising the user is about to see instead of
reporting on a turn that already ended.  They are NOT the same number as the
gate's ``_estimated_tokens`` though: the readout also counts the tool schemas,
which the gate ignores, so it is deliberately the larger of the two - and the
warning is the loop's ``context_level``, decided on the gate's own measure
against its own trigger, never a ratio of the readout.
"""

import json
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.api.v1.platform import RESTORED_ASSISTANT_META_KEYS
from app.llm.base import BaseLLMProvider, ChatMessage, ChatResponse
from app.llm.model_limits import (
    resolve_effective_context_window,
    resolve_snapshot_context_window,
)
from app.services.context_usage import (
    CONTEXT_LEVELS,
    CONTEXT_USAGE_META_KEYS,
    ContextUsage,
    ContextUsageTracker,
    context_usage_from_event,
    context_usage_meta,
    read_context_usage,
)
from app.tools.agent_loop import (
    _CHARS_PER_TOKEN,
    _CONTEXT_APPROACHING_SHARE,
    _context_level,
    run_agent_loop,
)


class _LoopStubLLM(BaseLLMProvider):
    provider_name = "stub"
    context_window = 10_000

    async def chat(self, messages, model=None, temperature=0.7, max_tokens=4096, tools=None, **kwargs):
        return ChatResponse(content="final answer", model=model or "stub")

    async def chat_stream(self, messages, model=None, temperature=0.7, max_tokens=4096, tools=None, **kwargs):
        yield "final answer"

    async def embed(self, text, model=None):
        return [0.0]

    async def list_models(self):
        return []


class _PerModelWindowLLM(_LoopStubLLM):
    """Provider with an admin per-model override on top of the flat default."""

    context_windows = {"big-model": 1_000_000}


async def _run_and_collect(
    llm, messages, tools=None, max_iterations=1, **kwargs
) -> list[dict]:
    """Run one turn and return every event it emitted.

    ``max_iterations`` is a parameter because the loop's wind-down strips tools
    two iterations before the cap: at the default 1 the very first iteration
    already runs tool-free, so a test that needs the tool schemas in context must
    leave the loop some headroom.
    """
    events: list[dict] = []

    async def _on_event(event: dict) -> None:
        events.append(event)

    await run_agent_loop(
        llm=llm,
        messages=messages,
        tools=tools,
        max_iterations=max_iterations,
        on_event=_on_event,
        **kwargs,
    )
    return events


def _thinking_events(events: list[dict]) -> list[dict]:
    return [e for e in events if e.get("status") == "thinking"]


@pytest.mark.asyncio
async def test_thinking_event_carries_context_occupancy():
    """Every iteration tick reports how full the window is."""
    content = "x" * 900
    events = await _run_and_collect(
        _LoopStubLLM(), [ChatMessage(role="user", content=content)]
    )

    thinking = _thinking_events(events)
    assert thinking, "the loop must still emit its iteration tick"
    first = thinking[0]
    assert first["context_window"] == 10_000
    # The loop's own char-derived estimate, not a provider token count.
    assert first["context_tokens"] == len(content) // _CHARS_PER_TOKEN
    # The tick keeps its existing payload — consumers reading `iteration`
    # must not have to care that the frame grew.
    assert first["iteration"] == 1


@pytest.mark.asyncio
async def test_context_window_honors_per_model_override():
    """A per-model window (admin override) wins over the provider default."""
    events = await _run_and_collect(
        _PerModelWindowLLM(),
        [ChatMessage(role="user", content="question")],
        model="big-model",
    )

    thinking = _thinking_events(events)
    assert thinking
    assert thinking[0]["context_window"] == 1_000_000


@pytest.mark.asyncio
async def test_reported_occupancy_is_what_the_turn_actually_carries():
    """The reported figure must describe the context AT SEND TIME.

    The loop curates and compacts before it calls the model, so a gauge read
    from before that work would tell the user to split a conversation the
    backend just made room in.  Asserted as an equality against the live
    message list (which ``run_agent_loop`` mutates in place) rather than "is
    smaller than before", so the test pins the ordering exactly instead of
    depending on how much any one compaction pass happens to reclaim.
    """
    messages = [ChatMessage(role="user", content="question")]
    for idx in range(6):
        messages.append(
            ChatMessage(
                role="tool",
                content=f"tool-{idx}-" + ("x" * 450),
                tool_call_id=f"tool-{idx}",
                name="stub_tool",
            )
        )
    reported: list[tuple[int, int]] = []
    statuses: list[str] = []

    async def _on_event(event: dict) -> None:
        statuses.append(str(event.get("status")))
        if event.get("status") != "thinking" or "context_tokens" not in event:
            return
        live = sum(len(m.content or "") for m in messages) // _CHARS_PER_TOKEN
        reported.append((event["context_tokens"], live))

    await run_agent_loop(
        llm=_LoopStubLLM(),
        messages=messages,
        tools=None,
        max_iterations=1,
        max_session_chars=1_200,
        on_event=_on_event,
    )

    # The fixture is sized to cross the compaction gate, so the equality below
    # is asserted on a run that really did rewrite the message list.
    assert "context_compaction" in statuses
    assert reported, "the iteration tick must carry a reading"
    for claimed, live in reported:
        assert claimed == live


# ── The warning level ─────────────────────────────────────────────


def _statuses(events: list[dict]) -> set[str]:
    return {str(e.get("status")) for e in events}


def test_the_level_is_read_on_the_gate_measure_against_its_trigger():
    trigger = 10_000
    below = int(trigger * _CONTEXT_APPROACHING_SHARE) - 1
    assert _context_level(0, trigger, overflowed=False) == "normal"
    assert _context_level(below, trigger, overflowed=False) == "normal"
    assert _context_level(below + 1, trigger, overflowed=False) == "approaching"
    # Over the trigger - compaction could not catch up - is still a warning:
    # nothing was cut.
    assert _context_level(trigger * 2, trigger, overflowed=False) == "approaching"
    # Once the emergency prune cut older turns, the run says so.
    assert _context_level(0, trigger, overflowed=True) == "overflowed"


@pytest.mark.asyncio
async def test_the_level_ignores_the_tool_schemas_the_gate_does_not_measure():
    """Tool schemas take the readout past the share of the window the gate
    compacts at, yet nothing is summarized: the level follows the gate, not a
    ratio of the readout."""
    from app.tools.base import ToolDefinition

    tools = [
        ToolDefinition(name=f"mcp_thing_{i}", description="d" * 9_000, source="mcp:acme")
        for i in range(2)
    ]
    events = await _run_and_collect(
        _LoopStubLLM(),
        [ChatMessage(role="user", content="question")],
        tools=tools,
        max_iterations=8,
    )

    first = _thinking_events(events)[0]
    assert first["context_tokens"] / first["context_window"] >= 0.6
    assert first["context_level"] == "normal"
    assert "context_compaction" not in _statuses(events)


@pytest.mark.asyncio
async def test_the_level_warns_before_the_conversation_reaches_the_compaction_trigger():
    """``max_session_chars`` is the gate's trigger: a conversation close to it
    warns before anything is summarized, one well below it does not."""
    budget = 2_000
    near = await _run_and_collect(
        _LoopStubLLM(), [ChatMessage(role="user", content="x" * 1_800)], max_session_chars=budget
    )
    far = await _run_and_collect(
        _LoopStubLLM(), [ChatMessage(role="user", content="x" * 900)], max_session_chars=budget
    )

    assert _thinking_events(near)[0]["context_level"] == "approaching"
    assert "context_compaction" not in _statuses(near)
    assert _thinking_events(far)[0]["context_level"] == "normal"


@pytest.mark.asyncio
async def test_the_level_says_so_once_the_emergency_prune_cut_the_run():
    """One message past 95% of the window: compaction has no older turn to
    summarize, so only the emergency prune makes it fit."""
    events = await _run_and_collect(
        _LoopStubLLM(), [ChatMessage(role="user", content="x" * 40_000)]
    )

    assert _thinking_events(events)[0]["context_level"] == "overflowed"


# ── Breakdown (the detail popover) ────────────────────────────────────────


@pytest.mark.asyncio
async def test_breakdown_buckets_sum_to_the_reported_total():
    """The popover shows the lines AND the headline, so they must agree.

    Per-bucket floor division leaves the lines short of the total by up to one
    token each; a breakdown whose parts do not add up reads as broken numbers
    rather than as rounding.
    """
    messages = [
        ChatMessage(role="system", content="p" * 700),
        ChatMessage(role="user", content="q" * 500),
        ChatMessage(role="tool", content="r" * 800, tool_call_id="t1", name="stub_tool"),
    ]
    events = await _run_and_collect(_LoopStubLLM(), messages)

    thinking = _thinking_events(events)
    assert thinking
    breakdown = thinking[0]["context_breakdown"]
    assert sum(breakdown.values()) == thinking[0]["context_tokens"]
    # And each kind landed in its own bucket rather than being lumped together.
    assert set(breakdown) == {"system", "conversation", "tool_results"}


@pytest.mark.asyncio
async def test_breakdown_counts_tool_schemas_and_splits_them_by_origin():
    """Tool definitions are part of what the call carries, and the ones the user
    can actually remove (integrations / MCP / custom tools) are reported apart
    from the platform's built-ins."""
    from app.tools.base import ToolDefinition

    tools = [
        ToolDefinition(name="builtin_search", description="d" * 300, source="builtin:search"),
        ToolDefinition(name="mcp_thing", description="d" * 3000, source="mcp:acme"),
        ToolDefinition(name="integration_thing", description="d" * 3000, source="integration:github"),
    ]
    events = await _run_and_collect(
        _LoopStubLLM(),
        [ChatMessage(role="user", content="question")],
        tools=tools,
        max_iterations=8,
    )

    thinking = _thinking_events(events)
    assert thinking
    breakdown = thinking[0]["context_breakdown"]
    assert breakdown["tools"] > 0
    # Two dynamic tools of ~3000 chars each against the builtin bucket, which
    # also carries the loop's own ``wait`` timer — its schema states the
    # resolved caps, so it is not negligible.  The margin is what makes this a
    # test of the split rather than of the timer's exact description length.
    assert breakdown["dynamic_tools"] > breakdown["tools"]
    assert sum(breakdown.values()) == thinking[0]["context_tokens"]


# ── Context-window resolution (the gauge's denominator) ──────────────────


def test_snapshot_window_reads_max_input_not_max_tokens():
    """``max_tokens`` in the snapshot is the OUTPUT cap, not the window.

    Claude Sonnet 4.5 lists ``max_input_tokens: 200000`` alongside
    ``max_tokens: 64000``. Accepting the latter would report a window three
    times too small and pin the gauge near 100 % on an almost-empty chat.
    """
    assert resolve_snapshot_context_window("claude-sonnet-4-5") == 200_000


@pytest.mark.parametrize(
    "model",
    [
        "claude-sonnet-4-5",
        # Vendor-prefixed (OpenRouter / Vertex style).
        "anthropic/claude-sonnet-4-5",
        # Bedrock cross-region id with a version suffix.
        "us.anthropic.claude-sonnet-4-5-20250929-v1:0",
    ],
)
def test_snapshot_window_matches_every_id_dialect(model):
    """The same model reaches us under several ids; all must resolve."""
    assert resolve_snapshot_context_window(model) == 200_000


def test_snapshot_beats_optimistic_provider_default():
    """THE bug this chain exists for.

    Anthropic / Bedrock / OpenRouter / Gemini all declare a flat 1M window for
    every model they can reach. Consulting that before the snapshot budgets a
    200k model against five times its real capacity, so the loop's compaction
    gate waits for headroom that does not exist and the provider rejects the
    prompt first.
    """
    assert (
        resolve_effective_context_window(
            model="claude-sonnet-4-5",
            overrides=None,
            provider_default=1_000_000,
        )
        == 200_000
    )


def test_admin_override_beats_the_snapshot():
    """An admin who sets a window in Settings -> AI Models has the last word."""
    assert (
        resolve_effective_context_window(
            model="claude-sonnet-4-5",
            overrides={"claude-sonnet-4-5": 500_000},
            provider_default=1_000_000,
        )
        == 500_000
    )


def test_family_floor_covers_a_model_the_snapshot_has_not_caught_up_with():
    assert (
        resolve_effective_context_window(
            model="claude-sonnet-9-9-20991231",
            overrides=None,
            provider_default=1_000_000,
        )
        == 200_000
    )


@pytest.mark.parametrize(
    "model",
    [
        "claude-opus-5-5-20991231",
        "claude-sonnet-5-5-20991231",
        "claude-fable-5-1-20991231",
        "claude-mythos-5-1",
        "us.anthropic.claude-mythos-5-1-v1:0",
        "claude-mythos-preview",
    ],
)
def test_family_floor_gives_every_claude_5_model_its_1m_window(model):
    """Behind a gateway whose own default is smaller, a Claude 5 model the
    snapshot does not list is still budgeted against its 1M window."""
    assert (
        resolve_effective_context_window(
            model=model,
            overrides=None,
            provider_default=128_000,
        )
        == 1_000_000
    )


@pytest.mark.parametrize(
    ("model", "expected"),
    [
        # mini/nano variants keep the 272k input window — the broad dotted-gen
        # floors (gpt-5.4/5.5/5.6 at 1.05M) must NOT catch them, or a new
        # dated variant missing from the snapshot is over-budgeted and hits
        # provider context errors. Dated suffixes miss the snapshot
        # on purpose so the family floor is what resolves.
        ("gpt-5.4-mini-2099-12-31", 272_000),
        ("gpt-5.4-nano-2099-12-31", 272_000),
        ("gpt-5.5-mini-2099-12-31", 272_000),
        ("gpt-5.6-nano-2099-12-31", 272_000),
        # The full-size dotted generations keep the 1.05M floor.
        ("gpt-5.4-2099-12-31", 1_050_000),
        ("gpt-5.6-pro-2099-12-31", 1_050_000),
    ],
)
def test_family_floor_mini_nano_variants_not_over_budgeted(model, expected):
    assert (
        resolve_effective_context_window(
            model=model,
            overrides=None,
            provider_default=128_000,
        )
        == expected
    )


def test_unknown_model_falls_back_to_the_provider_default():
    assert (
        resolve_effective_context_window(
            model="some-vendor/unlisted-model",
            overrides=None,
            provider_default=256_000,
        )
        == 256_000
    )


@pytest.mark.parametrize("provider_default", [None, 0, -1])
def test_window_is_always_positive(provider_default):
    """Every consumer divides by this, so it can never be 0 or None."""
    assert (
        resolve_effective_context_window(
            model=None, overrides=None, provider_default=provider_default
        )
        == 128_000
    )


def test_context_usage_read_from_thinking_event():
    assert context_usage_from_event(
        {
            "status": "thinking",
            "iteration": 2,
            "context_tokens": 84_000,
            "context_window": 200_000,
            "context_breakdown": {"conversation": 80_000, "system": 4_000},
            "context_level": "approaching",
        }
    ) == ContextUsage(84_000, 200_000, {"conversation": 80_000, "system": 4_000}, "approaching")


def test_context_usage_survives_a_missing_or_malformed_breakdown():
    """The breakdown only enriches the readout, so a bad one must not cost the
    gauge its number — it degrades to ``None`` instead."""
    base = {"status": "thinking", "context_tokens": 84_000, "context_window": 200_000}

    assert context_usage_from_event(base) == ContextUsage(84_000, 200_000)
    for bad in ("nope", [], {"conversation": "lots"}, {"conversation": 0}, {"conversation": True}, {}):
        assert context_usage_from_event({**base, "context_breakdown": bad}) == ContextUsage(
            84_000, 200_000
        )


def test_context_usage_keeps_only_a_level_the_loop_publishes():
    """The level is the loop's verdict: a value it never sends is dropped, and
    that costs the gauge its warning, never its number."""
    base = {"status": "thinking", "context_tokens": 84_000, "context_window": 200_000}

    for level in CONTEXT_LEVELS:
        assert context_usage_from_event({**base, "context_level": level}).level == level
    for bad in ("compacting", "", None, 1, True, ["normal"]):
        assert context_usage_from_event({**base, "context_level": bad}) == ContextUsage(
            84_000, 200_000
        )


def test_context_usage_rejects_a_partially_valid_breakdown():
    """Salvaging the good half of a bad breakdown is worse than dropping it.

    The readout shows the per-bucket lines and the headline together, so lines
    that no longer add up read as broken numbers — the exact failure the
    exact-sum design exists to prevent. Anything needing repair is foreign by
    definition: the loop always emits buckets summing to ``context_tokens``.
    """
    base = {"status": "thinking", "context_tokens": 84_000, "context_window": 200_000}

    # One unusable entry alongside valid ones: the whole breakdown goes.
    assert context_usage_from_event(
        {**base, "context_breakdown": {"conversation": 80_000, "system": "four thousand"}}
    ) == ContextUsage(84_000, 200_000)

    # Every entry usable, but they do not add up to the headline.
    assert context_usage_from_event(
        {**base, "context_breakdown": {"conversation": 40_000, "system": 4_000}}
    ) == ContextUsage(84_000, 200_000)

    # The whole thing, intact, still passes.
    intact = {"conversation": 80_000, "system": 4_000}
    assert context_usage_from_event(
        {**base, "context_breakdown": intact}
    ) == ContextUsage(84_000, 200_000, intact)


def test_tool_definition_chars_memoizes_per_run():
    """The readout runs every iteration; the schemas cannot have changed.

    Without the memo a heavily-wired agent re-serialises tens of thousands of
    characters of JSON per tick for a figure that is identical each time.
    """
    from app.tools.agent_loop import _tool_definition_chars
    from app.tools.base import ToolDefinition

    tool = ToolDefinition(
        name="search",
        description="d" * 200,
        input_schema={"type": "object", "properties": {"q": {"type": "string"}}},
    )
    cache: dict[int, int] = {}

    first = _tool_definition_chars(tool, cache)
    assert first > 0
    assert cache == {id(tool): first}
    # Second call is served from the memo and agrees with the uncached figure.
    assert _tool_definition_chars(tool, cache) == first
    assert _tool_definition_chars(tool) == first


def test_context_usage_accepts_an_empty_context():
    """Zero tokens is a legitimate reading — an empty ring, not a missing one."""
    assert context_usage_from_event(
        {"status": "thinking", "context_tokens": 0, "context_window": 200_000}
    ) == ContextUsage(0, 200_000)


@pytest.mark.parametrize(
    "event",
    [
        # Only the iteration tick carries the figures; other events must not be
        # mined for stray keys.
        {"status": "tool_start", "context_tokens": 10, "context_window": 100},
        {"status": "thinking"},  # pre-readout backend / older loop
        {"status": "thinking", "context_tokens": 10},  # no window to divide by
        {"status": "thinking", "context_window": 100},  # no count
        {"status": "thinking", "context_tokens": 10, "context_window": 0},
        {"status": "thinking", "context_tokens": -1, "context_window": 100},
        {"status": "thinking", "context_tokens": "10", "context_window": 100},
        {"status": "thinking", "context_tokens": 10.5, "context_window": 100},
        # `bool` is an `int` subclass in Python — `True` is not a token count.
        {"status": "thinking", "context_tokens": True, "context_window": 100},
        {"status": "thinking", "context_tokens": 10, "context_window": True},
        {},
    ],
)
def test_context_usage_rejects_unusable_events(event):
    assert context_usage_from_event(event) is None


# ── Restore ───────────────────────────────────────────────────────────────


def test_restore_includes_the_context_keys():
    """A reopened conversation gets its gauge back: the context keys ride the
    same restore payload as the reasoning details."""
    assert set(CONTEXT_USAGE_META_KEYS) <= set(RESTORED_ASSISTANT_META_KEYS)
    assert "context_level" in CONTEXT_USAGE_META_KEYS
    assert "tool_call_trace" in RESTORED_ASSISTANT_META_KEYS


# ── The native web chat ───────────────────────────────────────────


def test_a_stored_reading_reads_back_whatever_carried_it():
    """A message's metadata has no ``status``: the stored reading is read by
    the same rules as the event it came from, and round-trips unchanged."""
    usage = ContextUsage(84_000, 200_000, {"conversation": 80_000, "system": 4_000}, "approaching")
    meta = context_usage_meta(usage)
    assert tuple(meta) == CONTEXT_USAGE_META_KEYS
    assert read_context_usage(meta) == usage
    assert context_usage_meta(None) == {}
    # A reading without its breakdown or level stores the two numbers alone.
    assert context_usage_meta(ContextUsage(10, 100)) == {"context_tokens": 10, "context_window": 100}


def test_the_tracker_keeps_the_last_reading_of_the_run():
    tracker = ContextUsageTracker()
    assert tracker.meta() == {}

    tracker.observe({"status": "thinking", "context_tokens": 10, "context_window": 100})
    tracker.observe({"status": "thinking", "context_tokens": 30, "context_window": 100})
    # Another event, or a tick of an older loop without figures, keeps it.
    tracker.observe({"status": "tool_start", "context_tokens": 99, "context_window": 100})
    tracker.observe({"status": "thinking", "iteration": 3})
    # A consulted agent's tick rides a wrapper and is not this run's context.
    tracker.observe(
        {
            "status": "consult_progress",
            "inner": {"status": "thinking", "context_tokens": 70, "context_window": 100},
        }
    )
    assert tracker.meta() == {"context_tokens": 30, "context_window": 100}

    tracker.clear()
    assert tracker.meta() == {}


def _stored_message(metadata: dict | None) -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid4(),
        conversation_id=uuid4(),
        role="assistant",
        content="Hello",
        channel="web",
        channel_message_id=None,
        sequence=1,
        created_at=datetime.now(UTC),
        metadata_json=json.dumps(metadata) if metadata is not None else None,
        llm_trace_id=None,
    )


def test_a_reopened_web_conversation_serves_the_reading_of_its_messages():
    from app.api.v1.chat import _build_message_response

    breakdown = {"conversation": 80_000, "system": 4_000}
    response = _build_message_response(
        _stored_message(
            {
                "context_tokens": 84_000,
                "context_window": 200_000,
                "context_breakdown": breakdown,
                "context_level": "overflowed",
            }
        )
    )
    assert response.context_tokens == 84_000
    assert response.context_window == 200_000
    assert response.context_breakdown == breakdown
    assert response.context_level == "overflowed"

    # A level the loop never publishes is dropped with the reading kept.
    response = _build_message_response(
        _stored_message({"context_tokens": 10, "context_window": 100, "context_level": "compacting"})
    )
    assert (response.context_tokens, response.context_level) == (10, None)

    # No reading, or one the gauge could not show, serves none.
    for meta in (
        None,
        {},
        {"context_tokens": 84_000},
        {"context_tokens": 1, "context_window": 0, "context_level": "normal"},
    ):
        response = _build_message_response(_stored_message(meta))
        assert response.context_tokens is None
        assert response.context_window is None
        assert response.context_breakdown is None
        assert response.context_level is None
