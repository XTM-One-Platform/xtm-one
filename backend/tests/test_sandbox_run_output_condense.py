"""``sandbox_run_command`` output: what buries a failure is left out of the bounded result.

Pure tests of :func:`checks.condense`, :func:`checks.format_result` and the JUnit
failures file, one fixture per kind of noise met in real runs: a line repeated
back to back, a repeated stack block, a line repeated across the output, the
pytest warnings summary, a coverage table, and a long failure message.
"""

from __future__ import annotations

from app.services.sandbox_workspace import checks

ECONNREFUSED = "Error: connect ECONNREFUSED 127.0.0.1:3100"
STACK = [ECONNREFUSED, "    at TCPConnectWrap.afterConnect [as oncomplete] (node:net:1607:16)", "  errno: -111,"]


def test_back_to_back_repeated_lines_are_kept_once_and_counted():
    text = "\n".join(["start", *["same line"] * 6, "end"])
    assert checks.condense(text).splitlines() == [
        "start",
        "same line",
        "[... previous line repeated 5 more time(s) ...]",
        "end",
    ]


def test_a_repeated_stack_block_is_kept_once_and_counted():
    text = "\n".join(["vitest", *STACK * 4, "FAIL src/a.test.ts"])
    lines = checks.condense(text).splitlines()
    assert lines == ["vitest", *STACK, "[... previous 3 lines repeated 3 more time(s) ...]", "FAIL src/a.test.ts"]


def test_a_line_repeated_across_the_output_is_thinned():
    text = "\n".join(line for index in range(30) for line in (ECONNREFUSED, f"test {index} ok"))
    lines = checks.condense(text).splitlines()
    assert lines.count(ECONNREFUSED) == checks.FREQUENT_KEEP
    assert "test 29 ok" in lines
    assert lines[-1] == f"[... 27 more copies of 1 line(s) seen {checks.FREQUENT_LINE}+ times left out ...]"


def test_the_pytest_warnings_summary_is_left_out():
    text = "\n".join(
        [
            "tests/test_a.py F.",
            "=============================== warnings summary ===============================",
            "tests/test_a.py::test_x",
            "  DeprecationWarning: datetime.utcnow() is deprecated",
            "-- Docs: https://docs.pytest.org/en/stable/how-to/capture-warnings.html",
            "=========================== short test summary info ============================",
            "FAILED tests/test_a.py::test_bad - assert 1 == 2",
        ]
    )
    lines = checks.condense(text).splitlines()
    assert not any("DeprecationWarning" in line for line in lines)
    assert "[... pytest warnings summary, 4 line(s), left out: see the run directory ...]" in lines
    assert lines[-1] == "FAILED tests/test_a.py::test_bad - assert 1 == 2"


def test_a_pytest_cov_table_is_left_out():
    text = "\n".join(
        [
            "---------- coverage: platform linux, python 3.14.0 -----------",
            "Name                 Stmts   Miss  Cover",
            "----------------------------------------",
            "app/a.py                10      2    80%",
            "app/b.py                20      0   100%",
            "----------------------------------------",
            "TOTAL                   30      2    93%",
            "",
            "1 failed, 3 passed in 0.12s",
        ]
    )
    lines = checks.condense(text).splitlines()
    assert lines[0].startswith("[... coverage table, ") and "app/a.py" not in "\n".join(lines)
    assert lines[-1] == "1 failed, 3 passed in 0.12s"


def test_an_istanbul_table_is_left_out():
    text = "\n".join(
        [
            " % Coverage report from v8",
            "----------|---------|----------|---------|---------|",
            "File      | % Stmts | % Branch | % Funcs | % Lines |",
            "----------|---------|----------|---------|---------|",
            "All files |   81.2  |    70.1  |   90.0  |   81.2  |",
            " a.ts     |   81.2  |    70.1  |   90.0  |   81.2  |",
            "----------|---------|----------|---------|---------|",
            "Test Files  1 failed (1)",
        ]
    )
    lines = checks.condense(text).splitlines()
    assert lines[0].startswith("[... coverage table, ") and lines[-1] == "Test Files  1 failed (1)"


def test_plain_output_is_unchanged():
    text = "line 1\nline 2\n\nline 3"
    assert checks.condense(text) == text
    assert checks.condense("") == ""


def test_the_failure_survives_a_flood_in_the_result():
    flood = "\n".join([*STACK * 500, "FAIL src/a.test.ts > renders the card", "AssertionError: expected 1 to be 2"])
    text = checks.format_result(
        check="frontend-unit", argv=["yarn", "vitest"], cwd="frontend", exit_code=1, seconds=3.0, timed_out=False,
        stdout="", stderr=flood, run_dir="workspace/.xtm/runs/a", report=None,
    )
    assert "AssertionError: expected 1 to be 2" in text and "characters elided" not in text
    assert text.count(ECONNREFUSED) == 1


JUNIT = """<testsuites><testsuite name="pytest" tests="2">
<testcase classname="tests.test_a" name="test_ok" file="tests/test_a.py" line="3"/>
<testcase classname="tests.test_a" name="test_bad" file="tests/test_a.py" line="7"><failure message="assert {long} == 2">Traceback (most recent call last)
  File "tests/test_a.py", line 8
E   assert {long} == 2</failure></testcase>
</testsuite></testsuites>""".replace("{long}", "x" * 600)


def test_the_whole_failure_message_goes_to_the_failures_file_and_is_named():
    summary = checks.parse_junit(JUNIT)
    assert len(summary.findings[0].message) == 400
    (name, body), = summary.failures
    assert name == "tests/test_a.py:8 tests.test_a::test_bad"
    assert body.startswith("assert " + "x" * 600) and body.endswith("E   assert " + "x" * 600 + " == 2")
    assert summary.failures_text().startswith("=== tests/test_a.py:8 tests.test_a::test_bad\n")
    text = checks.format_result(
        check="unit", argv=["pytest"], cwd="backend", exit_code=1, seconds=1.0, timed_out=False,
        stdout="", stderr="", run_dir="workspace/.xtm/runs/a", report=summary, failures_saved=True,
    )
    assert f"Whole message of each failing test: workspace/.xtm/runs/a/{checks.FAILURES_FILE}." in text
    unsaved = checks.format_result(
        check="unit", argv=["pytest"], cwd="backend", exit_code=1, seconds=1.0, timed_out=False,
        stdout="", stderr="", run_dir="workspace/.xtm/runs/a", report=summary,
    )
    assert checks.FAILURES_FILE not in unsaved
