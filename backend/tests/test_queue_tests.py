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
    # A compose file or a workflow outside backend/ reaches the queue's selection too (gap pr-ci-selection-misses-guards).
    assert spec.affected_whole_repository
    # The unit job's key: a JWT test fails on an empty one.
    raw = tomllib.loads(CHECKS.read_text(encoding="utf-8"))
    assert raw["check"]["backend-queue-tests"]["env"]["SECRET_KEY"]


def test_options_and_files_are_split_and_an_unknown_option_is_refused(capsys: pytest.CaptureFixture[str]) -> None:
    options = queue_tests.parse_args(["--list", "./app/x.py", "--part", "2/3", "--budget", "60", "tests/test_y.py"])
    assert options.files == ["app/x.py", "tests/test_y.py"]
    assert options.listing
    assert options.part == (2, 3)
    assert options.budget == 60.0
    assert options.errors == []
    assert queue_tests.main(["-k", "x"]) == 2
    assert "unknown option -k" in capsys.readouterr().out


@pytest.mark.parametrize("args", [["--part"], ["--part", "4/3"], ["--budget", "soon"]])
def test_a_malformed_option_is_refused(args: list[str], capsys: pytest.CaptureFixture[str]) -> None:
    assert queue_tests.main([*args, "app/x.py"]) == 2
    assert "Options: --list, --part i/n, --budget SECONDS." in capsys.readouterr().out


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


def test_a_change_outside_backend_is_selected_as_the_queue_selects_it() -> None:
    """``../docker-compose.yml`` from ``backend/`` is ``docker-compose.yml`` for ``select_backend``."""
    assert queue_tests.repo_path("../docker-compose.yml") == "docker-compose.yml"
    assert queue_tests.repo_path("app/x.py") == "backend/app/x.py"
    expected = selection.select_backend(["docker-compose.yml"], selection.Graph.read())
    full, reasons, _run = queue_tests.unit_files(["../docker-compose.yml"])
    assert (full, reasons) == (expected.full, ({} if expected.full else expected.reasons))


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
    assert "1 selected, 1 in the unit job, about 1 s of one worker" in out


def _fake_run(monkeypatch: pytest.MonkeyPatch, code: int = 0) -> list[tuple[list[str], dict[str, str]]]:
    calls: list[tuple[list[str], dict[str, str]]] = []

    def run(argv: list[str], check: bool, env: dict[str, str]) -> SimpleNamespace:
        calls.append((argv, env))
        return SimpleNamespace(returncode=code)

    monkeypatch.setattr(subprocess, "run", run)
    monkeypatch.setattr(queue_tests, "workers", lambda: 2)
    return calls


@pytest.mark.parametrize("code", [0, 1])
def test_the_unit_job_s_files_run_under_its_marker_with_workers_and_the_stall_guard(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], code: int
) -> None:
    monkeypatch.setattr(
        queue_tests,
        "unit_files",
        lambda changed: (None, {"tests/test_a.py": "r", "tests/test_b.py": "r"}, ["tests/test_a.py"]),
    )
    monkeypatch.setattr(queue_tests, "durations", lambda: {"tests/test_a.py": 12.0})
    monkeypatch.delenv("PYTEST_STALL_SECONDS", raising=False)
    calls = _fake_run(monkeypatch, code)
    assert queue_tests.main(["app/x.py"], python="py") == code
    assert [argv for argv, _ in calls] == [
        [
            *("py", "-m", "pytest", "-m", "not integration", "-q", "-n", "2", "--dist", "loadgroup"),
            *("--continue-on-collection-errors", "tests/test_a.py"),
        ]
    ]
    assert calls[0][1]["PYTEST_STALL_SECONDS"] == "300"
    out = capsys.readouterr().out
    assert "tests/test_b.py: r (integration or chromium as a whole" in out
    assert "about 12 s of one worker (2 worker(s) here)" in out


def _heavy(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    files = [f"tests/test_{name}.py" for name in "abcd"]
    monkeypatch.setattr(queue_tests, "unit_files", lambda changed: (None, dict.fromkeys(files, "r"), files))
    monkeypatch.setattr(queue_tests, "durations", lambda: dict.fromkeys(files, 500.0))
    return files


def test_a_selection_over_the_budget_names_its_parts_and_runs_nothing(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _heavy(monkeypatch)
    calls = _fake_run(monkeypatch)
    assert queue_tests.main(["app/x.py"], python="py") == 3
    assert calls == []
    out = capsys.readouterr().out
    assert "over the 900 s budget: run it in 3 parts" in out
    assert '["--part", "3/3"]' in out


def test_a_part_runs_its_share_and_the_parts_cover_the_selection(monkeypatch: pytest.MonkeyPatch) -> None:
    files = _heavy(monkeypatch)
    calls = _fake_run(monkeypatch)
    for index in (1, 2):
        assert queue_tests.main(["--part", f"{index}/2", "app/x.py"], python="py") == 0
    shares = [argv[argv.index("--continue-on-collection-errors") + 1 :] for argv, _ in calls]
    assert [len(share) for share in shares] == [2, 2]
    assert sorted(shares[0] + shares[1]) == files


def test_budget_zero_runs_the_whole_selection_in_one_call(monkeypatch: pytest.MonkeyPatch) -> None:
    files = _heavy(monkeypatch)
    calls = _fake_run(monkeypatch)
    assert queue_tests.main(["--budget", "0", "app/x.py"], python="py") == 0
    assert calls[0][0][-len(files) :] == files
