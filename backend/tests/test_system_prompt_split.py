"""The stable system prefix is cacheable across runs (#11880).

A system prompt carries per-run bytes (the clock, the user's local time, the
fenced memory). Sent as one cached block, any of them invalidated the system
breakpoint and every run wrote persona, skills and policies to the cache
again. The prompt is split at the clock line: the stable prefix carries the
breakpoint, the volatile tail rides after it unmarked.
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.llm.anthropic_provider import AnthropicProvider, _apply_prompt_caching
from app.llm.base import ChatMessage
from app.llm.system_prompt_split import SYSTEM_VOLATILE_TAIL_MARKER, split_system_prompt
from app.models.agent import Agent
from app.models.assignment import Assignment
from app.services.builtin_tools.prompting import TIME_QUOTING_RULE, format_datetime_prompt

_CACHE = {"type": "ephemeral"}


def _prompt(clock: str, memory: str = "") -> str:
    return f"You are the digest agent.\n\nSkills catalog.\n{SYSTEM_VOLATILE_TAIL_MARKER}{clock}\n{memory}\nPolicy."


class TestSplit:
    def test_the_tail_starts_at_the_clock_line_and_nothing_is_lost(self):
        text = _prompt("Saturday at 17:14 UTC", "memory")
        stable, volatile = split_system_prompt(text)

        assert stable + volatile == text
        assert stable == "You are the digest agent.\n\nSkills catalog."
        assert volatile.startswith(f"\n{SYSTEM_VOLATILE_TAIL_MARKER}")

    def test_a_prompt_without_a_clock_is_stable_whole(self):
        assert split_system_prompt("You are a helpful agent.") == ("You are a helpful agent.", "")

    def test_a_prompt_that_opens_with_the_clock_is_stable_whole(self):
        text = f"\n{SYSTEM_VOLATILE_TAIL_MARKER}now\npersona"
        assert split_system_prompt(text) == (text, "")

    def test_the_clock_writer_uses_the_marker(self):
        assert format_datetime_prompt().startswith(f"\n{SYSTEM_VOLATILE_TAIL_MARKER}")
        assert format_datetime_prompt("Europe/Paris").startswith(f"\n{SYSTEM_VOLATILE_TAIL_MARKER}")

    def test_the_clock_block_carries_the_time_quoting_rule_inside_the_tail(self) -> None:
        # Issue #2764: the rule sits after the marker, so the cached prefix is unchanged.
        for block in (format_datetime_prompt(), format_datetime_prompt("Europe/Paris")):
            assert block.rstrip("\n").endswith(f"\n{TIME_QUOTING_RULE}")
            assert block.index(SYSTEM_VOLATILE_TAIL_MARKER) < block.index(TIME_QUOTING_RULE)
            stable, volatile = split_system_prompt(f"persona{block}\nPolicy.")
            assert stable == "persona"
            assert TIME_QUOTING_RULE in volatile
        assert "never estimate one" in TIME_QUOTING_RULE


class TestApplyPromptCaching:
    def test_two_runs_send_the_same_marked_prefix(self):
        first = {"system": _prompt("Saturday at 17:14 UTC", "run one memory")}
        second = {"system": _prompt("Saturday at 17:16 UTC", "run two memory")}
        _apply_prompt_caching(first)
        _apply_prompt_caching(second)

        assert first["system"][0] == second["system"][0]
        assert first["system"][0]["cache_control"] == _CACHE
        assert "cache_control" not in first["system"][1]
        assert first["system"][1] != second["system"][1]
        assert "".join(b["text"] for b in first["system"]) == _prompt("Saturday at 17:14 UTC", "run one memory")

    def test_a_prompt_without_a_clock_stays_one_marked_block(self):
        params = {"system": "You are a helpful agent."}
        _apply_prompt_caching(params)
        assert params["system"] == [{"type": "text", "text": "You are a helpful agent.", "cache_control": _CACHE}]

    def test_the_breakpoint_count_is_unchanged(self):
        params = {
            "system": _prompt("now", "memory"),
            "tools": [{"name": "a"}, {"name": "b"}],
        }
        _apply_prompt_caching(params)
        marked = [b for b in params["system"] if "cache_control" in b]
        marked += [t for t in params["tools"] if "cache_control" in t]
        assert len(marked) == 2
        assert params["extra_body"]["cache_control"] == _CACHE

    def test_caching_off_keeps_the_string(self):
        params = {"system": _prompt("now")}
        _apply_prompt_caching(params, prompt_cache=False)
        assert isinstance(params["system"], str)


@pytest.mark.asyncio
async def test_the_provider_sends_the_stable_prefix_with_the_breakpoint():
    from tests.test_anthropic_provider import _build_chat_response_stub

    provider = AnthropicProvider(api_key="test-key")
    provider.client = MagicMock()
    provider.client.messages.create = AsyncMock(return_value=_build_chat_response_stub())
    system = _prompt("Saturday at 17:14 UTC", "memory")

    await provider.chat(messages=[ChatMessage(role="system", content=system), ChatMessage(role="user", content="hi")])

    sent = provider.client.messages.create.call_args.kwargs["system"]
    assert len(sent) == 2
    assert sent[0]["cache_control"] == _CACHE
    assert sent[0]["text"] == "You are the digest agent.\n\nSkills catalog."
    assert "cache_control" not in sent[1]


class _NoUserDB:
    async def execute(self, *args, **kwargs):
        result = MagicMock()
        result.scalar_one_or_none.return_value = None
        return result


async def _assignment_prompt(memory: str) -> str:
    from app.services.assignment_helpers import _build_system_message

    return await _build_system_message(
        db=_NoUserDB(),
        agent=Agent(name="Digest", persona="You are the digest agent. " * 50, guardrails="Be careful."),
        assignment=Assignment(
            agent_id=uuid.uuid4(),
            name="Nightly digest",
            trigger_type="schedule",
            trigger_config={},
            prompt="Summarise the day",
        ),
        user_id=uuid.uuid4(),
        user_memory_text=memory,
        agent_memory_text="",
        element_bindings={},
    )


@pytest.mark.asyncio
async def test_two_assignment_prompts_share_a_byte_identical_stable_prefix(monkeypatch):
    clocks = iter(["Saturday at 17:14 UTC", "Saturday at 17:16 UTC"])
    monkeypatch.setattr(
        "app.services.builtin_tools.prompting.format_datetime_prompt",
        lambda *_a, **_k: f"\n{SYSTEM_VOLATILE_TAIL_MARKER}{next(clocks)}",
    )

    first = await _assignment_prompt("remembered on run one")
    second = await _assignment_prompt("remembered on run two")

    stable_1, volatile_1 = split_system_prompt(first)
    stable_2, volatile_2 = split_system_prompt(second)
    assert first != second
    assert stable_1 == stable_2
    assert "You are the digest agent." in stable_1
    assert "17:14" not in stable_1 and "run one" not in stable_1
    assert "17:14" in volatile_1 and "run one" in volatile_1
    assert volatile_1 != volatile_2
