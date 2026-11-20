"""The merge queue's backend selection run before the push (``scripts/queue_tests.py``, check ``backend-queue-tests``)."""

from __future__ import annotations

import subprocess
import tomllib
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.services.sandbox_workspace.checks import CheckSpec, load_checks
from scripts import queue_tests
from tests import selection

REPO_ROOT = Path(__file__).resolve().parents[2]
CHECKS = REPO_ROOT / ".xtm-one" / "checks.toml"


def _spec() -> CheckSpec:
    return load_checks(CHECKS.read_text(encoding="utf-8"))["backend-queue-tests"]


def test_the_check_hands_the_script_the_changed_backend_files() -> None:
    spec = _spec()
    assert spec.argv == ("python", "-m", "scripts.queue_tests")
    assert spec.cwd == "backend"
    assert spec.affected == "files"
    assert spec.affected_by_default
    # The unit job's key: a JWT test fails on an empty one.
    raw = tomllib.loads(CHECKS.read_text(encoding="utf-8"))
    assert raw["check"]["backend-queue-tests"]["env"]["SECRET_KEY"]


def test_options_and_files_are_split_and_an_unknown_option_is_refused(capsys: pytest.CaptureFixture[str]) -> None:
    assert queue_tests.split_args(["--list", "./app/x.py", "tests/test_y.py"]) == (
        ["app/x.py", "tests/test_y.py"],
        True,
        [],
    )
    assert queue_tests.main(["-k", "x"]) == 2
    assert "unknown option(s) -k" in capsys.readouterr().out


def test_no_changed_file_selects_nothing(capsys: pytest.CaptureFixture[str]) -> None:
    assert queue_tests.main([]) == 0
    out = capsys.readouterr().out
    assert out.startswith("queue_tests: the merge queue's backend selection")
    assert "selects no backend test" in out


def test_a_change_the_queue_runs_whole_for_is_named_not_run(capsys: pytest.CaptureFixture[str]) -> None:
    assert queue_tests.main(["tests/conftest.py"], python="never") == 0
    assert "the queue runs the whole backend suite" in capsys.readouterr().out


def test_the_selection_is_the_queue_s_own() -> None:
    """The script selects what ``select_backend`` selects for the same change, from the repository root."""
    changed = ["app/services/file_responses.py"]
    expected = selection.select_backend([f"backend/{p}" for p in changed], selection.Graph.read())
    full, reasons, run = queue_tests.unit_files(changed)
    assert full is None
    assert reasons == expected.reasons
    # The file the queue reached for #405 while its pull request's CI was green.
    assert "tests/test_file_inline_types.py" in reasons
    assert set(run) <= set(reasons)


def test_list_prints_the_selection_and_runs_nothing(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        queue_tests, "unit_files", lambda changed: (None, {"tests/test_a.py": "imports app.x"}, ["tests/test_a.py"])
    )

    def never(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("pytest must not run with --list")

    monkeypatch.setattr(subprocess, "run", never)
    assert queue_tests.main(["--list", "app/x.py"]) == 0
    out = capsys.readouterr().out
    assert "- tests/test_a.py: imports app.x" in out
    assert "1 selected, 1 in the unit job." in out


@pytest.mark.parametrize("code", [0, 1])
def test_the_unit_job_s_files_run_under_its_marker(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], code: int
) -> None:
    monkeypatch.setattr(
        queue_tests,
        "unit_files",
        lambda changed: (None, {"tests/test_a.py": "r", "tests/test_b.py": "r"}, ["tests/test_a.py"]),
    )
    calls: list[list[str]] = []

    def run(argv: list[str], check: bool) -> SimpleNamespace:
        calls.append(argv)
        return SimpleNamespace(returncode=code)

    monkeypatch.setattr(subprocess, "run", run)
    assert queue_tests.main(["app/x.py"], python="py") == code
    assert calls == [
        ["py", "-m", "pytest", "-m", "not integration", "-q", "--continue-on-collection-errors", "tests/test_a.py"]
    ]
    assert "tests/test_b.py: r (integration or chromium as a whole" in capsys.readouterr().out
