"""What no longer holds stays out of the Redis memory tiers (#11861).

The persistent tier already leaves a superseded entry out of the prompt; the
medium-term summary and the short-term run summaries are text written before
the supersede and reached the system prompt verbatim. These tests pin the
line filter, the loader that applies it, and its fail-open read.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from app.services import agent_memory
from app.services import memory_superseded as ms
from app.services.memory_superseded import Superseded, drop_superseded, drop_superseded_lines, states_superseded

_RULE_A = "Post the verdict as a PR review."
_RULE_B = "Post the verdict as a work item note, never as a PR review."


def test_a_line_stating_the_superseded_entry_is_dropped_and_its_successor_kept():
    superseded = [Superseded(_RULE_A, _RULE_B)]
    summary = "\n".join(
        [
            "- Post the verdict as a PR review",
            f"- {_RULE_B}",
            "- Captures are 1728x1080, dark and light.",
        ]
    )
    kept = drop_superseded_lines(summary, superseded)
    assert _RULE_B in kept
    assert "Captures are 1728x1080" in kept
    assert "- Post the verdict as a PR review\n" not in kept + "\n"
    assert kept.count("PR review") == 1  # only inside the successor's own words


def test_numbers_tell_two_versions_of_one_fact_apart():
    superseded = [Superseded("The deck has 12 slides.", "The deck has 14 slides.")]
    assert states_superseded("- The deck has 12 slides", superseded)
    assert not states_superseded("- The deck has 14 slides", superseded)


def test_a_retired_entry_drops_every_line_close_to_it():
    superseded = [Superseded("Hold every OpenAEV merge until Monday.", None)]
    assert states_superseded("* hold every openaev merge until monday", superseded)
    assert not states_superseded("* OpenAEV PRs merge on team approval.", superseded)


def test_unrelated_lines_and_empty_inputs_are_untouched():
    superseded = [Superseded(_RULE_A, _RULE_B)]
    assert drop_superseded_lines("", superseded) == ""
    assert drop_superseded_lines("- Use DS components only.", []) == "- Use DS components only."
    assert not states_superseded("- Use DS components only.", superseded)


def test_short_term_entries_stating_it_are_dropped_too():
    memory = {
        "medium_term": f"- {_RULE_A}",
        "short_term": [
            {"summary": f"Learned: {_RULE_A}", "timestamp": "2026-11-13T19:00"},
            {"summary": "Reviewed xtm-one #11843.", "timestamp": "2026-11-13T20:00"},
        ],
    }
    filtered = drop_superseded(memory, [Superseded(_RULE_A, _RULE_B)])
    assert filtered["medium_term"] == ""
    assert [entry["summary"] for entry in filtered["short_term"]] == ["Reviewed xtm-one #11843."]


class _Redis:
    def __init__(self, medium: str, short: list[str]) -> None:
        self.medium, self.short = medium, short

    async def lrange(self, *_args):
        return self.short

    async def get(self, _key):
        return self.medium


@pytest.mark.asyncio
async def test_load_memory_leaves_the_superseded_line_out_of_the_prompt(monkeypatch):
    import json

    redis = _Redis(f"- {_RULE_A}\n- Keep captures at 1728x1080.", [json.dumps({"summary": f"Stored rule: {_RULE_A}"})])
    monkeypatch.setattr(agent_memory, "redis_client", AsyncMock(return_value=redis))
    monkeypatch.setattr(ms, "load_superseded", AsyncMock(return_value=[Superseded(_RULE_A, _RULE_B)]))

    block = agent_memory.format_memory_for_prompt(await agent_memory.load_memory("a" * 8, scope_id=None))

    assert _RULE_A.rstrip(".") not in block
    assert "Keep captures at 1728x1080." in block


@pytest.mark.asyncio
async def test_a_failed_read_injects_the_memory_unfiltered(monkeypatch):
    async def _boom(*_args):
        raise RuntimeError("database down")

    monkeypatch.setattr(ms, "_read", _boom)
    assert await ms.load_superseded("00000000-0000-4000-8000-000000000001") == []
    assert await ms.load_superseded("not-a-uuid") == []
