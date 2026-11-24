"""search_config_text: where a word appears in the configuration a caller sees."""

from __future__ import annotations

import uuid
from types import SimpleNamespace

from app.services import config_text_search as cts
from app.services.hitl_whitelist_default import SHIPPED_DEFAULT_WHITELIST
from app.services.tool_classification import infer_tool_type


def test_find_hits_counts_case_insensitively_and_numbers_lines() -> None:
    count, lines = cts.find_hits("Intro\nMerge at GREEN only.\nnothing\ngreen again, green", "green")

    assert count == 3
    assert lines == [(2, "Merge at GREEN only."), (4, "green again, green")]


def test_find_hits_keeps_a_window_around_the_match_of_a_long_line() -> None:
    line = "a" * 400 + " needle " + "b" * 400
    _count, [(number, shown)] = cts.find_hits(line, "needle")

    assert number == 1
    assert "needle" in shown
    assert shown.startswith("...") and shown.endswith("...")
    assert len(shown) <= cts.LINE_WINDOW + 6


def test_find_hits_caps_the_lines_shown_per_field() -> None:
    count, lines = cts.find_hits("\n".join(["hit"] * 10), "hit")

    assert count == 10
    assert len(lines) == cts.MAX_LINES_PER_FIELD


def test_hits_of_reports_each_field_that_holds_the_query() -> None:
    agent = SimpleNamespace(id=uuid.uuid4(), name="Dev", persona="merge at green", guardrails="never merge red")

    hits = cts.hits_of("agent", [agent], "merge")

    assert [(h.field, h.count) for h in hits] == [("persona", 1), ("guardrails", 1)]
    assert hits[0].object_id == str(agent.id)


def test_parse_scope() -> None:
    assert cts.parse_scope("") == (list(cts.SCOPES), None)
    assert cts.parse_scope("skills, assignment") == (["skill", "assignment"], None)
    kinds, error = cts.parse_scope("skill,deck")
    assert kinds == [] and error is not None and "Unknown scope deck" in error


def test_render_lists_objects_with_their_lines() -> None:
    skill_id = str(uuid.uuid4())
    hits = [cts.ConfigHit("skill", skill_id, "Dev skill", "content", 2, [(12, "merge at green")])]

    text = cts.render("green", hits, ["skill"])

    assert text.startswith("'green' found in 1 object(s) (2 occurrence(s)):")
    assert f"skill **Dev skill** (id: `{skill_id}`)" in text
    assert "    L12: merge at green" in text
    assert cts.render("zz", [], ["skill"]) == "No skill visible to you holds 'zz' (case-insensitive)."


async def test_a_short_query_is_refused_before_any_read() -> None:
    assert (await cts.search_config_text(str(uuid.uuid4()), " a ")).startswith("Rejected:")


def test_it_is_a_read_that_runs_without_a_card() -> None:
    assert infer_tool_type("search_config_text") == "read"
    assert "search_config_text" in SHIPPED_DEFAULT_WHITELIST
