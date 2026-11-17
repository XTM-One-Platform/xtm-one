"""Tests for the conversation compactor's response parser.

These tests pin down the contract of
:func:`app.services.conversation_memory._parse_compaction_json` so its
hardening doesn't regress:

- valid JSON
- JSON wrapped in a Markdown code fence
- JSON with prose preamble / trailing commentary
- JSON with trailing commas (json-repair territory)
- truncated JSON ("max_tokens" cut-off)
- free-text bullet list with no JSON at all
- empty / non-dict / completely unparseable garbage

The high-level :func:`_compact_messages` LLM call is also exercised with
a stub provider to verify that the strict JSON schema is wired into the
``structured_output`` kwarg when the provider exposes a recognisable
``provider_name`` (regression coverage for the original silent-failure
mode where the compactor was sent without any schema enforcement).
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any

import pytest

from app.llm.base import ChatMessage, ChatResponse
from app.services import conversation_memory as cm

# ---------------------------------------------------------------------------
# _parse_compaction_json — happy paths
# ---------------------------------------------------------------------------


def test_parse_compaction_json_clean_json() -> None:
    raw = (
        '{"directives": ["use subregion not country", "prefix currency with €"], '
        '"summary": "- discussed dashboards\\n- agreed on YTD averages"}'
    )
    directives, summary = cm._parse_compaction_json(raw)
    assert directives == [
        "use subregion not country",
        "prefix currency with €",
    ]
    assert "discussed dashboards" in summary
    assert "YTD averages" in summary


def test_parse_compaction_json_strips_markdown_fence() -> None:
    raw = (
        "```json\n"
        '{"directives": ["answer in French"], '
        '"summary": "- one\\n- two"}\n'
        "```"
    )
    directives, summary = cm._parse_compaction_json(raw)
    assert directives == ["answer in French"]
    assert summary.startswith("- one")


def test_parse_compaction_json_skips_empty_fence_stub() -> None:
    """The model sometimes emits an empty ```json``` stub before the
    real payload. The parser must skip the empty fence and read the JSON from
    the later fence instead of failing at char 0 and dropping all directives.
    """
    raw = (
        "```json\n```\n\n"  # empty stub fence (the trigger)
        "```json\n"
        '{"directives": ["answer in French"], "summary": "- one\\n- two"}\n'
        "```"
    )
    directives, summary = cm._parse_compaction_json(raw)
    assert directives == ["answer in French"]
    assert summary.startswith("- one")


def test_parse_compaction_json_handles_prose_preamble() -> None:
    """A model that ignores the 'no preamble' instruction must still parse."""
    raw = (
        "Sure! Here is the compaction output you requested:\n\n"
        '{"directives": ["use dashboard X"], "summary": "- discussed dashboard X"}'
    )
    directives, summary = cm._parse_compaction_json(raw)
    assert directives == ["use dashboard X"]
    assert "dashboard X" in summary


def test_parse_compaction_json_repairs_trailing_commas() -> None:
    """json-repair must rescue trailing-comma JSON without dropping directives."""
    raw = (
        '{"directives": ["A", "B", "C",], '
        '"summary": "- ok",}'
    )
    directives, summary = cm._parse_compaction_json(raw)
    assert directives == ["A", "B", "C"]
    assert summary == "- ok"


# ---------------------------------------------------------------------------
# _parse_compaction_json — recovery paths
# ---------------------------------------------------------------------------


def test_parse_compaction_json_recovers_truncated_json_via_bullets(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A max_tokens-truncated response (no closing brace) must still recover
    directive-shaped bullet lines instead of silently dropping them."""
    raw = (
        # Deliberately-mangled JSON that json-repair cannot fully rescue:
        # mid-string truncation inside the second array element. Bullet
        # recovery must still find the standalone bullet list below.
        '{"directives": ["use subregion",\n'
        "- use subregion not country\n"
        "- prefix currency with €\n"
        "- answer in markdown\n"
    )
    with caplog.at_level(logging.WARNING, logger=cm.logger.name):
        directives, summary = cm._parse_compaction_json(raw)

    # When json-repair manages to coerce the broken JSON into a usable
    # dict, that wins (and is fine). When it can't, the bullet fallback
    # must kick in. Either way the user-visible constraints must
    # survive — the regression we are guarding against is silently
    # dropping them.
    flattened = " | ".join(directives).lower()
    assert "subregion" in flattened
    assert "currency" in flattened
    # Summary must not duplicate the bullets when we go through the
    # bullet-recovery branch.
    if any(
        "non-JSON output" in record.message for record in caplog.records
    ):
        assert summary == ""


def test_parse_compaction_json_recovers_free_text_when_summary_precedes_directives(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """End-to-end regression for the bidirectional section-aware recovery.

    A malformed free-text response that puts ``Summary:`` BEFORE
    ``Directives:`` must still recover every directive — the earlier
    behaviour would have returned an empty directive list, silently
    losing every constraint the user had set.
    """
    raw = (
        "Summary:\n"
        "- discussed dashboards\n"
        "- agreed on YTD averages\n"
        "\n"
        "Directives:\n"
        "- use subregion filter, not country\n"
        "- prefix currency with €\n"
        "- answer in markdown\n"
    )
    with caplog.at_level(logging.WARNING, logger=cm.logger.name):
        directives, summary = cm._parse_compaction_json(raw)

    lowered = {d.lower() for d in directives}
    assert any("subregion filter" in d for d in lowered)
    assert any("currency" in d for d in lowered)
    assert any("markdown" in d for d in lowered)
    # Summary-section bullets must NOT be promoted.
    assert not any("discussed dashboards" in d for d in lowered)
    assert not any("ytd averages" in d for d in lowered)
    # Bullet-recovery path → summary blank by construction.
    assert summary == ""


def test_parse_compaction_json_recovers_free_text_bullets(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """When the model abandons JSON entirely and just lists bullets, we
    still pull directive-shaped lines out of the raw text — AND we do
    NOT promote summary/recap bullets into pinned directives."""
    raw = (
        "Here are the active directives:\n"
        "- use subregion filter, not country\n"
        "- prefix currency with €\n"
        "- answer in markdown\n"
        "\n"
        "Summary:\n"
        "- discussed dashboards\n"
        "- agreed on YTD averages\n"
    )
    with caplog.at_level(logging.WARNING, logger=cm.logger.name):
        directives, summary = cm._parse_compaction_json(raw)

    # At least the three real constraint lines must come back.
    lowered = {d.lower() for d in directives}
    assert any("subregion filter" in d for d in lowered)
    assert any("currency" in d for d in lowered)
    assert any("markdown" in d for d in lowered)
    # Regression guard:
    # bullets BELOW the "Summary:" heading describe conversation
    # history and must NOT be re-injected as active directives.
    assert not any("discussed dashboards" in d for d in lowered), (
        "summary-section bullets must not leak into pinned directives"
    )
    assert not any("ytd averages" in d for d in lowered), (
        "summary-section bullets must not leak into pinned directives"
    )
    # Summary intentionally left blank when bullet recovery succeeded,
    # because the raw text is a directive dump rather than a real summary.
    assert summary == ""

    # The warning log must include metadata operators can act on
    # (length, recovered count, first parse error) but must NOT include
    # the raw response — that ships at DEBUG behind a redaction step.
    warning_records = [
        record for record in caplog.records
        if record.levelno == logging.WARNING
        and "non-JSON output" in record.message
    ]
    assert warning_records, "expected exactly one non-JSON output warning"
    for record in warning_records:
        assert "raw_len" in record.message, (
            "warning must surface the raw length so operators can size budgets"
        )
        assert "raw_preview" not in record.message, (
            "raw response content must not appear at WARNING (PII/credential "
            "leak risk)"
        )


def test_parse_compaction_json_handles_unparseable_garbage(
    caplog: pytest.LogCaptureFixture,
) -> None:
    raw = "💥 the LLM crashed mid-thought 💥"
    with caplog.at_level(logging.WARNING, logger=cm.logger.name):
        directives, summary = cm._parse_compaction_json(raw)
    assert directives == []
    # Falls back to raw summary preservation so the system message still
    # carries *something* from the failed compaction.
    assert summary.startswith("💥") or summary == ""


def test_parse_compaction_json_empty_string() -> None:
    """Empty input must not raise and must not log warnings."""
    directives, summary = cm._parse_compaction_json("")
    assert directives == []
    assert summary == ""


def test_parse_compaction_json_non_dict_top_level() -> None:
    """A bare JSON list at the top level is unsupported but must not raise."""
    raw = '["use subregion not country"]'
    directives, summary = cm._parse_compaction_json(raw)
    # No assertion on exact content — recovery may pick up the bullet
    # form or not depending on json-repair's behaviour. The contract
    # here is *no exception* and a typed tuple shape.
    assert isinstance(directives, list)
    assert isinstance(summary, str)


def test_parse_compaction_json_recovers_directives_when_field_is_string() -> None:
    """When SAP returns a dict whose
    ``directives`` field is the wrong shape (a single bullet-list string
    instead of an array of strings) but the ``summary`` is non-empty,
    the directives must be recovered via the bullet extractor instead
    of being silently dropped.

    The previous shape replaced the string with ``[]`` and returned
    immediately because the early-return gate only checked
    ``directives or summary.strip()`` — so any time ``summary`` was
    non-empty, every directive bullet inside the misshapen field was
    lost.
    """
    raw = (
        '{"directives": "- use subregion filter, not country\\n'
        '- prefix currency with €\\n'
        '- answer in markdown", '
        '"summary": "User asked for region-by-region revenue."}'
    )
    directives, summary = cm._parse_compaction_json(raw)
    lowered = {d.lower() for d in directives}
    assert any("subregion filter" in d for d in lowered)
    assert any("currency" in d for d in lowered)
    assert any("markdown" in d for d in lowered)
    # Summary must survive verbatim — the SAP-returned string was
    # already in the right shape, only the directives field was
    # misshapen.
    assert "region-by-region" in summary


def test_parse_compaction_json_string_directives_without_bullets_yields_empty() -> None:
    """A misshapen ``directives`` string with no bullet markers
    degrades cleanly to an empty list — we don't want to invent
    directives by splitting on commas / sentences.
    """
    raw = (
        '{"directives": "no specific constraints today", '
        '"summary": "Light conversation."}'
    )
    directives, summary = cm._parse_compaction_json(raw)
    assert directives == []
    assert summary == "Light conversation."


def test_parse_compaction_json_dict_directives_drops_silently() -> None:
    """A misshapen ``directives`` dict (or any other non-list, non-string
    shape) produces no directives but does not raise."""
    raw = (
        '{"directives": {"x": "y"}, '
        '"summary": "Some summary text."}'
    )
    directives, summary = cm._parse_compaction_json(raw)
    assert directives == []
    assert summary == "Some summary text."


def test_parse_compaction_json_list_directives_drops_blank_and_non_strings() -> None:
    """A list with mixed-type entries: SAP normalises non-strings into
    their stringified form (``42`` -> ``'42'``, ``null`` -> dropped),
    and the parser then drops any entry that is blank after ``.strip()``
    so an all-whitespace string doesn't survive as a directive. The
    real-string entries always survive.
    """
    raw = (
        '{"directives": ["use subregion", "", "   ", null, '
        '"answer in markdown"], "summary": "Mixed list."}'
    )
    directives, summary = cm._parse_compaction_json(raw)
    # Every entry left in the list is a non-empty stripped string.
    assert all(isinstance(d, str) and d.strip() for d in directives)
    # The two real directives must survive.
    assert "use subregion" in directives
    assert "answer in markdown" in directives
    assert summary == "Mixed list."


# ---------------------------------------------------------------------------
# _preview_raw_output
# ---------------------------------------------------------------------------


def test_preview_raw_output_flattens_newlines() -> None:
    raw = "line one\nline two\rline three\tindented"
    preview = cm._preview_raw_output(raw)
    assert "\n" not in preview
    assert "\r" not in preview
    assert "\t" not in preview
    assert "\\n" in preview
    assert "\\t" in preview


def test_preview_raw_output_truncates_with_marker() -> None:
    raw = "x" * 1000
    preview = cm._preview_raw_output(raw, max_chars=50)
    assert preview.endswith("…")
    assert len(preview) == 51  # 50 chars + 1-char ellipsis


def test_preview_raw_output_empty_string() -> None:
    assert cm._preview_raw_output("") == ""


# ---------------------------------------------------------------------------
# _extract_bullets_from_text
# ---------------------------------------------------------------------------


def test_extract_bullets_skips_section_headers() -> None:
    raw = (
        "- Directives:\n"
        "- use subregion not country\n"
        "* prefix currency with €\n"
        "• answer in markdown\n"
        "1. always use YTD averages\n"
        "2) align decimals to 2\n"
    )
    bullets = cm._extract_bullets_from_text(raw)
    # Header "- Directives:" must be dropped; the five real bullets kept.
    assert "Directives:" not in bullets
    assert len(bullets) == 5
    assert "use subregion not country" in bullets
    assert "always use YTD averages" in bullets


def test_extract_bullets_deduplicates() -> None:
    """Bullet recovery must collapse case-insensitive duplicates so a
    flaky model that repeats itself doesn't fill the directive cap."""
    raw = (
        "- use subregion not country\n"
        "- use subregion not country\n"
        "- Use Subregion Not Country\n"
        "* prefix currency with €\n"
    )
    assert cm._extract_bullets_from_text(raw) == [
        "use subregion not country",
        "prefix currency with €",
    ]


def test_extract_bullets_drops_too_short_fragments() -> None:
    """Single-letter / 2-char bullets are almost always noise (section
    labels, table-of-contents markers, etc.) — they must not pollute
    the recovered directive list."""
    raw = (
        "- A\n"  # 1 char — dropped
        "- ok\n"  # 2 chars — dropped
        "- yes\n"  # 3 chars (boundary) — kept
        "- use subregion not country\n"
    )
    bullets = cm._extract_bullets_from_text(raw)
    assert "A" not in bullets
    assert "ok" not in bullets
    assert "yes" in bullets
    assert "use subregion not country" in bullets


# ---------------------------------------------------------------------------
# _extract_bullets_from_text — section-aware recovery
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "heading",
    [
        "Summary:",
        "summary",
        "Rolling Summary:",
        "Rolling summary",
        "## Summary",
        "## Rolling Summary",
        "Topics discussed:",
        "topics discussed",
        "Decisions:",
        "Decisions made:",
        "Open questions:",
        "Open question",
        "Recap:",
        "Conversation summary:",
        "Conversation history",
        "Conversation recap",
    ],
)
def test_extract_bullets_stops_at_summary_heading(heading: str) -> None:
    """Bullets BELOW any summary-section heading must be excluded from
    the recovered directive list, regardless of the exact heading
    spelling / casing / Markdown decoration."""
    raw = (
        "- use subregion filter, not country\n"
        "- prefix currency with €\n"
        f"\n{heading}\n"
        "- discussed dashboards\n"
        "- agreed on YTD averages\n"
        "- decided to ship Friday\n"
    )
    bullets = cm._extract_bullets_from_text(raw)
    assert "use subregion filter, not country" in bullets
    assert "prefix currency with €" in bullets
    assert "discussed dashboards" not in bullets
    assert "agreed on YTD averages" not in bullets
    assert "decided to ship Friday" not in bullets


def test_extract_bullets_keeps_full_list_without_summary_heading() -> None:
    """When there is no summary heading at all, every directive-shaped
    bullet survives — the section gate must not trigger spuriously."""
    raw = (
        "- use subregion filter, not country\n"
        "- prefix currency with €\n"
        "- answer in markdown\n"
    )
    bullets = cm._extract_bullets_from_text(raw)
    assert len(bullets) == 3


def test_extract_bullets_summary_heading_in_middle_of_bullet_text_is_ignored() -> None:
    """A bullet whose text happens to contain the word ``summary`` is
    not a section heading and must not gate the rest of the recovery
    (the heading regex is anchored at start of line)."""
    raw = (
        "- use subregion, not country\n"
        "- the executive summary must include unit headcount\n"
        "- prefix currency with €\n"
    )
    bullets = cm._extract_bullets_from_text(raw)
    # All three bullets survive — the middle bullet is NOT a section
    # heading because the word "summary" isn't at line start.
    assert len(bullets) == 3


# ---------------------------------------------------------------------------
# _extract_bullets_from_text — bidirectional section-aware recovery
# ---------------------------------------------------------------------------


def test_extract_bullets_prefers_directives_section_when_summary_comes_first() -> None:
    """Regression for the bidirectional section-aware recovery.

    When a malformed free-text response puts ``Summary:`` BEFORE
    ``Directives:``, the bullets under the explicit ``Directives:``
    heading must still be recovered — the earlier ``cut at first
    summary heading`` rule was silently dropping every directive in
    this layout (the pre-summary slice was empty).
    """
    raw = (
        "Summary:\n"
        "- discussed dashboards\n"
        "- agreed on YTD averages\n"
        "\n"
        "Directives:\n"
        "- use subregion filter, not country\n"
        "- prefix currency with €\n"
        "- answer in markdown\n"
    )
    bullets = cm._extract_bullets_from_text(raw)
    # Every directive under the labelled Directives: section must be
    # recovered.
    assert "use subregion filter, not country" in bullets
    assert "prefix currency with €" in bullets
    assert "answer in markdown" in bullets
    # Summary-section bullets must still be excluded.
    assert "discussed dashboards" not in bullets
    assert "agreed on YTD averages" not in bullets


def test_extract_bullets_directives_section_followed_by_summary() -> None:
    """Directives section first, then Summary section — only bullets
    inside the labelled directives section are recovered, even though
    the bullets BELOW the summary heading have the same shape."""
    raw = (
        "Directives:\n"
        "- use subregion filter, not country\n"
        "- prefix currency with €\n"
        "\n"
        "Summary:\n"
        "- discussed dashboards\n"
        "- agreed on YTD averages\n"
    )
    bullets = cm._extract_bullets_from_text(raw)
    assert "use subregion filter, not country" in bullets
    assert "prefix currency with €" in bullets
    assert "discussed dashboards" not in bullets
    assert "agreed on YTD averages" not in bullets


def test_extract_bullets_directives_heading_only_no_summary() -> None:
    """When a ``Directives:`` heading is present with no ``Summary:``
    heading afterwards, every bullet from the heading through to
    end-of-text must be recovered."""
    raw = (
        "Here is the analysis you asked for.\n"
        "\n"
        "Directives:\n"
        "- use subregion filter, not country\n"
        "- prefix currency with €\n"
        "- answer in markdown\n"
    )
    bullets = cm._extract_bullets_from_text(raw)
    assert len(bullets) == 3
    assert "use subregion filter, not country" in bullets


def test_extract_bullets_directives_heading_takes_precedence_over_preamble_bullets() -> None:
    """If the LLM emits stray bullets ABOVE the ``Directives:`` heading
    (e.g. an introductory list), those must NOT be picked up — the
    explicit label is treated as authoritative."""
    raw = (
        "- this is a stray intro bullet\n"
        "- and another one outside any labelled section\n"
        "\n"
        "Directives:\n"
        "- use subregion filter, not country\n"
        "- prefix currency with €\n"
    )
    bullets = cm._extract_bullets_from_text(raw)
    # Only the two bullets inside the explicit Directives: section.
    assert len(bullets) == 2
    assert "use subregion filter, not country" in bullets
    assert "prefix currency with €" in bullets
    assert "this is a stray intro bullet" not in bullets
    assert "and another one outside any labelled section" not in bullets


@pytest.mark.parametrize(
    "dir_heading",
    [
        "Directives:",
        "directives",
        "Active directives:",
        "Active Directives",
        "Pinned Directives:",
        "Pinned directives",
        "Active Pinned Directives:",
        "## Directives",
        "## Active Directives",
        "### Pinned Directives",
        "Constraints:",
        "constraints",
        # ``Corrections`` and
        # ``Format requirements`` are explicitly named directive
        # categories in the compactor prompt but were missing from the
        # heading regex. A malformed response that put one of these
        # AFTER ``Summary:`` was silently dropping every directive.
        "Corrections:",
        "corrections",
        "Format requirements:",
        "Format Requirements:",
        "## Format requirements",
        "Rules:",
        "rules",
        "Preferences:",
        "Requirements:",
    ],
)
def test_extract_bullets_directives_heading_variants(dir_heading: str) -> None:
    """Every realistic directives-heading variant the LLM might emit
    must be recognised, regardless of casing, Markdown decoration, or
    the ``Active`` / ``Pinned`` qualifier."""
    raw = (
        "Summary:\n"
        "- recap line we must drop\n"
        "\n"
        f"{dir_heading}\n"
        "- use subregion, not country\n"
        "- prefix currency with €\n"
    )
    bullets = cm._extract_bullets_from_text(raw)
    assert "use subregion, not country" in bullets
    assert "prefix currency with €" in bullets
    assert "recap line we must drop" not in bullets


def test_extract_bullets_directives_heading_with_empty_section() -> None:
    """When an explicit ``Directives:`` heading is present but its
    section is empty, no bullets must be recovered — even if there
    are unlabelled bullets elsewhere. The explicit label is
    authoritative; we don't fall back to "scan everything"."""
    raw = (
        "- stray bullet above\n"
        "\n"
        "Directives:\n"
        "\n"
        "Summary:\n"
        "- recap\n"
    )
    bullets = cm._extract_bullets_from_text(raw)
    assert bullets == []


def test_extract_bullets_directives_heading_in_middle_of_text_is_ignored() -> None:
    """A bullet whose text contains the word ``directives`` is not a
    section heading (line-start anchor)."""
    raw = (
        "- use subregion, not country\n"
        "- the new directives override the previous ones\n"
        "- prefix currency with €\n"
    )
    bullets = cm._extract_bullets_from_text(raw)
    # All three bullets survive — middle bullet is NOT a section
    # heading because ``directives`` isn't at line start.
    assert len(bullets) == 3


# ---------------------------------------------------------------------------
# _redact_sensitive — credential-shaped tokens stripped before logging
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected_marker",
    [
        # Authorization headers
        ("Authorization: Bearer sk_abcdefghijklmnop", "<redacted-auth-header>"),
        ("Basic dXNlcjpwYXNzd29yZGFiYzEyMw==", "<redacted-auth-header>"),
        # Platform API keys
        ("fcp-1234567890abcdef1234567890abcdef", "<redacted-apikey>"),
        ("mcp-AABBCCDDEEFF0011223344556677889900", "<redacted-apikey>"),
        ("ghp_aaaabbbbccccddddeeeeffff00001111", "<redacted-apikey>"),
        ("xoxb-1234567890-1234567890123-abcdefghijklmnop", "<redacted-apikey>"),
        # JWT
        (
            "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NSJ9.SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c",
            "<redacted-jwt>",
        ),
        # Long base64-ish blob (catch-all)
        ("abcdefghijklmnopqrstuvwxyz0123456789ABCDEF1234", "<redacted-token>"),
    ],
)
def test_redact_sensitive_replaces_credential_shaped_tokens(
    raw: str, expected_marker: str
) -> None:
    redacted = cm._redact_sensitive(raw)
    assert expected_marker in redacted
    # The original credential body must NOT survive in the output.
    # We check by asserting the substring after the credential prefix
    # is gone — for the catch-all case we just check the full string
    # was replaced.
    if expected_marker == "<redacted-token>":
        assert raw not in redacted
    else:
        # First 8+ chars of the credential payload must be gone.
        cred_body = raw.split(maxsplit=1)[-1] if " " in raw else raw[-32:]
        # Allow short prefixes / dashes to remain — the redaction
        # marker just has to replace the long secret-shaped run.
        assert cred_body[-16:] not in redacted, (
            f"credential payload leaked through redaction: {redacted!r}"
        )


def test_redact_sensitive_keeps_directive_text_intact() -> None:
    """Realistic Pinned Directives must NOT be falsely redacted."""
    raw = (
        "- use field filter on subregion, not country\n"
        "- ADS metrics must be YTD average\n"
        "- currency prefix is €\n"
        "- always prefix dataset names with prod_\n"
    )
    # No credential-shaped tokens in this text — the redactor must be
    # a no-op so the warning log's debug preview stays informative.
    assert cm._redact_sensitive(raw) == raw


def test_preview_raw_output_redacts_credentials() -> None:
    """The debug-level preview helper must redact credential-shaped
    tokens *and* flatten control characters."""
    raw = (
        "User asked: please use the token\n"
        "Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.payload.signature\n"
        "and also the api key fcp-1234567890abcdef1234567890abcdef\n"
    )
    preview = cm._preview_raw_output(raw)
    # No raw newlines in the single-line preview.
    assert "\n" not in preview
    # Both credential shapes must be redacted away from the preview.
    assert "Bearer eyJ" not in preview
    assert "fcp-1234567890abcdef" not in preview
    assert "<redacted" in preview


# ---------------------------------------------------------------------------
# _parse_compaction_json — DEBUG-gated raw preview
# ---------------------------------------------------------------------------


def test_parse_compaction_json_emits_debug_preview_when_debug_enabled(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """When DEBUG logging is enabled, the redacted preview ships as a
    separate DEBUG record (operators can still diagnose); the WARNING
    remains metadata-only."""
    raw = (
        "Here are the active directives:\n"
        "- use subregion filter, not country\n"
        "- prefix currency with €\n"
    )
    with caplog.at_level(logging.DEBUG, logger=cm.logger.name):
        cm._parse_compaction_json(raw)

    debug_records = [
        record for record in caplog.records
        if record.levelno == logging.DEBUG and "raw_preview" in record.message
    ]
    assert debug_records, "DEBUG preview was not emitted when DEBUG was enabled"


def test_parse_compaction_json_omits_debug_preview_when_only_warning_enabled(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """At WARNING-only (default production) the raw response must NOT
    appear in any log record — no PII / credential exposure path."""
    raw = (
        "Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.payload.signature\n"
        "- use subregion filter, not country\n"
    )
    with caplog.at_level(logging.WARNING, logger=cm.logger.name):
        cm._parse_compaction_json(raw)

    # The raw text (including the JWT) must not survive in any record
    # the WARNING-level handler would emit.
    for record in caplog.records:
        assert "raw_preview" not in record.message, (
            "raw_preview must be DEBUG-only"
        )
        assert "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9" not in record.message, (
            "credential payload leaked into WARNING log"
        )


# ---------------------------------------------------------------------------
# _compact_messages — schema wiring
# ---------------------------------------------------------------------------


@dataclass
class _StubLLM:
    """Minimal LLM stub that records the kwargs it was called with."""

    provider_name: str = "openai"
    canned_response: str = (
        '{"directives": ["use subregion"], "summary": "- discussed dashboards"}'
    )
    last_kwargs: dict[str, Any] | None = None

    async def chat(self, **kwargs: Any) -> ChatResponse:
        self.last_kwargs = kwargs
        return ChatResponse(content=self.canned_response, model="stub")


def test_compact_messages_passes_native_structured_output() -> None:
    """OpenAI-style provider must receive a JSON-schema response_format."""
    llm = _StubLLM(provider_name="openai")
    messages = [
        ChatMessage(role="user", content="hi"),
        ChatMessage(role="assistant", content="hello"),
    ]
    directives, summary = asyncio.run(cm._compact_messages(llm, messages))  # type: ignore[arg-type]

    assert llm.last_kwargs is not None
    assert "structured_output" in llm.last_kwargs, (
        "compactor must pass structured_output when provider supports it"
    )
    so = llm.last_kwargs["structured_output"]
    assert so["type"] == "json_schema"
    # The schema must include both required keys so the model is forced
    # into the expected shape.
    schema_props = so["json_schema"]["schema"]["properties"]
    assert "directives" in schema_props
    assert "summary" in schema_props

    # max_tokens budget was raised from the cramped 700 default.
    assert llm.last_kwargs["max_tokens"] >= 1000

    # Sanity-check the parsed output round-tripped correctly.
    assert "use subregion" in directives
    assert "discussed dashboards" in summary


def test_compact_messages_handles_provider_without_native_support() -> None:
    """Unknown provider must still get the prompt + retry without the kwarg."""
    llm = _StubLLM(provider_name="some-exotic-provider")
    messages = [
        ChatMessage(role="user", content="hi"),
        ChatMessage(role="assistant", content="hello"),
    ]
    directives, _summary = asyncio.run(cm._compact_messages(llm, messages))  # type: ignore[arg-type]

    assert llm.last_kwargs is not None
    # No native structured output → kwarg omitted entirely.
    assert "structured_output" not in llm.last_kwargs
    assert "use subregion" in directives


# ---------------------------------------------------------------------------
# _compact_messages — narrow TypeError retry
# ---------------------------------------------------------------------------


class _RejectsStructuredOutputStub:
    """Provider stub whose signature does not accept ``structured_output``.

    Mimics the failure mode of a very old hand-rolled provider that
    forgot to declare ``**kwargs``: Python raises ``TypeError`` with
    the canonical ``...got an unexpected keyword argument 'structured_output'``
    message. The compactor must retry without the kwarg.
    """

    provider_name = "openai"

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def chat(
        self,
        *,
        messages: list[ChatMessage],
        max_tokens: int,
        temperature: float,
        model: str | None = None,
    ) -> ChatResponse:
        # Record what we were called with so the test can assert on the
        # exact retry shape. ``model`` is accepted (every real provider
        # takes it) so the stub rejects ONLY ``structured_output``.
        self.calls.append(
            {
                "messages": messages,
                "max_tokens": max_tokens,
                "temperature": temperature,
                "model": model,
            }
        )
        return ChatResponse(
            content='{"directives": ["use subregion"], "summary": "- ok"}',
            model="stub",
        )


def test_compact_messages_retries_without_structured_output_on_signature_mismatch() -> None:
    """When the provider stub specifically rejects ``structured_output``
    (Python's canonical unexpected-keyword-argument error), the compactor
    retries once without the kwarg and the call succeeds. Direct guard
    against a retry that is too broad (narrow the retry to the
    one case it was designed for)."""

    llm = _RejectsStructuredOutputStub()
    messages = [
        ChatMessage(role="user", content="hi"),
        ChatMessage(role="assistant", content="hello"),
    ]
    directives, _summary = asyncio.run(cm._compact_messages(llm, messages))  # type: ignore[arg-type]

    # First attempt raised TypeError (kwarg unsupported); second attempt
    # succeeded without the kwarg → 1 call recorded (the failed one
    # never made it past the signature check).
    assert len(llm.calls) == 1
    assert "use subregion" in directives


class _BuggyChatStub:
    """Provider stub that raises an UNRELATED ``TypeError`` mid-flight.

    Simulates an SDK-side bug (e.g. wrong type for an internal field
    after the request has been prepared). The compactor must NOT
    silently swallow this with a retry — that would double the latency
    and token cost for a call that's going to fail again, and mask the
    real bug from production logs.
    """

    provider_name = "openai"

    def __init__(self) -> None:
        self.call_count = 0

    async def chat(self, **kwargs: Any) -> ChatResponse:
        self.call_count += 1
        raise TypeError("internal SDK bug: 'NoneType' object is not subscriptable")


def test_compact_messages_does_not_retry_on_unrelated_type_error() -> None:
    """An unrelated ``TypeError`` (e.g. internal SDK bug, wrong argument
    type, NoneType subscript) must propagate so we don't double-charge
    LLM calls and don't mask real failures from production logs.
    Direct guard against an over-broad retry."""

    llm = _BuggyChatStub()
    messages = [
        ChatMessage(role="user", content="hi"),
        ChatMessage(role="assistant", content="hello"),
    ]

    with pytest.raises(TypeError, match="internal SDK bug"):
        asyncio.run(cm._compact_messages(llm, messages))  # type: ignore[arg-type]

    # Critically: the buggy chat method was called exactly ONCE — no
    # silent retry, no doubled cost.
    assert llm.call_count == 1


class _BuggyUnknownProviderStub:
    """Same as ``_BuggyChatStub`` but for a provider without native
    structured-output support.

    Here ``structured_output`` is NEVER added to ``chat_kwargs``, so even
    a TypeError mentioning structured_output should NOT trigger a retry
    (the kwarg wasn't passed in the first place — retrying would change
    nothing and just waste a second LLM call)."""

    provider_name = "some-exotic-provider"

    def __init__(self) -> None:
        self.call_count = 0

    async def chat(self, **kwargs: Any) -> ChatResponse:
        self.call_count += 1
        # Even though the message mentions structured_output, the
        # compactor never put that kwarg on the request for this
        # provider, so this is clearly a different bug we should
        # surface.
        raise TypeError(
            "chat() got an unexpected keyword argument 'structured_output' "
            "(misleading — kwarg was never passed)"
        )


def test_compact_messages_does_not_retry_when_kwarg_was_never_passed() -> None:
    """If the compactor never put ``structured_output`` on the call (e.g.
    unknown provider), even a TypeError whose message mentions the kwarg
    must NOT trigger a retry — the kwarg wasn't there, so retrying is
    pointless and just doubles the cost."""

    llm = _BuggyUnknownProviderStub()
    messages = [
        ChatMessage(role="user", content="hi"),
        ChatMessage(role="assistant", content="hello"),
    ]

    with pytest.raises(TypeError):
        asyncio.run(cm._compact_messages(llm, messages))  # type: ignore[arg-type]

    assert llm.call_count == 1


# ---------------------------------------------------------------------------
# _parse_compaction_response — clean vs fallback signalling
# ---------------------------------------------------------------------------


def test_parse_compaction_response_clean_on_valid_json() -> None:
    raw = '{"directives": ["use subregion not country"], "summary": "- recap"}'
    res = cm._parse_compaction_response(raw)
    assert res.clean is True
    assert res.directives == ["use subregion not country"]
    assert res.summary == "- recap"
    assert res.parse_error is None
    assert res.raw_len == len(raw)


def test_parse_compaction_response_not_clean_on_prose() -> None:
    """A free-text prose summary (no JSON, no bullets) is the exact shape
    that produced the ``Expecting value: line 1 column 1`` flood. It must
    be flagged ``clean=False`` and keep the prose as the summary."""
    raw = (
        "Here is a summary of what we covered. The user asked about quarterly "
        "revenue and we agreed to use the EMEA dashboard for the figures."
    )
    res = cm._parse_compaction_response(raw)
    assert res.clean is False
    assert res.directives == []  # no bullet markers in prose
    assert res.parse_error is not None
    assert res.raw_len == len(raw)
    assert res.summary.startswith("Here is a summary")


def test_parse_compaction_response_not_clean_but_recovers_bullets() -> None:
    raw = (
        "Directives:\n"
        "- use subregion filter, not country\n"
        "- prefix currency with €\n"
    )
    res = cm._parse_compaction_response(raw)
    assert res.clean is False
    assert any("subregion" in d for d in res.directives)
    # Bullet-recovery path leaves the summary blank by construction.
    assert res.summary == ""


def test_parse_compaction_response_empty_carries_no_warning_metadata() -> None:
    res = cm._parse_compaction_response("")
    assert res.clean is False
    assert res.raw_len == 0
    assert res.directives == []
    assert res.summary == ""


# ---------------------------------------------------------------------------
# _split_directive_lines
# ---------------------------------------------------------------------------


def test_split_directive_lines_strips_bullets_and_blanks() -> None:
    block = (
        "- use subregion not country\n"
        "* prefix currency with €\n"
        "\n"
        "• answer in markdown\n"
    )
    assert cm._split_directive_lines(block) == [
        "use subregion not country",
        "prefix currency with €",
        "answer in markdown",
    ]


def test_split_directive_lines_empty_block() -> None:
    assert cm._split_directive_lines("") == []


def test_split_directive_lines_preserves_leading_dash_or_star_in_text() -> None:
    """Regression: a directive whose text
    legitimately starts with a bullet glyph must NOT be corrupted. Only a
    single leading marker *followed by whitespace* is the list bullet; a
    glyph stuck to the content (``-O2``, ``*args``, ``*.csv``) is part of
    the directive and must survive round-tripping verbatim."""
    block = (
        "- compile with -O2 not -O0\n"
        "- expand *args before forwarding\n"
        "- only match *.csv files\n"
        "* -O2 is a leading-dash directive\n"
    )
    assert cm._split_directive_lines(block) == [
        "compile with -O2 not -O0",
        "expand *args before forwarding",
        "only match *.csv files",
        "-O2 is a leading-dash directive",
    ]


def test_strip_bullet_marker_only_strips_one_marker_with_trailing_space() -> None:
    # A real list marker (glyph + whitespace) is stripped exactly once.
    assert cm._strip_bullet_marker("-  use subregion") == "use subregion"
    assert cm._strip_bullet_marker("* prefix currency with €") == "prefix currency with €"
    assert cm._strip_bullet_marker("  • indented bullet") == "indented bullet"
    # A glyph fused to the content is NOT a marker — leave it intact.
    assert cm._strip_bullet_marker("-O2") == "-O2"
    assert cm._strip_bullet_marker("*.csv only") == "*.csv only"
    # Only the first marker goes; a second glyph belongs to the content.
    assert cm._strip_bullet_marker("- -O2") == "-O2"


# ---------------------------------------------------------------------------
# _compact_messages — preserve prior directives + JSON-only retry
# ---------------------------------------------------------------------------


_PROSE_NO_JSON = (
    "The user and assistant discussed quarterly revenue across regions and "
    "agreed to report figures from the EMEA dashboard going forward."
)


class _SequencedStub:
    """LLM stub that returns a scripted sequence of responses.

    The final entry is repeated for any further calls so a test can both
    control each response and assert how many times the compactor invoked
    ``chat`` (initial call + at most one JSON-only retry).
    """

    def __init__(self, responses: list[str], provider_name: str = "openai") -> None:
        self._responses = responses
        self.provider_name = provider_name
        self.calls = 0
        self.last_kwargs: dict[str, Any] | None = None

    async def chat(self, **kwargs: Any) -> ChatResponse:
        self.last_kwargs = kwargs
        idx = min(self.calls, len(self._responses) - 1)
        self.calls += 1
        return ChatResponse(content=self._responses[idx], model="stub-model")


def test_compact_messages_preserves_prior_directives_on_non_json() -> None:
    """Core regression: a prose (non-JSON) compactor response must NOT
    wipe the carried-forward Pinned Directives — the bug behind the
    ``recovered 0 directive(s)`` warning flood."""
    llm = _SequencedStub([_PROSE_NO_JSON])  # repeated for the retry too
    messages = [
        ChatMessage(role="user", content="hi"),
        ChatMessage(role="assistant", content="hello"),
    ]
    directives_block, summary_block = asyncio.run(
        cm._compact_messages(
            llm,  # type: ignore[arg-type]
            messages,
            prior_directives=(
                "- use subregion not country\n- prefix currency with €"
            ),
            prior_summary="- earlier we configured the dashboards",
        )
    )
    # Both prior directives survive despite the non-JSON response.
    assert "use subregion not country" in directives_block
    assert "prefix currency with €" in directives_block
    # Exactly one strict JSON-only retry was attempted (2 calls total).
    assert llm.calls == 2
    # The prose is preserved as the summary, so nothing is lost.
    assert summary_block


def test_compact_messages_json_only_retry_recovers() -> None:
    """When the first response is prose but the retry returns valid JSON,
    the retry result is used and the stale prior directive is NOT
    force-merged (the model's roll-forward is trusted on a clean parse)."""
    good = '{"directives": ["use region X only"], "summary": "- recap"}'
    llm = _SequencedStub([_PROSE_NO_JSON, good])
    messages = [
        ChatMessage(role="user", content="hi"),
        ChatMessage(role="assistant", content="hello"),
    ]
    directives_block, summary_block = asyncio.run(
        cm._compact_messages(
            llm,  # type: ignore[arg-type]
            messages,
            prior_directives="- stale prior directive",
        )
    )
    assert llm.calls == 2
    assert "use region X only" in directives_block
    assert "stale prior directive" not in directives_block
    assert "recap" in summary_block


def test_compact_messages_no_retry_when_first_response_is_clean() -> None:
    """A clean first response must NOT trigger the JSON-only retry."""
    good = '{"directives": ["a"], "summary": "- s"}'
    llm = _SequencedStub([good])
    messages = [ChatMessage(role="user", content="hi")]
    asyncio.run(cm._compact_messages(llm, messages))  # type: ignore[arg-type]
    assert llm.calls == 1


def test_compact_messages_logs_info_when_prior_directives_preserved(
    caplog: pytest.LogCaptureFixture,
) -> None:
    llm = _SequencedStub([_PROSE_NO_JSON])
    messages = [ChatMessage(role="user", content="hi")]
    with caplog.at_level(logging.INFO, logger=cm.logger.name):
        asyncio.run(
            cm._compact_messages(
                llm,  # type: ignore[arg-type]
                messages,
                prior_directives="- use subregion not country",
            )
        )
    info_records = [r for r in caplog.records if r.levelno == logging.INFO]
    assert any("preserved" in r.getMessage() for r in info_records), (
        "preservation of prior directives must log at INFO, not WARNING"
    )
    # No WARNING-level "non-JSON output" noise when nothing was lost.
    assert not any(
        r.levelno == logging.WARNING and "non-JSON output" in r.getMessage()
        for r in caplog.records
    )
    # Provider/model surfaced for diagnosis.
    assert any("provider=" in r.getMessage() for r in info_records)


def test_compact_messages_warns_when_no_prior_directives_to_preserve(
    caplog: pytest.LogCaptureFixture,
) -> None:
    llm = _SequencedStub([_PROSE_NO_JSON])
    messages = [ChatMessage(role="user", content="hi")]
    with caplog.at_level(logging.WARNING, logger=cm.logger.name):
        directives_block, _summary = asyncio.run(
            cm._compact_messages(llm, messages)  # type: ignore[arg-type]
        )
    assert directives_block == ""
    warnings = [
        r
        for r in caplog.records
        if r.levelno == logging.WARNING and "non-JSON output" in r.getMessage()
    ]
    assert warnings, "expected a WARNING when there are no prior directives to save"
    assert "provider=" in warnings[0].getMessage()


# ---------------------------------------------------------------------------
# _compact_messages — an answer cut at max_tokens is never used
# ---------------------------------------------------------------------------


_FULL_COMPACTION = (
    '{"directives": ["use the subregion field filter, not country", '
    '"currency prefix is EUR"], "summary": "- reviewed the Q3 dashboard"}'
)
# Cut inside the directives array: json-repair closes it into a CLEAN parse
# holding the fragment "currency" and no summary.
_CUT_COMPACTION = _FULL_COMPACTION[: _FULL_COMPACTION.index("currency") + len("currency")]


class _StopReasonStub:
    """Scripted ``(content, stop_reason)`` answers, the last one repeated."""

    provider_name = "openai"

    def __init__(self, *answers: tuple[str, str]) -> None:
        self._answers = answers
        self.calls: list[dict[str, Any]] = []

    async def chat(self, **kwargs: Any) -> ChatResponse:
        self.calls.append(kwargs)
        content, stop_reason = self._answers[min(len(self.calls), len(self._answers)) - 1]
        return ChatResponse(content=content, model="stub-model", stop_reason=stop_reason)


def test_compact_messages_retries_a_cut_answer_with_a_doubled_budget() -> None:
    """A cut answer is retried once, as the same request with twice the
    budget, and the complete retry is the compaction."""
    llm = _StopReasonStub((_CUT_COMPACTION, "max_tokens"), (_FULL_COMPACTION, "end_turn"))
    messages = [ChatMessage(role="user", content="hi")]

    directives, summary = asyncio.run(cm._compact_messages(llm, messages))  # type: ignore[arg-type]

    assert len(llm.calls) == 2
    first, retry = llm.calls
    assert first["max_tokens"] == cm._COMPACTOR_MAX_TOKENS
    assert retry["max_tokens"] == 2 * cm._COMPACTOR_MAX_TOKENS
    assert retry["messages"] == first["messages"]
    assert directives.splitlines() == [
        "- use the subregion field filter, not country",
        "- currency prefix is EUR",
    ]
    assert summary == "- reviewed the Q3 dashboard"


def test_compact_messages_caps_the_retry_budget_at_the_output_ceiling() -> None:
    """Twice the budget stops at the model's output ceiling: above it the
    retry itself would be an HTTP 400."""
    llm = _StopReasonStub((_CUT_COMPACTION, "max_tokens"), (_FULL_COMPACTION, "end_turn"))
    llm.max_output_tokens_overrides = {"small-model": 3000}  # type: ignore[attr-defined]
    messages = [ChatMessage(role="user", content="hi")]

    asyncio.run(cm._compact_messages(llm, messages, model="small-model"))  # type: ignore[arg-type]

    assert [call["max_tokens"] for call in llm.calls] == [cm._COMPACTOR_MAX_TOKENS, 3000]


def test_compact_messages_raises_when_the_retry_is_cut_too() -> None:
    """Still cut after the retry: no compaction is returned at all, so the
    caller keeps the previous one instead of a fragment."""
    llm = _StopReasonStub((_CUT_COMPACTION, "max_tokens"))
    messages = [ChatMessage(role="user", content="hi")]

    with pytest.raises(cm.CompactionTruncatedError):
        asyncio.run(
            cm._compact_messages(
                llm,  # type: ignore[arg-type]
                messages,
                prior_directives="- prefix currency with EUR",
            )
        )
    assert len(llm.calls) == 2


def test_compact_messages_falls_back_on_a_prose_retry_after_a_cut() -> None:
    """A cut first answer is dropped even when its retry is prose: the prose
    fallback runs on the retry, so the prior directives survive and the
    fragment never becomes a directive."""
    llm = _StopReasonStub((_CUT_COMPACTION, "max_tokens"), (_PROSE_NO_JSON, "end_turn"))
    messages = [ChatMessage(role="user", content="hi")]

    directives, summary = asyncio.run(
        cm._compact_messages(
            llm,  # type: ignore[arg-type]
            messages,
            prior_directives="- prefix currency with EUR",
        )
    )

    assert len(llm.calls) == 2
    assert directives.splitlines() == ["- prefix currency with EUR"]
    assert "EMEA dashboard" in summary


def test_compact_messages_never_uses_a_cut_json_only_retry() -> None:
    """The JSON-only retry of a prose answer keeps its budget and is held to
    the same rule: cut, it is ignored and the prose fallback runs on the
    first answer."""
    llm = _StopReasonStub((_PROSE_NO_JSON, "end_turn"), (_CUT_COMPACTION, "max_tokens"))
    messages = [ChatMessage(role="user", content="hi")]

    directives, summary = asyncio.run(
        cm._compact_messages(
            llm,  # type: ignore[arg-type]
            messages,
            prior_directives="- prefix currency with EUR",
        )
    )

    assert len(llm.calls) == 2
    assert llm.calls[1]["max_tokens"] == cm._COMPACTOR_MAX_TOKENS
    assert directives.splitlines() == ["- prefix currency with EUR"]
    assert "EMEA dashboard" in summary
