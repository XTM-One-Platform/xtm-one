"""``--repeat N``: run each selected test N times in one pytest session and report every outcome.

A flaky test is measured before and after its fix by running it many times
(``dev-docs/reference/backend-testing.md`` > "No flaky test on main"). Giving
pytest the same node id N times runs it once (pytest dedupes the arguments),
and the sandbox checks take no new plugin, so the suite carries its own
option, declared and registered by ``tests/conftest.py``::

    python -m pytest --repeat 20 tests/test_x.py::test_y

Each test is parametrized with ``repeatIofN`` (``test_y[repeat3of20]``, or
``test_y[case-repeat3of20]`` beside its own parameters), so the JUnit report
holds N test cases, and the terminal summary ends with one line per test:
the passes, the failures and the iterations that failed. ``--repeat 1`` (the
default) changes nothing.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

import pytest

#: Most iterations one run takes: a typo of 2000 for 20 would hold a worker for an hour.
MAX_REPEAT = 500

_PARAM = "_repeat_iteration"
# The repeat id inside the brackets, first, last or alone among the parameter ids.
_ID = re.compile(r"(?<=\[)repeat(\d+)of\d+(?:-|(?=\]))|-repeat(\d+)of\d+(?=[-\]])")


def iteration_id(index: int, count: int) -> str:
    """The parameter id of iteration *index* (from 1) of *count*."""
    return f"repeat{index}of{count}"


def base_node_id(node_id: str) -> tuple[str, int | None]:
    """The node id without its repeat parameter, and the iteration it names (None: not a repeated test)."""
    match = _ID.search(node_id)
    if match is None:
        return node_id, None
    stripped = node_id[: match.start()] + node_id[match.end() :]
    return stripped.replace("[]", ""), int(match.group(1) or match.group(2))


def _count(value: str) -> int:
    try:
        count = int(value)
    except ValueError:
        raise pytest.UsageError(f"--repeat takes a whole number of runs, not {value!r}.") from None
    if not 1 <= count <= MAX_REPEAT:
        raise pytest.UsageError(f"--repeat is between 1 and {MAX_REPEAT}, not {count}.")
    return count


def add_option(parser) -> None:
    """Declare ``--repeat`` (``tests/conftest.py``'s ``pytest_addoption``: options are read before this plugin registers)."""
    parser.addoption(
        "--repeat",
        action="store",
        default="1",
        metavar="N",
        help=f"run each selected test N times (at most {MAX_REPEAT}) and summarise every outcome (tests/repeat.py)",
    )


def register(config) -> None:
    """Register this module as the plugin of *config* (``tests/conftest.py``'s ``pytest_configure``)."""
    import sys

    if not config.pluginmanager.is_registered(sys.modules[__name__]):
        config.pluginmanager.register(sys.modules[__name__], "tests.repeat")


@dataclass
class _Tally:
    """The outcome of each iteration of each repeated test, in the order they ran."""

    outcomes: dict[str, dict[int, str]] = field(default_factory=dict)

    def record(self, node_id: str, outcome: str) -> None:
        base, index = base_node_id(node_id)
        if index is None:
            return
        runs = self.outcomes.setdefault(base, {})
        # A failed teardown after a passed call fails the iteration; a later phase never clears a failure.
        if runs.get(index) != "failed":
            runs[index] = outcome

    def lines(self, count: int) -> list[str]:
        lines = []
        for base, runs in self.outcomes.items():
            failed = sorted(i for i, outcome in runs.items() if outcome == "failed")
            passed = sum(1 for outcome in runs.values() if outcome == "passed")
            skipped = sum(1 for outcome in runs.values() if outcome == "skipped")
            line = f"{base}: {passed} passed, {len(failed)} failed"
            if skipped:
                line += f", {skipped} skipped"
            if len(runs) < count:
                line += f", {count - len(runs)} not run"
            if failed:
                line += f" (failed: {', '.join(str(i) for i in failed)})"
            lines.append(line)
        return lines


_TALLY: pytest.StashKey[_Tally] = pytest.StashKey()
_COUNT: pytest.StashKey[int] = pytest.StashKey()

#: The session's config: ``pytest_runtest_logreport`` receives the report alone (under xdist, on the controller too).
_CONFIG: list[pytest.Config] = []


def pytest_configure(config):
    config.stash[_COUNT] = _count(str(config.getoption("--repeat")))
    config.stash[_TALLY] = _Tally()
    _CONFIG[:] = [config]


@pytest.fixture
def _repeat_iteration(request):
    """The iteration a repeated test runs (from 1); a test never asks for it."""
    return request.param


def pytest_generate_tests(metafunc):
    count = metafunc.config.stash.get(_COUNT, 1)
    if count <= 1:
        return
    metafunc.fixturenames.append(_PARAM)
    metafunc.parametrize(
        _PARAM,
        range(1, count + 1),
        indirect=True,
        ids=lambda index: iteration_id(index, count),
    )


def pytest_runtest_logreport(report):
    tally = _tally_of(report)
    if tally is None:
        return
    if report.failed:
        tally.record(report.nodeid, "failed")
    elif report.when == "call":
        tally.record(report.nodeid, report.outcome)
    elif report.when == "setup" and report.skipped:
        tally.record(report.nodeid, "skipped")


def _tally_of(report) -> _Tally | None:
    if not _CONFIG or _CONFIG[0].stash.get(_COUNT, 1) <= 1:
        return None
    return _CONFIG[0].stash.get(_TALLY, None)


def pytest_terminal_summary(terminalreporter, exitstatus, config):
    count = config.stash.get(_COUNT, 1)
    tally = config.stash.get(_TALLY, None)
    if count <= 1 or tally is None or not tally.outcomes:
        return
    terminalreporter.write_sep("=", f"repeat: {count} runs per test")
    for line in tally.lines(count):
        terminalreporter.write_line(line)
