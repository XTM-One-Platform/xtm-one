#!/usr/bin/env python3
"""Type-check (mypy) the lines a change wrote and keep a file's older findings apart.

``sandbox_run_command(check="backend-types", affected=true)`` hands this
script the Python files a checkout changed under ``backend/``: an added file
as ``PATH``, a modified one as ``PATH:A-B,C-D``, the line ranges the change
wrote (``.xtm-one/checks.toml``, ``affected = "lines"``). Most of the backend
is outside the strict mypy gate, so a touched file carries findings main
already had; the answer must be about the change's own lines.

mypy runs once over the named files with the repository's configuration
(``pyproject.toml``; ``follow_imports = "silent"`` keeps imported modules
quiet). Each finding is then sorted by its line:

- on a changed line (or anywhere in a file named whole): the change's own,
  printed, and the check fails (exit 1);
- on an untouched line of a ranged file: pre-existing, counted per file and
  never failing the check;
- in a file the scope does not name: counted apart.

A ``[syntax]`` error on a line holding a ``# type:`` comment is named for
what it is: mypy reads such a comment (``# type: fail fast ...``) as a type
comment and fails to parse it, though Python parses the file
(``type_comment_causes``).

The first line states the scope: the files, their ranges, and that the
baseline is the file as checked out (the platform's selection line names the
commit). Without a file argument mypy runs as configured (CI's gate, the
extraction package) and its output passes through unchanged.

Usage, from ``backend/``::

    python -m scripts.types_changed app/x.py app/y.py:12-30,88-90
    python -m scripts.types_changed            # the configured gate, as CI

Exit 1 when a finding sits on the change's lines (or in a whole file), 2 when
mypy could not run, 0 otherwise.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from dataclasses import dataclass, field

from scripts.format_changed import lines_of, parse

CONFIG = ("--config-file", "pyproject.toml")
#: Files named from ``backend/`` map to their dotted module (``scripts/x.py`` is ``scripts.x``): ``scripts/`` has
#: no ``__init__.py``, and a test importing it otherwise makes mypy see one file under two module names.
NAMED = ("--explicit-package-bases",)
#: ``path:line[:col]: error|note|warning: message`` as mypy prints it.
_FINDING = re.compile(r"^(?P<path>[^:\n]+):(?P<line>\d+)(?::\d+)?: (?P<kind>error|note|warning): ")
#: A comment mypy parses as a type comment (PEP 484); ``# type: ignore`` is mypy's own and never a syntax cause.
_TYPE_COMMENT = re.compile(r"#\s*type:(?!\s*ignore\b)")


@dataclass
class Sorted:
    """mypy's findings split by where they sit against the change."""

    #: Lines of the change's own findings (errors and the notes that follow them), as mypy printed them.
    yours: list[str] = field(default_factory=list)
    #: Errors of the change's own lines.
    yours_errors: int = 0
    #: Pre-existing errors per file (untouched lines of a ranged file).
    preexisting: dict[str, int] = field(default_factory=dict)
    #: Errors in files the scope does not name.
    outside: int = 0


def scope_of(args: list[str]) -> tuple[dict[str, set[int] | None], list[str]]:
    """``({path: changed lines, or None for a whole file}, mypy options)`` of the script's arguments."""
    files: dict[str, set[int] | None] = {}
    options: list[str] = []
    for arg in args:
        if arg.startswith("-"):
            options.append(arg)
            continue
        path, ranges = parse(arg)
        files[os.path.normpath(path)] = lines_of(ranges) if ranges else None
    return files, options


def scope_line(files: dict[str, set[int] | None], args: list[str]) -> str:
    """The first line of the answer: what was checked against what."""
    shown = []
    for arg in args:
        if arg.startswith("-"):
            continue
        path, ranges = parse(arg)
        shown.append(f"{path} lines {','.join(ranges)}" if ranges else f"{path} whole")
    return (
        f"types_changed: scope {len(files)} file(s): {'; '.join(shown[:10])}"
        + (f"; +{len(shown) - 10} more" if len(shown) > 10 else "")
        + ". Baseline: each file as checked out, its untouched lines' findings counted as pre-existing."
    )


def sort_findings(output: str, files: dict[str, set[int] | None]) -> Sorted:
    """Sort mypy's *output* against the scope *files*; a note follows the error it explains."""
    out = Sorted()
    keep_notes = False
    for line in output.splitlines():
        found = _FINDING.match(line)
        if not found:
            continue
        path, number, kind = os.path.normpath(found.group("path")), int(found.group("line")), found.group("kind")
        if kind == "note":
            if keep_notes:
                out.yours.append(line)
            continue
        keep_notes = False
        if path not in files:
            out.outside += 1
            continue
        changed = files[path]
        if changed is None or number in changed:
            out.yours.append(line)
            out.yours_errors += 1
            keep_notes = True
        else:
            out.preexisting[path] = out.preexisting.get(path, 0) + 1
    return out


def type_comment_causes(output: str) -> list[str]:
    """One line per ``[syntax]`` finding of *output* whose source line holds a ``# type:`` comment.

    mypy parses ``# type: <text>`` as a PEP 484 type comment: prose after it (``# type: fail fast on ...``)
    is reported as a syntax error on that line while ``ast.parse`` accepts the file, which reads like a broken
    file. The line names the comment as the cause so the fix is a reworded comment, not a hunt.
    """
    causes: list[str] = []
    sources: dict[str, list[str]] = {}
    for line in output.splitlines():
        found = _FINDING.match(line)
        if not found or found.group("kind") != "error" or not line.rstrip().endswith("[syntax]"):
            continue
        path, number = found.group("path"), int(found.group("line"))
        if path not in sources:
            try:
                with open(path, encoding="utf-8") as handle:
                    sources[path] = handle.read().splitlines()
            except (OSError, UnicodeDecodeError):
                sources[path] = []
        text = sources[path][number - 1] if 0 < number <= len(sources[path]) else ""
        if _TYPE_COMMENT.search(text):
            causes.append(
                f"types_changed: {path}:{number} holds a '# type:' comment, which mypy parses as a type comment: "
                "that is the cause of its [syntax] error (Python parses the file). Reword the comment so 'type:' "
                "does not follow '#' directly."
            )
    return causes


def summary(parts: Sorted) -> str:
    """The closing line: the change's own errors, then what was kept apart."""
    verdict = f"{parts.yours_errors} error(s) on your lines" if parts.yours_errors else "no error on your lines"
    apart = []
    if parts.preexisting:
        total = sum(parts.preexisting.values())
        per_file = ", ".join(f"{p} {n}" for p, n in sorted(parts.preexisting.items())[:5])
        apart.append(f"{total} pre-existing on untouched lines, not yours ({per_file})")
    if parts.outside:
        apart.append(f"{parts.outside} in files outside the scope")
    return f"types_changed: {verdict}" + (f"; apart: {'; '.join(apart)}" if apart else "") + "."


def main(argv: list[str] | None = None, *, mypy: str = "mypy") -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    files, options = scope_of(args)
    if not files:
        # No file named: the configured gate, exactly as CI runs it.
        return subprocess.run([mypy, *CONFIG, *options], check=False).returncode
    print(scope_line(files, args))
    done = subprocess.run([mypy, *CONFIG, *NAMED, *options, *files], capture_output=True, text=True, check=False)
    if done.returncode not in (0, 1):
        sys.stdout.write(done.stdout)
        sys.stderr.write(done.stderr)
        for cause in type_comment_causes(done.stdout):
            print(cause)
        print("types_changed: mypy could not run (output above).")
        return 2
    parts = sort_findings(done.stdout, files)
    for line in parts.yours:
        print(line)
    for cause in type_comment_causes(done.stdout):
        print(cause)
    print(summary(parts))
    return 1 if parts.yours_errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
