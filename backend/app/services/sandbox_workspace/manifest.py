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

"""The workspace's manifest: entries, what a change does to it, and how it reads.

Pure: no I/O. The stores (``store.py``) and the sandbox backends
(``local.py``, ``remote.py``) share these rules, so the quota a commit is
held to, the diff a call reports and the manifest the tool description shows
are computed once.
"""

from __future__ import annotations

import fnmatch
import itertools
import time
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime

from app.services.sandbox_workspace.paths import display, is_under


@dataclass(frozen=True)
class ManifestEntry:
    path: str
    size: int
    sha256: str
    mtime: float


@dataclass(frozen=True)
class Limits:
    quota_bytes: int
    max_files: int

    @classmethod
    def from_settings(cls) -> Limits:
        from app.core.config import settings

        return cls(
            quota_bytes=max(1, int(settings.code_interpreter_workspace_quota_mb)) * 1024 * 1024,
            max_files=max(1, int(settings.code_interpreter_workspace_max_files)),
        )


@dataclass
class ManifestDiff:
    added: list[str] = field(default_factory=list)
    changed: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)

    @property
    def empty(self) -> bool:
        return not (self.added or self.changed or self.removed)

    def as_dict(self) -> dict[str, list[str]]:
        return {"added": list(self.added), "changed": list(self.changed), "removed": list(self.removed)}


@dataclass(frozen=True)
class CommitPlan:
    """What applying a change set leaves: sizes, the diff, the contents no path holds any more."""

    size_bytes: int
    file_count: int
    diff: ManifestDiff
    #: Contents the change replaced or removed and no remaining path holds.
    orphaned: frozenset[str]


class QuotaExceededError(Exception):
    """A change set the workspace's limits refuse; ``str()`` is the sentence the model reads."""


def format_size(size: int) -> str:
    value = float(max(0, int(size)))
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{int(value)} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GB"


def plan_commit(
    current: Mapping[str, ManifestEntry],
    upserts: Iterable[ManifestEntry],
    removals: Iterable[str],
    limits: Limits,
) -> CommitPlan:
    """Apply *upserts* then *removals* to *current* on paper, held to *limits*.

    Raises :class:`QuotaExceededError` naming the size or the count the change
    would reach: nothing of the change set is kept then, so the caller saves
    nothing of it.
    """
    entries = dict(current)
    diff = ManifestDiff()
    released: set[str] = set()
    for entry in upserts:
        before = entries.get(entry.path)
        if before is None:
            diff.added.append(entry.path)
        elif before.sha256 != entry.sha256:
            diff.changed.append(entry.path)
            released.add(before.sha256)
        entries[entry.path] = entry
    for path in removals:
        before = entries.pop(path, None)
        if before is not None:
            diff.removed.append(path)
            released.add(before.sha256)
    size = sum(entry.size for entry in entries.values())
    if size > limits.quota_bytes:
        raise QuotaExceededError(
            f"Workspace quota exceeded: this change would bring the workspace to {format_size(size)}, over its "
            f"{format_size(limits.quota_bytes)} quota. Nothing it wrote under workspace/ was saved; remove files "
            "(sandbox_remove_files or os.remove in the code) or write less, then run it again."
        )
    # Net growth only (#2186): a change that leaves no more files than the
    # workspace held (a rebase rewriting a checkout) passes in a workspace
    # already over the limit, else a stale checkout there could never be
    # brought up to date.
    if len(entries) > limits.max_files and len(entries) > len(current):
        raise QuotaExceededError(
            f"Workspace file limit exceeded: this change would leave {len(entries):,} files in the workspace, over "
            f"its {limits.max_files:,} file limit. Nothing it wrote under workspace/ was saved; remove files or "
            "pack them into an archive, then run it again."
        )
    held = {entry.sha256 for entry in entries.values()}
    diff.added.sort()
    diff.changed.sort()
    diff.removed.sort()
    return CommitPlan(
        size_bytes=size,
        file_count=len(entries),
        diff=diff,
        orphaned=frozenset(released - held),
    )


def _when(mtime: float, now: float) -> str:
    stamp = datetime.fromtimestamp(mtime, tz=UTC)
    if now - mtime < 86_400:
        return stamp.strftime("%H:%M UTC")
    return stamp.strftime("%Y-%m-%d")


#: Patterns a brace glob expands to, at most: past it the glob is matched as written.
MAX_BRACE_EXPANSIONS = 64


def expand_braces(pattern: str) -> list[str]:
    """The patterns a ``{a,b}`` glob stands for, as a shell expands it (nested groups included).

    A pattern with no complete group, a group with no comma, or one past
    :data:`MAX_BRACE_EXPANSIONS` patterns is answered as written.
    """
    results = [pattern]
    done: list[str] = []
    while results:
        current = results.pop(0)
        start = current.find("{")
        expanded = False
        while start != -1:
            depth, end = 0, -1
            commas: list[int] = []
            for index in range(start, len(current)):
                char = current[index]
                if char == "{":
                    depth += 1
                elif char == "}":
                    depth -= 1
                    if depth == 0:
                        end = index
                        break
                elif char == "," and depth == 1:
                    commas.append(index)
            if end == -1:
                break
            if commas:
                bounds = [start, *commas, end]
                head, tail = current[:start], current[end + 1 :]
                results.extend(head + current[a + 1 : b] + tail for a, b in itertools.pairwise(bounds))
                expanded = True
                break
            start = current.find("{", start + 1)
        if not expanded:
            done.append(current)
        if len(done) + len(results) > MAX_BRACE_EXPANSIONS:
            return [pattern]
    return done


def matches_listing_filter(path: str, prefix: str, depth: int | None = None, glob: str = "") -> bool:
    """Whether *path*, under *prefix*, passes a listing's ``depth`` and ``glob``.

    *depth*: directory levels below *prefix* that count (1 = its direct files
    only); ``None`` or less than 1 is no bound. *glob*: an fnmatch pattern on
    the file name, or on the path relative to *prefix* when it holds a ``/``;
    ``{a,b}`` alternatives are expanded (:func:`expand_braces`), any one matching.
    """
    relative = path[len(prefix) + 1 :] if prefix else path
    if depth is not None and depth >= 1 and relative.count("/") >= depth:
        return False
    if glob:
        subject = relative if "/" in glob else relative.rsplit("/", 1)[-1]
        if not any(fnmatch.fnmatchcase(subject, one) for one in expand_braces(glob)):
            return False
    return True


def _filter_label(depth: int | None, glob: str) -> str:
    parts = []
    if depth is not None and depth >= 1:
        parts.append(f"depth {depth}")
    if glob:
        parts.append(f"glob {glob}")
    return ", ".join(parts)


def format_manifest(
    entries: Mapping[str, ManifestEntry],
    *,
    limit: int,
    quota_bytes: int,
    prefix: str = "",
    now: float | None = None,
    depth: int | None = None,
    glob: str = "",
) -> str:
    """The workspace listing a description or a result shows: bounded, the rest counted.

    The most recently changed files first, since they are the ones the next
    call most likely needs; the installed packages (``.venv/``) are summed up
    on one line rather than listed file by file. *depth* and *glob* narrow it
    (see :func:`matches_listing_filter`) before the bound applies.
    """
    moment = time.time() if now is None else now
    chosen = [
        e for e in entries.values() if is_under(e.path, prefix) and matches_listing_filter(e.path, prefix, depth, glob)
    ]
    venv = [e for e in chosen if e.path.startswith(".venv/")]
    files = [e for e in chosen if not e.path.startswith(".venv/")]
    total = sum(e.size for e in chosen)
    narrowed = _filter_label(depth, glob)
    if not chosen:
        where = f" under {display(prefix)}" if prefix else ""
        if narrowed:
            return f"No file{where} matches {narrowed}."
        return f"The workspace is empty{where} (quota {format_size(quota_bytes)})."
    header = (
        f"{len(chosen):,} file(s), {format_size(total)}"
        + (f" under {display(prefix)}" if prefix else f" of {format_size(quota_bytes)}")
        + (f" matching {narrowed}" if narrowed else "")
        + ":"
    )
    lines = [header]
    files.sort(key=lambda e: (-e.mtime, e.path))
    shown = files[: max(0, limit)]
    for entry in shown:
        lines.append(f"- {display(entry.path)} ({format_size(entry.size)}, {_when(entry.mtime, moment)})")
    if len(files) > len(shown):
        rest = files[len(shown) :]
        lines.append(f"- ... and {len(rest):,} more file(s), {format_size(sum(e.size for e in rest))}")
    if venv:
        lines.append(
            f"- {display('.venv/')}: installed packages, {len(venv):,} file(s), {format_size(sum(e.size for e in venv))}"
        )
    return "\n".join(lines)


def format_diff(diff: ManifestDiff, *, limit: int = 12) -> str:
    """One bounded line per kind of change: what the call did to the workspace."""
    if diff.empty:
        return "Workspace unchanged."
    parts: list[str] = []
    for label, paths in (("added", diff.added), ("changed", diff.changed), ("removed", diff.removed)):
        if not paths:
            continue
        named = ", ".join(display(path) for path in paths[:limit])
        more = f", +{len(paths) - limit} more" if len(paths) > limit else ""
        parts.append(f"{label} {len(paths)}: {named}{more}")
    return "Workspace " + "; ".join(parts) + "."
