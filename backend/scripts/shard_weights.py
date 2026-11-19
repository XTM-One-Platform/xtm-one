#!/usr/bin/env python3
"""The weights the sharded CI jobs deal their files by, refreshed by the nightly runs.

Three tables, each the seconds a file took on CI:

* ``unit``: a test file of ``backend/tests/`` in the unit job, summed over one worker;
* ``chromium``: a test file in the Browser tool job, whose tests run one at a time;
* ``e2e``: a spec file of ``frontend/tests_e2e/``, first attempts only.

The committed tables are the fallback. The nightly runs of ``ci.yml`` and ``e2e.yml``
on ``main`` measure their own reports (``measure``) and publish the median of the
last nightly measures (``median``): one run is a noisy sample (a spec's time moves by
a fifth from one run to the next). The ``select`` job of every run reads the latest
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
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent.parent
REPO_ROOT = BACKEND_DIR.parent

#: A file under this many seconds is not worth a line: the jobs weigh it at their default.
MIN_SECONDS = 2
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


KINDS: dict[str, Kind] = {
    "unit": Kind("unit", re.compile(r"^tests/[\w./-]+\.py$"), BACKEND_DIR / "tests" / "shard_durations.json", 100),
    "chromium": Kind(
        "chromium", re.compile(r"^tests/[\w./-]+\.py$"), BACKEND_DIR / "tests" / "shard_durations_chromium.json", 20
    ),
    "e2e": Kind(
        "e2e", re.compile(r"^[\w./-]+\.spec\.ts$"), REPO_ROOT / "frontend" / "tests_e2e" / "shard-timings.json", 40
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
    """Seconds per spec file, the first attempts of Playwright's JSON reports (``scripts/lib/e2e-shard-timings.mjs``)."""
    seconds: dict[str, float] = {}
    for report in reports:
        for suite in json.loads(report.read_text(encoding="utf-8")).get("suites") or []:
            _playwright_suite(suite, seconds)
    return seconds


def measure(kind: str, reports: Iterable[Path]) -> dict[str, int]:
    """One run's table: the files of at least ``MIN_SECONDS``, rounded."""
    seconds = playwright_seconds(reports) if kind == "e2e" else junit_seconds(reports)
    return {name: round(total) for name, total in sorted(seconds.items()) if total >= MIN_SECONDS}


def median(tables: Iterable[Mapping[str, float]]) -> dict[str, int]:
    """Each file's median over the tables that name it."""
    values: dict[str, list[float]] = defaultdict(list)
    for table in tables:
        for name, seconds in table.items():
            values[name].append(seconds)
    return {name: round(statistics.median(seconds)) for name, seconds in sorted(values.items())}


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


def _write(table: Mapping[str, int], out: Path) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(dict(table), indent=1, sort_keys=True) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    commands = parser.add_subparsers(dest="command", required=True)
    measured = commands.add_parser("measure", help="one run's table, from its JUnit (unit, chromium) or Playwright (e2e) reports")
    measured.add_argument("--kind", choices=sorted(KINDS), required=True)
    measured.add_argument("--out", type=Path, required=True)
    measured.add_argument("reports", nargs="+", type=Path)
    medians = commands.add_parser("median", help="each file's median over several runs' tables")
    medians.add_argument("--kind", choices=sorted(KINDS), required=True)
    medians.add_argument("--out", type=Path, required=True)
    medians.add_argument("tables", nargs="+", type=Path)
    picked = commands.add_parser("pick", help="the table a run deals by: the nightly one when it passes, else the committed one")
    picked.add_argument("--kind", choices=sorted(KINDS), required=True)
    picked.add_argument("--candidate", type=Path)
    picked.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)

    if args.command == "pick":
        print(pick(args.kind, args.candidate, args.out))
        return 0
    if args.command == "measure":
        table = measure(args.kind, args.reports)
    else:
        tables = []
        for path in args.tables:
            try:
                loaded = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if problem(args.kind, loaded) is None:
                tables.append(loaded)
        table = median(tables)
    if reason := problem(args.kind, table):
        print(f"::error::the {args.kind} table is not published: {reason}", file=sys.stderr)
        return 1
    _write(table, args.out)
    print(f"{args.kind}: {len(table)} files, {sum(table.values())} s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
