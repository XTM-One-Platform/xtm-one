"""A failure block larger than the summary's remaining budget is clipped, never dropped whole.

A pytest FAILURES section of a few hundred long traceback lines can exceed the
whole 12,000-character cap. It used to be skipped with only "N more failure
block(s) not shown", which hid the failing test and sent the caller to the
whole multi-megabyte log.
"""

from __future__ import annotations

from app.integrations import ci_logs


def _summary(log: str) -> str:
    return ci_logs.format_summary(ci_logs.summarize(log), title="t")


def test_a_failures_section_larger_than_the_cap_is_clipped_to_its_first_lines():
    traceback = "".join(f"    frame {i}: " + "y" * 180 + "\n" for i in range(240))
    log = (
        "##[group]Run pytest\n##[endgroup]\n"
        "=================================== FAILURES ===================================\n"
        "_______________________ test_big _______________________\n"
        + traceback
        + "================== 1 failed, 10 passed in 3.00s ==================\n"
    )
    out = _summary(log)
    assert "_______________________ test_big _______________________" in out
    assert "frame 0:" in out
    assert "[block clipped:" in out and "line(s) shown" in out
    assert "more failure block(s) not shown" not in out
    assert len(out) <= ci_logs.MAX_SUMMARY_CHARS + 300


def test_the_short_summary_still_comes_first_when_the_failures_section_is_clipped():
    traceback = "".join(f"    frame {i}: " + "z" * 200 + "\n" for i in range(240))
    log = (
        "##[group]Run pytest\n##[endgroup]\n"
        "=================================== FAILURES ===================================\n"
        + traceback
        + "=========================== short test summary info ============================\n"
        "FAILED tests/test_s.py::test_shard - AssertionError\n"
        "================== 1 failed, 900 passed in 60.00s ==================\n"
    )
    out = _summary(log)
    assert "FAILED tests/test_s.py::test_shard - AssertionError" in out
    assert out.index("short test summary info") < out.index("FAILURES")
    assert "[block clipped:" in out


def test_an_over_long_line_keeps_its_start_and_end():
    line = "FAILED tests/test_l.py::test_long - " + "q" * 5000 + " END-OF-LINE"
    out = _summary("##[group]Run pytest\n##[endgroup]\n" + line + "\n")
    assert "FAILED tests/test_l.py::test_long" in out and "END-OF-LINE" in out
    assert " ... " in out
    assert len(out) < 2000


def test_blank_lines_inside_a_block_are_not_shown():
    log = "##[group]Run ruff\n##[endgroup]\napp/x.py:1:1: I001 [*] Import block is un-sorted\n\n\n\nFound 1 error.\n"
    out = _summary(log)
    assert "I001" in out and "Found 1 error." in out
    assert "\n\n\n" not in out


def test_a_summary_that_fits_is_unchanged_by_clipping():
    log = "##[group]Run ruff\nruff check .\n##[endgroup]\napp/x.py:120:1: I001 [*] Import block is un-sorted\nFound 1 error.\n##[error]Process completed with exit code 1.\n"
    out = _summary(log)
    assert "[block clipped:" not in out and "not shown" not in out
