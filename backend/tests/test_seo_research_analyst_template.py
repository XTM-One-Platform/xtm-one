"""Regression tests for the SEO Research Analyst built-in agent template.

Guards against the class of failures where the
agent was calling Ahrefs MCP tools with stale column names:
- ``current_position`` instead of ``best_position``
- ``sum_traffic_merged`` instead of ``sum_traffic``
and applied an invalid ``group_by``/``where`` filter on the ``domain`` field
in ``site-explorer-referring-domains``.

These tests ensure the built-in template prompts reference only the correct
current Ahrefs column names and never reintroduce the renamed/removed ones.
"""

from __future__ import annotations

import re

from app.agents.templates import BUILTIN_TEMPLATES

# ---------------------------------------------------------------------------
# Template presence
# ---------------------------------------------------------------------------


def _seo_template() -> dict:
    matches = [t for t in BUILTIN_TEMPLATES if t.get("slug") == "seo-research-analyst"]
    assert matches, "seo-research-analyst template not found in BUILTIN_TEMPLATES"
    return matches[0]


def _assignment(template: dict, name: str) -> dict:
    matches = [a for a in template.get("default_assignments", []) if a.get("name") == name]
    assert matches, f"Assignment '{name}' not found in seo-research-analyst template"
    return matches[0]


def test_seo_template_exists():
    """The SEO Research Analyst template must be present in BUILTIN_TEMPLATES."""
    _seo_template()


def test_seo_template_has_weekly_keyword_report_assignment():
    """The Weekly Competitor Keyword Report assignment must exist."""
    tmpl = _seo_template()
    _assignment(tmpl, "Weekly Competitor Keyword Report")


def test_seo_template_has_weekly_backlink_digest_assignment():
    """The Weekly Backlink Monitoring Digest assignment must exist."""
    tmpl = _seo_template()
    _assignment(tmpl, "Weekly Backlink Monitoring Digest")


# ---------------------------------------------------------------------------
# Ahrefs column correctness: keyword report
# ---------------------------------------------------------------------------


def _keyword_report_prompt() -> str:
    tmpl = _seo_template()
    return _assignment(tmpl, "Weekly Competitor Keyword Report").get("prompt", "")


def test_keyword_report_uses_best_position():
    """The keyword report prompt must reference `best_position` (current column name)."""
    prompt = _keyword_report_prompt()
    assert "best_position" in prompt, (
        "Weekly Competitor Keyword Report prompt must use `best_position` — the current "
        "Ahrefs column name for keyword ranking position."
    )


def test_keyword_report_does_not_use_current_position():
    """The keyword report prompt must NOT reference the obsolete `current_position` column."""
    prompt = _keyword_report_prompt()
    # Strict check: the obsolete column name must not appear anywhere in the
    # prompt, not even in a negation/correction sentence — LLMs latch onto any
    # occurrence of a token, so the safest prompt never mentions it at all.
    assert "current_position" not in prompt, (
        "Weekly Competitor Keyword Report prompt must not use `current_position` — this "
        "column was renamed to `best_position` and causes a validation error."
    )


def test_keyword_report_uses_sum_traffic():
    """The keyword report prompt must reference `sum_traffic` (current column name)."""
    prompt = _keyword_report_prompt()
    assert "sum_traffic" in prompt, (
        "Weekly Competitor Keyword Report prompt must use `sum_traffic` — the current "
        "Ahrefs column name for estimated organic traffic."
    )


def test_keyword_report_does_not_use_sum_traffic_merged():
    """The keyword report prompt must NOT reference the obsolete `sum_traffic_merged` column."""
    prompt = _keyword_report_prompt()
    assert "sum_traffic_merged" not in prompt, (
        "Weekly Competitor Keyword Report prompt must not use `sum_traffic_merged` — this "
        "column was renamed to `sum_traffic` and causes a validation error."
    )


# ---------------------------------------------------------------------------
# Ahrefs referring-domains correctness
# ---------------------------------------------------------------------------


def _backlink_digest_prompt() -> str:
    tmpl = _seo_template()
    return _assignment(tmpl, "Weekly Backlink Monitoring Digest").get("prompt", "")


def test_backlink_digest_warns_against_domain_group_filter():
    """The backlink digest prompt must warn against using an invalid group filter on `domain`."""
    prompt = _backlink_digest_prompt()
    # The warning must tie `group_by` to the `domain` field specifically — a
    # generic mention of group_by elsewhere in the prompt is not enough.
    warning = re.search(r"group_by.{0,120}?domain", prompt, re.DOTALL)
    assert warning, (
        "Weekly Backlink Monitoring Digest prompt must document that a group_by / group "
        "filter on the `domain` field is invalid in site-explorer-referring-domains "
        "('invalid group filter for field domain'). The warning must "
        "mention `domain` near `group_by`, not just group_by in isolation."
    )


def test_backlink_digest_does_not_use_domain_group_filter():
    """The backlink digest prompt must not contain `group_by=domain` or `group_by: domain`."""
    prompt = _backlink_digest_prompt()
    # Catch plain and structured forms alike: group_by=domain, group_by: domain,
    # group_by="domain", "group_by": "domain" (JSON), and YAML `group_by:` with
    # quoting/whitespace/newlines before the value. The delimiter run before
    # `domain` may mix quotes/backticks/whitespace around a single `=` or `:`.
    # It intentionally does NOT match the prose warning ("Do NOT set a
    # `group_by` or `where` filter on the `domain` field"), which has no
    # `=`/`:` between the two tokens.
    bad_pattern = re.compile(r"group_by[\"'`\s]*[=:][\"'`\s]*domain")
    assert not bad_pattern.search(prompt), (
        "Weekly Backlink Monitoring Digest prompt must not apply a group_by filter on "
        "`domain` — that field is not a valid group-by dimension in Ahrefs "
        "site-explorer-referring-domains."
    )


# ---------------------------------------------------------------------------
# Assignment configuration
# ---------------------------------------------------------------------------


def test_keyword_report_is_scheduled():
    """Weekly Competitor Keyword Report must use a schedule trigger."""
    tmpl = _seo_template()
    assignment = _assignment(tmpl, "Weekly Competitor Keyword Report")
    assert assignment.get("trigger_type") == "schedule", (
        "Weekly Competitor Keyword Report should run on a schedule (weekly cron)."
    )


def test_backlink_digest_is_scheduled():
    """Weekly Backlink Monitoring Digest must use a schedule trigger."""
    tmpl = _seo_template()
    assignment = _assignment(tmpl, "Weekly Backlink Monitoring Digest")
    assert assignment.get("trigger_type") == "schedule", (
        "Weekly Backlink Monitoring Digest should run on a schedule (weekly cron)."
    )


def test_keyword_report_has_bounded_iterations():
    """Weekly Competitor Keyword Report must carry an explicit max_iterations cap."""
    tmpl = _seo_template()
    assignment = _assignment(tmpl, "Weekly Competitor Keyword Report")
    mi = assignment.get("max_iterations")
    assert mi is not None, (
        "Weekly Competitor Keyword Report must declare max_iterations to prevent runaway loops."
    )
    assert mi <= 30, (
        f"max_iterations={mi} is too large for a bounded SEO keyword report task."
    )


def test_backlink_digest_has_bounded_iterations():
    """Weekly Backlink Monitoring Digest must carry an explicit max_iterations cap."""
    tmpl = _seo_template()
    assignment = _assignment(tmpl, "Weekly Backlink Monitoring Digest")
    mi = assignment.get("max_iterations")
    assert mi is not None, (
        "Weekly Backlink Monitoring Digest must declare max_iterations to prevent runaway loops."
    )
    assert mi <= 30, (
        f"max_iterations={mi} is too large for a bounded backlink monitoring task."
    )


def test_keyword_report_has_bounded_timeout():
    """Weekly Competitor Keyword Report must carry an explicit timeout_seconds cap."""
    tmpl = _seo_template()
    assignment = _assignment(tmpl, "Weekly Competitor Keyword Report")
    ts = assignment.get("timeout_seconds")
    assert ts is not None, (
        "Weekly Competitor Keyword Report must declare timeout_seconds so the "
        "template-load path stores a concrete budget instead of the platform default."
    )
    assert ts <= 600, (
        f"timeout_seconds={ts} is too large for a weekly SEO keyword report task."
    )


def test_backlink_digest_has_bounded_timeout():
    """Weekly Backlink Monitoring Digest must carry an explicit timeout_seconds cap."""
    tmpl = _seo_template()
    assignment = _assignment(tmpl, "Weekly Backlink Monitoring Digest")
    ts = assignment.get("timeout_seconds")
    assert ts is not None, (
        "Weekly Backlink Monitoring Digest must declare timeout_seconds so the "
        "template-load path stores a concrete budget instead of the platform default."
    )
    assert ts <= 600, (
        f"timeout_seconds={ts} is too large for a weekly backlink monitoring task."
    )


def test_seo_template_persona_documents_correct_column_names():
    """The SEO agent persona must document the correct Ahrefs column names."""
    tmpl = _seo_template()
    persona = tmpl.get("default_persona", "")
    assert "best_position" in persona, (
        "The SEO Research Analyst persona must mention `best_position` so the agent "
        "always uses the correct Ahrefs column name regardless of which assignment runs."
    )
    assert "sum_traffic" in persona, (
        "The SEO Research Analyst persona must mention `sum_traffic` so the agent "
        "always uses the correct Ahrefs traffic column name."
    )


def test_seo_template_persona_does_not_use_obsolete_column_names():
    """The SEO agent persona must NOT mention the renamed Ahrefs column names."""
    tmpl = _seo_template()
    persona = tmpl.get("default_persona", "")
    assert "current_position" not in persona, (
        "The SEO Research Analyst persona must not mention `current_position` — "
        "this column was renamed to `best_position` and any "
        "occurrence risks the agent reusing it in tool calls."
    )
    assert "sum_traffic_merged" not in persona, (
        "The SEO Research Analyst persona must not mention `sum_traffic_merged` — "
        "this column was renamed to `sum_traffic` and any "
        "occurrence risks the agent reusing it in tool calls."
    )
