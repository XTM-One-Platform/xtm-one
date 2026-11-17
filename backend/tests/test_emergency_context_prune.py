"""Tests for the emergency context pruning mechanism helpers.

Covers:
  - _emergency_prune_session_context: all five tiered phases
    * Phase 1: tool-result compression (delegates to _prune_session_context)
    * Phase 2: user/assistant/tool message truncation
    * Phase 3: oldest non-system message eviction
    * Phase 4: dynamic-floor in-place truncation of the protected tail
    * Phase 5: system-prompt overflow head+tail truncation (last resort)
  - _emergency_truncate_text: the shared, expansion-guarded truncation helper
  - Edge cases: conversation with no tool messages, already-small messages,
    and conversations too large even after all phases
  - Regression: the scenario where the last _EMERGENCY_KEEP_MESSAGES
    messages alone exceed the budget AND the system prompt is memory-bloated
"""

from __future__ import annotations

from app.llm.base import ChatMessage
from app.tools.agent_loop import (
    _CHARS_PER_TOKEN,
    _EMERGENCY_ASSISTANT_TRUNCATE_KEEP,
    _EMERGENCY_KEEP_MESSAGES,
    _EMERGENCY_SYSTEM_MIN_KEEP,
    _EMERGENCY_SYSTEM_TRUNCATE_MIN,
    _RECENT_TOOL_RESULTS_KEEP,
    _build_evictable_message_groups,
    _emergency_prune_session_context,
    _emergency_truncate_text,
)
from app.tools.base import ToolCall

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _msg(role: str, chars: int = 100, content: str | None = None) -> ChatMessage:
    """Create a ChatMessage with content of the given length (or explicit text)."""
    if content is None:
        content = "x" * chars
    return ChatMessage(role=role, content=content)


def _total_chars(messages: list[ChatMessage]) -> int:
    return sum(len(m.content or "") for m in messages)


# ---------------------------------------------------------------------------
# Phase 1 — tool result compression
# ---------------------------------------------------------------------------


class TestPhase1ToolCompression:
    """Emergency prune phase 1 delegates to _prune_session_context."""

    def test_compresses_prunable_tool_results(self) -> None:
        # Build a conversation with many large tool results.
        budget = 5_000  # chars
        messages = [
            _msg("system", 200),
            _msg("user", 300),
        ]
        # Add 8 tool results, each 1 000 chars  (> 4 = _RECENT_TOOL_RESULTS_KEEP)
        for _ in range(8):
            messages.append(_msg("tool", 1_000))
        messages.append(_msg("assistant", 300))

        assert _total_chars(messages) > budget

        _emergency_prune_session_context(messages, max_session_chars=budget)

        # Oldest tool results should have been compressed
        tool_msgs = [m for m in messages if m.role == "tool"]
        compressed = [m for m in tool_msgs if "Pruned tool result" in (m.content or "")]
        assert len(compressed) > 0

    def test_no_op_when_already_under_budget(self) -> None:
        budget = 100_000
        messages = [_msg("user", 100), _msg("assistant", 200)]
        before = [m.content for m in messages]

        _emergency_prune_session_context(messages, max_session_chars=budget)

        assert [m.content for m in messages] == before


# ---------------------------------------------------------------------------
# Phase 2 — assistant message truncation
# ---------------------------------------------------------------------------


class TestPhase2AssistantTruncation:
    """Phase 2 kicks in when tool compression alone is insufficient."""

    def test_truncates_long_assistant_messages(self) -> None:
        # Conversation with NO tool messages but very long assistant turns.
        # Budget is set so Phase 2's tail-truncation alone brings the session
        # under budget — otherwise Phase 4 would truncate the same message
        # *further* (down to a smaller dynamic keep), which is correct but is
        # exercised by its own test below. Here we assert Phase 2 in isolation.
        budget = 5_000  # chars
        long_assistant_content = "A" * 10_000

        messages = [
            _msg("system", 200),
            _msg("user", 300),
            ChatMessage(role="assistant", content=long_assistant_content),
            _msg("user", 300),
            _msg("assistant", 200),
        ]

        assert _total_chars(messages) > budget

        _emergency_prune_session_context(messages, max_session_chars=budget)

        # The long assistant message must have been shortened
        assistant_msgs = [m for m in messages if m.role == "assistant"]
        long_msg = assistant_msgs[0]
        assert len(long_msg.content or "") < 10_000
        # It should retain the tail of the original content
        assert long_assistant_content[-_EMERGENCY_ASSISTANT_TRUNCATE_KEEP:] in (
            long_msg.content or ""
        )
        # And signal that truncation occurred
        assert "Truncated" in (long_msg.content or "")

    def test_leaves_short_assistant_messages_unchanged(self) -> None:
        # Short assistant messages should not be touched during truncation.
        short_content = "short answer"
        messages = [
            _msg("system", 50),
            _msg("user", 50),
            ChatMessage(role="assistant", content=short_content),
        ]
        # At this point we may or may not hit phase 3; we just verify phase 2
        # doesn't damage a message that is already within the truncate threshold.
        _emergency_prune_session_context(messages, max_session_chars=100_000)
        assistant = next(m for m in messages if m.role == "assistant")
        assert assistant.content == short_content

    def test_no_tool_messages_still_reaches_phase2(self) -> None:
        """Reproduces the original bug: 0 tool messages, huge conversation."""
        # Simulate a 2.8 M-token-equivalent conversation with no tool calls.
        # _CHARS_PER_TOKEN = 3, so 2.8 M tokens ≈ 8.4 M chars.
        # 1 M-token window → _overflow_target_chars ≈ 0.70 × 1M × 3 = 2.1 M chars
        ctx_window = 1_000_000
        overflow_target = int(ctx_window * 0.70 * _CHARS_PER_TOKEN)  # 2_100_000

        # Build a minimal-but-oversized conversation: 5 very large user/assistant pairs
        messages = [_msg("system", 200)]
        for _ in range(5):
            messages.append(_msg("user", 500_000))
            messages.append(_msg("assistant", 1_000_000))

        initial_chars = _total_chars(messages)
        assert initial_chars > overflow_target  # confirm we are over budget

        _emergency_prune_session_context(messages, max_session_chars=overflow_target)

        after_chars = _total_chars(messages)
        # After pruning the context must have been reduced
        assert after_chars < initial_chars


# ---------------------------------------------------------------------------
# Phase 3 — oldest message eviction
# ---------------------------------------------------------------------------


class TestPhase3MessageEviction:
    """Phase 3 drops oldest non-system messages when phases 1+2 are insufficient."""

    def test_drops_oldest_non_system_messages(self) -> None:
        # Build a conversation where all content is in user/assistant messages
        # that are already short enough that phase 2 won't help much.
        budget = 500  # very tight
        messages = [
            _msg("system", 100),
        ]
        # Add 20 user/assistant pairs, each ~50 chars
        for i in range(20):
            messages.append(ChatMessage(role="user", content=f"question {i:02d} " + "u" * 40))
            messages.append(
                ChatMessage(role="assistant", content=f"answer {i:02d} " + "a" * 40)
            )

        initial_count = len(messages)
        assert _total_chars(messages) > budget

        _emergency_prune_session_context(messages, max_session_chars=budget)

        # Some messages must have been dropped
        assert len(messages) < initial_count
        # System message must be preserved
        assert messages[0].role == "system"

    def test_preserves_minimum_recent_messages(self) -> None:
        # After eviction, at least _EMERGENCY_KEEP_MESSAGES non-system messages
        # must remain.
        budget = 1  # impossibly tight — forces maximum eviction
        messages = [_msg("system", 20)]
        for _ in range(_EMERGENCY_KEEP_MESSAGES + 5):
            messages.append(_msg("user", 50))
            messages.append(_msg("assistant", 50))

        _emergency_prune_session_context(messages, max_session_chars=budget)

        non_system = [m for m in messages if m.role != "system"]
        assert len(non_system) >= _EMERGENCY_KEEP_MESSAGES

    def test_system_messages_never_dropped(self) -> None:
        budget = 1  # force eviction
        messages = [
            _msg("system", 100),
            _msg("system", 100),
            *[_msg("user", 50) for _ in range(20)],
        ]
        _emergency_prune_session_context(messages, max_session_chars=budget)

        system_msgs = [m for m in messages if m.role == "system"]
        assert len(system_msgs) == 2

    def test_cannot_evict_when_only_minimum_remain(self) -> None:
        # If there are exactly _EMERGENCY_KEEP_MESSAGES non-system messages,
        # phase 3 must not drop any — the list length should be unchanged.
        budget = 1  # way over budget
        messages = [_msg("system", 100)]
        for _ in range(_EMERGENCY_KEEP_MESSAGES):
            messages.append(_msg("user", 50))

        count_before = len(messages)
        _emergency_prune_session_context(messages, max_session_chars=budget)

        # No non-system messages should be removed (can't go below minimum)
        non_system = [m for m in messages if m.role != "system"]
        assert len(non_system) == _EMERGENCY_KEEP_MESSAGES
        assert len(messages) == count_before


# ---------------------------------------------------------------------------
# Integration: realistic overflow scenario
# ---------------------------------------------------------------------------


class TestRealisticOverflow:
    """End-to-end scenario mirroring the production bug report (278% utilisation)."""

    def test_massive_tool_dominated_overflow(self) -> None:
        """Many tool results already compressed → phase 2+3 must still reduce."""
        ctx_window = 1_000_000
        overflow_target = int(ctx_window * 0.70 * _CHARS_PER_TOKEN)

        messages = [_msg("system", 1_000)]
        # 3 tool results (< _RECENT_TOOL_RESULTS_KEEP=4 → phase 1 early-exits)
        for _ in range(3):
            messages.append(_msg("user", 200))
            messages.append(_msg("tool", 500_000))  # Already large
            messages.append(_msg("assistant", 500_000))

        initial_chars = _total_chars(messages)
        assert initial_chars > overflow_target

        _emergency_prune_session_context(messages, max_session_chars=overflow_target)

        after_chars = _total_chars(messages)
        assert after_chars < initial_chars, (
            "Emergency prune must reduce context even when there are fewer than "
            f"{_RECENT_TOOL_RESULTS_KEEP} tool messages"
        )

    def test_no_tool_calls_long_conversation(self) -> None:
        """Pure chat conversation (no tool calls) must still be prunable."""
        ctx_window = 128_000
        overflow_target = int(ctx_window * 0.70 * _CHARS_PER_TOKEN)

        messages = [_msg("system", 500)]
        for _ in range(30):
            messages.append(_msg("user", 5_000))
            messages.append(_msg("assistant", 10_000))

        initial_chars = _total_chars(messages)
        assert initial_chars > overflow_target

        _emergency_prune_session_context(messages, max_session_chars=overflow_target)

        after_chars = _total_chars(messages)
        assert after_chars < initial_chars


# ---------------------------------------------------------------------------
# Tool-call ↔ tool-result coupling (must survive Phase 3 eviction)
# ---------------------------------------------------------------------------


def _assistant_with_tool_calls(tool_call_ids: list[str]) -> ChatMessage:
    """Build an assistant ChatMessage carrying the given tool_call ids."""
    return ChatMessage(
        role="assistant",
        content="thinking…",
        tool_calls=[
            ToolCall(id=tc_id, name="dummy", arguments={}) for tc_id in tool_call_ids
        ],
    )


def _tool_result(tool_call_id: str, chars: int) -> ChatMessage:
    return ChatMessage(
        role="tool",
        content="r" * chars,
        tool_call_id=tool_call_id,
        name="dummy",
    )


def _assert_tool_coupling_intact(messages: list[ChatMessage]) -> None:
    """Every ``tool`` message must appear in the contiguous block that directly
    follows its assistant's ``tool_calls`` turn.

    Anthropic and OpenAI strict mode both require ``tool_result`` blocks to come
    *immediately* after the corresponding ``tool_use`` turn — a ``tool`` message
    that re-appears later (after an intervening ``user`` or plain ``assistant``
    turn that breaks the window) is an orphan and would be rejected with a 400.

    We model that by resetting the set of expected ``tool_call_id`` s on every
    non-``tool`` boundary: an assistant-with-``tool_calls`` opens a fresh
    window, any other non-tool role closes it. A ``tool`` message that arrives
    after the window is closed (or with an id that wasn't requested by the most
    recent assistant) fails the assertion.
    """
    active_ids: set[str] = set()
    for msg in messages:
        if msg.role == "assistant" and msg.tool_calls:
            # New tool-use turn: replace (don't extend) the window.
            active_ids = {tc.id for tc in msg.tool_calls if tc.id}
        elif msg.role == "tool":
            assert msg.tool_call_id in active_ids, (
                f"orphan tool_result with id={msg.tool_call_id!r} after "
                "emergency prune — Anthropic / OpenAI strict mode would "
                "reject this conversation"
            )
        else:
            # user / system / assistant-without-tool_calls all break the
            # tool_use → tool_result window.
            active_ids = set()


class TestToolCallCoupling:
    """Phase 3 must drop assistant(tool_calls) + matching tool messages as a unit.

    Otherwise downstream LLM calls hit:

      Anthropic: "each tool_result block must have a corresponding tool_use
      block in the previous turn" (400 invalid_request_error).
      OpenAI strict mode: "messages with role 'tool' must be a response to a
      preceding message with 'tool_calls'".
    """

    def _assert_coupling_intact(self, messages: list[ChatMessage]) -> None:
        """Delegate to the module-level coupling checker (kept as a method so
        the existing Phase 3 tests read unchanged)."""
        _assert_tool_coupling_intact(messages)

    def test_build_groups_pairs_assistant_with_following_tool_results(self) -> None:
        messages = [
            _msg("system", 50),
            _msg("user", 50),
            _assistant_with_tool_calls(["call-1", "call-2"]),
            _tool_result("call-1", 50),
            _tool_result("call-2", 50),
            _msg("assistant", 50),
            _msg("user", 50),
        ]

        groups = _build_evictable_message_groups(messages)

        # System messages are skipped; everything else should be grouped.
        # group 0: user
        # group 1: assistant(tool_calls) + tool(call-1) + tool(call-2)
        # group 2: assistant
        # group 3: user
        assert groups == [[1], [2, 3, 4], [5], [6]]

    def test_phase3_evicts_assistant_with_its_tool_results_atomically(self) -> None:
        """Reproduces the orphan-tool-result bug if the implementation dropped
        the assistant without its matching tool messages."""
        budget = 800
        messages = [_msg("system", 100), _msg("user", 100)]

        # 8 (assistant→tool→tool) turns, oldest first. Each turn is well over
        # _EMERGENCY_ASSISTANT_TRUNCATE_KEEP so Phase 2 also fires but isn't
        # enough on its own; Phase 3 has to take over.
        for n in range(8):
            messages.append(_assistant_with_tool_calls([f"call-{n}-a", f"call-{n}-b"]))
            messages.append(_tool_result(f"call-{n}-a", 3_000))
            messages.append(_tool_result(f"call-{n}-b", 3_000))

        # Final live turn the user is waiting on.
        messages.append(_msg("user", 200))

        initial_chars = _total_chars(messages)
        assert initial_chars > budget

        _emergency_prune_session_context(messages, max_session_chars=budget)

        # Coupling guarantee for the LLM provider.
        self._assert_coupling_intact(messages)
        # Phase 3 must have actually reduced the context.
        assert _total_chars(messages) < initial_chars

    def test_phase3_preserves_tool_results_when_assistant_is_kept(self) -> None:
        """If an assistant turn is in the protected tail, its tool messages
        must NOT be evicted just because they happen to be older than other
        droppable turns."""
        budget = 100  # impossibly tight to force maximal eviction
        messages = [_msg("system", 50)]
        # Old plain user turns we expect to be dropped.
        for _ in range(_EMERGENCY_KEEP_MESSAGES):
            messages.append(_msg("user", 50))
        # One recent assistant(tool_calls) + its tool results — must be kept
        # together because the assistant is in the protected suffix.
        messages.append(_assistant_with_tool_calls(["recent-call"]))
        messages.append(_tool_result("recent-call", 50))

        _emergency_prune_session_context(messages, max_session_chars=budget)

        self._assert_coupling_intact(messages)
        # The recent assistant + its tool result must still be in the list.
        assistants_with_calls = [
            m for m in messages if m.role == "assistant" and m.tool_calls
        ]
        assert len(assistants_with_calls) == 1
        tool_msgs = [m for m in messages if m.role == "tool"]
        assert any(m.tool_call_id == "recent-call" for m in tool_msgs)


# ---------------------------------------------------------------------------
# _emergency_truncate_text — shared, expansion-guarded truncation helper
# ---------------------------------------------------------------------------


class TestEmergencyTruncateText:
    """The truncation primitive used by Phases 2, 4, and 5."""

    def test_tail_mode_keeps_most_recent_content(self) -> None:
        content = "HEAD" + "x" * 5_000 + "TAILEND"
        out = _emergency_truncate_text(content, 1_000)
        assert len(out) < len(content)
        assert out.endswith("TAILEND")
        assert "Truncated" in out

    def test_head_tail_mode_preserves_both_ends(self) -> None:
        content = "HEADSTART" + "x" * 5_000 + "TAILFINISH"
        out = _emergency_truncate_text(content, 1_000, head_tail=True)
        assert len(out) < len(content)
        assert out.startswith("HEADSTART")
        assert out.endswith("TAILFINISH")
        assert "elided" in out

    def test_returns_input_unchanged_when_already_small(self) -> None:
        content = "short message"
        assert _emergency_truncate_text(content, 1_000) == content

    def test_never_expands_tiny_content(self) -> None:
        # keep smaller than the marker itself — the function must NOT grow the
        # string (size-monotonic contract relied on by all phases).
        content = "a" * 30
        out = _emergency_truncate_text(content, 5)
        assert len(out) <= len(content)


# ---------------------------------------------------------------------------
# Phase 4 — dynamic floor (in-place truncation of the protected tail)
# ---------------------------------------------------------------------------


class TestPhase4DynamicFloor:
    """When the last _EMERGENCY_KEEP_MESSAGES messages ALONE exceed the budget,
    Phase 3 cannot drop anything; Phase 4 truncates them in place instead."""

    def test_truncates_protected_tail_in_place_without_dropping(self) -> None:
        budget = 6_000
        # Exactly _EMERGENCY_KEEP_MESSAGES non-system messages, each large, so
        # Phase 3 protects all of them (floor) and can drop nothing — yet the
        # floor alone is ~20x the budget. This is the dead-end.
        messages = [_msg("system", 500)]
        for _ in range(_EMERGENCY_KEEP_MESSAGES):
            messages.append(_msg("assistant", 5_000))

        initial_chars = _total_chars(messages)
        count_before = len(messages)
        assert initial_chars > budget

        _emergency_prune_session_context(messages, max_session_chars=budget)

        # No messages dropped — the floor count is preserved ...
        assert len(messages) == count_before
        # ... but the bodies were truncated far below Phase 2's 2K keep, so the
        # old "Phase 2 + Phase 3 plateau" (~21K) is gone.
        assert _total_chars(messages) < 8_000
        # Each large turn is now much smaller than even Phase 2 would leave it.
        for m in messages:
            if m.role == "assistant":
                assert len(m.content or "") < 1_000

    def test_dynamic_floor_with_oversized_recent_tool_results(self) -> None:
        """The faithful shape: the protected tail is dominated by a few
        large RECENT tool results (kept intact by Phase 1). The budget is tight
        enough that Phase 2's 2K tail-keep is NOT sufficient, so Phase 4 must
        truncate the protected tool results further in place."""
        budget = 5_000
        messages = [_msg("system", 1_000), _msg("user", 200)]
        # 4 recent tool-call turns whose results are each huge and, being the
        # most recent, are NOT stubbed by Phase 1.
        for n in range(4):
            messages.append(_assistant_with_tool_calls([f"call-{n}"]))
            messages.append(_tool_result(f"call-{n}", 20_000))

        initial_chars = _total_chars(messages)
        count_before = len(messages)
        assert initial_chars > budget

        _emergency_prune_session_context(messages, max_session_chars=budget)

        # Coupling preserved, nothing dropped below the floor, context reduced
        # to the budget (Phase 2 alone left it well above).
        _assert_tool_coupling_intact(messages)
        assert len(messages) == count_before
        assert _total_chars(messages) < initial_chars
        assert _total_chars(messages) <= budget + 1_000


# ---------------------------------------------------------------------------
# Phase 5 — system-prompt overflow (absolute last resort)
# ---------------------------------------------------------------------------


class TestPhase5SystemOverflow:
    """When the system prompt ALONE exceeds the budget, oversized system
    messages are truncated head+tail (persona head + policy tail survive)."""

    def test_truncates_oversized_system_prompt_head_tail(self) -> None:
        budget = 20_000
        head = "HEAD_PERSONA_MARKER"
        tail = "TAIL_POLICY_MARKER"
        # Memory-bloated system prompt that alone exceeds the budget.
        system_content = head + "m" * 40_000 + tail
        messages = [
            ChatMessage(role="system", content=system_content),
            _msg("user", 200),
            _msg("assistant", 200),
        ]

        initial_chars = _total_chars(messages)
        assert len(system_content) > budget

        _emergency_prune_session_context(messages, max_session_chars=budget)

        sys = next(m for m in messages if m.role == "system").content or ""
        # Head + tail preserved, bulky middle elided, net smaller, never below
        # the safe floor.
        assert sys.startswith(head)
        assert sys.endswith(tail)
        assert len(sys) < len(system_content)
        assert len(sys) >= _EMERGENCY_SYSTEM_MIN_KEEP
        assert _total_chars(messages) < initial_chars

    def test_small_system_messages_left_untouched(self) -> None:
        # System messages at/below the truncate threshold are never touched
        # (transient in-loop nudges must survive verbatim).
        budget = 1  # force every later phase to run
        small_system = "x" * (_EMERGENCY_SYSTEM_TRUNCATE_MIN - 1)
        messages = [
            ChatMessage(role="system", content=small_system),
            _msg("user", 50),
        ]
        _emergency_prune_session_context(messages, max_session_chars=budget)
        sys = next(m for m in messages if m.role == "system").content
        assert sys == small_system


# ---------------------------------------------------------------------------
# Regression — the exact production failure
# ---------------------------------------------------------------------------


class TestLastMessagesOverBudgetRegression:
    """End-to-end reproduction of the cascade: the last
    _EMERGENCY_KEEP_MESSAGES messages alone exceed the budget AND the system
    prompt is dominated by a large injected agent-memory block. The old code
    logged 'nothing safe to drop' and returned without reducing anything,
    triggering the empty-response → forced-final-answer failure loop. The fix
    must reach the target."""

    def test_memory_bloated_system_with_oversized_recent_tool_results(
        self,
    ) -> None:
        # Mirrors the production trace: target ~40K, agent_memory ~34K, a
        # context ~67K dominated by recent tool results + the system memory.
        budget = 40_000
        persona_head = "PERSONA_HEAD_MARKER " + "p" * 2_000
        policy_tail = "s" * 2_000 + " SECURITY_POLICY_TAIL_MARKER"
        memory_blob = "m" * 34_000  # the dominant contributor
        system_content = persona_head + memory_blob + policy_tail
        messages: list[ChatMessage] = [
            ChatMessage(role="system", content=system_content)
        ]
        # The rendered task prompt the agent must answer (kept small).
        messages.append(_msg("user", 300))
        # Several tool-call turns whose recent results alone blow the budget.
        for n in range(6):
            messages.append(_assistant_with_tool_calls([f"call-{n}"]))
            messages.append(_tool_result(f"call-{n}", 8_000))

        initial_chars = _total_chars(messages)
        assert initial_chars > budget

        _emergency_prune_session_context(messages, max_session_chars=budget)

        final_chars = _total_chars(messages)
        # The core fix: the prune now REACHES the target (within marker
        # overhead) instead of plateauing ~1.6x above it.
        assert final_chars < initial_chars
        assert final_chars <= budget * 1.05
        # The system prompt survives with its persona head + security tail.
        system_msgs = [m for m in messages if m.role == "system"]
        assert len(system_msgs) == 1
        sys = system_msgs[0].content or ""
        assert "PERSONA_HEAD_MARKER" in sys
        assert "SECURITY_POLICY_TAIL_MARKER" in sys
        # No orphaned tool results for the next LLM call.
        _assert_tool_coupling_intact(messages)
