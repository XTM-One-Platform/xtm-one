#!/usr/bin/env python3
"""Run the backend unit tests the merge queue would run for a change, before the push.

A pull request's CI runs the static checks only: the backend suites run in
the merge queue, on the test files ``tests/selection.py`` selects for the
merge group's change. ``sandbox_run_command(check="backend-queue-tests",
affected=true)`` hands this script the files a checkout changed under
``backend/`` (``.xtm-one/checks.toml``, ``affected = "files"``: a checkout
has no git, so the script cannot diff the change itself). It calls
:func:`tests.selection.select_backend` on them with the real graph, prints
each selected file with the first reason it was picked, and runs the ones the
queue's unit job runs (a file marked ``integration`` or ``chromium`` as a
whole is left out, as :func:`tests.selection.plan_backend` leaves it out)
under ``pytest -m "not integration"``.

When the change runs under every test (a ``conftest.py``, the dependencies,
the selection itself), the queue runs the whole suite: the script says so and
runs nothing, since the whole suite is CI's (``backend-unit`` takes a file).

Only the files under ``backend/`` reach the script: a change elsewhere that
the queue's selection also reads (``frontend/``, ``.github/``) is not seen,
and a deleted file is not passed. The first line says so.

Usage, from ``backend/``::

    python -m scripts.queue_tests app/x.py tests/test_y.py
    python -m scripts.queue_tests --list app/x.py   # the selection, nothing run

Exit: pytest's own, 0 when nothing is selected or with ``--list``, 2 on an
unknown option.
"""

from __future__ import annotations

import subprocess
import sys

#: What the queue's unit job passes besides the files (``ci.yml``, Run unit tests).
PYTEST = ("-m", "pytest", "-m", "not integration", "-q")
SCOPE = (
    "queue_tests: the merge queue's backend selection (tests/selection.py) over {n} changed file(s) under backend/; "
    "files outside backend/ and deleted files are not seen."
)


def split_args(args: list[str]) -> tuple[list[str], bool, list[str]]:
    """``(changed files from backend/, --list given, unknown options)``."""
    files: list[str] = []
    unknown: list[str] = []
    listing = False
    for arg in args:
        if arg == "--list":
            listing = True
        elif arg.startswith("-"):
            unknown.append(arg)
        else:
            files.append(arg.removeprefix("./"))
    return files, listing, unknown


def unit_files(changed: list[str]) -> tuple[str | None, dict[str, str], list[str]]:
    """``(why every file runs or None, {selected file: reason}, the files the unit job runs)``.

    *changed* are paths from ``backend/``, as the check hands them.
    """
    from tests.selection import Graph, _marks_whole_file, select_backend

    graph = Graph.read()
    selection = select_backend([f"backend/{path}" for path in changed], graph)
    if selection.full:
        return selection.full, {}, []
    run = [path for path in selection.files if not _marks_whole_file(graph.sources.get(path, ""))]
    return None, dict(selection.reasons), run


def main(argv: list[str] | None = None, *, python: str = sys.executable) -> int:
    files, listing, unknown = split_args(list(sys.argv[1:] if argv is None else argv))
    if unknown:
        print(f"queue_tests: unknown option(s) {', '.join(unknown)}: only --list is taken.")
        return 2
    print(SCOPE.format(n=len(files)))
    if not files:
        print("queue_tests: no changed file under backend/, the queue selects no backend test for it.")
        return 0
    full, reasons, run = unit_files(files)
    if full:
        print(f"queue_tests: the queue runs the whole backend suite: {full}. Not run here; CI runs it.")
        return 0
    for path, why in reasons.items():
        kept = "" if path in run else " (integration or chromium as a whole: not in the unit job)"
        print(f"- {path}: {why}{kept}")
    print(f"queue_tests: {len(reasons)} selected, {len(run)} in the unit job.")
    if listing or not run:
        return 0
    return subprocess.run([python, *PYTEST, *run], check=False).returncode


if __name__ == "__main__":
    raise SystemExit(main())
