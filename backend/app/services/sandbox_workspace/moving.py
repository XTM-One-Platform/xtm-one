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

"""Move or rename workspace files: the plan, pure.

``sandbox_move_files`` (``builtin_tools/sandbox_workspace.py``) turns a list of
``{from, to}`` moves into one workspace commit: every moved file keeps its
blob (the manifest entry is re-pointed, nothing is uploaded again) and its old
path is removed in the same change set, so a rename across a package is one
call and one write, never a ``code_interpreter`` ``os.rename`` or a
``sandbox_write_file`` plus ``sandbox_remove_files`` pair.

A move names a file or a directory (everything under it moves). The plan
refuses, before anything is written: a source that is not in the workspace, a
destination inside its own source, two moves onto one path, a destination
that exists (a file may be replaced with ``overwrite``; a directory never),
a destination another move empties in the same call, and a checkout record
moved away from the checkout it describes.

With ``copy`` the same plan copies instead: every source stays where it is and
the destination points at the same blob (content-addressed, nothing is
uploaded), so a file is duplicated in one call without its content passing
through the context or a ``code_interpreter`` write.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from app.services.sandbox_workspace.checkout import is_record_name
from app.services.sandbox_workspace.manifest import ManifestEntry
from app.services.sandbox_workspace.paths import display, is_under, normalize

#: Moves one call takes, and files one call moves, at most.
MAX_MOVES = 100
MAX_FILES = 5_000


class MoveError(ValueError):
    """A move the plan refuses; ``str()`` says which and why, for the model."""


@dataclass(frozen=True)
class MovePlan:
    #: ``(old path, new entry)`` for every file moved, sorted by old path.
    moved: list[tuple[str, ManifestEntry]]
    #: Paths a moved file replaces (``overwrite``).
    replaced: list[str]

    @property
    def removals(self) -> list[str]:
        return [old for old, _entry in self.moved]

    @property
    def upserts(self) -> list[ManifestEntry]:
        return [entry for _old, entry in self.moved]


def parse(moves: object) -> list[tuple[str, str]]:
    """The ``(from, to)`` pairs of the tool's ``moves`` argument, normalised; :class:`MoveError` on a bad shape."""
    if isinstance(moves, Mapping):
        moves = [moves]
    if not isinstance(moves, list) or not moves:
        raise MoveError('give moves as a list of {"from": path, "to": path}. Nothing was written.')
    if len(moves) > MAX_MOVES:
        raise MoveError(f"{len(moves)} moves in one call, over the {MAX_MOVES} it takes. Nothing was written.")
    pairs: list[tuple[str, str]] = []
    for n, move in enumerate(moves, start=1):
        if not isinstance(move, Mapping):
            raise MoveError(f'move {n} is not an object {{"from": path, "to": path}}. Nothing was written.')
        src, dst = move.get("from"), move.get("to")
        if not isinstance(src, str) or not src.strip() or not isinstance(dst, str) or not dst.strip():
            raise MoveError(f'move {n} needs both "from" and "to" paths. Nothing was written.')
        pairs.append((normalize(src), normalize(dst)))
    return pairs


def plan(
    pairs: Iterable[tuple[str, str]],
    current: Mapping[str, ManifestEntry],
    *,
    overwrite: bool = False,
    now: float = 0.0,
    copy: bool = False,
) -> MovePlan:
    """The files each move carries and where; :class:`MoveError` refuses the whole call.

    With *copy* the sources stay in the workspace (:attr:`MovePlan.removals` is
    then not applied by the caller), so they still occupy their paths.
    """
    verb = "copied" if copy else "moved"
    moved: dict[str, ManifestEntry] = {}
    targets: dict[str, str] = {}
    for src, dst in pairs:
        if src == dst:
            raise MoveError(f"{display(src)} is {verb} onto itself. Nothing was written.")
        if is_under(dst, src):
            raise MoveError(
                f"{display(dst)} is inside {display(src)}: a path cannot go into itself. Nothing was written."
            )
        if src in current:
            files = {src: dst}
        else:
            files = {p: dst + p[len(src) :] for p in current if is_under(p, src)}
            if not files:
                raise MoveError(f"{display(src)} is not in the workspace; list it with sandbox_list_files. Nothing was written.")
        for old, new in files.items():
            if old in moved:
                raise MoveError(f"{display(old)} is {verb} twice in one call. Nothing was written.")
            if new in targets:
                raise MoveError(
                    f"{display(targets[new])} and {display(old)} would both land on {display(new)}. Nothing was written."
                )
            if old == src and is_record_name(old):
                raise MoveError(
                    f"{display(old)} is a checkout record: it describes the checkout it sits in and is not {verb} "
                    "alone. Take the whole checkout directory instead. Nothing was written."
                )
            entry = current[old]
            moved[old] = ManifestEntry(path=new, size=entry.size, sha256=entry.sha256, mtime=now or entry.mtime)
            targets[new] = old
    if len(moved) > MAX_FILES:
        raise MoveError(f"{len(moved):,} files in one call, over the {MAX_FILES:,} it takes. Nothing was written.")
    #: Paths this call empties: the moved sources; a copy keeps them.
    leaving: Mapping[str, ManifestEntry] = {} if copy else moved
    replaced: list[str] = []
    for new, old in sorted(targets.items()):
        if new in moved:
            raise MoveError(
                f"{display(new)} is both a destination and a path this call moves away: split it into two calls. "
                "Nothing was written."
            )
        holder = next((p for p in current if is_under(p, new) and p != new and p not in leaving), None)
        if holder is not None:
            raise MoveError(f"{display(new)} is a directory of the workspace: {display(old)} cannot replace it. Nothing was written.")
        parents = [new.rsplit("/", i)[0] for i in range(1, new.count("/") + 1)]
        blocking = next((p for p in parents if p in current and p not in leaving), None)
        if blocking is not None:
            raise MoveError(f"{display(blocking)} is a file, so it cannot hold {display(new)}. Nothing was written.")
        if new in current:
            if not overwrite:
                raise MoveError(
                    f"{display(new)} already exists: pass overwrite=true to replace it with {display(old)}. Nothing was written."
                )
            replaced.append(new)
    return MovePlan(moved=sorted(moved.items()), replaced=replaced)


def report(planned: MovePlan, limit: int = 50) -> str:
    """One line per moved file, the first *limit*, the rest counted."""
    lines = [
        f"{display(old)} -> {display(entry.path)}" + (" (replaced)" if entry.path in planned.replaced else "")
        for old, entry in planned.moved[:limit]
    ]
    rest = len(planned.moved) - limit
    if rest > 0:
        lines.append(f"... and {rest:,} more file(s)")
    return "\n".join(lines)
