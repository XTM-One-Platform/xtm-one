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

"""Stale checkouts: what a full workspace holds, and which checkouts may go.

Pure: no I/O. ``materialize_repository`` refused for the file limit reads the
checkout roots the workspace holds (a directory with a ``.xtm-checkout.json``
record) with :func:`footprints`, asks the provider whether each recorded ref
still exists, and evicts in the same change set the checkouts that are
:attr:`Footprint.evictable`: clean (no edit since the checkout, ``checkout.py``)
and whose ref is gone (a branch deleted after its pull request merged). A
checkout with a local change is never evicted; a refusal names every root by
size, ref and state, with the exact ``sandbox_remove_files`` call.

The same applies to an ``add_paths`` grow. ``prune_clean_checkouts=true``
(asked explicitly) widens the choice to every clean checkout whatever its
ref (:func:`chosen`): a branch merged by squash whose branch still exists
holds nothing the provider cannot give back.

A checkout also carries the run that wrote it (``CheckoutRecord.run_id``,
#11500): a clean checkout of a run that has ended (:data:`RUN_ENDED`), or
one untouched for :data:`IDLE_SECONDS` (a conversation's workspace, which
has no run), is reclaimed the same way whatever its ref, so the stale trees
of earlier runs never hold the file limit against the live run. A checkout
of the live run itself is never reclaimed by that rule.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from app.services.sandbox_workspace.checkout import RECORD_NAME, CheckoutRecord, changes_since
from app.services.sandbox_workspace.checks import RUNS_DIR
from app.services.sandbox_workspace.gitignore import GitIgnore
from app.services.sandbox_workspace.paths import display, is_bytecode_cache, is_under

#: The provider says the recorded ref no longer exists (a merged branch deleted).
REF_GONE = "gone"
#: The ref exists: the checkout is kept.
REF_PRESENT = "present"
#: The ref could not be read (no compare tool, an error): the checkout is kept.
REF_UNKNOWN = "unknown"
#: The checkout names no branch (the default branch, a tag or SHA it pinned): kept.
REF_PINNED = "pinned"

#: Checkout roots a refusal names at most; their refs read at most per call.
MAX_LISTED = 10

#: The run that wrote the checkout has ended (completed, failed, skipped, cancelled).
RUN_ENDED = "ended"
#: The checkout was written by the run making the call.
RUN_LIVE = "live"
#: No run recorded, another run still going, or its state not read: kept.
RUN_UNKNOWN = "unknown"

#: A clean checkout whose record was not written for this long is reclaimed at the limit (the TTL).
IDLE_SECONDS = 24 * 3600


@dataclass(frozen=True)
class Footprint:
    """One checkout root of the workspace: its record, what it holds, whether it may go."""

    path: str
    record: CheckoutRecord
    files: int
    size: int
    #: Files changed since the checkout (``checkout.changes_since``), the run outputs left out.
    changes: int
    ref_state: str = REF_UNKNOWN
    run_state: str = RUN_UNKNOWN
    #: Seconds since its record was last written, when the manifest says.
    idle: float | None = None

    @property
    def clean(self) -> bool:
        return self.changes == 0

    @property
    def stale(self) -> bool:
        """Left by an ended run, or untouched past the TTL; never a checkout of the live run."""
        if self.run_state == RUN_LIVE:
            return False
        return self.run_state == RUN_ENDED or (self.idle is not None and self.idle >= IDLE_SECONDS)

    @property
    def evictable(self) -> bool:
        """Clean, and its ref gone or its run over: nothing in it is lost by removing it."""
        return self.clean and (self.ref_state == REF_GONE or self.stale)

    def reason(self) -> str:
        """Why an evictable checkout may go, for the answer."""
        if self.ref_state == REF_GONE:
            return "ref gone"
        if self.run_state == RUN_ENDED:
            return "run ended"
        return "idle past the TTL"

    def describe(self) -> str:
        ref = self.record.ref or "default branch"
        state = {
            REF_GONE: "ref gone (merged or deleted)",
            REF_PRESENT: "ref present",
            REF_PINNED: "default branch or pinned ref",
        }.get(self.ref_state, "ref not read")
        local = "clean" if self.clean else f"{self.changes} local change(s), kept"
        holder = ""
        if self.record.run_id:
            run = self.record.run_id[:8]
            holder = {
                RUN_ENDED: f", run {run} ended",
                RUN_LIVE: f", run {run} (this run)",
            }.get(self.run_state, f", run {run}")
        elif self.idle is not None and self.idle >= IDLE_SECONDS:
            holder = f", idle {int(self.idle // 3600)} h"
        return (
            f"{display(self.path)}/: {self.files:,} files, {_size(self.size)}, "
            f"{self.record.repository}@{ref}, {state}{holder}, {local}"
        )


def _size(size: int) -> str:
    from app.services.sandbox_workspace.manifest import format_size

    return format_size(size)


def checkout_roots(entries: Iterable[str]) -> list[str]:
    """The directories holding a checkout record, sorted."""
    suffix = "/" + RECORD_NAME
    return sorted(path[: -len(suffix)] for path in entries if path.endswith(suffix))


def footprints(
    entries: Mapping[str, object],
    records: Mapping[str, CheckoutRecord],
    *,
    exclude: str = "",
    current_run: str = "",
    now: float | None = None,
    ignores: Mapping[str, GitIgnore] | None = None,
) -> list[Footprint]:
    """Every checkout of *records* (root -> record) outside *exclude*, the largest first.

    *entries* is the workspace manifest (path -> an object with ``size`` and
    ``sha256``). A root inside or around *exclude* (the destination of the
    checkout being written) is left out: that one is replaced, never evicted.
    A file is counted under the deepest root that holds it. A record written
    by *current_run* is :data:`RUN_LIVE`; with *now*, ``idle`` is the age of
    the record file (its manifest ``mtime``). *ignores* (root -> the rules of
    its ``.gitignore`` files) leaves an untracked file git would ignore (a
    build's output) out of the count, as ``sandbox_checkout_status`` does, so
    a checkout the status calls clean is clean here too.
    """
    roots = [r for r in records if not (exclude and (is_under(r, exclude) or is_under(exclude, r)))]
    owned: dict[str, list[str]] = {r: [] for r in roots}
    for path in entries:
        holders = [r for r in roots if is_under(path, r)]
        if holders:
            owned[max(holders, key=len)].append(path)
    out: list[Footprint] = []
    for root in roots:
        record = records[root]
        paths = owned[root]
        mine = {p: entries[p] for p in paths}
        ignore = (ignores or {}).get(root)
        changes = changes_since(record, mine, ignore=ignore)
        runs = record.workspace_path(RUNS_DIR)
        # A file outside the recorded sub_paths is no change of the checkout,
        # but an eviction would remove it with the root: it counts too, unless
        # it is still byte for byte what an earlier, wider tree of the same
        # checkout left (the record's out-of-scope baseline, #10144): nothing
        # written by the agent is lost with it, so it never keeps a clean
        # checkout from a prune.
        baseline = record.outside_baseline or {}
        stray = [
            p
            for p in record.outside(paths)
            if not is_bytecode_cache(p)
            and not (ignore and ignore.ignores(p))
            and not _leftover(baseline, p, entries.get(record.workspace_path(p)))
        ]
        counted = {
            p
            for p in (*changes.modified, *changes.added, *changes.removed, *stray)
            if not is_under(record.workspace_path(p), runs)
        }
        mtime = getattr(entries.get(record.record_path), "mtime", None)
        out.append(
            Footprint(
                path=root,
                record=record,
                files=len(paths),
                size=sum(int(getattr(entries[p], "size", 0) or 0) for p in paths),
                changes=len(counted),
                ref_state=REF_PINNED if not record.ref or _looks_pinned(record) else REF_UNKNOWN,
                run_state=RUN_LIVE if current_run and record.run_id == current_run else RUN_UNKNOWN,
                idle=max(0.0, now - float(mtime)) if now is not None and isinstance(mtime, int | float) else None,
            )
        )
    out.sort(key=lambda f: (-f.files, f.path))
    return out


def _leftover(baseline: Mapping[str, str], repo_path: str, entry: object) -> bool:
    """Whether *repo_path* is still as the record's out-of-scope baseline holds it (an earlier tree's file)."""
    sha = str(getattr(entry, "sha256", "") or "")
    return bool(sha) and baseline.get(repo_path) == sha


def _looks_pinned(record: CheckoutRecord) -> bool:
    """A ref that is the recorded commit itself (a SHA checkout) names no branch to be merged."""
    ref = record.ref.strip()
    return bool(record.commit) and len(ref) >= 7 and record.commit.startswith(ref)


def chosen(found: Iterable[Footprint], *, prune: bool = False) -> list[Footprint]:
    """The checkouts an over-the-limit write removes: clean with a gone ref or an ended run, or every clean one with *prune*."""
    return [f for f in found if (f.clean if prune else f.evictable)]


def evicted_line(evicted: Iterable[Footprint], *, pruned: bool = False) -> str:
    """The sentence an answer carries about the checkouts it removed."""
    evicted = list(evicted)
    if pruned:
        why = "clean, prune_clean_checkouts"
    else:
        reasons = sorted({f.reason() for f in evicted if f.evictable}) or ["ref gone"]
        why = "clean, " + " or ".join(reasons)
    return f"Evicted to stay under the file limit ({why}): " + "; ".join(f.describe() for f in evicted) + "."


def eviction_paths(chosen: Iterable[Footprint], entries: Iterable[str]) -> list[str]:
    """The workspace files the evicted checkouts hold (their records included).

    A file is removed with the deepest checkout root that holds it, as
    :func:`footprints` counts it: a checkout nested inside an evicted one (a
    lab checked out under a product tree) keeps its files unless it is
    chosen itself, so evicting a clean outer tree never takes an inner
    checkout, clean or not, with it.
    """
    entries = list(entries)
    roots = {f.path for f in chosen}
    every_root = set(checkout_roots(entries)) | roots
    out: list[str] = []
    for path in entries:
        holders = [r for r in every_root if is_under(path, r)]
        if holders and max(holders, key=len) in roots:
            out.append(path)
    return out


def refusal_hint(found: list[Footprint], *, limit: int = MAX_LISTED, pruned: bool = False) -> str:
    """The lines a file-limit refusal adds: each checkout root by size, then the removal call."""
    if not found:
        return ""
    shown = found[:limit]
    lines = ["Checkouts in the workspace, the largest first:"]
    lines.extend(f"- {f.describe()}" for f in shown)
    if len(found) > limit:
        lines.append(f"- +{len(found) - limit} more checkout(s)")
    removable = [f.path for f in shown if f.clean]
    if removable:
        quoted = ", ".join(f'"{p}"' for p in removable)
        lines.append(
            "A clean checkout loses nothing when removed (materialize_repository checks it out again): "
            f"sandbox_remove_files(paths=[{quoted}]) frees "
            f"{sum(f.files for f in shown if f.clean):,} files"
            + ("." if pruned else ", or call again with prune_clean_checkouts=true.")
        )
    else:
        lines.append("Every checkout listed holds local changes: commit or remove them first.")
    return "\n".join(lines)
