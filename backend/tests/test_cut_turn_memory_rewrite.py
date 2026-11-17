"""An answered turn replaces the "Interrupted" memory note its cut left (#11924).

A chat turn cut by a rollout records ``User asked: X -> Interrupted before the
agent replied: ...`` in the agent's short-term memory and the user memory
(``agent_chat.run_agent_chat``, a shutdown finalizer). Its resume answers the
same message later. The answer's entry takes the place of that note in both
tiers, so the recent activity never keeps "Interrupted" for a turn that was
answered. Other entries, the same question's earlier answers included, stay.

The real writers run against the in-memory Redis double.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from app.services import agent_chat, agent_memory, user_memory
from app.services.interrupted_turn import interrupted_memory_note, interrupted_memory_prefix
from tests.memory_redis_double import MemoryRedis, use_memory_redis

_ASKED = "User asked: where is the answer?"


def _tiers() -> dict[str, Any]:
    return dict(
        agent_id="agent-1",
        agent_name="Desk",
        user_id="user-1",
        scope_id="user-1",
        is_anonymous=False,
        is_company_managed=False,
        store_short=True,
        store_medium=False,
        store_user=True,
        llm=object(),
        model="m",
        consolidate=False,
    )


def _summaries(redis: MemoryRedis, key: str) -> list[str]:
    stored: Any = redis.data.get(key) or []
    return [json.loads(raw)["summary"] for raw in stored]


async def test_the_resumed_answer_replaces_the_cut_note_in_both_tiers(monkeypatch: pytest.MonkeyPatch) -> None:
    redis = MemoryRedis()  # type: ignore[no-untyped-call]
    use_memory_redis(monkeypatch, redis)
    await agent_chat._store_chat_memories("User asked: earlier → Agent replied: done", **_tiers())
    await agent_chat._store_chat_memories(f"{_ASKED} → {interrupted_memory_note(None, 'stop')}", **_tiers())

    await agent_chat._store_chat_memories(
        f"{_ASKED} → Agent replied: here it is",
        replaces_prefix=interrupted_memory_prefix(_ASKED),
        **_tiers(),
    )

    agent_key = agent_memory._short_term_key("agent-1", "user-1")
    user_key = user_memory._recent_key("user-1")
    for key in (agent_key, user_key):
        assert _summaries(redis, key) == [
            f"{_ASKED} → Agent replied: here it is",
            "User asked: earlier → Agent replied: done",
        ]


async def test_an_answer_without_a_cut_note_removes_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    redis = MemoryRedis()  # type: ignore[no-untyped-call]
    use_memory_redis(monkeypatch, redis)
    await agent_chat._store_chat_memories(f"{_ASKED} → Agent replied: first", **_tiers())

    await agent_chat._store_chat_memories(
        f"{_ASKED} → Agent replied: second",
        replaces_prefix=interrupted_memory_prefix(_ASKED),
        **_tiers(),
    )

    assert _summaries(redis, agent_memory._short_term_key("agent-1", "user-1")) == [
        f"{_ASKED} → Agent replied: second",
        f"{_ASKED} → Agent replied: first",
    ]


async def test_the_counter_still_counts_the_new_entry(monkeypatch: pytest.MonkeyPatch) -> None:
    redis = MemoryRedis()  # type: ignore[no-untyped-call]
    use_memory_redis(monkeypatch, redis)
    await agent_memory.store_short_term_memory(
        "agent-1", source="chat", summary=f"{_ASKED} → {interrupted_memory_note(None)}", scope_id=None
    )

    counter = await agent_memory.store_short_term_memory(
        "agent-1",
        source="chat",
        summary=f"{_ASKED} → Agent replied: ok",
        scope_id=None,
        replaces_prefix=interrupted_memory_prefix(_ASKED),
    )

    assert counter == 2
    assert _summaries(redis, agent_memory._short_term_key("agent-1")) == [f"{_ASKED} → Agent replied: ok"]


def test_the_prefix_matches_the_note_the_cut_writes() -> None:
    note = f"{_ASKED} → {interrupted_memory_note(None, 'stop')}"
    assert note.startswith(interrupted_memory_prefix(_ASKED))
    assert not f"{_ASKED} → Agent replied: x".startswith(interrupted_memory_prefix(_ASKED))
