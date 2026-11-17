"""scripts/types_changed.py: backend-types fails on the change's own mypy findings only, and says its scope."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.services.sandbox_workspace.affected import FileSelection
from app.services.sandbox_workspace.checks import load_checks
from scripts import types_changed

#: mypy's answer on a file with a pre-existing error (line 3) and a new one on a changed line (line 12).
MYPY_OUT = (
    "app/legacy.py:3: error: Function is missing a return type annotation  [no-untyped-def]\n"
    'app/legacy.py:12:5: error: Incompatible return value type (got "int", expected "str")  [return-value]\n'
    "app/legacy.py:12:5: note: See https://mypy.rtfd.io\n"
    'app/other.py:7: error: Name "x" is not defined  [name-defined]\n'
    "Found 3 errors in 2 files (checked 1 source file)\n"
)


class _Done:
    def __init__(self, stdout: str, returncode: int) -> None:
        self.stdout, self.stderr, self.returncode = stdout, "", returncode


def _fake_mypy(monkeypatch: pytest.MonkeyPatch, stdout: str, returncode: int) -> list[list[str]]:
    calls: list[list[str]] = []

    def run(call: list[str], **_kw: object) -> _Done:
        calls.append(call)
        return _Done(stdout, returncode)

    monkeypatch.setattr("scripts.types_changed.subprocess.run", run)
    return calls


def test_only_the_new_error_on_a_changed_line_fails_and_the_scope_comes_first(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    calls = _fake_mypy(monkeypatch, MYPY_OUT, 1)
    code = types_changed.main(["app/legacy.py:10-14"])
    out = capsys.readouterr().out.splitlines()
    assert code == 1
    assert calls == [["mypy", "--config-file", "pyproject.toml", "--explicit-package-bases", "app/legacy.py"]]
    assert out[0].startswith("types_changed: scope 1 file(s): app/legacy.py lines 10-14.")
    assert "Baseline" in out[0]
    assert any("app/legacy.py:12:5: error" in line for line in out)
    assert any("app/legacy.py:12:5: note" in line for line in out)
    assert not any("app/legacy.py:3:" in line for line in out)
    assert out[-1] == (
        "types_changed: 1 error(s) on your lines; apart: 1 pre-existing on untouched lines, not yours "
        "(app/legacy.py 1); 1 in files outside the scope."
    )


def test_pre_existing_errors_alone_pass(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    _fake_mypy(monkeypatch, MYPY_OUT, 1)
    assert types_changed.main(["app/legacy.py:20-30"]) == 0
    last = capsys.readouterr().out.splitlines()[-1]
    assert last.startswith("types_changed: no error on your lines; apart: 2 pre-existing")


def test_a_new_file_named_whole_owns_every_finding(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _fake_mypy(monkeypatch, MYPY_OUT, 1)
    assert types_changed.main(["app/legacy.py"]) == 1
    out = capsys.readouterr().out
    assert "app/legacy.py whole" in out and "2 error(s) on your lines" in out


def test_mypy_failing_to_run_is_exit_2(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    _fake_mypy(monkeypatch, "", 2)
    assert types_changed.main(["app/x.py:1-2"]) == 2
    assert "mypy could not run" in capsys.readouterr().out


def test_no_file_runs_the_configured_gate_as_ci(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _fake_mypy(monkeypatch, "", 0)
    assert types_changed.main([]) == 0
    assert calls == [["mypy", "--config-file", "pyproject.toml"]]


def test_the_selection_line_names_its_baseline() -> None:
    selection = FileSelection(args=["app/x.py:3-4"])
    text = selection.describe(mode="lines", baseline="o/r@main (commit abc)")
    assert text.startswith(
        "Changed lines: 1 file(s) of the change, passed to the check (baseline: o/r@main (commit abc))."
    )
    assert selection.describe(mode="lines").startswith("Changed lines: 1 file(s) of the change, passed to the check.")


def test_the_repository_backend_types_check_runs_on_the_changed_lines_only() -> None:
    text = (Path(__file__).resolve().parents[2] / ".xtm-one" / "checks.toml").read_text(encoding="utf-8")
    spec = load_checks(text)["backend-types"]
    assert spec.argv == ("python", "-m", "scripts.types_changed")
    # The changed files themselves, never the tests that name them (affected = "tests" would select those).
    assert spec.affected == "lines" and "*.py" in spec.affected_globs
    assert spec.affected_by_default and spec.affected_narrows_args
