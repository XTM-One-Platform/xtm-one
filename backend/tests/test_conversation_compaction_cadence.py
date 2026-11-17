"""Conversation compaction cadence.

After a compaction the history is ``[summary] + KEEP_RECENT recent
messages``, and the strict ``>`` trigger fires again only once the real
message count EXCEEDS the threshold — so the summarizer must NOT run
again until a full new batch of ``SUMMARIZE_THRESHOLD - KEEP_RECENT + 1``
messages has accumulated (the gap plus one).

With the old 20/15 split that interval was just 6 messages (~3 user
turns, since every turn stores a user + assistant pair), so users saw
"Summarizing earlier conversation..." every couple of messages and each
compactor LLM call folded in a mere 6-message sliver.  These tests pin:

1. The constants invariants — including the cadence rule that the gap
   is at least as large as the keep-window.
2. No re-compaction (no LLM call, history unchanged) until the full
   batch has accumulated after a prior compaction.
3. Re-compaction fires exactly once the batch is complete.
4. ``should_compact`` — the shared trigger predicate used by the chat
   status frame — always agrees with what the engine actually does, in
   particular on the boundary turn where ``len(history)`` exceeds the
   threshold only because of the rehydrated compaction system message
   (the engine early-returns there, so the UI must not announce a
   summarization).
"""

from __future__ import annotations

import json

import pytest

import app.services.conversation_memory as cm
from app.llm.base import BaseLLMProvider, ChatMessage, ChatResponse


class _CompactorStubLLM(BaseLLMProvider):
    """Returns a fixed, valid compactor JSON payload and counts calls."""

    provider_name = "stub"
    context_window = 100_000

    def __init__(self) -> None:
        self.chat_calls: list[list[ChatMessage]] = []

    async def chat(self, messages, **kwargs):
        self.chat_calls.append(list(messages))
        return ChatResponse(
            content=json.dumps(
                {"directives": ["use subregion filter"], "summary": "- recap"}
            ),
            model="stub-model",
        )

    async def chat_stream(self, messages, **kwargs):  # pragma: no cover - unused
        yield ""

    async def embed(self, text, model=None):  # pragma: no cover - unused
        return [0.0]

    async def list_models(self):  # pragma: no cover - unused
        return []

    async def list_embedding_models(self):  # pragma: no cover - unused
        return []

    async def list_image_models(self):  # pragma: no cover - unused
        return []


def _make_history(count: int, *, start_seq: int = 1) -> list[ChatMessage]:
    out: list[ChatMessage] = []
    for i in range(count):
        role = "user" if i % 2 == 0 else "assistant"
        msg = ChatMessage(role=role, content=f"message {i} " + "x" * 50)
        msg.db_sequence = start_seq + i
        out.append(msg)
    return out


def _post_compaction_history(new_messages: int) -> list[ChatMessage]:
    """History as ``get_conversation_history`` rebuilds it after a compaction:
    rendered compaction message + KEEP_RECENT survivors + *new_messages*."""
    rendered = cm.render_stored_compaction(
        {
            "directives": "- currency prefix is EUR",
            "summary": "- discussed dashboards",
            "through_sequence": 100,
            "compacted_count": 15,
        }
    )
    real = _make_history(cm.KEEP_RECENT + new_messages, start_seq=101)
    return [rendered, *real]


@pytest.fixture()
def persist_spy(monkeypatch):
    calls: list[dict] = []

    async def _spy(conversation_id, **kwargs):
        calls.append({"conversation_id": conversation_id, **kwargs})

    monkeypatch.setattr(cm, "_persist_compaction_state", _spy)
    return calls


# ── Constants invariants ──────────────────────────────────────────────────


def test_constants_invariants():
    assert cm.MAX_HISTORY_MESSAGES >= cm.SUMMARIZE_THRESHOLD > cm.KEEP_RECENT


def test_cadence_gap_is_at_least_the_keep_window():
    """The gap (THRESHOLD - KEEP_RECENT) must be at least as large as
    the keep-window itself — otherwise the summarizer thrashes every few
    turns (the old 20/15 split re-ran it every 6 messages)."""
    gap = cm.SUMMARIZE_THRESHOLD - cm.KEEP_RECENT
    assert gap >= cm.KEEP_RECENT


# ── Cadence behaviour after a prior compaction ────────────────────────────


@pytest.mark.asyncio
async def test_no_recompaction_until_full_batch_accumulates(persist_spy):
    """After a compaction cycle, adding up to (THRESHOLD - KEEP_RECENT)
    new messages must NOT trigger another compactor LLM call — the
    strict ``>`` trigger needs the count to EXCEED the threshold."""
    llm = _CompactorStubLLM()
    gap = cm.SUMMARIZE_THRESHOLD - cm.KEEP_RECENT

    for new_messages in range(0, gap + 1):  # 0..gap new msgs: still below
        history = _post_compaction_history(new_messages)
        result = await cm.manage_conversation_memory(
            llm, history, conversation_id="conv-cadence"
        )
        assert llm.chat_calls == [], (
            f"compactor ran with only {new_messages} new message(s) "
            f"after the last compaction (gap is {gap})"
        )
        assert result == history
    assert persist_spy == []


@pytest.mark.asyncio
async def test_recompaction_fires_once_batch_is_complete(persist_spy):
    llm = _CompactorStubLLM()
    gap = cm.SUMMARIZE_THRESHOLD - cm.KEEP_RECENT

    history = _post_compaction_history(gap + 1)
    result = await cm.manage_conversation_memory(
        llm, history, conversation_id="conv-cadence-2"
    )

    assert len(llm.chat_calls) == 1
    assert cm._is_compaction_message(result[0])
    assert len(result) == 1 + cm.KEEP_RECENT
    # The prior directives were rolled forward into the compactor prompt.
    assert "currency prefix is EUR" in llm.chat_calls[0][1].content
    assert len(persist_spy) == 1


# ── should_compact: shared trigger predicate ──────────────────────────────


@pytest.mark.asyncio
async def test_should_compact_agrees_with_engine(persist_spy):
    """``should_compact`` (used by the chat "Summarizing..." status frame)
    must predict exactly whether the engine runs a compactor call — for
    fresh histories AND rehydrated ones, across the boundary."""
    gap = cm.SUMMARIZE_THRESHOLD - cm.KEEP_RECENT
    cases: list[list[ChatMessage]] = [
        _make_history(cm.SUMMARIZE_THRESHOLD - 1),
        _make_history(cm.SUMMARIZE_THRESHOLD),
        _make_history(cm.SUMMARIZE_THRESHOLD + 1),
        _post_compaction_history(gap - 1),
        _post_compaction_history(gap),
        _post_compaction_history(gap + 1),
    ]
    for history in cases:
        llm = _CompactorStubLLM()
        predicted = cm.should_compact(history)
        await cm.manage_conversation_memory(
            llm, history, conversation_id="conv-predicate"
        )
        ran = len(llm.chat_calls) > 0
        assert predicted == ran, (
            f"should_compact={predicted} but engine ran={ran} for "
            f"history of {len(history)} message(s)"
        )


def test_should_compact_ignores_rehydrated_compaction_message():
    """The boundary that used to flash a phantom "Summarizing..." status:
    ``len(history) > THRESHOLD`` only because of the compaction system
    message at index 0 — the engine early-returns, so the predicate must
    be False."""
    rendered = cm.render_stored_compaction(
        {
            "directives": "- a",
            "summary": "- b",
            "through_sequence": 1,
            "compacted_count": 1,
        }
    )
    history = [rendered, *_make_history(cm.SUMMARIZE_THRESHOLD, start_seq=2)]
    assert len(history) > cm.SUMMARIZE_THRESHOLD
    assert cm.should_compact(history) is False

    assert cm.should_compact([]) is False
    assert cm.should_compact(_make_history(1)) is False


# ── Window-aware trigger ────────────────────────────────────────────────────


class _WideCompactorStubLLM(_CompactorStubLLM):
    context_window = 1_000_000


def _heavy_history(count: int, total_tokens: int) -> list[ChatMessage]:
    """*count* messages weighing about *total_tokens* estimated tokens."""
    per_message = total_tokens * cm._CHARS_PER_TOKEN // count
    out: list[ChatMessage] = []
    for i in range(count):
        role = "user" if i % 2 == 0 else "assistant"
        msg = ChatMessage(role=role, content=f"message {i} " + "x" * per_message)
        msg.db_sequence = i + 1
        out.append(msg)
    return out


def test_message_count_alone_no_longer_compacts_far_from_the_window():
    """40 messages, ~150k tokens, on a 1M window: 15% of the window, below
    the safety net. The old count trigger (30) summarised it."""
    history = _heavy_history(40, 150_000)
    assert len(history) <= cm.SUMMARIZE_THRESHOLD
    assert cm.should_compact(history, context_window=1_000_000) is False


@pytest.mark.asyncio
async def test_no_compactor_call_under_half_of_a_1m_window(persist_spy):
    llm = _WideCompactorStubLLM()
    history = _heavy_history(cm.SUMMARIZE_THRESHOLD, 480_000)
    result = await cm.manage_conversation_memory(llm, history, conversation_id="conv-wide")
    assert result is history
    assert llm.chat_calls == []
    assert persist_spy == []


@pytest.mark.asyncio
async def test_history_past_the_window_share_is_compacted(persist_spy):
    llm = _CompactorStubLLM()  # 100k window: the share is 60k tokens
    history = _heavy_history(20, 70_000)
    assert cm.should_compact(history, context_window=llm.context_window) is True
    result = await cm.manage_conversation_memory(llm, history, conversation_id="conv-heavy")
    assert len(llm.chat_calls) == 1
    assert cm._is_compaction_message(result[0])
    assert result[1:] == history[-cm.KEEP_RECENT :]
    assert len(persist_spy) == 1


def test_the_share_applies_to_each_model_window():
    history = _heavy_history(30, 90_000)
    assert cm.should_compact(history, context_window=128_000) is True
    assert cm.should_compact(history, context_window=1_000_000) is False


def test_a_heavy_recent_window_alone_is_not_resummarised():
    """Just after a compaction the recent window may alone weigh past the
    share; a cycle waits for a batch of new messages beyond it."""
    short = _heavy_history(cm.KEEP_RECENT + cm._MIN_TOKEN_TRIGGER_BATCH - 1, 90_000)
    assert cm.should_compact(short, context_window=100_000) is False
    batch = _heavy_history(cm.KEEP_RECENT + cm._MIN_TOKEN_TRIGGER_BATCH, 90_000)
    assert cm.should_compact(batch, context_window=100_000) is True


def test_the_safety_net_still_fires_past_the_threshold():
    history = _make_history(cm.SUMMARIZE_THRESHOLD + 1)
    assert cm.should_compact(history, context_window=1_000_000) is True


def test_compaction_context_window_reads_the_model_override():
    llm = _CompactorStubLLM()
    llm.context_windows = {"custom-model": 64_000}
    assert cm.compaction_context_window(llm, "custom-model") == 64_000
    assert cm.compaction_context_window(llm, None) == 100_000


@pytest.mark.asyncio
async def test_window_predicate_agrees_with_engine(persist_spy):
    """The status frame measures with the compactor's own window: frame and
    compactor call agree on heavy histories too."""
    cases = [
        _heavy_history(20, 50_000),
        _heavy_history(20, 70_000),
        _heavy_history(cm.KEEP_RECENT + 1, 90_000),
        _heavy_history(cm.SUMMARIZE_THRESHOLD + 1, 1_000),
    ]
    for history in cases:
        llm = _CompactorStubLLM()
        predicted = cm.should_compact(
            history, context_window=cm.compaction_context_window(llm, None)
        )
        await cm.manage_conversation_memory(llm, history, conversation_id="conv-window")
        assert predicted == (len(llm.chat_calls) > 0)
