"""A job log with a line of tens of megabytes is summarised in bounded time (#9074).

A pytest id carrying a 5 MB parametrised bytes value printed two ~20 MB lines
in a backend-unit job log. Scanning them with the cue patterns took seconds
on the worker's event loop, and every resume of the turn read the log again
and was cut again. Each raw line is now bounded before it is scanned, its
start and end kept, and the cut named.
"""

from __future__ import annotations

import time

from app.integrations import ci_logs

_HUGE = 6_000_000


def _log(huge_line: str) -> str:
    return (
        "##[group]Run pytest\n##[endgroup]\n"
        + "2026-11-05T23:55:39.0091950Z "
        + huge_line
        + "\n"
        + "=================================== FAILURES ===================================\n"
        + "_______________________ test_x _______________________\n"
        + "E   AssertionError: boom\n"
        + "================== 1 failed, 10 passed in 3.00s ==================\n"
        + "##[error]Process completed with exit code 1.\n"
    )


def test_a_multi_megabyte_line_is_bounded_and_the_failure_still_found():
    line = "tests/test_files.py::test_media[" + "\\x00" * (_HUGE // 4) + "-image/png] PASSED"
    log = _log(line)
    started = time.perf_counter()
    summary = ci_logs.summarize(log)
    elapsed = time.perf_counter() - started
    out = ci_logs.format_summary(summary, title="t", log_chars=len(log))
    assert "AssertionError: boom" in out
    assert "test_x" in out
    # Unbounded, the cue scan alone of this line took over a second.
    assert elapsed < 1.0
    assert len(out) <= ci_logs.MAX_SUMMARY_CHARS + 300


def test_clean_line_keeps_start_and_end_and_names_the_cut():
    raw = "2026-11-05T23:55:39.0091950Z " + "a" * 10 + "b" * 50_000 + "z" * 10
    cleaned = ci_logs.clean_line(raw)
    assert cleaned.startswith("a" * 10)
    assert cleaned.endswith("z" * 10)
    assert "characters of this line not scanned" in cleaned
    assert len(cleaned) < ci_logs.MAX_SCAN_LINE_CHARS + 120


def test_a_short_line_and_a_bom_prefixed_timestamp_are_cleaned_as_before():
    assert ci_logs.clean_line("2026-11-05T23:55:39.0091950Z hello\r") == "hello"
    assert ci_logs.clean_line("\ufeff2026-11-05T23:56:07.8319835Z [gw1] ok") == "[gw1] ok"


def test_job_log_answer_returns_none_without_a_cue_unless_failures_only():
    quiet = "##[group]Run echo\n##[endgroup]\nall good\n"
    assert ci_logs.job_log_answer(quiet, title="t", failures_only=False) is None
    assert "no failure cue found" in ci_logs.job_log_answer(quiet, title="t", failures_only=True)
    loud = _log("short line")
    out = ci_logs.job_log_answer(loud, title="t", failures_only=False)
    assert out is not None and "AssertionError: boom" in out and "--- the log's last" in out
