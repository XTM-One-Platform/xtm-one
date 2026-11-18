#!/usr/bin/env python3
"""Check (or fix) the formatting of the lines a change wrote, never the drift around them.

``sandbox_run_command(check="backend-format", affected=true)`` hands this
script the files a checkout changed under ``backend/``: an added file as
``PATH``, a modified one as ``PATH:A-B,C-D``, the line ranges the change
wrote (``.xtm-one/checks.toml``, ``affected = "lines"``). main is not
format-clean (some 3,400 files drift), so the answer must be about the
change's own lines.

A ``PATH`` without a range (a new file, or one named explicitly) goes to
``ruff format --check --diff PATH`` (``ruff format PATH`` with ``--fix``),
whole, as before.

A ``PATH:A-B,...`` is formatted once through ``ruff format --stdin-filename
PATH`` (the repository's ``[tool.ruff.format]`` applies) and the formatter's
edits are compared with the source line by line (:func:`split`):

- an edit whose lines all sit in the ranges is the change's own: shown as a
  diff (exit 1) or, with ``--fix``, written;
- an edit spanning changed and untouched lines (a statement ruff joins or
  splits as a whole) is named with the ``PATH:X-Y`` that would accept it, and
  never written or counted as a finding;
- an edit on untouched lines only is reported apart as "pre-existing, not
  yours" and never written.

``ruff format --range`` is not used: it widens a range to the enclosing
statement and reformatted (or reported) drift the change never touched.
Ranges are inclusive (``8-10`` is lines 8, 9 and 10).

Usage, from ``backend/``::

    python -m scripts.format_changed app/x.py app/y.py:12-30,88-90
    python -m scripts.format_changed --fix app/y.py:12-30,88-90

Without ``--fix`` it writes nothing. ``--write`` is the same switch as
``--fix`` (``backend-format`` args ``["--write"]`` writes the change's lines in
one call, like ``backend-format-fix``); any other option is refused (exit 2)
rather than passed to ruff as a path. Exit 1 when the change's own lines (or a
whole file) would be reformatted (check mode), 2 when ruff could not run on
one or an option is unknown, 0 otherwise; no file argument does nothing.
"""

from __future__ import annotations

import difflib
import re
import subprocess
import sys
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

_RANGES = re.compile(r"^(?P<path>.+?):(?P<ranges>\d+-\d+(?:,\d+-\d+)*)$")
FIX = "--fix"
#: The switches that write: ``--write`` is what a caller of the check mode reaches for first.
WRITE_SWITCHES = frozenset({FIX, "--write"})

#: One edit of the formatter: source lines ``[i1, i2)`` become formatted lines ``[j1, j2)`` (0-based).
Edit = tuple[int, int, int, int]


def parse(arg: str) -> tuple[str, list[str]]:
    """``(path, ["A-B", ...])`` of one argument; no range for a whole file."""
    found = _RANGES.match(arg)
    if not found:
        return arg, []
    return found.group("path"), found.group("ranges").split(",")


def lines_of(ranges: list[str]) -> set[int]:
    """The 1-based line numbers the inclusive ranges name."""
    out: set[int] = set()
    for span in ranges:
        start, end = (int(x) for x in span.split("-", 1))
        out.update(range(start, end + 1))
    return out


def commands(args: list[str], ruff: str = "ruff", *, fix: bool = False) -> list[list[str]]:
    """The ruff calls of the whole files among *args*; a file with ranges is formatted through stdin instead."""
    base = [ruff, "format"] if fix else [ruff, "format", "--check", "--diff"]
    return [[*base, path] for path, ranges in map(parse, args) if not ranges]


@dataclass
class Split:
    """The formatter's edits of one file, sorted by whose lines they touch."""

    yours: list[Edit] = field(default_factory=list)
    enclosing: list[Edit] = field(default_factory=list)
    preexisting: list[Edit] = field(default_factory=list)


def _pieces(opcodes: Iterable[tuple[str, int, int, int, int]]) -> list[Edit]:
    """The non-equal opcodes; a same-size replace is split line by line, so one drifted line never drags its neighbours in."""
    out: list[Edit] = []
    for tag, i1, i2, j1, j2 in opcodes:
        if tag == "equal":
            continue
        if tag == "replace" and i2 - i1 == j2 - j1:
            out.extend((i1 + k, i1 + k + 1, j1 + k, j1 + k + 1) for k in range(i2 - i1))
        else:
            out.append((i1, i2, j1, j2))
    return out


def split(source: list[bytes], formatted: list[bytes], changed: set[int]) -> Split:
    """Sort the edits turning *source* into *formatted* by the 1-based *changed* lines they touch."""
    out = Split()
    matcher = difflib.SequenceMatcher(a=source, b=formatted, autojunk=False)
    for edit in _pieces(matcher.get_opcodes()):
        i1, i2 = edit[0], edit[1]
        if i1 == i2:  # an insertion belongs to the change when a line next to it does
            (out.yours if {i1, i1 + 1} & changed else out.preexisting).append(edit)
            continue
        touched = set(range(i1 + 1, i2 + 1))
        if touched <= changed:
            out.yours.append(edit)
        elif touched & changed:
            out.enclosing.append(edit)
        else:
            out.preexisting.append(edit)
    return out


def apply(source: list[bytes], formatted: list[bytes], edits: list[Edit]) -> list[bytes]:
    """*source* with only *edits* applied."""
    out: list[bytes] = []
    at = 0
    for i1, i2, j1, j2 in sorted(edits):
        out.extend(source[at:i1])
        out.extend(formatted[j1:j2])
        at = i2
    out.extend(source[at:])
    return out


#: The pre-existing ranges named per file; the rest is a count (a drifted module holds hundreds).
PREEXISTING_SHOWN = 8


def label(edits: list[Edit], limit: int | None = None) -> str:
    """The source lines *edits* touch, as ``A-B`` ranges (an insertion as ``after N``), the first *limit* named."""
    parts = []
    ordered = sorted(edits)
    for i1, i2, _, _ in ordered if limit is None else ordered[:limit]:
        parts.append(f"after {i1}" if i1 == i2 else (f"{i1 + 1}" if i2 == i1 + 1 else f"{i1 + 1}-{i2}"))
    if limit is not None and len(ordered) > limit:
        parts.append(f"and {len(ordered) - limit} more")
    return ", ".join(parts)


def _ruff_stdin(ruff: str, path: str, source: bytes) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run([ruff, "format", "--stdin-filename", path], input=source, capture_output=True, check=False)


def ranged(path: str, ranges: list[str], *, fix: bool, ruff: str = "ruff") -> tuple[int, Split]:
    """Check or fix the changed lines of *path*; ``(exit code, split)``, the report written to stdout."""
    target = Path(path)
    raw = target.read_bytes()
    done = _ruff_stdin(ruff, path, raw)
    if done.returncode != 0:
        sys.stderr.write(done.stderr.decode("utf-8", "replace"))
        return 2, Split()
    source, formatted = raw.splitlines(keepends=True), done.stdout.splitlines(keepends=True)
    parts = split(source, formatted, lines_of(ranges))
    mine = apply(source, formatted, parts.yours)
    code = 0
    if parts.yours and fix:
        target.write_bytes(b"".join(mine))
        print(f"{path}: {len(parts.yours)} edit(s) written on your lines {label(parts.yours)}.")
    elif parts.yours:
        diff = difflib.unified_diff(
            [line.decode("utf-8", "replace") for line in source],
            [line.decode("utf-8", "replace") for line in mine],
            fromfile=path,
            tofile=path,
        )
        sys.stdout.writelines(line if line.endswith("\n") else line + "\n" for line in diff)
        code = 1
    if parts.enclosing:
        print(
            f"{path}: lines {label(parts.enclosing)} reformat as one statement with your lines and untouched ones: "
            f"not {'written' if fix else 'counted'}; pass {_widen(path, parts.enclosing)} to backend-format-fix "
            "to format the whole statement."
        )
    if parts.preexisting:
        print(
            f"{path}: pre-existing, not yours: {len(parts.preexisting)} edit(s) on lines "
            f"{label(parts.preexisting, PREEXISTING_SHOWN)} (never written)."
        )
    return code, parts


def _widen(path: str, edits: list[Edit]) -> str:
    return f"{path}:" + ",".join(f"{i1 + 1}-{max(i2, i1 + 1)}" for i1, i2, _, _ in sorted(edits))


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    fix = any(a in WRITE_SWITCHES for a in args)
    args = [a for a in args if a not in WRITE_SWITCHES]
    unknown = [a for a in args if a.startswith("-")]
    if unknown:
        print(
            f"format_changed: unknown option(s) {', '.join(unknown)}: nothing ran. backend-format checks the change's "
            "lines and writes nothing; args ['--write'] (or the check backend-format-fix) writes them in place.",
            file=sys.stderr,
        )
        return 2
    worst = 0
    calls = commands(args, fix=fix)
    for call in calls:
        done = subprocess.run(call, capture_output=True, text=True, check=False)
        if done.stdout:
            sys.stdout.write(done.stdout)
        if done.returncode == 0:
            continue
        worst = max(worst, 1) if done.returncode == 1 and not fix else 2
        if done.stderr:
            sys.stderr.write(done.stderr)
    preexisting = enclosing = 0
    for path, ranges in map(parse, args):
        if not ranges:
            continue
        code, parts = ranged(path, ranges, fix=fix)
        worst = max(worst, code)
        preexisting += len(parts.preexisting)
        enclosing += len(parts.enclosing)
    if worst == 2:
        verdict = "ruff failed (stderr above)"
    elif fix:
        verdict = "applied on your lines (read sandbox_checkout_diff)"
    else:
        verdict = "would be reformatted (diff above)" if worst else "formatted"
    apart = ""
    if preexisting or enclosing:
        apart = f"; apart, not written: {preexisting} pre-existing, {enclosing} enclosing edit(s)"
    print(f"format_changed{' --fix' if fix else ''}: {len(args)} file(s): {verdict}{apart}.")
    return worst


if __name__ == "__main__":
    raise SystemExit(main())
