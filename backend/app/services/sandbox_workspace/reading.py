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

"""Reading a workspace file without echoing it: a window, a search, an outline.

Pure: no I/O. The ``sandbox_read_file``, ``sandbox_grep``, ``sandbox_outline``
and ``sandbox_read_symbol`` (one definition whole, by name) tools
(``builtin_tools/sandbox_workspace.py``) read the
bytes through the :class:`~app.services.sandbox_workspace.session.WorkspaceSession`
and shape them here, so a 300 KB source file costs the model the lines it
asked for, a few matches or a list of definitions, never the whole file.
Every result is bounded: lines per window, matches per search, characters
per result; what is left is counted, never silently dropped.
"""

from __future__ import annotations

import ast
import codecs
import json
import re
from dataclasses import dataclass, field
from fnmatch import fnmatch

from app.services.sandbox_workspace.manifest import format_size

#: Lines a window shows by default, and at most.
DEFAULT_WINDOW_LINES = 200
MAX_WINDOW_LINES = 2000
#: Matches a search returns by default, and at most.
DEFAULT_MAX_MATCHES = 50
MAX_MATCHES = 500
#: Context lines around a match, at most.
MAX_CONTEXT_LINES = 5
#: Characters a line shows in a window or a match by default, and at most (the rest counted).
#: ``max_line_chars`` raises it for a long line: a Markdown table row, a minified JSON or YAML
#: line, a generated file.
DEFAULT_LINE_CHARS = 400
MAX_LINE_CHARS = 20_000
#: Characters a whole result may hold (lines are dropped past it, and counted).
MAX_RESULT_CHARS = 60_000
#: Characters a window holds when no ``max_lines`` was asked: 200 long lines of a spec reached the
#: 60,000 cap for a range of sixty; an explicit ``max_lines`` keeps :data:`MAX_RESULT_CHARS`.
DEFAULT_WINDOW_CHARS = 16_000
#: Characters the matches of a search hold by default (``max_chars`` lifts it up to :data:`MAX_RESULT_CHARS`):
#: 60 matches of a JSON log line at 400 characters were about 25 KB for one fact.
DEFAULT_GREP_CHARS = 12_000
#: Keys a matched JSON log line is reduced to when ``compact_json`` is set without ``json_fields``.
DEFAULT_JSON_FIELDS = ("timestamp", "level", "message")
#: The key a grouped search reads each matched line's time from (``group_by``).
DEFAULT_TIME_FIELD = "timestamp"
#: Groups a grouped search keeps; the rest are counted in one trailer.
MAX_GROUPS = 500
#: Keys ``json_fields`` names at most, and characters a key is at most.
MAX_JSON_FIELDS = 20
MAX_JSON_FIELD_CHARS = 200
#: Files a search opens at most, and bytes it decodes at most, per call (32 MB stopped an
#: unscoped search of one backend tree after about 1,100 files; 4,000 files stopped a whole
#: xtm-one checkout before frontend/src and licensing, so two readers of a renamed path were
#: missed). The call's time budget is the bound that stops a larger search first.
MAX_GREP_FILES = 25_000
MAX_GREP_BYTES = 128 * 1024 * 1024
#: A file larger than this is not searched (a CI job log of a few MB is).
MAX_GREP_FILE_BYTES = 32 * 1024 * 1024
#: Blobs a search reads at once, and bytes a batch holds at most (a larger file is read alone).
GREP_READ_FILES = 8
GREP_READ_BYTES = 16 * 1024 * 1024
#: Files one read of an installed tree fetches at once: each read is a helper run and a tar round trip
#: in the command container, so a package of a few thousand small files is read in a few batches.
INSTALLED_READ_FILES = 400
#: A file larger than this is not outlined nor written whole.
MAX_TEXT_FILE_BYTES = 4 * 1024 * 1024
#: Suffixes a search skips without reading the file: images, fonts, archives, media, compiled objects.
BINARY_SUFFIXES = frozenset(
    (
        *(".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".bmp", ".tif", ".tiff", ".avif"),
        *(".woff", ".woff2", ".ttf", ".otf", ".eot", ".pdf"),
        *(".zip", ".gz", ".tgz", ".bz2", ".xz", ".zst", ".7z", ".tar", ".jar", ".whl"),
        *(".mp3", ".mp4", ".mov", ".webm", ".wav", ".ogg", ".flac"),
        *(".so", ".dylib", ".dll", ".exe", ".o", ".a", ".pyc", ".class", ".wasm"),
        *(".sqlite", ".db", ".parquet", ".pkl", ".npy"),
    )
)
#: Directories a search skips unless the prefix names them (installed packages, checkout records).
SKIPPED_DIRS = (".venv", ".xtm")
#: Definitions an outline lists at most.
MAX_OUTLINE_ENTRIES = 400


#: Bytes of a file's head :func:`is_text` reads.
TEXT_HEAD_BYTES = 8192


def is_text(data: bytes) -> bool:
    """Whether *data* reads as UTF-8 text: no NUL byte in its head, decodable.

    The head is decoded incrementally with ``final`` left false, so a multi-byte
    character cut at its end, after any of its bytes, is not taken for a binary
    file; an invalid byte inside the head still is.
    """
    head = data[:TEXT_HEAD_BYTES]
    if b"\x00" in head:
        return False
    decoder = codecs.getincrementaldecoder("utf-8")()
    try:
        decoder.decode(head, final=False)
    except UnicodeDecodeError:
        return False
    return True


def decode(data: bytes) -> str:
    return data.decode("utf-8", errors="replace")


def clamp_line_chars(max_line_chars: object) -> int:
    """Characters shown per line: the default when unset, not an integer or below 1; capped at the maximum."""
    if max_line_chars is None or isinstance(max_line_chars, bool):
        return DEFAULT_LINE_CHARS
    try:
        chars = int(max_line_chars)
    except (TypeError, ValueError):
        return DEFAULT_LINE_CHARS
    if chars < 1:
        return DEFAULT_LINE_CHARS
    return min(chars, MAX_LINE_CHARS)


def _cut(line: str, limit: int = DEFAULT_LINE_CHARS) -> str:
    if len(line) <= limit:
        return line
    rest = len(line) - limit
    if limit < MAX_LINE_CHARS:
        return f"{line[:limit]} ... [+{rest} chars: raise max_line_chars (at most {MAX_LINE_CHARS:,}) to read the line whole]"
    return f"{line[:limit]} ... [+{rest} chars past the {MAX_LINE_CHARS:,}-character line cap]"


def _bounded(lines: list[str], limit: int = MAX_RESULT_CHARS) -> tuple[list[str], int]:
    """*lines* up to *limit* characters; the count of lines dropped."""
    kept: list[str] = []
    total = 0
    for i, line in enumerate(lines):
        total += len(line) + 1
        if total > limit:
            return kept, len(lines) - i
        kept.append(line)
    return kept, 0


# -- window --------------------------------------------------------------------


@dataclass(frozen=True)
class Window:
    """Lines ``first``..``last`` of a text (1-based, inclusive) out of ``total``."""

    lines: list[str]
    first: int
    last: int
    total: int


def clamp_window(offset_line: object, max_lines: object) -> tuple[int, int]:
    try:
        first = max(1, int(offset_line))
    except (TypeError, ValueError):
        first = 1
    try:
        count = max(1, min(int(max_lines), MAX_WINDOW_LINES))
    except (TypeError, ValueError):
        count = DEFAULT_WINDOW_LINES
    return first, count


def window(text: str, offset_line: int = 1, max_lines: int = DEFAULT_WINDOW_LINES) -> Window:
    """The lines of *text* from *offset_line*, at most *max_lines* of them."""
    first, count = clamp_window(offset_line, max_lines)
    lines = text.splitlines()
    total = len(lines)
    if first > total:
        return Window(lines=[], first=first, last=total, total=total)
    chunk = lines[first - 1 : first - 1 + count]
    return Window(lines=chunk, first=first, last=first - 1 + len(chunk), total=total)


def format_window(
    path: str,
    win: Window,
    *,
    size: int,
    line_chars: int = DEFAULT_LINE_CHARS,
    max_chars: int = MAX_RESULT_CHARS,
) -> str:
    """The window as the model reads it: a header, numbered lines (each up to *line_chars*), what remains.

    The numbered lines hold at most *max_chars* characters; the lines past it are left for the
    next window, whose ``offset_line`` the trailer names.
    """
    if win.total == 0:
        return f"{path} is empty (0 lines, {size} bytes)."
    if not win.lines:
        return f"{path} has {win.total} lines: offset_line {win.first} is past its end."
    width = len(str(win.last))
    numbered = [f"{n:>{width}} | {_cut(line, line_chars)}" for n, line in enumerate(win.lines, start=win.first)]
    limit = max(1, min(max_chars, MAX_RESULT_CHARS))
    body, dropped = _bounded(numbered, limit)
    if not body:
        # One line longer than the cap still shows: a window is never empty.
        body, dropped = numbered[:1], len(numbered) - 1
    last = win.last - dropped
    header = f"{path}: lines {win.first}-{last} of {win.total} ({size} bytes)"
    tail = []
    if dropped and limit < MAX_RESULT_CHARS:
        tail.append(
            f"[window cut at {limit:,} characters: {win.total} lines in total; pass max_lines to read up to "
            f"{MAX_RESULT_CHARS:,} characters, or sandbox_grep / sandbox_read_symbol for the part you need]"
        )
    elif dropped:
        tail.append(f"[{dropped} line(s) of the window dropped: the result is capped at {MAX_RESULT_CHARS:,} characters; ask fewer lines]")
    if last < win.total:
        tail.append(f"[{win.total - last} more line(s): continue with offset_line={last + 1}]")
    return "\n".join([header, *body, *tail])


# -- grep ----------------------------------------------------------------------


@dataclass(frozen=True)
class Match:
    path: str
    line: int
    text: str
    context: list[str] = field(default_factory=list)
    #: With context lines: the last line (0-based) the block shows, and whether it continues the previous block.
    last: int = -1
    joined: bool = False


@dataclass
class GrepResult:
    matches: list[Match] = field(default_factory=list)
    more: int = 0
    files_scanned: int = 0
    files_matched: int = 0
    files_skipped: int = 0
    bytes_scanned: int = 0
    stopped: str = ""
    #: The paths a budget left unsearched, in order.
    left: list[str] = field(default_factory=list)
    #: The paths whose content was missing from storage even through the current manifest.
    unreadable: list[str] = field(default_factory=list)
    #: With a :class:`Grouping`: group value -> file -> its matched lines.
    groups: dict[str, dict[str, GroupStats]] = field(default_factory=dict)
    #: With a :class:`Grouping`: the files that matched, in search order (the table's columns).
    group_files: list[str] = field(default_factory=list)
    #: With a :class:`Grouping`: per file, the matched lines without a JSON object or without the group key.
    ungrouped: dict[str, int] = field(default_factory=dict)
    #: With a :class:`Grouping`: matched lines of the group values past :data:`MAX_GROUPS`.
    groups_cut: int = 0


@dataclass
class GroupStats:
    """The matched lines of one group in one file: how many, and the first and last (line, time) in log order."""

    count: int = 0
    first_line: int = 0
    first_time: str = ""
    last_line: int = 0
    last_time: str = ""

    def add(self, line: int, time: str) -> None:
        if not self.count:
            self.first_line, self.first_time = line, time
        self.count += 1
        self.last_line, self.last_time = line, time


@dataclass(frozen=True)
class Grouping:
    """``sandbox_grep(group_by=...)``: matched JSON log lines counted per value of *key*, per file."""

    key: str
    time_field: str = DEFAULT_TIME_FIELD


def unread_binary(path: str) -> bool:
    """Whether a search skips *path* without reading it: a suffix of :data:`BINARY_SUFFIXES`."""
    name = path.rsplit("/", 1)[-1].lower()
    dot = name.rfind(".")
    return dot > 0 and name[dot:] in BINARY_SUFFIXES


def plan_grep(sized: list[tuple[str, int]], out: GrepResult, *, batch_files: int = GREP_READ_FILES) -> list[list[str]]:
    """The read batches of a search over *sized* (``(path, size)`` in path order).

    Files past :data:`MAX_GREP_FILE_BYTES` or with a binary suffix are skipped
    and counted; the rest are taken in order until :data:`MAX_GREP_FILES` or
    :data:`MAX_GREP_BYTES`, then recorded in ``out.left`` with ``out.stopped``
    saying which budget ran out. Each batch holds at most
    *batch_files* files (default :data:`GREP_READ_FILES`) and
    :data:`GREP_READ_BYTES` bytes (a larger file is a batch of its own), so the
    reads of a batch run at once with a bounded memory.
    """
    batches: list[list[str]] = []
    batch: list[str] = []
    batch_bytes = taken = total = 0
    for path, size in sized:
        if out.stopped:
            out.left.append(path)
            continue
        if size > MAX_GREP_FILE_BYTES or unread_binary(path):
            out.files_skipped += 1
            continue
        if taken >= MAX_GREP_FILES:
            out.stopped = f"{MAX_GREP_FILES} files opened"
            out.left.append(path)
            continue
        if total + size > MAX_GREP_BYTES:
            out.stopped = f"{format_size(MAX_GREP_BYTES)} read"
            out.left.append(path)
            continue
        if batch and (len(batch) >= batch_files or batch_bytes + size > GREP_READ_BYTES):
            batches.append(batch)
            batch, batch_bytes = [], 0
        batch.append(path)
        batch_bytes += size
        taken += 1
        total += size
    if batch:
        batches.append(batch)
    return batches


def format_left(left: list[str], *, shown: int = 5) -> str:
    """What a stopped search did not reach: the count, the directories that hold most of it, the first path."""
    counts: dict[str, int] = {}
    for path in left:
        folder = path.rsplit("/", 1)[0] if "/" in path else ""
        counts[folder] = counts.get(folder, 0) + 1
    top = sorted(counts.items(), key=lambda item: (-item[1], item[0]))[:shown]
    where = ", ".join(f"{_folder(folder)} ({n})" for folder, n in top)
    rest = len(counts) - len(top)
    more = f" and {rest} more director{'y' if rest == 1 else 'ies'}" if rest else ""
    return f"{len(left)} file(s) not searched, in {where}{more}; the first is workspace/{left[0]}"


def _folder(path: str) -> str:
    return f"workspace/{path}/" if path else "workspace/"


class PatternError(ValueError):
    """A pattern that does not compile; ``str()`` says why, for the model."""


def compile_pattern(pattern: object, *, ignore_case: bool = False) -> re.Pattern[str]:
    if not isinstance(pattern, str) or not pattern:
        raise PatternError("give a regular expression to search for")
    if len(pattern) > 1000:
        raise PatternError("a search pattern is at most 1000 characters")
    flags = re.IGNORECASE if ignore_case else 0
    try:
        return re.compile(pattern, flags)
    except re.error as exc:
        hoisted = _hoist_global_flags(pattern) if "global flags not at the start" in str(exc) else None
        if hoisted is not None:
            try:
                return re.compile(hoisted, flags)
            except re.error:
                pass
        hint = " (pass ignore_case=true instead of an inline (?i))" if "flag" in str(exc) else ""
        raise PatternError(f"{pattern[:80]!r} does not compile as a regular expression: {exc}{hint}") from exc


# A global inline flag group such as ``(?i)``, unescaped (an even run of
# backslashes before it), not a scoped ``(?i:...)`` group.
_GLOBAL_FLAG_GROUP = re.compile(r"(?<!\\)((?:\\\\)*)\(\?([aiLmsux]+)\)")


def _hoist_global_flags(pattern: str) -> str | None:
    """Move every global inline flag group of ``pattern`` to its start.

    Python refuses ``foo(?i)bar`` since 3.11, where older versions applied the
    flag to the whole expression; a pattern written that way (a habit of grep
    and of other regex engines) is read with that meaning instead of refused.
    None when the pattern holds no such group.
    """
    found: list[str] = []

    def _drop(match: re.Match[str]) -> str:
        found.append(match.group(2))
        return match.group(1)

    rest = _GLOBAL_FLAG_GROUP.sub(_drop, pattern)
    if not found:
        return None
    letters = "".join(sorted(set("".join(found))))
    return f"(?{letters}){rest}"


def clamp_matches(max_matches: object) -> int:
    try:
        return max(1, min(int(max_matches), MAX_MATCHES))
    except (TypeError, ValueError):
        return DEFAULT_MAX_MATCHES


def clamp_context(context_lines: object) -> int:
    try:
        return max(0, min(int(context_lines), MAX_CONTEXT_LINES))
    except (TypeError, ValueError):
        return 0


def selected(path: str, prefix: str, include_globs: list[str]) -> bool:
    """Whether a search over *prefix* opens *path*: inside it, not a skipped directory, matching a glob when given."""
    if prefix and path != prefix and not path.startswith(prefix + "/"):
        return False
    rest = path[len(prefix) :].lstrip("/") if prefix else path
    if any(seg in SKIPPED_DIRS for seg in rest.split("/")[:-1]):
        return False
    if include_globs:
        # A glob is read against the file name, the path from path_prefix and the workspace path, so
        # ``sub/*.py`` under ``src`` and ``src/sub/*.py`` pick the same files (sandbox_replace_in_files too).
        name = path.rsplit("/", 1)[-1]
        return any(fnmatch(name, g) or fnmatch(rest, g) or fnmatch(path, g) for g in include_globs)
    return True


def clamp_grep_chars(max_chars: object) -> int:
    """Characters the matches of a search hold: the default when unset or not an integer; 1,000 to the result cap."""
    if max_chars is None or isinstance(max_chars, bool):
        return DEFAULT_GREP_CHARS
    try:
        return max(1000, min(int(max_chars), MAX_RESULT_CHARS))
    except (TypeError, ValueError):
        return DEFAULT_GREP_CHARS


def clamp_json_fields(json_fields: object, compact_json: object = False) -> tuple[str, ...]:
    """The keys a matched JSON line is reduced to: *json_fields* when given, else the default set under *compact_json*."""
    if isinstance(json_fields, str):
        json_fields = [json_fields]
    if isinstance(json_fields, list | tuple):
        keys = [str(k).strip()[:MAX_JSON_FIELD_CHARS] for k in json_fields if str(k).strip()]
        if keys:
            return tuple(dict.fromkeys(keys))[:MAX_JSON_FIELDS]
    return DEFAULT_JSON_FIELDS if compact_json is True else ()


#: Characters before the ``{`` of a JSON log line that are kept as its prefix (a CI runner's timestamp).
_JSON_PREFIX_CHARS = 64


def _json_value(value: object) -> str:
    if value is None:
        return "-"
    if isinstance(value, str):
        return value.replace("\n", "\\n")
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def json_object(line: str) -> tuple[str, dict] | None:
    """``(prefix, object)`` when *line* holds a JSON object after at most a short prefix, else ``None``."""
    start = line.find("{")
    if start < 0 or start > _JSON_PREFIX_CHARS or not line.rstrip().endswith("}"):
        return None
    try:
        parsed = json.loads(line[start:])
    except ValueError:
        return None
    if not isinstance(parsed, dict):
        return None
    return line[:start].strip(), parsed


def json_get(obj: dict, key: str) -> object:
    """The value of *key* in *obj*: the key as written, else a dotted path (``attributes.queue``); ``None`` when absent."""
    if key in obj:
        return obj[key]
    value: object = obj
    for part in key.split("."):
        if not isinstance(value, dict) or part not in value:
            return None
        value = value[part]
    return value


def compact_json_line(line: str, fields: tuple[str, ...]) -> str | None:
    """*line* reduced to the values of *fields* joined by `` | `` when it holds a JSON object, else ``None``.

    A short prefix before the object (the timestamp a CI runner writes on every
    log line) is kept in front; a key the object lacks is written ``-``; a
    dotted key reads a nested value.
    """
    found = json_object(line)
    if found is None:
        return None
    prefix, parsed = found
    values = " | ".join(_json_value(json_get(parsed, key)) for key in fields)
    return f"{prefix} {values}" if prefix else values


def clamp_grouping(group_by: object, time_field: object = None) -> Grouping | None:
    """The grouping of a search: ``None`` without a *group_by* key."""
    key = str(group_by).strip()[:MAX_JSON_FIELD_CHARS] if isinstance(group_by, str) else ""
    if not key:
        return None
    time_key = str(time_field).strip()[:MAX_JSON_FIELD_CHARS] if isinstance(time_field, str) else ""
    return Grouping(key=key, time_field=time_key or DEFAULT_TIME_FIELD)


def _group_line(path: str, number: int, line: str, grouping: Grouping, out: GrepResult) -> None:
    """Count one matched line under its group value in *path*, or as ungrouped."""
    if path not in out.group_files:
        out.group_files.append(path)
    found = json_object(line)
    value = json_get(found[1], grouping.key) if found else None
    if found is None or value is None:
        out.ungrouped[path] = out.ungrouped.get(path, 0) + 1
        return
    name = _short(_json_value(value), MAX_JSON_FIELD_CHARS)
    if name not in out.groups and len(out.groups) >= MAX_GROUPS:
        out.groups_cut += 1
        return
    stamp = json_get(found[1], grouping.time_field)
    time = _json_value(stamp) if stamp is not None else found[0]
    out.groups.setdefault(name, {}).setdefault(path, GroupStats()).add(number, _short(time or "-", 64))


def _short(text: str, limit: int) -> str:
    return text if len(text) <= limit else f"{text[:limit]}..."


def _cell(stats: GroupStats | None) -> str:
    if stats is None:
        return "-"
    if stats.count == 1:
        return f"1 (L{stats.first_line} {stats.first_time})"
    return f"{stats.count} (L{stats.first_line} {stats.first_time} .. L{stats.last_line} {stats.last_time})"


def format_groups(
    pattern: str, grouping: Grouping, result: GrepResult, *, prefix: str, max_chars: int = DEFAULT_GREP_CHARS
) -> str:
    """A grouped search as the model reads it: one row per group value, one cell per file side by side.

    Each cell is the count of matched lines of that group in that file and the
    first and last of them (line number and time, in log order); ``-`` when the
    file has none. Rows are sorted by group value and bounded by *max_chars*,
    the rows cut counted.
    """
    where = f" under {prefix}/" if prefix else " in the workspace"
    files = result.group_files
    if not files:
        return f"No match for {pattern!r}{where} ({result.files_scanned} file(s) searched)."
    total = sum(s.count for per_file in result.groups.values() for s in per_file.values())
    body = [
        f"{total} matched line(s) for {pattern!r}{where} grouped by {grouping.key!r} into {len(result.groups)} group(s), "
        f"time from {grouping.time_field!r}; {len(files)} file(s) matched, {result.files_scanned} searched.",
        "Columns: " + "; ".join(f"[{i}] {p}" for i, p in enumerate(files, 1)),
        "Each cell: count (first line and time .. last line and time); - when that file has none.",
    ]
    rows = [
        f"{name}: " + " | ".join(f"[{i}] {_cell(per_file.get(p))}" for i, p in enumerate(files, 1))
        for name, per_file in sorted(result.groups.items())
    ]
    shown, cut = _within([[row] for row in rows], max_chars)
    body.extend(shown)
    if cut:
        body.append(f"[{cut} group(s) not shown: the rows are capped at {max_chars:,} characters; raise max_chars or narrow the pattern]")
    if result.groups_cut:
        body.append(f"[{result.groups_cut} matched line(s) of groups past the first {MAX_GROUPS} not counted: narrow the pattern]")
    loose = [f"[{files.index(p) + 1}] {n}" for p, n in result.ungrouped.items()]
    if loose:
        body.append(f"[matched lines with no JSON object or no {grouping.key!r}: {', '.join(loose)}]")
    if result.files_skipped:
        body.append(f"[{result.files_skipped} binary or oversized file(s) skipped]")
    if result.unreadable:
        body.append(f"[{len(result.unreadable)} file(s) not searched, their content missing from storage: workspace/{result.unreadable[0]}]")
    if result.stopped:
        left = f"; {format_left(result.left)}" if result.left else ""
        body.append(f"[search stopped early: {result.stopped}{left}. Narrow path_prefix or include_globs to search the rest]")
    return "\n".join(body)


def _shown(line: str, line_chars: int, fields: tuple[str, ...], regex: re.Pattern[str] | None = None) -> str:
    if fields:
        compact = compact_json_line(line, fields)
        if compact is not None:
            return _cut(compact, line_chars)
    if regex is not None and len(line) > line_chars:
        return _match_window(line, line_chars, regex)
    return _cut(line, line_chars)


def _match_window(line: str, limit: int, regex: re.Pattern[str]) -> str:
    """A matched line longer than *limit*: the window of *limit* characters centred on the first match."""
    found = regex.search(line)
    if found is None or found.end() <= limit:
        return _cut(line, limit)
    start = max(0, min(found.start() - (limit - (found.end() - found.start())) // 2, len(line) - limit))
    end = min(len(line), start + limit)
    before, after = start, len(line) - end
    tail = f" ... [+{after} chars]" if after else ""
    return (
        f"[{before} chars] ... {line[start:end]}{tail} "
        f"[window around the match; raise max_line_chars (at most {MAX_LINE_CHARS:,}) to read the line whole]"
    )


def grep_text(
    path: str,
    text: str,
    regex: re.Pattern[str],
    *,
    context: int,
    budget: int,
    out: GrepResult,
    line_chars: int = DEFAULT_LINE_CHARS,
    fields: tuple[str, ...] = (),
    grouping: Grouping | None = None,
) -> bool:
    """Append the matches of *regex* in *text* to *out*; ``True`` when the file matched.

    Each line is shown up to *line_chars*; with *fields*, a line holding a JSON
    object is shown as the values of those keys only (the regex still runs on
    the whole line). With *grouping*, no line is kept: each matched line is
    counted under its group value for this file (:func:`format_groups`).
    """
    lines = text.splitlines()
    hit = False
    for i, line in enumerate(lines):
        if not regex.search(line):
            continue
        hit = True
        if grouping is not None:
            _group_line(path, i + 1, line, grouping, out)
            continue
        if len(out.matches) >= budget:
            out.more += 1
            continue
        text = _shown(line, line_chars, fields, regex)
        if not context:
            out.matches.append(Match(path=path, line=i + 1, text=text))
            continue
        lo, hi = max(0, i - context), min(len(lines), i + context + 1)
        previous = out.matches[-1] if out.matches else None
        joined = previous is not None and previous.path == path and previous.last >= 0 and lo <= previous.last + 1
        if joined:
            # The previous match's block overlaps or touches this one: its lines are not printed again, the blocks join.
            lo = previous.last + 1
        ctx = [
            f"{path}:{n + 1}{':' if n == i or regex.search(lines[n]) else '-'} "
            f"{text if n == i else _shown(lines[n], line_chars, fields, regex)}"
            for n in range(lo, hi)
        ]
        last = max(hi - 1, previous.last) if joined and previous is not None else hi - 1
        out.matches.append(Match(path=path, line=i + 1, text=text, context=ctx, last=last, joined=joined))
    return hit


def _within(blocks: list[list[str]], limit: int) -> tuple[list[str], int]:
    """The lines of the whole *blocks* (one per match) that fit in *limit* characters; the count of blocks cut."""
    kept: list[str] = []
    total = 0
    for i, block in enumerate(blocks):
        size = sum(len(line) + 1 for line in block)
        if kept and total + size > limit:
            return kept, len(blocks) - i
        if not kept and size > limit:
            # A first match alone past the budget is still shown, cut at a line boundary.
            lines, _dropped = _bounded(block, limit)
            return lines, len(blocks) - 1
        kept.extend(block)
        total += size
    return kept, 0


def format_grep(pattern: str, result: GrepResult, *, prefix: str, max_chars: int = DEFAULT_GREP_CHARS) -> str:
    """The search as the model reads it: a header, the matches up to *max_chars* characters, what was not shown."""
    where = f" under {prefix}/" if prefix else " in the workspace"
    if not result.matches:
        body = [f"No match for {pattern!r}{where} ({result.files_scanned} file(s) searched)."]
    else:
        blocks: list[list[str]] = []
        for m in result.matches:
            if m.joined and blocks and blocks[-1] and blocks[-1][-1] == "--":
                # A block that overlaps or touches the previous one: no separator, each line printed once.
                blocks[-1] = blocks[-1][:-1]
            if m.context or m.last >= 0:
                blocks.append([*m.context, "--"])
            else:
                blocks.append([f"{m.path}:{m.line}: {m.text}"])
        while blocks and not blocks[-1]:
            blocks.pop()
        if blocks and blocks[-1][-1] == "--":
            blocks[-1] = blocks[-1][:-1]
        shown, cut = _within(blocks, max_chars)
        if shown and shown[-1] == "--":
            shown.pop()
        body = [
            f"{len(result.matches)} match(es) for {pattern!r}{where} in {result.files_matched} file(s), "
            f"{result.files_scanned} searched:",
            *shown,
        ]
        if cut:
            lift = (
                f"raise max_chars (at most {MAX_RESULT_CHARS:,})"
                if max_chars < MAX_RESULT_CHARS
                else f"the {MAX_RESULT_CHARS:,}-character cap is reached"
            )
            body.append(
                f"[{cut} match(es) not shown: the matches are capped at {max_chars:,} characters; {lift}, "
                "pass json_fields for a JSON log, or narrow the search]"
            )
    if result.more:
        body.append(f"[{result.more} more match(es) not shown: raise max_matches or narrow path_prefix / include_globs]")
    if result.files_skipped:
        body.append(f"[{result.files_skipped} binary or oversized file(s) skipped]")
    if result.unreadable:
        first = ", ".join(f"workspace/{p}" for p in result.unreadable[:5])
        more = f" and {len(result.unreadable) - 5} more" if len(result.unreadable) > 5 else ""
        body.append(
            f"[{len(result.unreadable)} file(s) not searched, their content missing from storage: {first}{more}; "
            "write them again or search them alone]"
        )
    if result.stopped:
        left = f"; {format_left(result.left)}" if result.left else ""
        body.append(f"[search stopped early: {result.stopped}{left}. Narrow path_prefix or include_globs to search the rest]")
    return "\n".join(body)


# -- outline -------------------------------------------------------------------


@dataclass(frozen=True)
class Definition:
    line: int
    kind: str
    name: str
    depth: int = 0


#: Declaration lines per file extension: a bounded scan, chosen by the file's
#: extension (a property of the file), never by a tool or language name in a
#: generic layer. Python goes through ``ast`` instead.
_DECLARATIONS: dict[str, list[tuple[str, re.Pattern[str]]]] = {
    "ts": [
        ("class", re.compile(r"^\s*(?:export\s+)?(?:default\s+)?(?:abstract\s+)?class\s+([A-Za-z_$][\w$]*)")),
        ("interface", re.compile(r"^\s*(?:export\s+)?interface\s+([A-Za-z_$][\w$]*)")),
        ("type", re.compile(r"^\s*(?:export\s+)?type\s+([A-Za-z_$][\w$]*)\s*[<=]")),
        ("enum", re.compile(r"^\s*(?:export\s+)?(?:const\s+)?enum\s+([A-Za-z_$][\w$]*)")),
        ("function", re.compile(r"^\s*(?:export\s+)?(?:default\s+)?(?:async\s+)?function\s*\*?\s*([A-Za-z_$][\w$]*)")),
        (
            "const",
            re.compile(
                r"^(?:export\s+)?(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*(?::[^=]+)?=\s*(?:async\s*)?(?:(?:<[^>]*>\s*)?\([^)]*\)\s*(?::[^=]+)?=>|function\b|(?:React\.)?(?:memo|forwardRef)\s*(?:<.*>)?\()"
            ),
        ),
    ],
    "go": [
        ("func", re.compile(r"^func\s+(?:\([^)]*\)\s*)?([A-Za-z_]\w*)")),
        ("type", re.compile(r"^type\s+([A-Za-z_]\w*)")),
    ],
    "rs": [
        ("fn", re.compile(r"^\s*(?:pub(?:\([^)]*\))?\s+)?(?:async\s+)?(?:unsafe\s+)?fn\s+([A-Za-z_]\w*)")),
        ("struct", re.compile(r"^\s*(?:pub(?:\([^)]*\))?\s+)?struct\s+([A-Za-z_]\w*)")),
        ("enum", re.compile(r"^\s*(?:pub(?:\([^)]*\))?\s+)?enum\s+([A-Za-z_]\w*)")),
        ("trait", re.compile(r"^\s*(?:pub(?:\([^)]*\))?\s+)?trait\s+([A-Za-z_]\w*)")),
        ("impl", re.compile(r"^\s*impl(?:<[^>]*>)?\s+(?:[A-Za-z_]\w*\s+for\s+)?([A-Za-z_]\w*)")),
    ],
    "java": [
        (
            "class",
            re.compile(
                r"^\s*(?:public|private|protected|static|final|abstract|\s)*(?:class|interface|enum|record)\s+([A-Za-z_]\w*)"
            ),
        ),
        (
            "method",
            re.compile(
                r"^\s{2,}(?:public|private|protected|static|final|abstract|synchronized|\s)*[\w<>\[\],\s]+\s+([A-Za-z_]\w*)\s*\([^;]*$"
            ),
        ),
    ],
    "sql": [
        (
            "create",
            re.compile(
                r"^\s*create\s+(?:or\s+replace\s+)?(?:table|view|function|index|type|materialized\s+view)\s+(?:if\s+not\s+exists\s+)?([\w.\"]+)",
                re.IGNORECASE,
            ),
        ),
    ],
    "md": [("heading", re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$"))],
    # A GraphQL schema (SDL): its top-level definitions, so one input or type of a
    # large schema file is read whole by name (the fields of an input at a ref)
    # without the server's introspection, often disabled in production.
    "graphql": [
        ("extend type", re.compile(r"^extend\s+type\s+([A-Za-z_]\w*)")),
        ("extend input", re.compile(r"^extend\s+input\s+([A-Za-z_]\w*)")),
        ("type", re.compile(r"^type\s+([A-Za-z_]\w*)")),
        ("input", re.compile(r"^input\s+([A-Za-z_]\w*)")),
        ("interface", re.compile(r"^interface\s+([A-Za-z_]\w*)")),
        ("enum", re.compile(r"^enum\s+([A-Za-z_]\w*)")),
        ("union", re.compile(r"^union\s+([A-Za-z_]\w*)")),
        ("scalar", re.compile(r"^scalar\s+([A-Za-z_]\w*)")),
        ("directive", re.compile(r"^directive\s+@([A-Za-z_]\w*)")),
    ],
}
#: Declarations whose header spans several lines: an opening line (``const Row = ({``) listed only
#: when a closing line (``}: Props) => {``) follows within :data:`_HEADER_LOOKAHEAD` lines, before
#: any other line at column 0. A parenthesised expression (``const el = (`` and JSX) never closes so.
_OPEN_HEADERS: dict[str, list[tuple[str, re.Pattern[str], re.Pattern[str]]]] = {
    "ts": [
        (
            "const",
            re.compile(
                r"^(?:export\s+)?(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*(?::[^=]+)?=\s*(?:async\s*)?(?:<[^>]*>\s*)?\([^)]*$"
            ),
            re.compile(r"^\s*[}\])]?[^()]*\)\s*(?::[^=]+)?=>"),
        )
    ],
}
_HEADER_LOOKAHEAD = 40
_ALIASES = {
    "tsx": "ts",
    "js": "ts",
    "jsx": "ts",
    "mjs": "ts",
    "cjs": "ts",
    "kt": "java",
    "scala": "java",
    "mdx": "md",
    "markdown": "md",
    "gql": "graphql",
    "graphqls": "graphql",
}
_PYTHON = {"py", "pyi"}


def outline_kinds() -> tuple[str, ...]:
    """The extensions an outline is defined for (for the tool description)."""
    return tuple(sorted({*_PYTHON, *_DECLARATIONS, *_ALIASES}))


def _python_outline(text: str) -> list[Definition]:
    tree = ast.parse(text)
    out: list[Definition] = []

    def walk(nodes, depth: int) -> None:
        for node in nodes:
            if isinstance(node, ast.ClassDef):
                out.append(Definition(node.lineno, "class", node.name, depth))
                walk(node.body, depth + 1)
            elif isinstance(node, ast.AsyncFunctionDef | ast.FunctionDef):
                kind = "async def" if isinstance(node, ast.AsyncFunctionDef) else "def"
                out.append(Definition(node.lineno, kind, node.name, depth))
                if depth == 0:
                    walk(node.body, depth + 1)  # nested closures of a module function (a tool factory)
            elif depth == 0 and isinstance(node, ast.Assign | ast.AnnAssign):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                for target in targets:
                    if isinstance(target, ast.Name):
                        kind = "const" if target.id.lstrip("_").isupper() else "var"
                        out.append(Definition(node.lineno, kind, target.id, depth))
            elif depth == 0 and isinstance(node, ast.If | ast.Try):
                walk(getattr(node, "body", []), depth)
    walk(tree.body, 0)
    return out


def _closes_header(lines: list[str], start: int, closing: re.Pattern[str]) -> bool:
    """Whether a closing line follows *start* within the lookahead, before another column-0 line."""
    for line in lines[start + 1 : start + 1 + _HEADER_LOOKAHEAD]:
        if closing.match(line):
            return True
        if line[:1] and not line[:1].isspace() and line[:1] not in "})]":
            return False
    return False


def _scan_outline(
    text: str,
    rules: list[tuple[str, re.Pattern[str]]],
    *,
    markdown: bool,
    headers: list[tuple[str, re.Pattern[str], re.Pattern[str]]] | None = None,
) -> list[Definition]:
    out: list[Definition] = []
    lines = text.splitlines()
    for i, line in enumerate(lines):
        for kind, regex in rules:
            m = regex.match(line)
            if m:
                if markdown:
                    out.append(Definition(i + 1, "heading", m.group(2).strip(), len(m.group(1)) - 1))
                else:
                    out.append(Definition(i + 1, kind, m.group(1), 1 if line[:1].isspace() else 0))
                break
        else:
            for kind, opening, closing in headers or ():
                m = opening.match(line)
                if m and _closes_header(lines, i, closing):
                    out.append(Definition(i + 1, kind, m.group(1), 0))
                    break
    return out


def outline(path: str, text: str) -> list[Definition] | None:
    """The definitions of *text*, by *path*'s extension; ``None`` when no outline is defined for it."""
    ext = path.rsplit(".", 1)[-1].casefold() if "." in path.rsplit("/", 1)[-1] else ""
    ext = _ALIASES.get(ext, ext)
    if ext in _PYTHON:
        try:
            return _python_outline(text)
        except SyntaxError as exc:
            return _scan_outline(text, [("def", re.compile(r"^\s*(?:async\s+)?def\s+(\w+)")), ("class", re.compile(r"^\s*class\s+(\w+)"))], markdown=False) or [
                Definition(exc.lineno or 1, "syntax error", str(exc.msg))
            ]
    rules = _DECLARATIONS.get(ext)
    if rules is None:
        return None
    return _scan_outline(text, rules, markdown=ext == "md", headers=_OPEN_HEADERS.get(ext))


# -- symbol ------------------------------------------------------------------


@dataclass(frozen=True)
class Symbol:
    """A definition and its lines ``first``..``last`` (1-based, inclusive), under its qualified name."""

    name: str
    kind: str
    first: int
    last: int


def _python_symbols(text: str) -> list[Symbol]:
    """Every class and function of a Python module, nested ones included, with its exact span."""
    out: list[Symbol] = []

    def walk(nodes, prefix: str) -> None:
        for node in nodes:
            if isinstance(node, ast.ClassDef | ast.AsyncFunctionDef | ast.FunctionDef):
                kind = "class" if isinstance(node, ast.ClassDef) else ("async def" if isinstance(node, ast.AsyncFunctionDef) else "def")
                first = min([node.lineno, *(d.lineno for d in node.decorator_list)])
                qual = f"{prefix}{node.name}"
                out.append(Symbol(qual, kind, first, node.end_lineno or node.lineno))
                walk(node.body, f"{qual}.")
            elif isinstance(node, ast.If | ast.Try | ast.With | ast.AsyncWith):
                for field_name in ("body", "orelse", "finalbody"):
                    walk(getattr(node, field_name, []), prefix)
                for handler in getattr(node, "handlers", []):
                    walk(handler.body, prefix)

    walk(ast.parse(text).body, "")
    return out


def _parsed_python_symbols(text: str) -> list[Symbol] | None:
    """The symbols of a Python module, ``None`` when it does not parse."""
    try:
        return _python_symbols(text)
    except SyntaxError:
        return None


def _scanned_symbols(defs: list[Definition], total_lines: int, lines: list[str]) -> list[Symbol]:
    """Spans of scanned declarations: a definition runs to the line before the next one at its depth or above."""
    out: list[Symbol] = []
    for i, d in enumerate(defs):
        end = total_lines
        for nxt in defs[i + 1 :]:
            if nxt.depth <= d.depth:
                end = nxt.line - 1
                break
        while end > d.line and not lines[end - 1].strip():
            end -= 1
        out.append(Symbol(d.name, d.kind, d.line, end))
    return out


def symbols(path: str, text: str) -> list[Symbol] | None:
    """The symbols of *text* with their spans, by *path*'s extension; ``None`` when no outline is defined for it."""
    ext = path.rsplit(".", 1)[-1].casefold() if "." in path.rsplit("/", 1)[-1] else ""
    ext = _ALIASES.get(ext, ext)
    if ext in _PYTHON:
        parsed = _parsed_python_symbols(text)
        if parsed is not None:
            return parsed
        # A module that does not parse: the declaration scan of outline() still finds its definitions.
    defs = outline(path, text)
    if defs is None:
        return None
    lines = text.splitlines()
    return _scanned_symbols([d for d in defs if d.kind != "syntax error"], len(lines), lines)


def find_symbol(found: list[Symbol], name: str) -> list[Symbol]:
    """The symbols *name* designates: its qualified name exactly, else its last part (``Class.method`` or ``method``)."""
    wanted = name.strip()
    exact = [s for s in found if s.name == wanted]
    if exact:
        return exact
    return [s for s in found if s.name.rsplit(".", 1)[-1] == wanted or s.name.endswith(f".{wanted}")]


def clamp_occurrence(occurrence: object) -> int:
    """The 1-based occurrence asked for; 0 (list the matches) for anything not a positive integer."""
    if isinstance(occurrence, bool):
        return 0
    try:
        return max(0, int(str(occurrence)))
    except (TypeError, ValueError):
        return 0


def format_symbol_choices(path: str, name: str, matches: list[Symbol]) -> str:
    shown = matches[:MAX_OUTLINE_ENTRIES]
    lines = [f"  {s.first}-{s.last}  {s.kind} {s.name}" for s in shown]
    return (
        f"{path}: {len(matches)} symbols named {name!r}; pass the qualified name of one (or occurrence=N, "
        "1-based in this order):\n" + "\n".join(lines)
    )


def format_outline(path: str, defs: list[Definition] | None, *, total_lines: int) -> str:
    if defs is None:
        ext = path.rsplit(".", 1)[-1] if "." in path.rsplit("/", 1)[-1] else "(none)"
        return (
            f"No outline for {path}: extension {ext!r} has no declaration scan "
            f"(defined for {', '.join(outline_kinds())}). Read it with sandbox_read_file or search it with sandbox_grep."
        )
    if not defs:
        return f"{path} ({total_lines} lines): no definition found."
    shown = defs[:MAX_OUTLINE_ENTRIES]
    width = len(str(max(d.line for d in shown)))
    lines = [f"{d.line:>{width}}  {'  ' * d.depth}{d.kind} {d.name}" for d in shown]
    body, dropped = _bounded(lines)
    out = [f"{path} ({total_lines} lines): {len(defs)} definition(s)", *body]
    rest = len(defs) - len(shown) + dropped
    if rest:
        out.append(f"[{rest} more definition(s) not shown]")
    return "\n".join(out)
