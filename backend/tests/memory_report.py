"""Name the test files that make an xdist worker grow.

The CI runner has about 8 GB for two workers. A worker that ends a shard at
3 to 3.5 GB of resident memory leaves the runner a few hundred MB, and one more
spike makes the host kill it ("The runner has received a shutdown signal"),
with a log that just stops (#10437). The runner sampler says memory ran out,
not which tests kept it.

With ``PYTEST_MEMORY_REPORT`` set to a directory, each worker reads its resident
set size before and after every test and adds the difference to the test's
file. At the end of the session it writes ``memory-<worker>.txt`` there: the
files ranked by the memory they added and did not give back, with their test
count and the worker's resident size at its first and last test. A CI step
prints the reports, so the next run names the modules to bound.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

MEMORY_REPORT_ENV = "PYTEST_MEMORY_REPORT"

#: Files listed per worker report.
TOP_FILES = 25

_PAGE_SIZE = os.sysconf("SC_PAGE_SIZE") if hasattr(os, "sysconf") else 4096


def report_dir(environ: os._Environ[str] | dict[str, str] = os.environ) -> Path | None:
    """The directory set by ``PYTEST_MEMORY_REPORT``; ``None`` when unset or empty."""
    raw = (environ.get(MEMORY_REPORT_ENV) or "").strip()
    return Path(raw) if raw else None


def resident_bytes(statm: Path = Path("/proc/self/statm")) -> int | None:
    """This process's resident set size now; ``None`` where ``/proc`` is absent."""
    try:
        fields = statm.read_text(encoding="ascii").split()
    except OSError:
        return None
    return int(fields[1]) * _PAGE_SIZE


def file_of(nodeid: str) -> str:
    """The test file of a node id (``tests/test_x.py::test_y[1]`` gives ``tests/test_x.py``)."""
    return nodeid.split("::", 1)[0]


@dataclass
class MemoryLedger:
    """Resident memory added per test file over one worker's session."""

    added: dict[str, int] = field(default_factory=dict)
    tests: dict[str, int] = field(default_factory=dict)
    first: int | None = None
    last: int | None = None

    def record(self, nodeid: str, before: int | None, after: int | None) -> None:
        """Add one test's growth (``after - before``, negative when it gave memory back) to its file."""
        if before is None or after is None:
            return
        if self.first is None:
            self.first = before
        self.last = after
        name = file_of(nodeid)
        self.added[name] = self.added.get(name, 0) + after - before
        self.tests[name] = self.tests.get(name, 0) + 1

    def ranked(self, top: int = TOP_FILES) -> list[tuple[str, int, int]]:
        """The ``top`` files that added the most, as (file, bytes added, tests), largest first."""
        rows = sorted(self.added.items(), key=lambda row: (-row[1], row[0]))
        return [(name, added, self.tests[name]) for name, added in rows[:top]]

    def render(self, worker: str, top: int = TOP_FILES) -> str:
        """The report text: the worker's resident size at its first and last test, then the ranked files."""
        lines = [
            f"Memory added per test file on {worker}: {_mb(self.first)} at the first test, {_mb(self.last)} at the last"
        ]
        lines.extend(f"{_mb(added):>10}  {count:>5} tests  {name}" for name, added, count in self.ranked(top))
        return "\n".join(lines) + "\n"


def write_report(directory: Path, worker: str, ledger: MemoryLedger, top: int = TOP_FILES) -> Path:
    """Write ``memory-<worker>.txt`` under ``directory`` and return its path."""
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"memory-{worker}.txt"
    path.write_text(ledger.render(worker, top), encoding="utf-8")
    return path


def _mb(value: int | None) -> str:
    return (
        "?" if value is None else f"{value / (1024 * 1024):+.0f} MB" if value < 0 else f"{value / (1024 * 1024):.0f} MB"
    )
