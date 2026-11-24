"""The persistent block of an assignment run fits the memory budget whole.

Merged facts are complete bullet lists of up to 1,600 tokens, so ten
persistent entries reach 30-60K characters. Slicing the combined memory text
at ``_MAX_ASSIGNMENT_MEMORY_CHARS`` dropped the most relevant entries mid-word,
logged a WARNING on every run and scheduled a self-heal that cannot shrink
them. The block now takes what the Redis tiers leave, in whole entries.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest

from app.models.agent import Agent
from app.models.agent_memory_entry import AgentMemoryEntry
from app.services.assignment_helpers import _MAX_ASSIGNMENT_MEMORY_CHARS, _load_memory_context
from app.services.persistent_memory import format_persistent_context

_HEADER = "── Persistent Knowledge (durable facts & learned patterns) ──"


def _entry(content: str, *, age_minutes: int) -> AgentMemoryEntry:
    return AgentMemoryEntry(
        id=uuid.uuid4(),
        agent_id=uuid.uuid4(),
        user_id=None,
        content=content,
        memory_type="fact",
        source="consolidation",
        embedding=None,
        created_at=datetime.now(UTC) - timedelta(minutes=age_minutes),
    )


def _bullets(label: str, count: int) -> str:
    return "\n".join(f"- {label} bullet {i} keeps a complete statement." for i in range(count))


def _legacy_render(entries: list[AgentMemoryEntry]) -> str:
    ordered = sorted(entries, key=lambda e: e.created_at)
    return "\n".join([_HEADER, *(f"• [{e.memory_type}] {e.content}" for e in ordered)])


# ---------------------------------------------------------------------------
# format_persistent_context
# ---------------------------------------------------------------------------


def test_without_a_budget_the_block_is_the_historical_rendering():
    entries = [_entry("newest", age_minutes=1), _entry("oldest", age_minutes=60)]
    assert format_persistent_context(entries) == _legacy_render(entries)


def test_a_block_within_its_budget_is_unchanged():
    entries = [_entry("relevant", age_minutes=5), _entry("recent", age_minutes=1)]
    rendered = _legacy_render(entries)
    assert format_persistent_context(entries, max_chars=len(rendered)) == rendered


def test_over_budget_the_block_keeps_whole_entries_in_priority_order():
    first = _entry(_bullets("alpha", 40), age_minutes=300)
    second = _entry(_bullets("bravo", 40), age_minutes=1)
    third = _entry(_bullets("delta", 40), age_minutes=100)
    line = len("• [fact] ") + len(first.content)
    widest_notice = len(
        "[...3 more persistent memory entries left out to fit the prompt budget...]"
    )
    budget = len(_HEADER) + 2 * (1 + line) + 1 + widest_notice

    block = format_persistent_context([first, second, third], max_chars=budget)

    assert len(block) <= budget
    assert first.content in block
    assert second.content in block
    assert "delta bullet" not in block
    # Kept in priority order, rendered oldest first.
    assert block.index("alpha bullet") < block.index("bravo bullet")
    assert block.endswith(
        "[...1 more persistent memory entry left out to fit the prompt budget...]"
    )


def test_an_entry_too_large_for_the_room_left_is_skipped_for_a_smaller_one():
    large = _entry(_bullets("large", 200), age_minutes=10)
    small = _entry("- a short fact the agent needs.", age_minutes=5)
    budget = len(_HEADER) + len(large.content) // 2

    block = format_persistent_context([large, small], max_chars=budget)

    assert len(block) <= budget
    assert small.content in block
    assert "large bullet" not in block
    assert "1 more persistent memory entry left out" in block


def test_when_no_entry_fits_whole_the_first_is_cut_at_a_boundary():
    first = _entry(_bullets("first", 200), age_minutes=10)
    second = _entry(_bullets("second", 200), age_minutes=5)
    budget = 3_000

    block = format_persistent_context([first, second], max_chars=budget)

    assert len(block) <= budget
    assert "first bullet 0 keeps a complete statement." in block
    assert "second bullet" not in block
    assert "complete statement. [...]\n" in block
    assert "1 more persistent memory entry left out" in block


def test_a_budget_too_small_for_any_knowledge_renders_nothing():
    entries = [_entry(_bullets("only", 50), age_minutes=1)]
    assert format_persistent_context(entries, max_chars=len(_HEADER) + 50) == ""
    assert format_persistent_context(entries, max_chars=-10) == ""


# ---------------------------------------------------------------------------
# _load_memory_context
# ---------------------------------------------------------------------------


def _agent() -> Agent:
    return Agent(
        name="Mail Desk",
        user_id=uuid.uuid4(),
        memory_short_term_enabled=True,
        memory_medium_term_enabled=True,
        memory_persistent_enabled=True,
        memory_user_enabled=False,
    )


async def _load(redis_block: str, entries: list[AgentMemoryEntry]):
    heal = AsyncMock()
    with (
        patch("app.services.agent_memory.load_memory", new=AsyncMock(return_value={})),
        patch("app.services.agent_memory.format_memory_for_prompt", return_value=redis_block),
        patch(
            "app.services.builtin_tools.memory._resolve_provider_model",
            new=AsyncMock(return_value=None),
        ),
        patch(
            "app.services.persistent_memory.load_persistent_entries",
            new=AsyncMock(return_value=entries),
        ),
        patch("app.services.persistent_memory.auto_heal_agent_memory", new=heal),
    ):
        text, _ = await _load_memory_context(db=object(), agent=_agent(), user_id=None)  # type: ignore[arg-type]
    return text, heal


@pytest.mark.asyncio
async def test_memory_within_the_budget_reaches_the_prompt_unchanged():
    redis_block = "── Agent Memory (accumulated knowledge) ──\n- a durable summary."
    entries = [_entry("- fact one.", age_minutes=10), _entry("- fact two.", age_minutes=1)]

    text, heal = await _load(redis_block, entries)

    assert text == f"{redis_block}\n\n{_legacy_render(entries)}"
    heal.assert_not_called()


@pytest.mark.asyncio
async def test_large_persistent_entries_fit_whole_without_a_warning_or_a_self_heal(
    caplog: pytest.LogCaptureFixture,
):
    redis_block = "── Agent Memory (accumulated knowledge) ──\n" + "- summary line.\n" * 500
    entries = [_entry(_bullets(f"merged{i}", 90), age_minutes=i) for i in range(10)]
    caplog.set_level(logging.WARNING, logger="app.services.assignment_helpers")

    text, heal = await _load(redis_block, entries)

    assert len(text) <= _MAX_ASSIGNMENT_MEMORY_CHARS
    assert text.startswith(redis_block)
    # The most relevant entry is kept whole, the block says what it left out.
    assert entries[0].content in text
    assert "left out to fit the prompt budget" in text
    assert "memory truncated to fit the prompt budget" not in text
    heal.assert_not_called()
    assert not [r for r in caplog.records if "combined memory context" in r.getMessage()]
