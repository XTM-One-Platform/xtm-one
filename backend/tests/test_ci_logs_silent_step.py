"""failures_only on a step that fails without an error line, and the runner's echo of a step.

The runner writes each step's command, ``with:`` and ``env:`` between
``##[group]Run ...`` and ``##[endgroup]``: that echo is no output and is never
scanned for a cue (``expect-error: false`` matched ``error:``). A step that dies
without any ``##[error]`` line (an upload cut) leaves no cue at all: the
summary then shows the last lines of the step the job stopped in.
"""

from __future__ import annotations

from app.integrations import ci_logs

_WORKFLOW_B64 = "bmFtZTogQ29kZVFMCm9uOgogIHB1c2g6CiAgICBicmFuY2hlczogW21haW5dCg" * 8

SILENT_LOG = (
    "2026-11-05T16:00:00.1Z ##[group]Run actions/checkout@v4\n"
    "2026-11-05T16:00:00.1Z with:\n"
    "2026-11-05T16:00:00.1Z   fetch-depth: 1\n"
    "2026-11-05T16:00:00.1Z ##[endgroup]\n"
    "2026-11-05T16:00:01.1Z Syncing repository: acme/repo\n"
    "2026-11-05T16:00:02.1Z ##[group]Run github/codeql-action/analyze@v3\n"
    "2026-11-05T16:00:02.1Z with:\n"
    "2026-11-05T16:00:02.1Z   category: /language:java\n"
    "2026-11-05T16:00:02.1Z   expect-error: false\n"
    "2026-11-05T16:00:02.1Z env:\n"
    "2026-11-05T16:00:02.1Z   CODE_SCANNING_REF: refs/heads/main\n"
    f"2026-11-05T16:00:02.1Z   CODE_SCANNING_WORKFLOW_FILE: {_WORKFLOW_B64}\n"
    "2026-11-05T16:00:02.1Z ##[endgroup]\n"
    + "".join(f"2026-11-05T16:00:03.1Z Finalizing database step {i}\n" for i in range(80))
    + "2026-11-05T16:00:04.1Z ##[group]Interpreting results\n"
    "2026-11-05T16:00:04.1Z java/sql-injection: 0 results\n"
    "2026-11-05T16:00:04.1Z ##[endgroup]\n"
    "2026-11-05T16:00:05.1Z ##[group]Uploading results\n"
    "2026-11-05T16:00:05.1Z Uploading code scanning results\n"
    f"2026-11-05T16:00:05.1Z Payload: {_WORKFLOW_B64}\n"
    "2026-11-05T16:00:06.1Z Waiting for processing to finish\n"
    "2026-11-05T16:00:06.1Z ##[endgroup]\n"
    "2026-11-05T16:00:07.1Z Post job cleanup.\n"
    "2026-11-05T16:00:07.1Z Cleaning up orphan processes\n"
)


def _summary(log: str) -> str:
    return ci_logs.format_summary(ci_logs.summarize(log), title="t", log_chars=len(log))


def test_a_step_echo_is_never_a_failure_cue():
    summary = ci_logs.summarize(SILENT_LOG)
    assert summary.blocks == []
    out = _summary(SILENT_LOG)
    assert "expect-error" not in out and "CODE_SCANNING_REF" not in out


def test_a_silent_failed_step_shows_where_the_job_stopped():
    out = _summary(SILENT_LOG)
    assert out.startswith("t: no failure cue found")
    assert "stopped in step 'Run github/codeql-action/analyze@v3'" in out
    assert "> Uploading results" in out and "Waiting for processing to finish" in out
    assert "Post job cleanup." not in out and "Cleaning up orphan processes" not in out
    assert "Syncing repository" not in out and "Finalizing database step 0\n" not in out
    assert f"{len(SILENT_LOG):,} characters" in out


def test_encoded_dumps_are_omitted_and_the_lines_are_bounded():
    out = _summary(SILENT_LOG)
    assert _WORKFLOW_B64[:200] not in out and "encoded value omitted" in out
    shown = out.split("\n\n", 1)[1].splitlines()
    assert len(shown) <= ci_logs.STOPPED_LINES
    assert sum(len(ln) + 1 for ln in shown) <= ci_logs.MAX_STOPPED_CHARS


def test_an_error_line_in_a_step_is_still_its_block():
    log = (
        "##[group]Run ./gradlew build\n"
        "env:\n"
        "  expect-error: false\n"
        "##[endgroup]\n"
        "Compiling\n"
        "##[error]Upload failed: 504 Gateway Timeout\n"
        "##[error]Process completed with exit code 1.\n"
        "Post job cleanup.\n"
    )
    out = _summary(log)
    assert "failure block(s) in step(s) 'Run ./gradlew build'" in out
    assert "Upload failed: 504 Gateway Timeout" in out
    assert "expect-error" not in out and "stopped in step" not in out


def test_a_run_group_never_closed_keeps_its_lines_as_output():
    steps = ci_logs.split_steps("##[group]Run ruff\nerror: app/x.py is broken\n")
    assert steps[-1].name == "Run ruff" and steps[-1].lines == ["error: app/x.py is broken"]
    assert ci_logs.summarize("##[group]Run ruff\nerror: app/x.py is broken\n").blocks


def test_the_step_header_is_kept_apart():
    steps = ci_logs.split_steps(SILENT_LOG)
    analyze = next(s for s in steps if s.name.startswith("Run github/codeql-action/analyze"))
    assert "  expect-error: false" in analyze.header
    assert all("expect-error" not in ln for ln in analyze.lines)
