"""Semantic in-loop compaction (the provider-agnostic "auto-compact" layer).

These tests exercise :func:`agent_loop._semantic_compact_session_context`
directly — the information-preserving compaction that distils the OLDEST turns
into a single high-fidelity summary while keeping the original task and the most
recent turns verbatim, and never orphaning a ``tool_result`` from its
``tool_use`` turn.
"""

from __future__ import annotations

import pytest

from app.llm.base import BaseLLMProvider, ChatMessage, ChatResponse
from app.tools import agent_loop as al
from app.tools.base import ToolCall


class _StubSummarizerLLM(BaseLLMProvider):
    """Records the summariser call and returns a canned summary."""

    provider_name = "stub"
    context_window = 200_000

    def __init__(self, summary: str = "SUMMARY: prior work distilled.") -> None:
        self.summary = summary
        self.calls = 0
        self.last_messages: list[ChatMessage] | None = None

    async def chat(self, messages, **kwargs):
        self.calls += 1
        self.last_messages = list(messages)
        return ChatResponse(content=self.summary, model="stub", stop_reason="end_turn")

    async def chat_stream(self, messages, **kwargs):
        yield "x"

    async def embed(self, text, model=None):
        return [0.0]

    async def list_models(self):
        return []


def _big(role: str, n: int, tag: str = "") -> ChatMessage:
    return ChatMessage(role=role, content=f"{tag}" + ("z" * n))


@pytest.mark.asyncio
async def test_semantic_compaction_keeps_task_and_recent_and_summarises_middle():
    llm = _StubSummarizerLLM()
    messages = [
        ChatMessage(role="system", content="persona"),
        ChatMessage(role="user", content="ORIGINAL TASK: investigate the incident."),
    ]
    # 20 old assistant turns (the "middle") + a few recent ones.
    for i in range(20):
        messages.append(_big("assistant", 3_000, tag=f"old-{i}-"))
    messages.append(_big("assistant", 3_000, tag="recent-A-"))
    messages.append(_big("assistant", 3_000, tag="recent-B-"))

    before = sum(len(m.content or "") for m in messages)
    did = await al._semantic_compact_session_context(
        llm, messages, model=None, reason="test"
    )
    after = sum(len(m.content or "") for m in messages)

    assert did is True
    assert llm.calls == 1
    assert after < before
    # Original task preserved verbatim.
    assert any(
        m.role == "user" and "ORIGINAL TASK" in (m.content or "") for m in messages
    )
    # System persona preserved.
    assert messages[0].role == "system" and messages[0].content == "persona"
    # Most recent turns preserved verbatim.
    assert any("recent-B-" in (m.content or "") for m in messages)
    # A summary marker block was inserted.
    summary_msgs = [
        m for m in messages if al._SEMANTIC_COMPACTION_MARKER in (m.content or "")
    ]
    assert len(summary_msgs) == 1
    # The summary is a USER-role context turn — never ``system`` (providers fold
    # all system messages into the single system instruction, and eviction skips
    # system, so a system summary would be elevated to an instruction and become
    # un-prunable). As a user turn it stays an evictable group.
    assert summary_msgs[0].role == "user"
    groups = al._build_evictable_message_groups(messages)
    summary_idx = messages.index(summary_msgs[0])
    assert any(summary_idx in group for group in groups), (
        "the summary turn must be evictable (present in an eviction group)"
    )
    # The oldest middle turns are gone (summarised away).
    assert not any("old-0-" in (m.content or "") for m in messages)


@pytest.mark.asyncio
async def test_semantic_compaction_preserves_tool_call_coupling():
    """Dropping the middle must never leave a ``tool`` result without its
    parent assistant ``tool_calls`` turn (Anthropic/OpenAI strict mode reject
    orphans with a 400)."""
    llm = _StubSummarizerLLM()
    messages = [
        ChatMessage(role="system", content="persona"),
        ChatMessage(role="user", content="task"),
    ]
    # 12 tool-call groups (assistant w/ tool_calls + matching tool result).
    for i in range(12):
        call_id = f"call-{i}"
        messages.append(
            ChatMessage(
                role="assistant",
                content=f"step-{i} " + ("z" * 3_000),
                tool_calls=[ToolCall(id=call_id, name="search", arguments={})],
            )
        )
        messages.append(
            ChatMessage(
                role="tool",
                content=f"result-{i} " + ("z" * 3_000),
                tool_call_id=call_id,
                name="search",
            )
        )

    did = await al._semantic_compact_session_context(
        llm, messages, model=None, reason="test"
    )
    assert did is True

    # Every surviving tool result must have its parent assistant tool_call id
    # present in an immediately-preceding assistant turn.
    pending_ids: set[str] = set()
    for m in messages:
        if m.role == "assistant" and m.tool_calls:
            pending_ids = {tc.id for tc in m.tool_calls if tc.id}
        elif m.role == "tool":
            assert m.tool_call_id in pending_ids, (
                f"orphaned tool result {m.tool_call_id!r} after compaction"
            )


@pytest.mark.asyncio
async def test_semantic_compaction_noop_on_single_large_prompt():
    """A single huge user prompt (no old turns to summarise) must be left
    untouched — semantic compaction never guts a legitimately-long prompt."""
    llm = _StubSummarizerLLM()
    messages = [
        ChatMessage(role="system", content="persona"),
        ChatMessage(role="user", content="HUGE PROMPT " + ("z" * 200_000)),
    ]
    snapshot = [m.content for m in messages]

    did = await al._semantic_compact_session_context(
        llm, messages, model=None, reason="test"
    )

    assert did is False
    assert llm.calls == 0
    assert [m.content for m in messages] == snapshot


@pytest.mark.asyncio
async def test_semantic_compaction_noop_when_history_not_user_led():
    """A history whose first non-system turn is NOT a user turn must be left
    untouched. Compaction keeps the head verbatim, so summarising the middle
    would still leave a non-user turn leading the conversation — an invalid
    Anthropic message sequence. The deterministic fallback handles it instead."""
    llm = _StubSummarizerLLM()
    # First non-system turn is an assistant turn (no leading user task).
    messages = [
        ChatMessage(role="system", content="persona"),
        _big("assistant", 3_000, tag="lead-assistant-"),
    ]
    for i in range(20):
        messages.append(_big("assistant", 3_000, tag=f"old-{i}-"))
    messages.append(ChatMessage(role="user", content="a late user turn"))
    for tag in ("recent-A-", "recent-B-"):
        messages.append(_big("assistant", 3_000, tag=tag))

    snapshot = [m.content for m in messages]
    did = await al._semantic_compact_session_context(
        llm, messages, model=None, reason="test"
    )

    assert did is False
    assert llm.calls == 0
    # Nothing dropped — the leading non-user turn is preserved untouched.
    assert [m.content for m in messages] == snapshot


@pytest.mark.asyncio
async def test_semantic_compaction_returns_false_when_summary_empty():
    """If the summariser yields nothing, compaction reports failure (the caller
    then falls back to the deterministic pruner) and leaves messages intact."""
    llm = _StubSummarizerLLM(summary="   ")  # whitespace → treated as empty
    messages = [
        ChatMessage(role="system", content="persona"),
        ChatMessage(role="user", content="task"),
    ]
    for i in range(20):
        messages.append(_big("assistant", 3_000, tag=f"old-{i}-"))
    for tag in ("recent-A-", "recent-B-"):
        messages.append(_big("assistant", 3_000, tag=tag))

    snapshot_len = len(messages)
    did = await al._semantic_compact_session_context(
        llm, messages, model=None, reason="test"
    )

    assert did is False
    # Nothing dropped — the caller is responsible for the fallback.
    assert len(messages) == snapshot_len


def test_inloop_transcript_keeps_tail_identifiers():
    """The per-message excerpt must keep BOTH ends so identifiers that live at
    the tail of a large tool output (IDs, URLs, error tails) reach the
    summariser — a prefix-only cut would drop them."""
    cap = al._SEMANTIC_COMPACTION_TRANSCRIPT_MSG_CHARS
    tail_id = "INCIDENT_ID=abc-123-TAIL-MARKER"
    # A tool output far larger than the cap, with the identifier at the very end.
    content = ("x" * (cap * 2)) + tail_id
    transcript = al._build_inloop_transcript(
        [ChatMessage(role="tool", content=content, name="search")]
    )

    # Tail identifier survived (prefix-only truncation would have dropped it).
    assert tail_id in transcript
    # The head is still present and the middle was elided with a marker.
    assert "xxxx" in transcript
    assert "elided" in transcript


def test_inloop_transcript_caps_tool_call_names():
    """A tool-heavy assistant turn must not spell out every tool name in its
    transcript label — the names are capped with a ``+N more`` suffix so the
    summariser prompt stays bounded no matter how many tools fired in one turn."""
    cap = al._SEMANTIC_COMPACTION_MAX_TOOL_NAMES
    n_calls = cap + 7
    tool_calls = [
        ToolCall(id=f"call-{i}", name=f"tool_{i}", arguments={}) for i in range(n_calls)
    ]
    transcript = al._build_inloop_transcript(
        [ChatMessage(role="assistant", content="working on it", tool_calls=tool_calls)]
    )

    # Only the first ``cap`` names are shown, with a "+N more" overflow marker.
    assert f"tool_{cap - 1}" in transcript
    assert f"tool_{cap}" not in transcript
    assert f"+{n_calls - cap} more" in transcript
