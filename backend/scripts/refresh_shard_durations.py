#!/usr/bin/env python3
"""Rebuild ``tests/shard_durations.json`` from the JUnit reports of a full CI run.

The committed unit table, which deals the shards when no nightly table can be
(``scripts/shard_weights.py``: the nightly runs publish the weights every run
deals by). A full run of ``ci.yml`` (nightly or ``workflow_dispatch``) keeps
each shard's report in its ``unit-junit-<n>`` artifact:

    gh run download <run id> --pattern 'unit-junit-*' --dir /tmp/junit
    uv run python scripts/refresh_shard_durations.py /tmp/junit/*/*.xml

A file's weight is the seconds its tests took on one worker, summed; files
under ``MIN_SECONDS`` are left out (``UNTIMED_SECONDS`` weighs them).
"""

from __future__ import annotations

import argparse
import json
import xml.etree.ElementTree as ET
from collections import defaultdict
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent.parent
DURATIONS = BACKEND_DIR / "tests" / "shard_durations.json"
#: A file under this many seconds is not worth a line: the median file takes under one.
MIN_SECONDS = 2


def file_seconds(reports: list[Path]) -> dict[str, int]:
    """Seconds per test file (from the backend root), read from ``junit_family=xunit1`` reports."""
    seconds: dict[str, float] = defaultdict(float)
    for report in reports:
        for case in ET.parse(report).iter("testcase"):  # noqa: S314 - the CI's own reports
            name = case.get("file")
            if name and name.startswith("tests/"):
                seconds[name] += float(case.get("time") or 0)
    return {name: round(total) for name, total in sorted(seconds.items()) if total >= MIN_SECONDS}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("reports", nargs="+", type=Path, help="the JUnit XML reports of every unit shard of one run")
    args = parser.parse_args()
    durations = file_seconds(args.reports)
    if not durations:
        parser.error("the reports name no test file: were they written with -o junit_family=xunit1?")
    DURATIONS.write_text(json.dumps(durations, indent=1) + "\n", encoding="utf-8")
    print(f"{len(durations)} files, {sum(durations.values())} s, written to {DURATIONS.relative_to(BACKEND_DIR)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
