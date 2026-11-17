"""The activation-gate skip summary must be a clean, single-line string.

Regression test for the bug where a *gated* assignment's skipped
``result_summary`` rendered on two lines with truncated junk text, e.g.::

    Skipped: activation check returned 'NO'
    THIS IS AN ISSUE CLOSURE EVENT (ACTION

The gate is instructed to reply with exactly ``YES`` / ``NO`` / ``SKIP``, but
models frequently append a free-text explanation that the tiny gate token
budget truncates mid-sentence (with embedded newlines). The old code embedded
that raw multi-line answer verbatim into ``result_summary``.
``_format_skip_summary`` collapses whitespace and keeps only the decision
token (plus an explicit ``SKIP`` reason).
"""

from __future__ import annotations

from app.services.assignment_engine import _format_skip_summary


def test_multiline_no_answer_collapses_to_single_clean_line():
    answer = "NO\nTHIS IS AN ISSUE CLOSURE EVENT (ACTION"
    summary = _format_skip_summary(answer)
    assert summary == "Skipped: activation check returned 'NO'"
    assert "\n" not in summary


def test_plain_no_answer():
    assert (
        _format_skip_summary("NO")
        == "Skipped: activation check returned 'NO'"
    )


def test_no_with_trailing_explanation_drops_junk():
    # The model isn't supposed to justify a NO; any trailing text is dropped.
    answer = "NO - the issue was already labelled correctly so nothing to do"
    assert (
        _format_skip_summary(answer)
        == "Skipped: activation check returned 'NO'"
    )


def test_skip_without_reason():
    assert (
        _format_skip_summary("SKIP")
        == "Skipped: activation check returned 'SKIP'"
    )


def test_skip_with_reason_is_preserved_and_single_line():
    answer = "SKIP: NO ISSUE CONTENT\nPROVIDED"
    summary = _format_skip_summary(answer)
    assert summary == "Skipped: activation check returned 'SKIP' (NO ISSUE CONTENT PROVIDED)"
    assert "\n" not in summary


def test_skip_reason_strips_leading_separators():
    # "SKIP - reason" and "SKIP : reason" both yield the bare reason.
    assert (
        _format_skip_summary("SKIP - not applicable")
        == "Skipped: activation check returned 'SKIP' (not applicable)"
    )


def test_summary_is_always_single_line():
    for answer in [
        "NO\n\n\nblah",
        "SKIP:\n\nfoo\nbar",
        "  NO  ",
        "SKIP   :   spaced reason   here  ",
    ]:
        assert "\n" not in _format_skip_summary(answer)
