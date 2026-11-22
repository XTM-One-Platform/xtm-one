"""Keep a ``from_checkout`` commit from reverting what the branch gained since the checkout.

The change set of ``create_github_commit(from_checkout=...)`` is whole files:
the workspace version of every file changed since the checkout record's
commit. When the branch tip is that commit, writing them whole is exact. When
it is not (a checkout older than the branch, a branch cut from a newer main,
someone else's push meanwhile), a file the branch also changed would be
written back at the checkout's text, silently reverting those commits.

:func:`reconcile` reads the recorded commit and the tip and, for each path of
the change set the branch changed since the record, merges the three versions
line by line on the server (the diff3 merge ``sandbox_checkout_rebase`` and
``merge_github_branch`` use, :func:`.merge_hunks.merge_path`):

- a clean merge replaces the workspace bytes in the commit, and is handed back
  so the workspace file is brought to the merged text with the record;
- an overlap, a binary changed on both sides, a delete against a change or a
  path that is not a plain file refuses the commit with the hunks named, and
  nothing is written;
- a deletion of a file the branch already removed is dropped.

The files of the checkout the commit does not carry are compared too (#10061):
when the branch changed one of them since the record, the workspace still holds
it at the record's text, so moving the record to the new commit would make the
next commit's tip look like its base and write it, whole, over the branch's
change. ``Reconciled.stale`` names them; the commit then keeps the record at its
commit (only the written files' hashes move), so the next commit merges again,
and the answer says to run ``sandbox_checkout_rebase``.

The ancestor of that merge is the merge base of the recorded commit and the
tip, read with one compare: the recorded commit itself when the branch grew
from it, an older commit when the record sits on another line (a commit made
from the checkout on another branch, then a new branch cut from main). Using
the recorded commit there made the workspace look unchanged, so the merge took
the tip's text for every file and answered "No change" over real changes.

A file needs no merge when the workspace already holds the tip's bytes, or
when the record holds the tip's version of it (an earlier commit from this
checkout wrote it): the workspace then changed the tip's text and is written
as it is. Neither costs a blob read beyond the tip's version of a candidate.

Nothing reaches the model but the bounded hunks of a refusal.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from . import _common
from ._common import _PROVIDER, _coerce_str
from .contents import _GIT_OBJECT_ID_RE, _json_object
from .merge_hunks import MAX_MERGE_READ_BYTES, MAX_MERGED_PATHS, PathMerge, describe_conflicts, merge_path
from .merge_resolution import _read_blob, _read_side, _Side

#: Modes merged line by line; anything else (a link, a submodule) changed on the branch is a conflict.
_PLAIN_MODES = frozenset({"100644", "100755"})
#: Paths named in the merge note; the rest are counted.
_MAX_NAMED = 10


@dataclass
class Reconciled:
    """The change set to commit on the tip, and what was merged or dropped to get it."""

    #: Repository path -> the bytes to commit (the merged text where the branch drifted).
    writes: dict[str, bytes]
    deletes: list[str]
    #: The paths merged with the branch -> the merged bytes the workspace must now hold.
    merged: dict[str, bytes] = field(default_factory=dict)
    #: Deletions the branch had already made.
    dropped: list[str] = field(default_factory=list)
    #: Files of the checkout this commit does not carry that the branch changed since the record.
    stale: list[str] = field(default_factory=list)
    #: Paths written as the workspace holds them because their base is the tip's version.
    on_tip: list[str] = field(default_factory=list)
    note: str = ""


def _named(paths: list[str]) -> str:
    more = len(paths) - _MAX_NAMED
    return ", ".join(paths[:_MAX_NAMED]) + (f" (and {more} more)" if more > 0 else "")


def _sha(side: _Side, path: str) -> str | None:
    entry = side.entries.get(path)
    return None if entry is None else entry.sha


def _git_blob_sha(data: bytes) -> str:
    """The object id git gives *data* as a blob: compared with a tree entry, no read."""
    return hashlib.sha1(b"blob %d\0" % len(data) + data, usedforsecurity=False).hexdigest()


async def _merge_base(client: Any, headers: dict[str, str], repo: str, repo_url: str, recorded: str, tip: str) -> str:
    """The merge base of *recorded* and *tip*; *recorded* when GitHub does not name one."""
    resp, err = await _common.safe_request(
        client,
        "get",
        f"{repo_url}/compare/{recorded}...{tip}",
        _PROVIDER,
        f"compare {recorded[:8]}...{tip[:8]} in {repo}",
        headers=headers,
        params={"per_page": "1"},
    )
    if err:
        return recorded
    base = _json_object(resp).get("merge_base_commit")
    sha = _coerce_str(base.get("sha")) if isinstance(base, dict) else ""
    return sha if _GIT_OBJECT_ID_RE.fullmatch(sha) else recorded


def _rebase_hint(checkout: str, branch: str) -> str:
    return (
        f'Bring the checkout onto the branch with sandbox_checkout_rebase(checkout="{checkout}", ref="{branch}"), '
        "settle what it reports, then commit again."
    )


async def reconcile(
    client: Any,
    headers: dict[str, str],
    repo: str,
    repo_url: str,
    *,
    branch: str,
    tip: str,
    recorded: str,
    checkout: str,
    writes: dict[str, bytes],
    deletes: list[str],
    tracked: Iterable[str] = (),
    recorded_files: Mapping[str, str] | None = None,
    baseline: str = "",
    committed: Iterable[str] = (),
) -> Reconciled | str:
    """The change set made safe to commit on *tip*, or the refusal (``Error: ...``) naming what overlaps.

    *tracked* are the files the checkout record holds: the ones this commit
    does not carry and the branch changed since are ``Reconciled.stale``.
    *recorded_files* maps a repository path to the sha256 the record holds for
    it: a path whose tip version has that hash is based on the tip already.
    *baseline* is the commit the tree was checked out at when a commit made
    from the checkout moved the record past it (``record.base_commit``), and
    *committed* the paths those commits wrote or deleted (#1040): any other
    path still holds the baseline's text in the workspace, so its merge
    ancestor is the baseline's version, even when the tip is the record's commit.
    """
    moved_record = bool(baseline) and baseline != recorded and bool(_GIT_OBJECT_ID_RE.fullmatch(baseline))
    if not recorded or not tip or (tip == recorded and not moved_record):
        return Reconciled(dict(writes), list(deletes))
    if tip == recorded:
        moved = (
            f"the checkout of {repo} holds files at {baseline[:8]}, the commit it was read at, while its record "
            f"moved to {recorded[:8]} on branch '{branch}'"
        )
        ancestor_ref = recorded
    else:
        moved = f"branch '{branch}' of {repo} is at {tip[:8]}, not at the checkout's commit {recorded[:8]}"
        # The ancestor of the merge: the recorded commit when the branch grew from it, else their merge base
        # (a record moved by a commit on another branch, a branch cut from a newer main).
        ancestor_ref = await _merge_base(client, headers, repo, repo_url, recorded, tip)
    read: dict[str, _Side] = {}
    for ref in dict.fromkeys((recorded, ancestor_ref, tip, *((baseline,) if moved_record else ()))):
        side = await _read_side(client, headers, repo, repo_url, ref)
        if isinstance(side, str):
            return (
                f"Error: {moved}, and the two could not be compared ({side.removeprefix('Error: ')}), so nothing "
                f"was committed: writing the workspace files whole could revert the branch's changes. "
                + _rebase_hint(checkout, branch)
            )
        read[ref] = side
    record_side, base, head = read[recorded], read[ancestor_ref], read[tip]
    committed_paths = set(committed)

    def base_for(path: str) -> _Side:
        """The ancestor of *path*: the baseline's version when no commit from the checkout wrote it."""
        return read[baseline] if moved_record and path not in committed_paths else base

    def held(path: str) -> _Side:
        """The commit whose version of *path* the workspace holds when this commit does not carry it."""
        return read[baseline] if moved_record and path not in committed_paths else record_side

    carried = {*writes, *deletes}
    stale = sorted(path for path in set(tracked) - carried if _sha(held(path), path) != _sha(head, path))
    drifted = [
        path
        for path in [*writes, *deletes]
        if _sha(base_for(path), path) != _sha(head, path)
        # The workspace already holds the tip's bytes: nothing to merge, the commit tells it apart.
        and not (path in writes and _sha(head, path) == _git_blob_sha(writes[path]))
    ]
    if not drifted:
        out = Reconciled(dict(writes), list(deletes), stale=stale)
        out.note = _stale_note(stale, moved, recorded, checkout, branch)
        return out
    if len(drifted) > MAX_MERGED_PATHS:
        return (
            f"Error: {moved}, and it changed {len(drifted)} of the files this commit carries since, over the "
            f"{MAX_MERGED_PATHS} merged on the server; nothing was committed. " + _rebase_hint(checkout, branch)
        )
    listed = sum(
        entry.size
        for path in drifted
        for side in (base_for(path), head)
        if (entry := side.entries.get(path)) is not None
    )
    if listed > MAX_MERGE_READ_BYTES:
        return (
            f"Error: {moved}, and the versions of the files it changed since add up to {listed} bytes, over the "
            f"{MAX_MERGE_READ_BYTES} merged on the server; nothing was committed. " + _rebase_hint(checkout, branch)
        )
    out = Reconciled(dict(writes), [path for path in deletes if path not in drifted], stale=stale)
    conflicts: list[PathMerge] = []
    blobs: dict[str, bytes] = {}

    async def blob(side: _Side, path: str) -> bytes | None | str:
        entry = side.entries.get(path)
        if entry is None:
            return None
        if entry.sha not in blobs:
            data = await _read_blob(client, headers, repo, repo_url, entry.sha, path)
            if isinstance(data, str):
                return data
            blobs[entry.sha] = data
        return blobs[entry.sha]

    for path in drifted:
        odd = next(
            (
                entry
                for side in (base_for(path), head)
                if (entry := side.entries.get(path)) is not None and entry.mode not in _PLAIN_MODES
            ),
            None,
        )
        if odd is not None:
            conflicts.append(PathMerge(path, reason=f"not a plain file on the branch (mode {odd.mode}): settle it whole"))
            continue
        if path in deletes:
            if head.entries.get(path) is None:
                out.dropped.append(path)
            else:
                conflicts.append(PathMerge(path, reason=f"removed in the workspace, changed on '{branch}': keep one side whole"))
            continue
        theirs = await blob(head, path)
        if isinstance(theirs, str):
            return f"{theirs}\nNothing was committed to branch '{branch}' of {repo}."
        if theirs is None:
            conflicts.append(PathMerge(path, reason=f"changed in the workspace, removed on '{branch}': keep one side whole"))
            continue
        if (recorded_files or {}).get(path) == hashlib.sha256(theirs).hexdigest():
            # The record holds the tip's version (an earlier commit from this checkout wrote it):
            # the workspace changed the tip's text, so it is written as it is.
            out.on_tip.append(path)
            continue
        ancestor = await blob(base_for(path), path)
        if isinstance(ancestor, str):
            return f"{ancestor}\nNothing was committed to branch '{branch}' of {repo}."
        merge = merge_path(path, ancestor, writes[path], theirs)
        if merge.clean:
            data = (merge.merged or "").encode("utf-8")
            out.writes[path] = data
            if data != writes[path]:
                out.merged[path] = data
        else:
            conflicts.append(merge)
    if conflicts:
        count = len(conflicts)
        return (
            f"Error: {moved}, and the workspace and the branch changed the same part of {count} "
            f"file{'s' if count != 1 else ''}; nothing was committed, so the branch keeps its changes. "
            + _rebase_hint(checkout, branch)
            + "\n"
            + describe_conflicts(conflicts, "workspace", branch)
        )
    notes: list[str] = []
    if out.merged:
        notes.append(
            f"Merged with the branch: {moved}, so the {len(out.merged)} file(s) it changed since were merged line by "
            f"line with the workspace's changes instead of written over: {_named(sorted(out.merged))}. The workspace "
            "files now hold the merged text."
        )
    if out.dropped:
        notes.append(f"Not deleted, the branch already removed them: {_named(sorted(out.dropped))}.")
    if stale:
        notes.append(_stale_note(stale, moved, recorded, checkout, branch))
    out.note = " ".join(notes)
    return out


def _stale_note(stale: list[str], moved: str, recorded: str, checkout: str, branch: str) -> str:
    """The sentence for the files the branch changed that this commit does not carry; ``""`` when none."""
    if not stale:
        return ""
    return (
        f"Checkout behind the branch: {moved}, which changed {len(stale)} file(s) of the checkout this commit does "
        f"not carry ({_named(stale)}). The workspace still holds them at {recorded[:8]}, so the checkout record "
        f"stays at that commit and the next commit merges them instead of writing over them. "
        f'Run sandbox_checkout_rebase(checkout="{checkout}", ref="{branch}") before editing them.'
    )
