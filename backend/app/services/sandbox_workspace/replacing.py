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

"""A regular-expression replace across workspace files: all or nothing.

Pure: no I/O. ``sandbox_replace_in_files`` (``builtin_tools/sandbox_workspace.py``)
selects the text files of a prefix and its globs (the selection of
``sandbox_grep``), applies the pattern to each here, and writes every changed
file back in ONE workspace commit only when the whole change set is within the
caps: a rename across a package, a moved import, an option renamed in forty
call sites, without a code_interpreter script or forty ``sandbox_edit_file``
calls. The answer is never the files: a diffstat per file, then for a dry run
a bounded diff, for a write each distinct line change once with its count
(:func:`distinct_changes`), since the write's diff repeats one hunk per site.

The replacement is Python's ``re`` template (``\\1``, ``\\g<name>``) with the
character escapes ``\\uXXXX``, ``\\UXXXXXXXX`` and ``\\xXX`` expanded first
(:func:`expand_escapes`: ``re`` templates have none, and a French ``\\u00a0:``
was refused as a bad escape); a ``max_replacements`` cap refuses a pattern
broader than meant before anything is written, and ``dry_run`` answers the same
report without writing. Matches whose replacement leaves a file as it was are
counted apart (:attr:`Plan.unchanged`), never reported as no match.
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass, field

from app.services.sandbox_workspace import editing

#: Replacements one call makes by default, and at most.
DEFAULT_MAX_REPLACEMENTS = 200
MAX_REPLACEMENTS = 5_000
#: Files one call changes, at most.
MAX_FILES = 200
#: Characters of the diffs shown together; the rest counted by file.
MAX_DIFFS_CHARS = 12_000
#: Diffstat lines shown; the rest counted in one line (#460: 141 files answered 26k characters).
MAX_STAT_LINES = 40
#: Characters of the distinct line changes a written replace answers (a dry run keeps the diffs):
#: one run made 18 replaces whose diffs were 160k characters, 59% of its tool results.
MAX_WRITE_CHANGES_CHARS = 4_000
#: Characters of one side of a distinct line change; the rest counted.
MAX_CHANGE_LINE_CHARS = 160


#: A character escape of a replacement, after an even run of backslashes (``\\\\u0041`` is a literal backslash then ``u0041``).
_ESCAPE = re.compile(r"(?<!\\)((?:\\\\)*)\\(u[0-9a-fA-F]{4}|U[0-9a-fA-F]{8}|x[0-9a-fA-F]{2})")


class ReplaceError(ValueError):
    """A replace that cannot apply; ``str()`` says which and why, for the model."""


@dataclass(frozen=True)
class FileChange:
    path: str
    before: str
    after: str
    replacements: int


@dataclass
class Plan:
    changes: list[FileChange] = field(default_factory=list)
    files_scanned: int = 0
    #: Files the pattern matched where the replacement left the text as it was: (path, matches).
    unchanged: list[tuple[str, int]] = field(default_factory=list)

    @property
    def replacements(self) -> int:
        return sum(c.replacements for c in self.changes)


def clamp_max(max_replacements: object) -> int:
    """The cap of one call: the default when unset or not an integer; 1 to :data:`MAX_REPLACEMENTS`."""
    if max_replacements is None or isinstance(max_replacements, bool):
        return DEFAULT_MAX_REPLACEMENTS
    try:
        return max(1, min(int(max_replacements), MAX_REPLACEMENTS))
    except (TypeError, ValueError):
        return DEFAULT_MAX_REPLACEMENTS


def expand_escapes(template: str) -> str:
    """*template* with ``\\uXXXX``, ``\\UXXXXXXXX`` and ``\\xXX`` replaced by their character.

    An escape preceded by an odd run of backslashes is a literal backslash and is
    kept; a character that is itself a backslash is written ``\\\\`` so the ``re``
    template reads it as one. A code point past U+10FFFF is :class:`ReplaceError`.
    """

    def one(match: re.Match[str]) -> str:
        code = int(match.group(2)[1:], 16)
        if code > 0x10FFFF:
            raise ReplaceError(f"\\{match.group(2)} is not a Unicode code point (at most \\U0010FFFF)")
        char = chr(code)
        return match.group(1) + ("\\\\" if char == "\\" else char)

    return _ESCAPE.sub(one, template)


def check_template(replacement: object) -> str:
    """*replacement* within the snippet cap with its character escapes expanded, else :class:`ReplaceError`.

    A template that names a group the pattern does not have fails on the first
    match, in :func:`plan`, before anything is written.
    """
    if not isinstance(replacement, str):
        raise ReplaceError("replacement must be a string (empty deletes each match)")
    if len(replacement) > editing.MAX_SNIPPET_CHARS:
        raise ReplaceError(f"replacement is at most {editing.MAX_SNIPPET_CHARS:,} characters")
    return expand_escapes(replacement)


def plan(texts: list[tuple[str, str]], regex: re.Pattern[str], replacement: str, *, cap: int) -> Plan:
    """The change set of *regex* over *texts* (path, text), or :class:`ReplaceError` past a cap."""
    out = Plan()
    for path, before in texts:
        out.files_scanned += 1
        try:
            after, n = regex.subn(replacement, before)
        except (re.error, IndexError) as exc:
            raise ReplaceError(
                f"replacement {replacement[:80]!r} cannot expand on a match in {path}: {exc}. Nothing was written."
            ) from exc
        if n == 0:
            continue
        if after == before:
            out.unchanged.append((path, n))
            continue
        out.changes.append(FileChange(path=path, before=before, after=after, replacements=n))
        if out.replacements > cap:
            raise ReplaceError(
                f"the pattern makes more than {cap:,} replacement(s) (over max_replacements, {len(out.changes)} file(s) "
                "so far): narrow the pattern, path_prefix or include_globs, or raise max_replacements. Nothing was written."
            )
        if len(out.changes) > MAX_FILES:
            raise ReplaceError(
                f"the pattern changes more than {MAX_FILES} files: narrow path_prefix or include_globs and run it in "
                "parts. Nothing was written."
            )
    return out


def report(changes: list[FileChange], display) -> tuple[str, str]:
    """The diffstat lines and the bounded diffs of *changes*, *display* naming each path."""
    stats: list[str] = []
    diffs: list[str] = []
    used = 0
    hidden: list[str] = []
    for change in changes:
        added, removed = editing.summarize(change.before, change.after)
        shown = display(change.path)
        if len(stats) < MAX_STAT_LINES:
            stats.append(f"- {shown}: {change.replacements} replacement(s), +{added} -{removed}")
        diff = editing.unified_diff(shown, change.before, change.after)
        if used + len(diff) <= MAX_DIFFS_CHARS:
            diffs.append(diff)
            used += len(diff)
        else:
            hidden.append(shown)
    if len(changes) > MAX_STAT_LINES:
        stats.append(
            f"- ... and {len(changes) - MAX_STAT_LINES} more file(s) changed (sandbox_checkout_status lists them)"
        )
    tail = ""
    if hidden:
        tail = f"\n... [diff of {len(hidden)} more file(s) not shown: {', '.join(hidden[:10])}; sandbox_checkout_diff or sandbox_read_file reads them]"
    return "\n".join(stats), "".join(diffs) + tail


def _clip(line: str) -> str:
    line = line.rstrip("\r\n")
    if len(line) <= MAX_CHANGE_LINE_CHARS:
        return line
    return f"{line[:MAX_CHANGE_LINE_CHARS]} ... [+{len(line) - MAX_CHANGE_LINE_CHARS:,} chars]"


def distinct_changes(changes: list[FileChange], *, max_chars: int = MAX_WRITE_CHANGES_CHARS) -> str:
    """The distinct line changes of *changes*, each once with its count and file count, bounded at *max_chars*.

    A written replace usually makes the same change many times (a rename, a moved
    import): its diff repeats one hunk per site. This answers each distinct
    removed-lines / added-lines pair once, most frequent first, so the model reads
    what the pattern did without the repeated context; the rest is counted and
    ``sandbox_checkout_diff`` reads the whole diff.
    """
    counts: dict[tuple[tuple[str, ...], tuple[str, ...]], int] = {}
    files: dict[tuple[tuple[str, ...], tuple[str, ...]], set[str]] = {}
    for change in changes:
        before = change.before.splitlines()
        after = change.after.splitlines()
        matcher = difflib.SequenceMatcher(a=before, b=after)
        for tag, i1, i2, j1, j2 in matcher.get_opcodes():
            if tag == "equal":
                continue
            key = (tuple(before[i1:i2]), tuple(after[j1:j2]))
            counts[key] = counts.get(key, 0) + 1
            files.setdefault(key, set()).add(change.path)
    ordered = sorted(counts, key=lambda k: (-counts[k], k))
    lines: list[str] = []
    used = 0
    shown = 0
    for key in ordered:
        removed, added = key
        block = [f"{counts[key]}x in {len(files[key])} file(s):"]
        block += [f"  - {_clip(line)}" for line in removed]
        block += [f"  + {_clip(line)}" for line in added]
        text = "\n".join(block)
        if shown and used + len(text) + 1 > max_chars:
            break
        lines.append(text)
        used += len(text) + 1
        shown += 1
    rest = len(ordered) - shown
    head = f"Distinct line changes: {len(ordered)}, most frequent first"
    tail = f"\n... [{rest} more distinct change(s); sandbox_checkout_diff reads the whole diff]" if rest else ""
    return head + "\n" + "\n".join(lines) + tail
