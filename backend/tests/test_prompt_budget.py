"""Prompt size observability helpers (services/prompt_budget.py).

Covers the shared token-estimate heuristic, the per-component breakdown
used by the large-prompt warnings, and the per-key debounce that stops
the warnings from firing on every message of a large-persona agent.
"""

from __future__ import annotations

import pytest

from app.services import prompt_budget
from app.services.prompt_budget import (
    LARGE_PROMPT_TOKEN_THRESHOLD,
    estimate_tokens,
    fixed_context,
    format_section_breakdown,
    reset_warning_state,
    section_sizes,
    sent_context,
    should_log_large_prompt,
    tool_schema_sizes,
)


@pytest.fixture(autouse=True)
def _clean_state():
    reset_warning_state()
    yield
    reset_warning_state()


class TestEstimateTokens:
    def test_chars_per_token_heuristic(self):
        assert estimate_tokens("x" * 81_380) == 27_126

    def test_empty(self):
        assert estimate_tokens("") == 0

    def test_threshold_matches_legacy_value(self):
        # The warnings previously hardcoded 20_000 — keep behaviour stable.
        assert LARGE_PROMPT_TOKEN_THRESHOLD == 20_000


class TestShouldLogLargePrompt:
    def test_first_call_logs(self):
        assert should_log_large_prompt("agent_chat:abc") is True

    def test_second_call_within_interval_is_debounced(self):
        assert should_log_large_prompt("agent_chat:abc") is True
        assert should_log_large_prompt("agent_chat:abc") is False

    def test_distinct_keys_are_independent(self):
        assert should_log_large_prompt("agent_chat:a") is True
        assert should_log_large_prompt("agent_chat:b") is True

    def test_logs_again_after_interval(self, monkeypatch):
        base = 1000.0
        monkeypatch.setattr(prompt_budget.time, "monotonic", lambda: base)
        assert should_log_large_prompt("k", interval_seconds=60) is True
        monkeypatch.setattr(prompt_budget.time, "monotonic", lambda: base + 30)
        assert should_log_large_prompt("k", interval_seconds=60) is False
        monkeypatch.setattr(prompt_budget.time, "monotonic", lambda: base + 61)
        assert should_log_large_prompt("k", interval_seconds=60) is True

    def test_key_cardinality_is_bounded(self):
        for i in range(prompt_budget._MAX_TRACKED_KEYS + 50):
            should_log_large_prompt(f"key-{i}")
        assert len(prompt_budget._last_warned) <= prompt_budget._MAX_TRACKED_KEYS


class TestFormatSectionBreakdown:
    def test_largest_first_and_scaffolding_remainder(self):
        sections = [("persona", "p" * 300), ("skills", "s" * 900)]
        out = format_section_breakdown(sections, total_chars=1500)
        # Sorted by size: skills (900) > scaffolding (300) > persona (300)
        assert out.index("skills=900") < out.index("persona=300")
        assert "platform_scaffolding=300" in out

    def test_empty_sections_are_skipped(self):
        out = format_section_breakdown(
            [("persona", "abc"), ("skills", "")], total_chars=3
        )
        assert "skills" not in out
        assert "persona=3" in out

    def test_no_negative_scaffolding(self):
        # Sections joined with separators can exceed the raw total — the
        # remainder must clamp at zero instead of going negative.
        out = format_section_breakdown([("persona", "abcd")], total_chars=3)
        assert "platform_scaffolding" not in out

    def test_token_estimate_included(self):
        out = format_section_breakdown([("persona", "x" * 3000)], total_chars=3000)
        assert "persona=3000 chars (~1000 tok)" in out


def _tool(name: str, source: str, description: str = "d", schema: dict | None = None):
    from types import SimpleNamespace

    return SimpleNamespace(
        name=name,
        description=description,
        input_schema=schema if schema is not None else {"type": "object"},
        source=source,
    )


class TestSectionSizes:
    def test_repeated_label_summed_and_scaffolding_remainder(self):
        sizes = section_sizes(
            [("attached_prompts", "a" * 100), ("skills", "s" * 300), ("attached_prompts", "a" * 50)],
            total_chars=500,
        )
        assert sizes == {"skills": 300, "attached_prompts": 150, "platform_scaffolding": 50}
        assert list(sizes) == ["skills", "attached_prompts", "platform_scaffolding"]

    def test_empty_sections_and_no_negative_scaffolding(self):
        assert section_sizes([("persona", "abcd"), ("skills", "")], total_chars=3) == {"persona": 4}


class TestToolSchemaSizes:
    def test_total_count_and_by_source(self):
        tools = [_tool("a", "builtin"), _tool("b", "builtin"), _tool("c", "mcp:x", description="long" * 10)]
        sizes = tool_schema_sizes(tools)
        one = len("a") + len("d") + len('{"type":"object"}')
        assert sizes["tool_count"] == 3
        assert sizes["by_source"]["builtin"] == 2 * one
        assert sizes["by_source"]["mcp:x"] == 1 + 40 + len('{"type":"object"}')
        assert sizes["tool_schema_chars"] == sum(sizes["by_source"].values())
        assert next(iter(sizes["by_source"])) == "mcp:x"

    def test_sources_past_the_top_are_summed_as_other(self):
        tools = [_tool(f"t{i}", f"src{i}") for i in range(5)]
        sizes = tool_schema_sizes(tools, top=2)
        assert len(sizes["by_source"]) == 3
        assert "other" in sizes["by_source"]
        assert sum(sizes["by_source"].values()) == sizes["tool_schema_chars"]

    def test_malformed_tool_is_counted_without_raising(self):
        from types import SimpleNamespace

        sizes = tool_schema_sizes([SimpleNamespace(), _tool("a", "builtin")])
        assert sizes["tool_count"] == 2
        assert sizes["by_source"]["unknown"] == len("{}")


class TestFixedContext:
    def test_sizes_only_and_estimate(self):
        prompt = "p" * 600
        out = fixed_context([("persona", "p" * 600)], prompt, [_tool("a", "builtin")])
        assert out["system_prompt_chars"] == 600
        assert out["sections"] == {"persona": 600}
        assert out["total_chars"] == 600 + out["tool_schema_chars"]
        assert out["est_tokens"] == out["total_chars"] // 3
        assert "p" * 10 not in repr(out)


class TestSentContext:
    def test_visible_schemas_plus_catalog(self):
        out = sent_context([_tool("a", "builtin"), _tool("b", "mcp:x")], "c" * 90)
        assert out["tool_count"] == 2
        assert out["catalog_prompt_chars"] == 90
        assert out["total_chars"] == out["tool_schema_chars"] + 90
        assert out["est_tokens"] == out["total_chars"] // 3
        assert "c" * 10 not in repr(out)

    def test_empty_catalog(self):
        out = sent_context([], "")
        assert out["tool_count"] == 0
        assert out["total_chars"] == 0
