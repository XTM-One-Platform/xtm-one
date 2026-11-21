"""A failure block the log repeats (a retried setup error, a crash printed by every worker) is shown once.

A job log held seven copies of the same setup error, each a C stack of about
a hundred lines differing only by addresses and pids: the failures_only answer
spent 12,000 characters on one cause and squeezed every other block out.
"""

from __future__ import annotations

from app.integrations import ci_logs


def _crash(pid: int, base: int) -> str:
    frames = "".join(f"  #{i} 0x{base + i * 16:x} in frame_{i} (lib.so+0x{i * 8:x})\n" for i in range(30))
    return f"Fatal Python error: Segmentation fault (pid {pid})\n" + frames


def _log(blocks: list[str], tail: str = "") -> str:
    return "##[group]Run pytest\n##[endgroup]\n" + "".join(blocks) + tail


def _summary(log: str) -> str:
    return ci_logs.format_summary(ci_logs.summarize(log), title="t")


def test_seven_copies_of_one_crash_are_shown_once_with_their_count():
    log = _log([_crash(1000 + k, 0x7F0000 + k * 0x1000) for k in range(7)])
    summary = ci_logs.summarize(log)
    assert len(summary.blocks) == 1
    assert summary.repeated_blocks == 6
    out = ci_logs.format_summary(summary, title="t")
    assert out.count("Fatal Python error") == 1
    assert "appears 6 more time(s)" in out
    assert "(6 repeat(s) of them folded)" in out


def test_a_different_failure_after_the_repeats_is_no_longer_squeezed_out():
    tail = "FAILED tests/test_other.py::test_distinct - AssertionError: 1 != 2\n"
    out = _summary(_log([_crash(1000 + k, 0x7F0000 + k * 0x1000) for k in range(7)], tail))
    assert "test_distinct" in out
    assert "not shown" not in out


def test_blocks_that_differ_by_a_name_are_both_kept():
    def err(name: str) -> str:
        return f"error: cannot open {name}\n  while loading module\n  at loader.c:42\n"

    log = "".join(f"##[group]Run load {n}\n##[endgroup]\n{err(n)}" for n in ("plugin_a2", "plugin_b3"))
    summary = ci_logs.summarize(log)
    assert summary.repeated_blocks == 0
    assert len(summary.blocks) == 2


def test_a_short_cue_repeated_by_two_steps_stays_under_each_step():
    log = (
        "##[group]Run a\n##[endgroup]\n##[error]Process completed with exit code 1.\n"
        "##[group]Run b\n##[endgroup]\n##[error]Process completed with exit code 1.\n"
    )
    summary = ci_logs.summarize(log)
    assert summary.repeated_blocks == 0
    assert len(summary.blocks) == 2


def test_the_pytest_report_is_never_folded():
    section = (
        "=================================== FAILURES ===================================\n"
        "_______________________ test_x _______________________\n"
        "    assert 1 == 2\nE   assert 1 == 2\n"
        "================== 1 failed in 0.10s ==================\n"
    )
    summary = ci_logs.summarize(_log([section, section]))
    assert summary.repeated_blocks == 0
