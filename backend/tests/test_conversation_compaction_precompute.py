"""Compaction precomputed after a turn.

A compaction cycle used to run its compactor LLM call before the turn that
crossed the threshold could start. The WebSocket chat now runs it right after
the turn that takes the conversation past the threshold, persisted like any
compaction; a turn that starts while it still runs waits for it and
catches up with its result instead of calling the compactor again.
"""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import json
import uuid

import pytest

import app.services.conversation_memory as cm
from app.llm.base import BaseLLMProvider, ChatMessage, ChatResponse

CONV = str(uuid.uuid4())
OTHER_CONV = str(uuid.uuid4())


class _Compactor(BaseLLMProvider):
    """A compactor that can be held until the test releases it."""

    provider_name = "stub"
    context_window = 100_000

    def __init__(self, directives: str = "use subregion filter") -> None:
        self.directives = directives
        self.calls: list[list[ChatMessage]] = []
        self.release = asyncio.Event()
        self.release.set()

    async def chat(self, messages, **kwargs):
        self.calls.append(list(messages))
        await self.release.wait()
        return ChatResponse(
            content=json.dumps({"directives": [self.directives], "summary": "- recap"}),
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


def _history(count: int, *, start_seq: int = 1) -> list[ChatMessage]:
    out: list[ChatMessage] = []
    for i in range(count):
        msg = ChatMessage(role="user" if i % 2 == 0 else "assistant", content=f"message {i} " + "x" * 50)
        msg.db_sequence = start_seq + i
        out.append(msg)
    return out


@pytest.fixture(autouse=True)
def _clean_registry():
    cm._precomputed.clear()
    yield
    cm._precomputed.clear()


@pytest.fixture()
def persisted(monkeypatch):
    calls: list[dict] = []

    async def _spy(conversation_id, **kwargs):
        calls.append({"conversation_id": conversation_id, **kwargs})

    monkeypatch.setattr(cm, "_persist_compaction_state", _spy)
    return calls


@pytest.fixture()
def stored(monkeypatch):
    """What ``get_conversation_history`` loads for the precompute."""
    box: dict[str, object] = {"history": []}

    async def _load(db, conversation_id, limit=50):
        return list(box["history"])

    @contextlib.asynccontextmanager
    async def _session():
        yield object()

    monkeypatch.setattr("app.channels.router.get_conversation_history", _load)
    monkeypatch.setattr("app.core.database.async_session", _session)
    return box


def _start(llm, conversation_id: str = CONV):
    return cm.precompute_compaction(
        llm,
        conversation_id=conversation_id,
        agent_id=None,
        user_id=None,
        memory_persistent_enabled=False,
        model=None,
    )


@pytest.mark.asyncio
async def test_the_next_turns_compaction_is_computed_and_persisted_after_the_turn(
    persisted, stored
):
    history = _history(cm.SUMMARIZE_THRESHOLD + 2)
    stored["history"] = history
    llm = _Compactor()

    result = await _start(llm)

    old_count = len(history) - cm.KEEP_RECENT
    assert len(llm.calls) == 1
    assert result.through_sequence == history[old_count - 1].db_sequence
    assert cm._is_compaction_message(result.message)
    assert [call["through_sequence"] for call in persisted] == [result.through_sequence]


@pytest.mark.asyncio
async def test_a_turn_that_loaded_its_history_first_catches_up_without_a_second_call(
    persisted, stored
):
    history = _history(cm.SUMMARIZE_THRESHOLD + 2)
    stored["history"] = history
    llm = _Compactor()
    llm.release.clear()
    task = _start(llm)
    await asyncio.sleep(0)

    turn = asyncio.create_task(
        cm.manage_conversation_memory(llm, list(history), conversation_id=CONV)
    )
    await asyncio.sleep(0)
    assert not turn.done()
    llm.release.set()
    result = await turn

    precomputed = task.result()
    assert len(llm.calls) == 1
    assert len(persisted) == 1
    assert result == [precomputed.message, *history[-cm.KEEP_RECENT :]]
    assert CONV not in cm._precomputed


@pytest.mark.asyncio
async def test_a_history_loaded_after_the_persist_keeps_its_own_newer_compaction(
    persisted, stored
):
    stored["history"] = _history(cm.SUMMARIZE_THRESHOLD + 2)
    llm = _Compactor(directives="old directive")
    await _start(llm)

    newer = cm.render_stored_compaction(
        {"directives": "- newer directive", "summary": "- newer", "compacted_count": 40}
    )
    later = [newer, *_history(cm.SUMMARIZE_THRESHOLD + 2, start_seq=100)]
    llm.directives = "rolled forward"
    result = await cm.manage_conversation_memory(llm, later, conversation_id=CONV)

    assert len(llm.calls) == 2
    prompt = "\n".join(m.content or "" for m in llm.calls[-1])
    assert "newer directive" in prompt
    assert "old directive" not in prompt
    assert "rolled forward" in (result[0].content or "")
    assert CONV not in cm._precomputed


@pytest.mark.asyncio
async def test_a_precompute_with_nothing_to_compact_leaves_the_turn_to_compact(
    persisted, stored
):
    stored["history"] = _history(cm.SUMMARIZE_THRESHOLD - 2)
    llm = _Compactor()
    assert await _start(llm) is None
    assert llm.calls == []

    history = _history(cm.SUMMARIZE_THRESHOLD + 2)
    result = await cm.manage_conversation_memory(llm, history, conversation_id=CONV)

    assert len(llm.calls) == 1
    assert cm._is_compaction_message(result[0])


@pytest.mark.asyncio
async def test_a_turn_cancelled_while_waiting_leaves_the_precompute_running(persisted, stored):
    history = _history(cm.SUMMARIZE_THRESHOLD + 2)
    stored["history"] = history
    llm = _Compactor()
    llm.release.clear()
    task = _start(llm)
    await asyncio.sleep(0)

    turn = asyncio.create_task(
        cm.manage_conversation_memory(llm, list(history), conversation_id=CONV)
    )
    await asyncio.sleep(0)
    turn.cancel()
    with pytest.raises(asyncio.CancelledError):
        await turn

    assert not task.done()
    llm.release.set()
    assert (await task) is not None
    assert len(persisted) == 1


@pytest.mark.asyncio
async def test_one_precompute_per_conversation_at_a_time(persisted, stored):
    stored["history"] = _history(cm.SUMMARIZE_THRESHOLD + 2)
    llm = _Compactor()
    llm.release.clear()
    first = _start(llm)

    assert _start(llm) is None
    assert _start(llm, OTHER_CONV) is not None
    llm.release.set()
    await first
    await asyncio.gather(*list(cm._running_precomputes))


@pytest.mark.asyncio
async def test_the_precompute_does_not_inherit_the_turns_context(persisted, monkeypatch):
    marker: contextvars.ContextVar[str | None] = contextvars.ContextVar("marker", default=None)
    seen: list[str | None] = []

    async def _load(db, conversation_id, limit=50):
        seen.append(marker.get())
        return []

    @contextlib.asynccontextmanager
    async def _session():
        yield object()

    monkeypatch.setattr("app.channels.router.get_conversation_history", _load)
    monkeypatch.setattr("app.core.database.async_session", _session)
    marker.set("turn trace")

    await _start(_Compactor())

    assert seen == [None]


def test_the_end_of_a_turn_counts_the_messages_it_stored():
    history = _history(cm.SUMMARIZE_THRESHOLD - 2)
    assert not cm.should_compact(history)
    assert cm.should_compact(history, ahead=4)
