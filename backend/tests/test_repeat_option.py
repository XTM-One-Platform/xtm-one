"""``--repeat N`` (``tests/repeat.py``): one pytest call runs a test N times and reports every outcome."""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest
from defusedxml import ElementTree

from tests import repeat

BACKEND = Path(__file__).resolve().parents[1]

CONFTEST = """
from tests import repeat

def pytest_addoption(parser):
    repeat.add_option(parser)

def pytest_configure(config):
    repeat.register(config)
"""

TESTS = """
import os
import pytest

def test_steady():
    pass

def test_flaky(_counter=[0]):
    _counter[0] += 1
    assert _counter[0] % 4 != 0

@pytest.mark.parametrize("case", ["a", "b"])
def test_cases(case):
    pass
"""


def _pytest(tmp_path: Path, *args: str) -> subprocess.CompletedProcess[str]:
    (tmp_path / "conftest.py").write_text(textwrap.dedent(CONFTEST))
    (tmp_path / "test_sample.py").write_text(textwrap.dedent(TESTS))
    env = {**os.environ, "PYTHONPATH": str(BACKEND)}
    return subprocess.run(  # noqa: S603
        [sys.executable, "-m", "pytest", "-p", "no:randomly", "-p", "no:cacheprovider", "-q", *args],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )


def test_one_call_runs_a_named_test_twenty_times_and_reports_twenty_outcomes(tmp_path):
    done = _pytest(tmp_path, "--repeat", "20", "--junitxml=report.xml", "test_sample.py::test_flaky")

    assert done.returncode == 1, done.stdout + done.stderr
    cases = ElementTree.parse(tmp_path / "report.xml").getroot().iter("testcase")
    names = [case.get("name") for case in cases]
    assert names == [f"test_flaky[repeat{i}of20]" for i in range(1, 21)]
    assert "test_sample.py::test_flaky: 15 passed, 5 failed (failed: 4, 8, 12, 16, 20)" in done.stdout
    assert "repeat: 20 runs per test" in done.stdout


def test_every_selected_test_gets_its_own_line_beside_its_own_parameters(tmp_path):
    done = _pytest(tmp_path, "--repeat", "3", "test_sample.py::test_steady", "test_sample.py::test_cases")

    assert done.returncode == 0, done.stdout + done.stderr
    assert "test_sample.py::test_steady: 3 passed, 0 failed" in done.stdout
    assert "test_sample.py::test_cases[a]: 3 passed, 0 failed" in done.stdout
    assert "test_sample.py::test_cases[b]: 3 passed, 0 failed" in done.stdout
    assert "9 passed" in done.stdout


def test_without_the_option_nothing_changes(tmp_path):
    done = _pytest(tmp_path, "test_sample.py::test_steady")

    assert done.returncode == 0, done.stdout + done.stderr
    assert "repeat:" not in done.stdout and "1 passed" in done.stdout


@pytest.mark.parametrize("value", ["0", "-1", "ten", str(repeat.MAX_REPEAT + 1)])
def test_a_count_out_of_range_stops_the_run_before_any_test(tmp_path, value):
    done = _pytest(tmp_path, "--repeat", value, "test_sample.py")

    assert done.returncode == pytest.ExitCode.USAGE_ERROR, done.stdout + done.stderr
    assert "--repeat" in done.stderr


@pytest.mark.parametrize(
    ("node_id", "expected"),
    [
        ("t.py::test_x[repeat3of20]", ("t.py::test_x", 3)),
        ("t.py::test_x[a-repeat12of20]", ("t.py::test_x[a]", 12)),
        ("t.py::test_x[repeat2of5-a]", ("t.py::test_x[a]", 2)),
        ("t.py::test_x[a]", ("t.py::test_x[a]", None)),
    ],
)
def test_the_base_node_id_drops_the_repeat_parameter_wherever_it_stands(node_id, expected):
    assert repeat.base_node_id(node_id) == expected
