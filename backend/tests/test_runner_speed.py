"""The degraded-runner watcher (``tests/runner_speed.py``): a slow collection is named, a normal one is not."""

from __future__ import annotations

import io
import os
import subprocess
import sys
from pathlib import Path

import pytest

from tests import runner_speed

BACKEND = Path(__file__).resolve().parents[1]
CI = BACKEND.parent / ".github" / "workflows" / "ci.yml"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [(None, None), ("", None), ("  ", None), ("0", None), ("-5", None), ("90", 90.0), ("1.5", 1.5)],
)
def test_the_median_is_read_from_the_environment(raw, expected):
    environ = {} if raw is None else {runner_speed.MEDIAN_ENV: raw}
    assert runner_speed.median_seconds(environ) == expected


def test_a_median_that_is_not_a_number_is_refused():
    with pytest.raises(ValueError, match=runner_speed.MEDIAN_ENV):
        runner_speed.median_seconds({runner_speed.MEDIAN_ENV: "fast"})


def test_the_limit_is_three_times_the_median():
    assert runner_speed.limit_seconds(90) == 270


class _FakeTime:
    """A clock that moves only when the watcher sleeps, and creates the marker at a given time."""

    def __init__(self, marker: Path, collected_at: float):
        self.now = 0.0
        self.marker = marker
        self.collected_at = collected_at

    def clock(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds
        if self.now >= self.collected_at:
            self.marker.touch()


def _watch(tmp_path: Path, median: float, collected_at: float) -> tuple[bool, str]:
    marker = tmp_path / "collected"
    fake = _FakeTime(marker, collected_at)
    out = io.StringIO()
    degraded = runner_speed.watch(median, marker, out=out, clock=fake.clock, sleep=fake.sleep, poll=1.0)
    return degraded, out.getvalue()


def test_a_collection_under_the_limit_prints_no_annotation(tmp_path):
    # The slowest green backend shard of main run 42767506994: 102 s against a 90 s median.
    degraded, text = _watch(tmp_path, median=90, collected_at=102)
    assert degraded is False
    assert "::error::" not in text
    assert text == "[runner] collection 102s (median 90s, limit 270s)\n"


def test_a_collection_past_the_limit_is_named_once_before_it_ends(tmp_path):
    # Red run 42766031078, Backend 4/6: 460 s to collect.
    degraded, text = _watch(tmp_path, median=90, collected_at=460)
    assert degraded is True
    lines = text.splitlines()
    assert len(lines) == 2, text
    assert lines[0].startswith("::error::runner degraded: collection 270s vs median 90s (limit 3x)")
    assert lines[1] == "[runner] collection 460s (median 90s, limit 270s)"


def test_a_collection_that_never_ends_is_still_named(tmp_path):
    """The step timeout kills the watcher: the annotation must already be out."""
    marker = tmp_path / "collected"
    out = io.StringIO()
    now = [0.0]

    def sleep(seconds: float) -> None:
        now[0] += seconds
        if now[0] > 1500:  # the 25-minute step timeout
            raise TimeoutError

    with pytest.raises(TimeoutError):
        runner_speed.watch(70, marker, out=out, clock=lambda: now[0], sleep=sleep, poll=1.0)
    assert out.getvalue().count("::error::runner degraded") == 1


def test_the_marker_is_written_only_when_its_variable_is_set(tmp_path):
    marker = tmp_path / "nested" / "collected"
    runner_speed.mark_collected({})
    assert not marker.exists()
    runner_speed.mark_collected({runner_speed.MARKER_ENV: str(marker)})
    assert marker.exists()


def test_the_watcher_returns_at_once_when_unset():
    out = io.StringIO()
    assert runner_speed.main({}, out) == 0
    assert runner_speed.main({runner_speed.MEDIAN_ENV: "90"}, out) == 0
    assert out.getvalue() == ""


def test_the_suite_writes_the_marker_when_collection_ends(tmp_path):
    """Through the suite's ``pytest_collection_finish``, in a pytest process of its own."""
    (tmp_path / "test_one.py").write_text("def test_one():\n    assert True\n")
    (tmp_path / "conftest.py").write_text("from tests.conftest import pytest_collection_finish  # noqa: F401\n")
    marker = tmp_path / "out" / "collected"
    env = {
        **os.environ,
        "PYTHONPATH": os.pathsep.join(filter(None, [str(BACKEND), os.environ.get("PYTHONPATH")])),
        runner_speed.MARKER_ENV: str(marker),
    }
    for name in ("PYTEST_MARKED_FILES", "PYTEST_SHARD", "PYTEST_ADDOPTS"):
        env.pop(name, None)
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
            "-p",
            "no:randomly",
            str(tmp_path),
        ],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert done.returncode == 0, done.stdout + done.stderr
    assert marker.exists()


@pytest.mark.parametrize(
    ("step", "median"), [("- name: Run unit tests", "90"), ("- name: Run integration tests", "70")]
)
def test_ci_watches_collection_on_the_backend_and_bootstrap_steps(step, median):
    ci = CI.read_text(encoding="utf-8")
    block = ci.split(step, 1)[1].split("- name:", 1)[0]
    assert f'{runner_speed.MEDIAN_ENV}: "{median}"' in block
    assert f"{runner_speed.MARKER_ENV}: ${{{{ runner.temp }}}}/pytest-collected" in block
    assert "python -m tests.runner_speed &" in block
    # Started before pytest, so it times collection from the step's start.
    assert block.index("python -m tests.runner_speed &") < block.index("python -m pytest")
