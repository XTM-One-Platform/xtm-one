# Copyright (c) 2025-2026 Filigran SAS
#
# This file is part of the XTM One Enterprise Edition ("EE") and is
# licensed under the XTM One Enterprise Edition License (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://github.com/XTM-One-Platform/xtm-one/blob/main/LICENSE
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.

"""Diff two workspace files, or merge three, without code_interpreter.

``sandbox_checkout_diff`` compares a checkout with the files it came from and
``sandbox_checkout_rebase`` merges a whole checkout onto a ref; neither compares
two files an agent holds side by side (a file at the merge base, the PR
branch's version, main's version). :func:`two_way` is ``diff -u`` of two texts
and :func:`three_way` the ``diff3 -m`` of three, on the same line merge as the
rebase (:func:`app.services.sandbox_workspace.rebase.merge_text`), so a merged
file is what ``merge_github_branch(resolutions=[{workspace_path}])`` takes.
"""

from __future__ import annotations

import difflib
from dataclasses import dataclass

from app.services.sandbox_workspace import structured
from app.services.sandbox_workspace.rebase import merge_text

#: Characters of one diff before it is cut.
MAX_DIFF_CHARS = 30_000
#: A diff line longer than this shows its head and counts the rest.
LONG_LINE_CHARS = 400
#: Context lines around each hunk: the default and the ceiling.
DEFAULT_CONTEXT_LINES = 3
MAX_CONTEXT_LINES = 10
#: Conflict markers located by line in the answer.
MAX_LISTED_CONFLICTS = 30
CONFLICT_START = "<<<<<<< "
CONFLICT_END = ">>>>>>> "


def clamp_context(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int | str):
        return DEFAULT_CONTEXT_LINES
    try:
        return max(0, min(int(value), MAX_CONTEXT_LINES))
    except ValueError:
        return DEFAULT_CONTEXT_LINES


def _bounded(line: str) -> str:
    body = line.rstrip("\r\n")
    end = line[len(body) :]
    sign, text = body[:1], body[1:]
    if len(text) <= LONG_LINE_CHARS:
        return line if end else line + "\n\\ No newline at end of file\n"
    return f"{sign}{text[:LONG_LINE_CHARS]} ... [+{len(text) - LONG_LINE_CHARS:,} chars]{end or chr(10)}"


def _endings(text: str) -> str:
    crlf = text.count("\r\n")
    lf = text.count("\n") - crlf
    if crlf and lf:
        return f"mixed ({crlf:,} CRLF, {lf:,} LF)"
    return "CRLF" if crlf else "LF"


def line_endings_only(before: str, after: str) -> str | None:
    """``CRLF -> LF`` when *before* and *after* differ by their line endings alone, else ``None``."""
    if before == after or before.replace("\r\n", "\n") != after.replace("\r\n", "\n"):
        return None
    return f"{_endings(before)} -> {_endings(after)}"


def two_way(from_name: str, before: str, to_name: str, after: str, *, context: int = DEFAULT_CONTEXT_LINES) -> str:
    """The unified diff of *before* (``from_name``) to *after* (``to_name``), long lines and the whole bounded.

    Texts that differ by their line endings alone are said so in one line, never
    a whole-file diff of identical-looking lines.
    """
    endings = line_endings_only(before, after)
    if endings is not None:
        return f"{from_name} and {to_name} differ only by their line endings: {endings}.\n"
    lines = difflib.unified_diff(
        before.splitlines(keepends=True),
        after.splitlines(keepends=True),
        fromfile=from_name,
        tofile=to_name,
        n=context,
    )
    text = "".join(_bounded(line) for line in lines)
    if not text:
        return f"{from_name} and {to_name} have the same content.\n"
    if len(text) > MAX_DIFF_CHARS:
        return text[:MAX_DIFF_CHARS] + f"\n... [diff cut: {len(text) - MAX_DIFF_CHARS:,} more characters]\n"
    return text


def changed_lines(before: str, after: str) -> tuple[int, int]:
    """Lines added and removed from *before* to *after*, as a diffstat counts them."""
    added = removed = 0
    for line in difflib.unified_diff(before.splitlines(), after.splitlines(), n=0, lineterm=""):
        if line.startswith("+") and not line.startswith("+++"):
            added += 1
        elif line.startswith("-") and not line.startswith("---"):
            removed += 1
    return added, removed


@dataclass(frozen=True)
class ThreeWay:
    """A three-way merge: the merged text, its conflict count and the line of each conflict's first marker."""

    text: str
    conflicts: int
    marker_lines: tuple[int, ...]
    #: The structured format (JSON, YAML, TOML) a clean merge was parsed as, '' when not judged.
    validated: str = ""
    #: Why a merge without markers breaks its structured file (a parse error, a key written twice), '' when sound.
    defect: str = ""


def three_way(base: str, ours: str, theirs: str, *, ours_label: str, theirs_label: str) -> ThreeWay:
    """``diff3 -m``: *ours* and *theirs* merged from *base*, overlapping hunks between conflict markers.

    A clean merge of a structured file (by *ours_label*'s extension, or JSON by content) whose two sides
    parse is parsed again (#11986): :attr:`ThreeWay.defect` names a parse error or a duplicate key.
    """
    merged = merge_text(base, ours, theirs, ours_label=ours_label, theirs_label=theirs_label)
    markers = tuple(
        number
        for number, line in enumerate(merged.text.splitlines(), start=1)
        if line.startswith(CONFLICT_START + ours_label)
    )
    kind, defect = ("", "") if merged.conflicts else structured.merge_defect(ours_label, ours, theirs, merged.text)
    return ThreeWay(merged.text, merged.conflicts, markers, validated="" if defect else kind, defect=defect)


def conflict_hunks(text: str, marker_lines: tuple[int, ...], *, context: int = DEFAULT_CONTEXT_LINES) -> str:
    """Each conflict of a merged *text* with *context* lines around it, numbered as in the text.

    The answer of a merge written to a file: the hunks to settle, never the whole
    merged-against-ours diff (a lock file merged with one conflict answered every
    clean change of the other side). Bounded like :func:`two_way`.
    """
    lines = text.splitlines()
    blocks: list[str] = []
    for start in marker_lines:
        end = start
        while end < len(lines) and not lines[end - 1].startswith(CONFLICT_END):
            end += 1
        first, last = max(1, start - context), min(len(lines), end + context)
        numbered = "".join(f"{n:>6} |{_bounded(' ' + lines[n - 1] + chr(10))}" for n in range(first, last + 1))
        blocks.append(f"@@ lines {first}-{last} @@\n{numbered}")
    out = "".join(blocks)
    if len(out) > MAX_DIFF_CHARS:
        return out[:MAX_DIFF_CHARS] + f"\n... [hunks cut: {len(out) - MAX_DIFF_CHARS:,} more characters]\n"
    return out


def describe_markers(lines: tuple[int, ...]) -> str:
    """``lines 12, 40 and 3 more``: where the conflicts start in the merged text."""
    shown = ", ".join(str(n) for n in lines[:MAX_LISTED_CONFLICTS])
    more = f" and {len(lines) - MAX_LISTED_CONFLICTS} more" if len(lines) > MAX_LISTED_CONFLICTS else ""
    return f"line(s) {shown}{more}"
