"""failures_only keeps what a failed step printed before the runner's exit-code line.

The runner ends a failed step with ``##[error]Process completed with exit code N``;
the tool's own explanation (a docker pull error, a vitest or Playwright report)
sits just above it and often matches no failure cue. The summary must carry it.
"""

from __future__ import annotations

from app.integrations import ci_logs


def _summary(log: str) -> str:
    return ci_logs.format_summary(ci_logs.summarize(log), title="t")


def test_a_docker_pull_error_above_the_exit_code_is_kept():
    log = (
        "##[group]Run docker run python:3.12 pytest\n##[endgroup]\n"
        "Unable to find image 'python:3.12' locally\n"
        "docker: Error response from daemon: Get \"https://registry-1.docker.io/v2/library/python/blobs/sha256:7fac\": "
        "502 Bad Gateway.\n"
        "See 'docker run --help'.\n"
        "##[error]Process completed with exit code 125.\n"
    )
    out = _summary(log)
    assert "##[error]Process completed with exit code 125." in out
    assert "502 Bad Gateway" in out
    assert "Unable to find image 'python:3.12' locally" in out
    assert out.index("Process completed") < out.index("502 Bad Gateway")


def test_a_vitest_failure_without_a_known_cue_is_kept():
    log = (
        "##[group]Run yarn test\n##[endgroup]\n"
        " ✓ src/a.test.tsx (3 tests) 120ms\n"
        " ❯ src/ConfigPresetImportWizard.test.tsx > preview > sends one import when Apply is clicked twice\n"
        "TestingLibraryElementError: Unable to find an accessible element with the role \"button\" and name \"Apply\"\n"
        " Test Files  1 failed | 41 passed (42)\n"
        "##[error]Process completed with exit code 1.\n"
    )
    out = _summary(log)
    assert "Unable to find an accessible element with the role \"button\"" in out
    assert "sends one import when Apply is clicked twice" in out
    assert "1 failed | 41 passed" in out


def test_a_playwright_report_above_the_exit_code_is_kept():
    log = (
        "##[group]Run npx playwright test\n##[endgroup]\n"
        "  1) [core] › presentations.spec.ts:168:3 › should open the shortcut sheet\n"
        "    Expected: visible\n"
        "    Received: hidden\n"
        "  1 failed\n"
        "  140 passed (6.2m)\n"
        "##[error]Process completed with exit code 1.\n"
    )
    out = _summary(log)
    assert "presentations.spec.ts:168:3" in out and "Received: hidden" in out


def test_leading_lines_are_bounded():
    log = "##[group]Run x\n##[endgroup]\n" + "".join(f"line {i}\n" for i in range(200)) + (
        "##[error]Process completed with exit code 2.\n"
    )
    out = _summary(log)
    assert "line 199" in out
    assert f"line {199 - ci_logs.LEADING_LINES}" not in out
    assert "line 0\n" not in out


def test_lines_an_earlier_block_shows_are_not_repeated():
    log = (
        "##[group]Run ruff\n##[endgroup]\n"
        "app/x.py:120:1: I001 [*] Import block is un-sorted\n"
        "Found 1 error.\n"
        "##[error]Process completed with exit code 1.\n"
    )
    out = _summary(log)
    assert out.count("app/x.py:120:1: I001") == 1
    assert out.count("Found 1 error.") == 1
    assert "##[error]Process completed with exit code 1." in out


def test_a_pytest_report_is_not_duplicated_under_the_exit_code():
    log = (
        "##[group]Run pytest\n##[endgroup]\n"
        "=========================== short test summary info ============================\n"
        "FAILED tests/test_d.py::test_dedup - AttributeError\n"
        "================== 1 failed, 2 passed in 1.00s ==================\n"
        "##[error]Process completed with exit code 1.\n"
    )
    out = _summary(log)
    assert out.count("FAILED tests/test_d.py::test_dedup") == 1
    assert "line(s) before it" not in out


def test_progress_lines_before_the_exit_code_are_skipped():
    log = (
        "##[group]Run pytest\n##[endgroup]\n"
        "tests/test_x.py::test_1 PASSED [ 50%]\n"
        "Killed\n"
        "##[error]Process completed with exit code 137.\n"
    )
    out = _summary(log)
    assert "Killed" in out and "test_1 PASSED" not in out
