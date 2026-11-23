#!/usr/bin/env python3
"""The weights the sharded CI jobs deal their files by, refreshed by the nightly runs.

Three tables, each the seconds a file took on CI:

* ``unit``: a test file of ``backend/tests/`` in the unit job, summed over one worker;
* ``chromium``: a test file in the Browser tool job, whose tests run one at a time;
* ``e2e``: a spec file of ``frontend/tests_e2e/``, first attempts only, to the tenth.

The committed tables are the fallback. The nightly runs of ``ci.yml`` and ``e2e.yml``
on ``main`` measure their own reports (``measure``) and publish the median of the
last nightly measures (``median``): one run is a noisy sample (a spec's time moves by
a fifth from one run to the next). ``refresh`` rebuilds a committed table by hand from
the reports of one or more runs. The ``select`` job of every run reads the latest
table published (``pick``), checks it, and hands that one table to every job of the
run: each job deals the same files by the same weights, so each file runs in exactly
one job. A table that fails a check, or that cannot be fetched, gives way to the
committed one: weights only decide which shard runs a file, never whether it runs.

Standard library only: ``e2e.yml``'s select job runs it without the backend's venv.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import statistics
import sys
import xml.etree.ElementTree as ET
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent.parent
REPO_ROOT = BACKEND_DIR.parent

#: No test file takes more on CI: a larger value is not a measure.
MAX_SECONDS = 4 * 3600


@dataclass(frozen=True)
class Kind:
    """One table: the files it may name, where its committed copy lives, how many files a whole one names."""

    name: str
    file: re.Pattern[str]
    committed: Path
    #: A table naming fewer files measured a run that lost shards: it is refused.
    min_files: int
    #: A file under this many seconds is left out, and its job weighs it at its default: worth it only
    #: where that default is small (``tests/sharding.py``: 1 s). The E2E jobs weigh an unnamed spec at
    #: the median of the others (``tests_e2e/shards.ts``), so every spec keeps its line.
    min_seconds: float
    #: Decimals kept: whole seconds for the test files, tenths for the specs, many of which take less than one.
    digits: int

    def rounded(self, seconds: float) -> float:
        return round(seconds, self.digits) if self.digits else round(seconds)


KINDS: dict[str, Kind] = {
    "unit": Kind(
        "unit", re.compile(r"^tests/[\w./-]+\.py$"), BACKEND_DIR / "tests" / "shard_durations.json", 100, 2, 0
    ),
    "chromium": Kind(
        "chromium",
        re.compile(r"^tests/[\w./-]+\.py$"),
        BACKEND_DIR / "tests" / "shard_durations_chromium.json",
        20,
        2,
        0,
    ),
    "e2e": Kind(
        "e2e",
        re.compile(r"^[\w./-]+\.spec\.ts$"),
        REPO_ROOT / "frontend" / "tests_e2e" / "shard-timings.json",
        40,
        0,
        1,
    ),
}


def junit_seconds(reports: Iterable[Path]) -> dict[str, float]:
    """Seconds per test file (from the backend root), summed from ``junit_family=xunit1`` reports."""
    seconds: dict[str, float] = defaultdict(float)
    for report in reports:
        for case in ET.parse(report).iter("testcase"):  # noqa: S314 - the CI's own reports
            name = case.get("file")
            if name and name.startswith("tests/"):
                seconds[name] += float(case.get("time") or 0)
    return dict(seconds)


def _playwright_suite(suite: Mapping[str, object], into: dict[str, float]) -> None:
    for spec in suite.get("specs") or []:
        file = spec.get("file") if isinstance(spec, dict) else None
        if not isinstance(file, str) or not file.endswith(".spec.ts"):
            continue
        for test in spec.get("tests") or []:
            results = test.get("results") or []
            duration = results[0].get("duration") if results else None
            if isinstance(duration, int | float):
                into[file] = into.get(file, 0.0) + duration / 1000
    for child in suite.get("suites") or []:
        if isinstance(child, dict):
            _playwright_suite(child, into)


def playwright_seconds(reports: Iterable[Path]) -> dict[str, float]:
    """Seconds per spec file, the first attempts of Playwright's JSON reports (``frontend/playwright.config.ts``)."""
    seconds: dict[str, float] = {}
    for report in reports:
        for suite in json.loads(report.read_text(encoding="utf-8")).get("suites") or []:
            _playwright_suite(suite, seconds)
    return seconds


def measure(kind: str, reports: Iterable[Path]) -> dict[str, float]:
    """One run's table: the files of at least the kind's ``min_seconds``, rounded."""
    spec = KINDS[kind]
    seconds = playwright_seconds(reports) if kind == "e2e" else junit_seconds(reports)
    return {name: spec.rounded(total) for name, total in sorted(seconds.items()) if total >= spec.min_seconds}


def median(kind: str, tables: Sequence[Mapping[str, float]]) -> dict[str, float]:
    """The files of the newest table, ``tables[0]``, each at its median over the tables that name it.

    A file the newest table leaves out was deleted, or now takes less than the
    kind's ``min_seconds``: it leaves the table at once, rather than keep its old
    weight until the earlier measures age out.
    """
    if not tables:
        return {}
    values: dict[str, list[float]] = defaultdict(list)
    for table in tables:
        for name, seconds in table.items():
            if name in tables[0]:
                values[name].append(seconds)
    return {name: KINDS[kind].rounded(statistics.median(seconds)) for name, seconds in sorted(values.items())}


def problem(kind: str, table: object) -> str | None:
    """Why ``table`` cannot be the ``kind`` table; ``None`` when it can."""
    spec = KINDS[kind]
    if not isinstance(table, dict):
        return "it is not a JSON object"
    if len(table) < spec.min_files:
        return f"it names {len(table)} files, fewer than {spec.min_files}"
    for name, seconds in table.items():
        if not spec.file.fullmatch(name) or ".." in name.split("/"):
            return f"{name!r} is not a {kind} file"
        if isinstance(seconds, bool) or not isinstance(seconds, int | float) or not math.isfinite(seconds):
            return f"{name!r} weighs {seconds!r}, not a number"
        if not 0 <= seconds <= MAX_SECONDS:
            return f"{name!r} weighs {seconds} s"
    return None


def pick(kind: str, candidate: Path | None, out: Path) -> str:
    """Write the table this run deals by to ``out``: ``candidate`` when it passes, else the committed one. Says which."""
    reason = "no nightly table was fetched"
    if candidate is not None and candidate.is_file():
        try:
            table = json.loads(candidate.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            table, reason = None, f"the nightly table does not read ({exc.__class__.__name__})"
        else:
            reason = problem(kind, table) or ""
        if not reason:
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(json.dumps(table, indent=1, sort_keys=True) + "\n", encoding="utf-8")
            return f"{kind}: the nightly table ({len(table)} files)"
        reason = f"the nightly table was refused: {reason}"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(KINDS[kind].committed.read_text(encoding="utf-8"), encoding="utf-8")
    return f"{kind}: the committed table ({reason})"


def _write(table: Mapping[str, float], out: Path) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(dict(table), indent=1, sort_keys=True) + "\n", encoding="utf-8")


def _reports(kind: str, run: Path) -> list[Path]:
    """The reports a downloaded run holds: its JUnit files, or its Playwright ``timings.json``."""
    return sorted(run.rglob("timings.json" if kind == "e2e" else "*.xml"))


def _published(kind: str, table: Mapping[str, float], out: Path) -> int:
    if reason := problem(kind, table):
        print(f"::error::the {kind} table is not published: {reason}", file=sys.stderr)
        return 1
    _write(table, out)
    print(f"{kind}: {len(table)} files, {round(sum(table.values()))} s")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    commands = parser.add_subparsers(dest="command", required=True)
    measured = commands.add_parser(
        "measure", help="one run's table, from its JUnit (unit, chromium) or Playwright (e2e) reports"
    )
    measured.add_argument("--kind", choices=sorted(KINDS), required=True)
    measured.add_argument("--out", type=Path, required=True)
    measured.add_argument("reports", nargs="+", type=Path)
    medians = commands.add_parser(
        "median", help="the newest table's files, each at its median over several runs' tables"
    )
    medians.add_argument("--kind", choices=sorted(KINDS), required=True)
    medians.add_argument("--out", type=Path, required=True)
    medians.add_argument("tables", nargs="+", type=Path, help="the newest first")
    picked = commands.add_parser(
        "pick", help="the table a run deals by: the nightly one when it passes, else the committed one"
    )
    picked.add_argument("--kind", choices=sorted(KINDS), required=True)
    picked.add_argument("--candidate", type=Path)
    picked.add_argument("--out", type=Path, required=True)
    refreshed = commands.add_parser(
        "refresh",
        help="rebuild the committed table from the reports of runs downloaded by hand",
        description="Each RUN is a directory holding one run's reports, as `gh run download <id> --pattern "
        "'unit-junit-*' --dir RUN` (or 'chromium-junit-*', or 'test-results-*' for e2e) leaves them. "
        "Several runs give each file its median; the newest first.",
    )
    refreshed.add_argument("--kind", choices=sorted(KINDS), required=True)
    refreshed.add_argument("--out", type=Path, help="default: the committed table")
    refreshed.add_argument("runs", nargs="+", type=Path)
    args = parser.parse_args(argv)

    if args.command == "pick":
        print(pick(args.kind, args.candidate, args.out))
        return 0
    if args.command == "measure":
        return _published(args.kind, measure(args.kind, args.reports), args.out)
    if args.command == "refresh":
        tables = []
        for run in args.runs:
            table = measure(args.kind, _reports(args.kind, run))
            if reason := problem(args.kind, table):
                print(f"::error::{run} is not a whole run: {reason}", file=sys.stderr)
                return 1
            tables.append(table)
        return _published(args.kind, median(args.kind, tables), args.out or KINDS[args.kind].committed)
    newest, *earlier = args.tables
    tables = [json.loads(newest.read_text(encoding="utf-8"))]
    for path in earlier:
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if problem(args.kind, loaded) is None:
            tables.append(loaded)
    return _published(args.kind, median(args.kind, tables), args.out)


if __name__ == "__main__":
    raise SystemExit(main())
