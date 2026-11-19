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

"""In-place edition of a workspace file: exact find/replace, all or nothing.

Pure: no I/O. ``sandbox_edit_file`` (``builtin_tools/sandbox_workspace.py``)
reads the file through the session, applies the edits here and writes the
result back only when every edit applied; the unified diff is what the model
reads back, never the file. The contract is ``edit_file_content``'s (the
conversation-file sibling): each ``find`` must exist and be unique unless
``replace_all``; the edits apply in order, each to the output of the previous.

An edit may name a line range instead of a ``find``: ``{start_line, end_line,
expect_first, expect_last?, replace}`` replaces lines ``start_line`` to
``end_line`` (1-based, inclusive, counted in the text as the previous edits left
it) with ``replace``; an empty ``replace`` deletes them. ``expect_first`` must
occur in the first line of the range and, for a range of several lines,
``expect_last`` in the last one, so a line number read before the file changed
is refused instead of cutting the wrong block. A long block (a 290-line JSX
function, a 110-line obsolete test) is replaced or removed in one edit without
copying it verbatim into a ``find``. A range end that is a blank line is named
with ``expect_first_blank`` / ``expect_last_blank`` instead of a text, which a
blank line cannot give.

An edit may also insert: ``{insert_after_line, expect_first?, replace}`` puts
``replace`` after line ``insert_after_line`` (0 for the top of the file) and
replaces nothing; ``expect_first`` (or ``expect_first_blank``) checks that line
as for a range, and is not asked for line 0.

An edit may move a block: ``{start_line, end_line, expect_first, expect_last?,
move_to_after_line, expect_target?, replace: ""}`` moves lines ``start_line``
to ``end_line`` after line ``move_to_after_line`` (0: the top of the file),
both numbered in the text before the move; the block is anchored as a range is,
the target line by ``expect_target`` (or ``expect_target_blank``), not asked for
line 0. A doc comment or a function is moved without copying it into a call.

A ``find`` that occurs several times may name the one to replace with
``occurrence`` (1-based, counted in the text as the previous edits left it;
``sandbox_edit_file`` only): the second of three identical blocks is changed
without widening the snippet into its neighbours or numbering lines that the
earlier edits of the call shift. ``occurrence`` past the count is refused with
the count.

An edit may take its replacement from a workspace file: ``replace_from`` (a
workspace path, ``sandbox_edit_file`` only) instead of ``replace``. The caller
reads each file named by :func:`replace_from_sources` and passes its text to
:func:`with_replace_from`; a section drafted in ``work/`` is appended or inserted
into a source file without re-sending its text.

The diff is a read-back, not a patch: a line longer than :data:`LONG_LINE_CHARS`
shows only what changed, with :data:`CHANGE_CONTEXT_CHARS` unchanged characters
around it and the rest counted, so a one-word edit of a 17 KB table row
does not echo the row three times.

A file with Windows line endings (every line ending is CRLF) is matched with
its endings normalised to LF, as the model reads and copies it, and written
back with CRLF (:func:`apply_edits`): a ``find`` copied from
``sandbox_read_file`` matches, and the file keeps its endings.
"""

from __future__ import annotations

import difflib
import os
import re
from collections.abc import Sequence
from dataclasses import dataclass
from dataclasses import replace as replace_fields

#: Edits per call, at most.
MAX_EDITS = 50
#: Characters of a ``find`` or ``replace`` snippet, at most.
MAX_SNIPPET_CHARS = 100_000
#: Characters of the diff shown; the rest counted.
MAX_DIFF_CHARS = 40_000
#: Characters of the diff sandbox_edit_file echoes: the model wrote the edit, the
#: read-back only proves where it landed; the rest is counted with how to read it.
EDIT_ECHO_CHARS = 4_000
#: A diff line longer than this (without its sign) is shortened in the read-back.
LONG_LINE_CHARS = 400
#: Unchanged characters kept on each side of the change in a long changed line.
CHANGE_CONTEXT_CHARS = 80
#: Characters a long unchanged (context) line keeps: it only says where the hunk is.
CONTEXT_LINE_CHARS = 120
#: A run of added lines with nothing removed (an insertion) longer than this is
#: echoed as its first INSERTION_HEAD and last INSERTION_TAIL lines, the rest
#: counted with the line range that reads it: the model wrote those lines.
MAX_INSERTION_LINES = 40
INSERTION_HEAD = 30
INSERTION_TAIL = 5
_HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")


class EditError(ValueError):
    """An edit that cannot apply; ``str()`` says which and why, for the model."""


@dataclass(frozen=True)
class Edit:
    find: str
    replace: str
    replace_all: bool = False
    start_line: int = 0
    """First line of a line-range edit (1-based); 0 for a find/replace edit."""
    end_line: int = 0
    expect_first: str = ""
    expect_last: str = ""
    first_blank: bool = False
    """The anchor line (first of a range, or the line of an insert) must be blank."""
    last_blank: bool = False
    insert_after: int = -1
    """Line an insert edit goes after (0: the top of the file); -1 for any other edit."""
    occurrence: int = 0
    """The occurrence of ``find`` to replace (1-based); 0: the find must be unique, or ``replace_all``."""
    move_after: int = -1
    """Line a moved range goes after (0: the top of the file); -1 for any other edit."""
    expect_target: str = ""
    target_blank: bool = False

    @property
    def is_move(self) -> bool:
        return self.move_after >= 0

    @property
    def is_range(self) -> bool:
        return self.start_line > 0 and not self.is_move

    @property
    def is_insert(self) -> bool:
        return self.insert_after >= 0


@dataclass(frozen=True)
class Applied:
    text: str
    replacements: list[int]
    """Occurrences replaced by each edit, in order."""


def parse_edits(raw: object, *, allow_ranges: bool = False) -> list[Edit]:
    """The validated edits; *allow_ranges* accepts line-range edits (``sandbox_edit_file`` only)."""
    if not isinstance(raw, list) or not raw:
        raise EditError("give edits as a non-empty list of {find, replace, replace_all?} objects")
    if len(raw) > MAX_EDITS:
        raise EditError(f"at most {MAX_EDITS} edits per call ({len(raw)} given)")
    edits: list[Edit] = []
    for i, item in enumerate(raw, start=1):
        if not isinstance(item, dict):
            raise EditError(f"edit {i} is not an object")
        if allow_ranges and "move_to_after_line" in item:
            edits.append(_parse_move(i, item))
            continue
        if allow_ranges and "insert_after_line" in item:
            edits.append(_parse_insert(i, item))
            continue
        if allow_ranges and ("start_line" in item or "end_line" in item):
            edits.append(_parse_range(i, item))
            continue
        find = item.get("find")
        replace = item.get("replace", "")
        if not isinstance(find, str) or not find:
            raise EditError(f"edit {i}: 'find' must be a non-empty string")
        if not isinstance(replace, str):
            raise EditError(f"edit {i}: 'replace' must be a string (empty to delete the found text)")
        if len(find) > MAX_SNIPPET_CHARS or len(replace) > MAX_SNIPPET_CHARS:
            raise EditError(f"edit {i}: a snippet is at most {MAX_SNIPPET_CHARS:,} characters")
        replace_all = bool(item.get("replace_all", False))
        occurrence = _parse_occurrence(i, item, replace_all) if allow_ranges else 0
        edits.append(Edit(find=find, replace=replace, replace_all=replace_all, occurrence=occurrence))
    return edits


def replace_from_sources(raw: object) -> list[tuple[int, str]]:
    """``(index, path)`` of each edit of *raw* whose replacement is a workspace file (``replace_from``).

    Raises :class:`EditError` for a ``replace_from`` that is not a path or that
    comes with a non-empty ``replace``. Anything else is left to :func:`parse_edits`.
    """
    if not isinstance(raw, list):
        return []
    sources: list[tuple[int, str]] = []
    for index, item in enumerate(raw):
        if not isinstance(item, dict) or item.get("replace_from") in (None, ""):
            continue
        source = item["replace_from"]
        if not isinstance(source, str) or not source.strip():
            raise EditError(f"edit {index + 1}: 'replace_from' must be a workspace path, e.g. work/section.md")
        if item.get("replace"):
            raise EditError(f"edit {index + 1}: give either 'replace' or 'replace_from', not both")
        sources.append((index, source.strip()))
    return sources


def with_replace_from(raw: Sequence[object], texts: dict[int, str]) -> list[object]:
    """*raw* with the ``replace`` of each edit indexed in *texts* set to that text, ``replace_from`` dropped."""
    out: list[object] = []
    for index, item in enumerate(raw):
        if index in texts and isinstance(item, dict):
            item = {key: value for key, value in item.items() if key != "replace_from"}
            item["replace"] = texts[index]
        out.append(item)
    return out


def _parse_occurrence(i: int, item: dict[str, object], replace_all: bool) -> int:
    if "occurrence" not in item or item["occurrence"] is None:
        return 0
    value = item["occurrence"]
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise EditError(f"edit {i}: 'occurrence' must be an integer >= 1 (1: the first occurrence of 'find')")
    if replace_all:
        raise EditError(f"edit {i}: give either 'occurrence' or 'replace_all', not both")
    return value


def _parse_range(i: int, item: dict) -> Edit:
    if item.get("find"):
        raise EditError(f"edit {i}: give either 'find' or 'start_line'/'end_line', not both")
    start, end = item.get("start_line"), item.get("end_line", item.get("start_line"))
    if not isinstance(start, int) or isinstance(start, bool) or not isinstance(end, int) or isinstance(end, bool):
        raise EditError(f"edit {i}: 'start_line' and 'end_line' must be integers")
    if start < 1 or end < start:
        raise EditError(f"edit {i}: lines {start}-{end} are not a range (1-based, end_line >= start_line)")
    replace = item.get("replace", "")
    if not isinstance(replace, str):
        raise EditError(f"edit {i}: 'replace' must be a string (empty to delete the lines)")
    first_blank = _flag(i, item, "expect_first_blank")
    last_blank = _flag(i, item, "expect_last_blank")
    first, last = item.get("expect_first", ""), item.get("expect_last", "")
    if not isinstance(first, str) or not isinstance(last, str):
        raise EditError(f"edit {i}: 'expect_first' and 'expect_last' must be strings")
    if not first.strip() and not first_blank:
        raise EditError(
            f"edit {i}: a line-range edit needs 'expect_first', text of line {start} as the file reads, "
            "so a stale line number is refused (expect_first_blank=true when that line is blank)"
        )
    if end > start and not last.strip() and not last_blank:
        raise EditError(
            f"edit {i}: a range of several lines needs 'expect_last', text of line {end} "
            "(expect_last_blank=true when that line is blank)"
        )
    if max(len(replace), len(first), len(last)) > MAX_SNIPPET_CHARS:
        raise EditError(f"edit {i}: a snippet is at most {MAX_SNIPPET_CHARS:,} characters")
    return Edit(
        find="",
        replace=replace,
        start_line=start,
        end_line=end,
        expect_first=first,
        expect_last=last,
        first_blank=first_blank,
        last_blank=last_blank and end > start,
    )


def _flag(i: int, item: dict, key: str) -> bool:
    value = item.get(key, False)
    if not isinstance(value, bool):
        raise EditError(f"edit {i}: '{key}' must be true or false")
    return value


def _parse_insert(i: int, item: dict) -> Edit:
    if item.get("find") or "start_line" in item or "end_line" in item:
        raise EditError(f"edit {i}: give one of 'find', 'start_line'/'end_line' or 'insert_after_line'")
    after = item.get("insert_after_line")
    if not isinstance(after, int) or isinstance(after, bool) or after < 0:
        raise EditError(f"edit {i}: 'insert_after_line' must be an integer >= 0 (0 inserts at the top of the file)")
    replace = item.get("replace", "")
    if not isinstance(replace, str) or not replace:
        raise EditError(f"edit {i}: an insert needs 'replace', the non-empty text to insert")
    blank = _flag(i, item, "expect_first_blank")
    first = item.get("expect_first", "")
    if not isinstance(first, str):
        raise EditError(f"edit {i}: 'expect_first' must be a string")
    if after > 0 and not first.strip() and not blank:
        raise EditError(
            f"edit {i}: an insert after line {after} needs 'expect_first', text of that line as the file reads "
            "(expect_first_blank=true when it is blank), so a stale line number is refused"
        )
    if max(len(replace), len(first)) > MAX_SNIPPET_CHARS:
        raise EditError(f"edit {i}: a snippet is at most {MAX_SNIPPET_CHARS:,} characters")
    return Edit(find="", replace=replace, expect_first=first, first_blank=blank and after > 0, insert_after=after)


def _parse_move(i: int, item: dict[str, object]) -> Edit:
    if item.get("find") or "insert_after_line" in item:
        raise EditError(
            f"edit {i}: a move takes 'start_line'/'end_line' and 'move_to_after_line', "
            "not 'find' or 'insert_after_line'"
        )
    if item.get("replace"):
        raise EditError(f"edit {i}: a move writes no text: leave 'replace' empty (the block is moved as it is)")
    if "start_line" not in item:
        raise EditError(f"edit {i}: a move needs 'start_line' (and 'end_line' for several lines), the block to move")
    block = _parse_range(i, {**item, "replace": ""})
    target = item.get("move_to_after_line")
    if not isinstance(target, int) or isinstance(target, bool) or target < 0:
        raise EditError(
            f"edit {i}: 'move_to_after_line' must be an integer >= 0 (0 moves the block to the top of the file)"
        )
    if block.start_line - 1 <= target <= block.end_line:
        raise EditError(
            f"edit {i}: move_to_after_line {target} is inside lines {block.start_line}-{block.end_line} or right "
            "before them: the block would not move. Nothing was written."
        )
    blank = _flag(i, item, "expect_target_blank")
    want = item.get("expect_target", "")
    if not isinstance(want, str):
        raise EditError(f"edit {i}: 'expect_target' must be a string")
    if target > 0 and not want.strip() and not blank:
        raise EditError(
            f"edit {i}: a move after line {target} needs 'expect_target', text of that line as the file reads "
            "(expect_target_blank=true when it is blank), so a stale line number is refused"
        )
    if len(want) > MAX_SNIPPET_CHARS:
        raise EditError(f"edit {i}: a snippet is at most {MAX_SNIPPET_CHARS:,} characters")
    return replace_fields(block, move_after=target, expect_target=want, target_blank=blank and target > 0)


def _check_line(i: int, label: str, want: str, blank: bool, line: str, number: int) -> None:
    """Refuse when *line* (number *number*) does not hold *want*, or is not blank when *blank* asks it."""
    shown = _preview(line.rstrip("\r\n"))
    if blank and line.strip():
        raise EditError(
            f"edit {i}: line {number} is {shown!r}, not blank as {label}_blank says: the line numbers are stale, "
            "read the lines again. Nothing was written."
        )
    if want and want.replace("\r\n", "\n").rstrip("\n") not in line:
        raise EditError(
            f"edit {i}: line {number} is {shown!r}, it does not contain {label} "
            f"{_preview(want)!r}: the line numbers are stale, read the lines again. Nothing was written."
        )


def _apply_insert(i: int, text: str, edit: Edit) -> str:
    lines = text.splitlines(keepends=True)
    after = edit.insert_after
    if after > len(lines):
        raise EditError(
            f"edit {i}: insert_after_line {after} is past the end ({len(lines)} lines). Nothing was written."
        )
    insert = edit.replace
    if after:
        anchor = lines[after - 1]
        _check_line(i, "expect_first", edit.expect_first, edit.first_blank, anchor, after)
        if not anchor.endswith(("\n", "\r")):
            # The anchor is the last line and has no ending: the insert starts on a new line.
            lines[after - 1] = anchor + "\n"
    if not insert.endswith(("\n", "\r")):
        insert += "\n"
    return "".join(lines[:after]) + insert + "".join(lines[after:])


def _apply_range(i: int, text: str, edit: Edit) -> str:
    # Numbered as ``reading.window`` numbers them (``splitlines``), so the numbers sandbox_read_file shows apply.
    lines = text.splitlines(keepends=True)
    if edit.end_line > len(lines):
        raise EditError(
            f"edit {i}: lines {edit.start_line}-{edit.end_line} run past the end ({len(lines)} lines). "
            "Nothing was written."
        )
    first, last = lines[edit.start_line - 1], lines[edit.end_line - 1]
    _check_line(i, "expect_first", edit.expect_first, edit.first_blank, first, edit.start_line)
    _check_line(i, "expect_last", edit.expect_last, edit.last_blank, last, edit.end_line)
    replace = edit.replace
    ending = last[len(last.rstrip("\r\n")) :]
    if replace and ending and not replace.endswith(("\n", "\r")):
        replace += ending
    return "".join(lines[: edit.start_line - 1]) + replace + "".join(lines[edit.end_line :])


def _apply_move(i: int, text: str, edit: Edit) -> str:
    """*text* with lines ``start_line``..``end_line`` moved after line ``move_after``, both numbered before the move."""
    lines = text.splitlines(keepends=True)
    start, end, target = edit.start_line, edit.end_line, edit.move_after
    if max(end, target) > len(lines):
        raise EditError(
            f"edit {i}: the move of lines {start}-{end} after line {target} runs past the end ({len(lines)} lines). "
            "Nothing was written."
        )
    _check_line(i, "expect_first", edit.expect_first, edit.first_blank, lines[start - 1], start)
    _check_line(i, "expect_last", edit.expect_last, edit.last_blank, lines[end - 1], end)
    if target:
        _check_line(i, "expect_target", edit.expect_target, edit.target_blank, lines[target - 1], target)
    for at in (end - 1, target - 1):
        # A line without an ending (the last of the file) gets one before it is followed by another.
        if at >= 0 and not lines[at].endswith(("\n", "\r")):
            lines[at] += "\n"
    block = lines[start - 1 : end]
    rest = lines[: start - 1] + lines[end:]
    at = target if target < start else target - len(block)
    return "".join(rest[:at] + block + rest[at:])


def _line_of(text: str, index: int) -> int:
    return text.count("\n", 0, index) + 1


def _preview(snippet: str) -> str:
    one = snippet.replace("\n", "\\n")
    return one if len(one) <= 60 else one[:57] + "..."


def is_crlf(text: str) -> bool:
    """Whether every line ending of *text* is CRLF (at least one): a Windows-ended file."""
    lf = text.count("\n")
    return lf > 0 and text.count("\r\n") == lf


def apply_edits(text: str, edits: list[Edit], *, read_with: str = "sandbox_read_file") -> Applied:
    """*text* with every edit applied in order, or :class:`EditError` naming the first that cannot.

    A CRLF file (:func:`is_crlf`) is edited with LF endings, the snippets'
    CRLF normalised the same way, and its endings restored after.
    *read_with* names the tool a refused ``find`` should be copied from
    (``get_github_file`` for a repository file edited by ``create_github_commit``).
    """
    if is_crlf(text):
        lf_edits = [
            replace_fields(e, find=e.find.replace("\r\n", "\n"), replace=e.replace.replace("\r\n", "\n")) for e in edits
        ]
        applied = _apply(text.replace("\r\n", "\n"), lf_edits, read_with)
        return Applied(text=applied.text.replace("\n", "\r\n"), replacements=applied.replacements)
    return _apply(text, edits, read_with)


def _apply(text: str, edits: list[Edit], read_with: str) -> Applied:
    counts: list[int] = []
    for i, edit in enumerate(edits, start=1):
        if edit.is_move:
            text = _apply_move(i, text, edit)
            counts.append(1)
            continue
        if edit.is_range:
            text = _apply_range(i, text, edit)
            counts.append(1)
            continue
        if edit.is_insert:
            text = _apply_insert(i, text, edit)
            counts.append(1)
            continue
        n = text.count(edit.find)
        if n == 0:
            raise EditError(
                f"edit {i}: 'find' text not found ({_preview(edit.find)!r}). Copy the snippet exactly from "
                f"{read_with} (whitespace included); nothing was written."
            )
        if edit.occurrence:
            text = _replace_occurrence(i, text, edit, n)
            counts.append(1)
            continue
        if n > 1 and not edit.replace_all:
            first = _line_of(text, text.find(edit.find))
            raise EditError(
                f"edit {i}: 'find' text occurs {n} times (first at line {first}); widen the snippet or set "
                "replace_all=true. Nothing was written."
            )
        text = text.replace(edit.find, edit.replace) if edit.replace_all else text.replace(edit.find, edit.replace, 1)
        counts.append(n if edit.replace_all else 1)
    return Applied(text=text, replacements=counts)


def _replace_occurrence(i: int, text: str, edit: Edit, count: int) -> str:
    """*text* with occurrence ``edit.occurrence`` of ``edit.find`` replaced (*count* occurrences, non-overlapping)."""
    if edit.occurrence > count:
        raise EditError(
            f"edit {i}: occurrence {edit.occurrence} asked but 'find' text occurs {count} time(s). Nothing was written."
        )
    at = -len(edit.find)
    for _ in range(edit.occurrence):
        at = text.find(edit.find, at + len(edit.find))
    return text[:at] + edit.replace + text[at + len(edit.find) :]


def unified_diff(path: str, before: str, after: str, *, max_chars: int = MAX_DIFF_CHARS, rest_hint: str = "") -> str:
    """The diff the model reads back, bounded at *max_chars* (*rest_hint* says how to read the rest)."""
    lines = list(
        difflib.unified_diff(
            before.splitlines(keepends=True),
            after.splitlines(keepends=True),
            fromfile=f"a/{path}",
            tofile=f"b/{path}",
            n=3,
        )
    )
    lines = _compact(lines)
    text = "".join(line if line.endswith("\n") else line + "\n\\ No newline at end of file\n" for line in lines)
    if len(text) > max_chars:
        cut = text.rfind("\n", 0, max_chars) + 1 or max_chars
        hint = f"; {rest_hint}" if rest_hint else ""
        return text[:cut] + f"... [diff cut: {len(text) - cut:,} more characters{hint}]\n"
    return text


def _split(line: str) -> tuple[str, str, str]:
    """A diff line as its sign, its text and its line ending."""
    end = "\r\n" if line.endswith("\r\n") else "\n" if line.endswith("\n") else ""
    return line[:1], line[1 : len(line) - len(end)], end


def _head(text: str, keep: int) -> str:
    if len(text) <= keep:
        return text
    return f"{text[:keep]} ... [+{len(text) - keep:,} chars]"


def _shorten(line: str, keep: int) -> str:
    """An unpaired diff line: kept whole when short, else its first *keep* characters and the rest counted."""
    sign, text, end = _split(line)
    if len(text) <= LONG_LINE_CHARS:
        return line
    return f"{sign}{_head(text, keep)}{end}"


def _around(text: str, prefix: int, suffix: int) -> str:
    """The changed part of *text* (between its *prefix* and *suffix* shared with the other side), with context."""
    start = max(0, prefix - CHANGE_CONTEXT_CHARS)
    stop = min(len(text), len(text) - suffix + CHANGE_CONTEXT_CHARS)
    shown = _head(text[start:stop], LONG_LINE_CHARS)
    head = f"[{start:,} unchanged chars] ..." if start else ""
    tail = f"... [{len(text) - stop:,} unchanged chars]" if stop < len(text) else ""
    return head + shown + tail


def _pair(old: str, new: str) -> tuple[str, str]:
    """A removed line and the added line that replaces it, each shortened to what changed when one is long."""
    old_sign, a, old_end = _split(old)
    new_sign, b, new_end = _split(new)
    if max(len(a), len(b)) <= LONG_LINE_CHARS:
        return old, new
    prefix = len(os.path.commonprefix([a, b]))
    suffix = min(len(os.path.commonprefix([a[::-1], b[::-1]])), min(len(a), len(b)) - prefix)
    return old_sign + _around(a, prefix, suffix) + old_end, new_sign + _around(b, prefix, suffix) + new_end


def _compact(lines: list[str]) -> list[str]:
    """*lines* of a unified diff with every long line shortened; short lines and headers untouched.

    In a hunk a run of removed lines followed by a run of added lines is paired
    line by line: a long pair shows the changed part only, an unpaired long line
    its head, a long context line a shorter head.
    """
    out = lines[:2]
    body = lines[2:]
    i = 0
    new_line = 1  # the line number, in the new text, of the next added or context line
    while i < len(body):
        sign = body[i][:1]
        if sign == "@":
            hunk = _HUNK.match(body[i])
            new_line = int(hunk.group(1)) if hunk else new_line
            out.append(body[i])
            i += 1
            continue
        if sign == "+":
            k = i
            while k < len(body) and body[k][:1] == "+":
                k += 1
            out.extend(_insertion(body[i:k], new_line))
            new_line += k - i
            i = k
            continue
        if sign != "-":
            out.append(_shorten(body[i], CONTEXT_LINE_CHARS if sign == " " else LONG_LINE_CHARS))
            new_line += sign == " "
            i += 1
            continue
        j = i
        while j < len(body) and body[j][:1] == "-":
            j += 1
        k = j
        while k < len(body) and body[k][:1] == "+":
            k += 1
        removed = [_shorten(line, LONG_LINE_CHARS) for line in body[i:j]]
        added = [_shorten(line, LONG_LINE_CHARS) for line in body[j:k]]
        for n in range(min(j - i, k - j)):
            removed[n], added[n] = _pair(body[i + n], body[j + n])
        out.extend(removed + added)
        new_line += k - j
        i = k
    return out


def _insertion(added: list[str], first: int) -> list[str]:
    """A run of added lines with nothing removed, the line *first* of the new text onward: bounded past MAX_INSERTION_LINES."""
    shown = [_shorten(line, LONG_LINE_CHARS) for line in added]
    if len(shown) <= MAX_INSERTION_LINES:
        return shown
    hidden = len(shown) - INSERTION_HEAD - INSERTION_TAIL
    start = first + INSERTION_HEAD
    note = f"+... [{hidden:,} more added lines, {start:,} to {start + hidden - 1:,}: sandbox_read_file reads them]\n"
    return [*shown[:INSERTION_HEAD], note, *shown[-INSERTION_TAIL:]]


def summarize(before: str, after: str) -> tuple[int, int]:
    """Lines added and removed, as a diffstat would count them."""
    added = removed = 0
    for line in difflib.unified_diff(before.splitlines(), after.splitlines(), n=0, lineterm=""):
        if line.startswith("+") and not line.startswith("+++"):
            added += 1
        elif line.startswith("-") and not line.startswith("---"):
            removed += 1
    return added, removed
