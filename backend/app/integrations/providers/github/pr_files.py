"""The answer of ``get_github_pr_files``: the file list first, patches on request.

An agent reads a pull request's files to learn one fact far more often than to
review every patch: which files changed, whether a path is touched, whether a
pattern appears in the change. The default answer is therefore one line per
file (status, path, additions, deletions) and no patch. ``path`` narrows the
files (a glob, a directory or a file), ``pattern`` answers only the hunks whose
lines match a regular expression, and ``include_patch`` reads the patches of
the selected files by window.
"""

from __future__ import annotations

import fnmatch
import re
from typing import Any

from app.services.sandbox_workspace.manifest import expand_braces

#: GitHub's page size on ``/pulls/{n}/files`` and its cap of 3,000 files.
PAGE_SIZE = 100
MAX_PAGES = 30

#: Lines of the compact file list; the rest are counted.
MAX_LIST_FILES = 300

_GLOB_CHARS = frozenset("*?[")


def _names(entry: dict[str, Any]) -> list[str]:
    names = [str(entry.get("filename") or "")]
    previous = entry.get("previous_filename")
    if previous:
        names.append(str(previous))
    return names


def path_matches(name: str, path: str) -> bool:
    """*name* is selected by *path*: a glob, a directory prefix or the file itself.

    A ``{a,b}`` group expands as a shell expands it (nested groups included, as
    the sandbox globs do): *name* is selected when one alternative selects it.
    """
    wanted = path.strip()
    if wanted.startswith("./"):
        wanted = wanted[2:]
    if not wanted:
        return True
    alternatives = expand_braces(wanted)
    if alternatives != [wanted]:
        return any(_matches_one(name, alternative) for alternative in alternatives)
    return _matches_one(name, wanted)


def _matches_one(name: str, wanted: str) -> bool:
    """*name* is selected by one brace-free *wanted*: a glob, a directory prefix or the file."""
    if wanted.startswith("./"):
        wanted = wanted[2:]
    if not wanted:
        return False
    if any(c in _GLOB_CHARS for c in wanted):
        if "/" not in wanted:
            return fnmatch.fnmatchcase(name.rsplit("/", 1)[-1], wanted) or fnmatch.fnmatchcase(name, wanted)
        return fnmatch.fnmatchcase(name, wanted)
    directory = wanted.rstrip("/")
    return name == directory or name.startswith(directory + "/")


def select(files: list[dict[str, Any]], path: str | None) -> list[dict[str, Any]]:
    if not path or not path.strip():
        return files
    return [f for f in files if any(path_matches(n, path) for n in _names(f))]


def file_line(entry: dict[str, Any]) -> str:
    status = str(entry.get("status") or "modified")
    name = str(entry.get("filename") or "?")
    if status == "renamed" and entry.get("previous_filename"):
        name = f"{entry['previous_filename']} → {name}"
    return f"{status.upper()} {name} (+{entry.get('additions', 0)} −{entry.get('deletions', 0)})"


def hunks(patch: str) -> list[str]:
    """The hunks of a file patch, each from its ``@@`` line to the next one."""
    out: list[str] = []
    current: list[str] = []
    for line in patch.splitlines():
        if line.startswith("@@") and current:
            out.append("\n".join(current))
            current = []
        current.append(line)
    if current:
        out.append("\n".join(current))
    return out


def matching_hunks(patch: str, regex: re.Pattern[str]) -> tuple[list[str], int]:
    """``(hunks with a changed or context line matching, total hunks)``; ``@@`` headers are not searched."""
    all_hunks = hunks(patch)
    found = [h for h in all_hunks if any(regex.search(line) for line in h.splitlines() if not line.startswith("@@"))]
    return found, len(all_hunks)


def compact_list(number: int, files: list[dict[str, Any]], selected: list[dict[str, Any]], path: str | None) -> str:
    total_add = sum(f.get("additions", 0) for f in files)
    total_del = sum(f.get("deletions", 0) for f in files)
    lines = [f"PR #{number}: {len(files)} files changed (+{total_add} −{total_del})"]
    if path and path.strip():
        lines.append(f"{len(selected)} of them match path {path!r}")
    lines.append("")
    if not selected:
        lines.append("No changed file matches the path.")
        return "\n".join(lines)
    for entry in selected[:MAX_LIST_FILES]:
        lines.append(file_line(entry))
    if len(selected) > MAX_LIST_FILES:
        lines.append(f"[… {len(selected) - MAX_LIST_FILES:,} more files; narrow with path]")
    lines.append("")
    lines.append("Patches not shown: pattern=REGEX answers the matching hunks, include_patch=true the patches.")
    return "\n".join(lines)


def pattern_body(selected: list[dict[str, Any]], regex: re.Pattern[str]) -> tuple[str, int, int]:
    """``(text, files with a match, matching hunks)`` of a pattern search."""
    parts: list[str] = []
    files_hit = 0
    hunks_hit = 0
    for entry in selected:
        patch = entry.get("patch") or ""
        if not patch:
            continue
        found, total = matching_hunks(patch, regex)
        if not found:
            continue
        files_hit += 1
        hunks_hit += len(found)
        parts.append(f"── {file_line(entry)}: {len(found)} of {total} hunks match ──")
        parts.extend(found)
        parts.append("")
    return "\n".join(parts), files_hit, hunks_hit


def patch_body(selected: list[dict[str, Any]]) -> str:
    parts: list[str] = []
    for entry in selected:
        parts.append(f"── {file_line(entry)} ──")
        patch = entry.get("patch") or ""
        parts.append(patch if patch else "(no patch: binary, too large for GitHub, or a pure rename)")
        parts.append("")
    return "\n".join(parts)


def window(text: str, offset: int, size: int) -> tuple[str, str]:
    """``(slice, footer)`` of *text* from *offset*; the footer names the next offset or the end."""
    total = len(text)
    end = min(offset + size, total)
    if end < total:
        return text[offset:end], (
            f"[characters {offset:,} to {end:,} of {total:,}; {total - end:,} more, continue with offset={end}]"
        )
    return text[offset:end], f"[characters {offset:,} to {end:,} of {total:,}; the end]"
