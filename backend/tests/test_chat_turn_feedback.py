"""Tests for the Phase-2 chat-turn feedback events.

Covers:

* P2.2 — ``_semantic_compact_session_context`` emits a ``context_compaction``
  liveness event BEFORE the summarizer LLM call (previously the badge only
  appeared after the call returned).
* P2.5 — the WS handler's terminal-frame decision: a user-cancelled turn
  closes with ``cancelled`` instead of ``done`` — but ONLY when both the
  cancel event fired AND the loop returned the cancellation sentinel (an
  answer that completed just before the cancel is still delivered as done).
"""

from __future__ import annotations

import asyncio

import pytest

import app.tools.agent_loop as al
from app.api.v1.chat import _is_cancelled_terminal
from app.llm.base import BaseLLMProvider, ChatMessage, ChatResponse

# ---------------------------------------------------------------------------
# P2.2 — compaction liveness event
# ---------------------------------------------------------------------------


class _SummariserLLM(BaseLLMProvider):
    """Records call order relative to the emitted events."""

    provider_name = "stub"
    context_window = 100_000

    def __init__(self, log: list[str]) -> None:
        self._log = log

    async def chat(self, messages, model=None, tools=None, **kwargs):
        self._log.append("llm_call")
        return ChatResponse(
            content="- Task: summarise the data\n- Result: chunks 0-9 analysed",
            model=model or "stub",
        )

    async def chat_stream(self, messages, **kwargs):
        yield ""

    async def embed(self, text, model=None):
        return [0.0]

    async def list_models(self):
        return []


def _compactable_messages() -> list[ChatMessage]:
    filler = "tool output line " * 600  # ~10k chars each
    messages = [ChatMessage(role="user", content="original task")]
    for i in range(12):
        messages.append(ChatMessage(role="assistant", content=f"step {i}: {filler}"))
    return messages


@pytest.mark.asyncio
async def test_compaction_event_emitted_before_summarizer_call():
    """The ``context_compaction`` event must precede the summarizer LLM call
    so the badge is live DURING the call, not after it (P2.2)."""
    log: list[str] = []
    llm = _SummariserLLM(log)

    async def _on_event(event: dict) -> None:
        log.append(f"event:{event.get('status')}:{event.get('phase')}")

    compacted = await al._semantic_compact_session_context(
        llm,
        _compactable_messages(),
        model=None,
        reason="unit test",
        on_event=_on_event,
    )

    assert compacted is True
    assert "event:context_compaction:summarizing" in log
    assert log.index("event:context_compaction:summarizing") < log.index(
        "llm_call"
    ), "the liveness event must be emitted BEFORE the summarizer call"


@pytest.mark.asyncio
async def test_compaction_event_skipped_without_consumer():
    """No ``on_event`` consumer — compaction still works (no crash)."""
    log: list[str] = []
    llm = _SummariserLLM(log)
    compacted = await al._semantic_compact_session_context(
        llm,
        _compactable_messages(),
        model=None,
        reason="unit test",
    )
    assert compacted is True
    assert log == ["llm_call"]


# ---------------------------------------------------------------------------
# P2.5 — terminal-frame decision for cancelled turns
# ---------------------------------------------------------------------------


def test_cancelled_terminal_requires_event_and_sentinel():
    """Both the fired cancel event AND the cancellation sentinel text are
    required to close the turn with ``cancelled`` (P2.5)."""
    fired = asyncio.Event()
    fired.set()
    unfired = asyncio.Event()

    sentinel = al._CANCELLED_MESSAGE

    # Cancelled turn: event fired + loop returned the sentinel.
    assert _is_cancelled_terminal(fired, sentinel) is True
    # Whitespace around the sentinel is tolerated.
    assert _is_cancelled_terminal(fired, f"  {sentinel}\n") is True

    # A real answer that completed just before the cancel landed must still
    # be delivered as ``done``.
    assert _is_cancelled_terminal(fired, "Here is your report.") is False
    # Sentinel text without a fired event (echo / coincidence) is not a
    # cancellation.
    assert _is_cancelled_terminal(unfired, sentinel) is False
    # No cancel support at all (event is None).
    assert _is_cancelled_terminal(None, sentinel) is False
    # Blank answers are never classified as cancelled.
    assert _is_cancelled_terminal(fired, "") is False
