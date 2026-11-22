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
under ``pytest -m "not integration"``, with ``--continue-on-collection-errors``
so a module a narrow checkout cannot import is named and the rest still runs.

When the change runs under every test (a ``conftest.py``, the dependencies,
the selection itself), the queue runs the whole suite: the script says so and
runs nothing, since the whole suite is CI's (``backend-unit`` takes a file).

The check declares ``affected_whole_repository``: a changed file outside
``backend/`` reaches the script too, relative to it (``../docker-compose.yml``),
and counts in the selection as the queue counts it (a test naming a compose
file or a workflow). A deleted file is not passed. The first line says so.

The selection runs as the queue's unit job runs it: pytest-xdist workers with
``--dist loadgroup`` (``WORKERS`` at most) and the stall guard of
``tests/stall_guard.py`` (``PYTEST_STALL_SECONDS``, 300 s unless set). The
seconds the selection weighs are read from ``tests/shard_durations.json``
(:mod:`tests.sharding`) and printed. A selection heavier than ``--budget``
seconds (``BUDGET_SECONDS`` by default) is not run in one call: the script
names the parts and exits 3; ``--part i/n`` runs one share, dealt out by
:func:`tests.sharding.shard_files` so the parts weigh about the same.

Usage, from ``backend/``::

    python -m scripts.queue_tests app/x.py tests/test_y.py
    python -m scripts.queue_tests --list app/x.py   # the selection, nothing run
    python -m scripts.queue_tests --part 1/3 app/x.py
    python -m scripts.queue_tests --budget 0 app/x.py   # no budget: the whole selection

Exit: pytest's own, 0 when nothing is selected or with ``--list``, 2 on an
unknown or malformed option, 3 when the selection is over the budget and no
part is named.
"""

from __future__ import annotations

import math
import os
import posixpath
import subprocess
import sys
from dataclasses import dataclass, field

#: What the queue's unit job passes besides the files (``ci.yml``, Run unit tests).
PYTEST = ("-m", "pytest", "-m", "not integration", "-q")
#: Added here only: a narrow checkout (``sub_paths``) lacks files some selected modules read at
#: import (a frontend fixture), and one such collection error would stop every other file from
#: running. The error is still reported and still makes the exit code non-zero.
CONTINUE = ("--continue-on-collection-errors",)
#: The most xdist workers a sandbox run takes: each one collects its files (about 1.6 GB for the
#: whole suite, ``tests/sharding.py``), so more would trade speed for the container's memory.
WORKERS = 4
#: Seconds of one worker a selection may weigh before it is run in parts (``--part``).
BUDGET_SECONDS = 900.0
#: The unit job's stall bound (``ci.yml``): a hung test ends its worker with its stacks.
STALL_SECONDS = "300"
SCOPE = (
    "queue_tests: the merge queue's backend selection (tests/selection.py) over {n} changed file(s) "
    "of the repository; deleted files are not seen."
)


@dataclass
class Options:
    """The command line: changed files from ``backend/`` and the options taken."""

    files: list[str] = field(default_factory=list)
    listing: bool = False
    part: tuple[int, int] | None = None
    budget: float = BUDGET_SECONDS
    errors: list[str] = field(default_factory=list)


def parse_args(args: list[str]) -> Options:
    """Read ``--list``, ``--part i/n`` and ``--budget SECONDS``; anything else starting with ``-`` is an error."""
    from tests.sharding import parse_shard

    options = Options()
    queue = list(args)
    while queue:
        arg = queue.pop(0)
        if arg == "--list":
            options.listing = True
        elif arg in ("--part", "--budget"):
            if not queue:
                options.errors.append(f"{arg} takes a value")
                continue
            value = queue.pop(0)
            try:
                if arg == "--part":
                    options.part = parse_shard(value)
                else:
                    options.budget = float(value)
            except ValueError as exc:
                options.errors.append(f"{arg} {value}: {exc}")
        elif arg.startswith("-"):
            options.errors.append(f"unknown option {arg}")
        else:
            options.files.append(arg.removeprefix("./"))
    return options


def unit_files(changed: list[str]) -> tuple[str | None, dict[str, str], list[str]]:
    """``(why every file runs or None, {selected file: reason}, the files the unit job runs)``.

    *changed* are paths from ``backend/``, as the check hands them (``../x`` for a file outside it).
    """
    from tests.selection import Graph, _marks_whole_file, select_backend

    graph = Graph.read()
    selection = select_backend([repo_path(path) for path in changed], graph)
    if selection.full:
        return selection.full, {}, []
    run = [path for path in selection.files if not _marks_whole_file(graph.sources.get(path, ""))]
    return None, dict(selection.reasons), run


def repo_path(path: str) -> str:
    """*path*, given from ``backend/``, as a path from the repository root (``../ci.yml`` -> ``ci.yml``)."""
    return posixpath.normpath(posixpath.join("backend", path))


def durations() -> dict[str, float]:
    """Seconds of one CI worker per test file (``tests/shard_durations.json``)."""
    from tests.sharding import load_durations

    return load_durations()


def weight(files: list[str], seconds: dict[str, float]) -> float:
    """Seconds of one worker *files* weigh; a file the table does not name weighs ``UNTIMED_SECONDS``."""
    from tests.sharding import UNTIMED_SECONDS

    return sum(seconds.get(path, UNTIMED_SECONDS) for path in files)


def workers() -> int:
    return max(1, min(WORKERS, os.cpu_count() or 1))


def pytest_argv(python: str, files: list[str]) -> list[str]:
    """The unit job's command on *files*: xdist workers grouped as on CI, collection errors reported."""
    return [python, *PYTEST, "-n", str(workers()), "--dist", "loadgroup", *CONTINUE, *files]


def main(argv: list[str] | None = None, *, python: str = sys.executable) -> int:
    options = parse_args(list(sys.argv[1:] if argv is None else argv))
    if options.errors:
        print(f"queue_tests: {'; '.join(options.errors)}. Options: --list, --part i/n, --budget SECONDS.")
        return 2
    print(SCOPE.format(n=len(options.files)))
    if not options.files:
        print("queue_tests: no changed file, the queue selects no backend test for it.")
        return 0
    full, reasons, run = unit_files(options.files)
    if full:
        print(f"queue_tests: the queue runs the whole backend suite: {full}. Not run here; CI runs it.")
        return 0
    for path, why in reasons.items():
        kept = "" if path in run else " (integration or chromium as a whole: not in the unit job)"
        print(f"- {path}: {why}{kept}")
    seconds = durations()
    total = weight(run, seconds)
    print(
        f"queue_tests: {len(reasons)} selected, {len(run)} in the unit job, "
        f"about {total:.0f} s of one worker ({workers()} worker(s) here)."
    )
    if options.listing or not run:
        return 0
    if options.part is not None:
        from tests.sharding import shard_files

        index, parts = options.part
        run = sorted(shard_files(run, seconds, index, parts))
        print(f"queue_tests: part {index}/{parts}: {len(run)} file(s), about {weight(run, seconds):.0f} s.")
        if not run:
            return 0
    elif 0 < options.budget < total:
        parts = math.ceil(total / options.budget)
        print(
            f"queue_tests: the selection is over the {options.budget:.0f} s budget: run it in {parts} parts, "
            f'args ["--part", "1/{parts}"] to ["--part", "{parts}/{parts}"] (or ["--budget", "0"] for one call). '
            "Nothing run."
        )
        return 3
    env = {**os.environ, "PYTEST_STALL_SECONDS": os.environ.get("PYTEST_STALL_SECONDS") or STALL_SECONDS}
    return subprocess.run(pytest_argv(python, run), check=False, env=env).returncode


if __name__ == "__main__":
    raise SystemExit(main())
