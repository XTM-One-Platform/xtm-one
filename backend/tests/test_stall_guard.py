"""The per-test stall guard (``tests/stall_guard.py``): a hung test ends its worker with its stacks."""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from tests import stall_guard

BACKEND = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize(
    ("raw", "expected"),
    [(None, None), ("", None), ("  ", None), ("0", None), ("-5", None), ("120", 120.0), ("1.5", 1.5)],
)
def test_the_bound_is_read_from_the_environment(raw, expected):
    environ = {} if raw is None else {stall_guard.STALL_SECONDS_ENV: raw}
    assert stall_guard.stall_seconds(environ) == expected


def test_a_bound_that_is_not_a_number_is_refused():
    with pytest.raises(ValueError, match=stall_guard.STALL_SECONDS_ENV):
        stall_guard.stall_seconds({stall_guard.STALL_SECONDS_ENV: "ten"})


def _run(body: str, stall_dir: Path) -> subprocess.CompletedProcess[str]:
    script = textwrap.dedent(
        f"""
        import sys, time
        sys.path.insert(0, {str(BACKEND)!r})
        from tests import stall_guard
        report = stall_guard.open_report("gw7", {{"{stall_guard.STALL_DIR_ENV}": {str(stall_dir)!r}}})
        """
    ) + textwrap.dedent(body)
    return subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=60)  # noqa: S603


def test_a_test_past_the_bound_ends_the_process_and_leaves_its_stack(tmp_path):
    done = _run(
        """
        def stuck_in_the_test():
            time.sleep(30)

        stall_guard.arm(0.5, report, "tests/test_x.py::test_hangs")
        stuck_in_the_test()
        """,
        tmp_path,
    )
    assert done.returncode != 0
    text = (tmp_path / "stall-gw7.log").read_text()
    assert text.startswith("Test still running after 0.5s: tests/test_x.py::test_hangs\n")
    assert "stuck_in_the_test" in text


def test_a_test_that_returns_leaves_an_empty_report_and_the_process_running(tmp_path):
    done = _run(
        """
        stall_guard.arm(0.5, report, "tests/test_x.py::test_returns")
        stall_guard.disarm(report)
        time.sleep(1)
        print("still here")
        """,
        tmp_path,
    )
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip() == "still here"
    assert (tmp_path / "stall-gw7.log").read_text() == ""


def test_the_suite_hook_ends_a_hung_test_and_lets_a_quick_one_pass(tmp_path):
    """Through the suite's ``pytest_runtest_protocol``, in a pytest process of its own."""
    tests = tmp_path / "test_two.py"
    tests.write_text(
        "import time\n\ndef test_quick():\n    assert True\n\ndef test_stuck_forever():\n    time.sleep(30)\n"
    )
    # The suite's hook alone: its autouse fixtures (the app import) would take
    # the 2 seconds of the bound on the first test.
    (tmp_path / "conftest.py").write_text("from tests.conftest import pytest_runtest_protocol  # noqa: F401\n")
    stall_dir = tmp_path / "stall"
    env = {
        "PYTHONPATH": os.pathsep.join(filter(None, [str(BACKEND), os.environ.get("PYTHONPATH")])),
        **os.environ,
        stall_guard.STALL_SECONDS_ENV: "2",
        stall_guard.STALL_DIR_ENV: str(stall_dir),
        "PYTEST_XDIST_WORKER": "gw9",
    }
    env.pop("PYTEST_MARKED_FILES", None)
    env.pop("PYTEST_SHARD", None)
    done = subprocess.run(  # noqa: S603
        [
            sys.executable,
            "-m",
            "pytest",
            "-c",
            os.devnull,
            "--rootdir",
            str(tmp_path),
            "-p",
            "no:cacheprovider",
            str(tests),
        ],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert done.returncode != 0
    report = (stall_dir / "stall-gw9.log").read_text()
    assert report.startswith("Test still running after 2s: "), report
    # The quick test ran first and returned: the report names only the stuck one,
    # and its stack runs through the test's own file.
    assert report.splitlines()[0].endswith("test_two.py::test_stuck_forever"), report
    assert 'test_two.py", line 7 in test_stuck_forever' in report, report
