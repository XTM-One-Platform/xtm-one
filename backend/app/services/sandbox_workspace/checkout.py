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

"""The checkout record ``materialize_repository`` leaves beside a tree.

A checkout is a tree without a ``.git`` directory: nothing in the workspace
said where it came from or what has changed in it since. The record,
``<path>/.xtm-checkout``, holds the provider, the repository, the ref,
the commit the provider resolved it to when it names one, the ``sub_paths``
kept and the sha256 and size of every file written. :func:`changes_since`
compares the workspace manifest against it: the files modified, added or
removed under the recorded ``sub_paths`` (the whole tree when none), which
is what ``create_github_commit(from_checkout=...)`` commits.
:func:`format_status` and :func:`file_diff` are the ``git status`` and
``git diff`` of a checkout (``sandbox_checkout_status`` /
``sandbox_checkout_diff``). :func:`advance` moves the record to the
commit ``create_github_commit(from_checkout=...)`` made from it, as a commit
moves ``HEAD``: what was committed is no longer a change.

The record is a regular workspace file: ``sandbox_list_files`` lists it, the
sandbox can read it, and a second checkout at the same path replaces it with
the tree. It never leaves the workspace: :func:`changes_since` skips it.
"""

from __future__ import annotations

import dataclasses
import difflib
import hashlib
import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from app.services.sandbox_workspace.checks import RUNS_DIR
from app.services.sandbox_workspace.gitignore import GitIgnore
from app.services.sandbox_workspace.paths import WorkspacePathError, is_bytecode_cache, is_under, normalize
from app.services.sandbox_workspace.reading import decode, is_text

#: The record's file name, under the checkout's directory. It has no extension on purpose: a formatter's
#: directory walk (``prettier --check .``) infers no parser for it and skips it, where the JSON name was
#: formatted and failed the check (gap checkout-record-trips-format-check).
RECORD_NAME = ".xtm-checkout"
#: The name a record written before the rename carries: read, recognised and replaced at the next write.
LEGACY_RECORD_NAME = ".xtm-checkout.json"
#: Every name a checkout record may carry, the current one first.
RECORD_NAMES = (RECORD_NAME, LEGACY_RECORD_NAME)
#: The record's format version: a reader refuses a version it does not know.
RECORD_VERSION = 1
#: Directory names an install or a tool writes: a file under one, outside the scope, is never named (#10144).
GENERATED_DIRS = frozenset(
    {"node_modules", ".venv", "site-packages", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache"}
)


def is_record_name(name: str) -> bool:
    """Whether *name* (a file name or a path) names a checkout record, under either name."""
    return name.rsplit("/", 1)[-1] in RECORD_NAMES


def record_root(path: str) -> str | None:
    """The directory of the checkout record at *path* (``""`` at the workspace root), ``None`` when it is none."""
    head, _sep, name = path.rpartition("/")
    return head if name in RECORD_NAMES else None


def record_paths(root: str) -> tuple[str, ...]:
    """The paths a record of the checkout at *root* may sit at, the current name first."""
    return tuple(f"{root}/{name}" if root else name for name in RECORD_NAMES)


def held_record_path(entries: Iterable[str], root: str) -> str | None:
    """The record path of the checkout at *root* that *entries* hold (the current name first), ``None`` when none."""
    held = entries if isinstance(entries, Mapping | set | frozenset) else set(entries)
    return next((path for path in record_paths(root) if path in held), None)


async def read_record_bytes(session: Any, root: str) -> tuple[Any, bytes]:
    """``session.get_bytes`` of the record of the checkout at *root*: the current name, else the legacy one.

    Raises :class:`FileNotFoundError` when neither is held.
    """
    current, legacy = record_paths(root)
    try:
        return await session.get_bytes(current)
    except FileNotFoundError:
        return await session.get_bytes(legacy)


async def write_record(session: Any, record: CheckoutRecord, *, operation: str) -> Any:
    """Write *record* at its path and remove a legacy-named record of the same checkout, in one commit."""
    return await session.put_tree(
        [(record.record_path, record.to_json())], remove=[record.legacy_record_path], operation=operation
    )


def _shown(paths: tuple[str, ...] | list[str], limit: int) -> str:
    """*paths* joined, the first *limit* of them, the rest counted."""
    return ", ".join(paths[:limit]) + (f", +{len(paths) - limit} more" if len(paths) > limit else "")


def is_generated(repo_path: str) -> bool:
    """A check's output, a bytecode cache or a file of an installed or cache directory."""
    return (
        is_under(repo_path, RUNS_DIR)
        or is_bytecode_cache(repo_path)
        or any(segment in GENERATED_DIRS for segment in repo_path.split("/"))
    )


class CheckoutRecordError(ValueError):
    """The record is missing, unreadable or of an unknown version; ``str()`` says what to do."""


@dataclass(frozen=True)
class RecordedFile:
    sha256: str
    size: int


@dataclass(frozen=True)
class CheckoutRecord:
    provider: str
    repository: str
    ref: str
    #: The commit the ref resolved to when the provider named it, else "".
    commit: str
    #: The checkout's directory in the workspace (normalized).
    path: str
    #: The sub_paths kept, relative to the repository root; empty for the whole tree.
    sub_paths: tuple[str, ...] = ()
    #: Repository-relative path -> what was written.
    files: Mapping[str, RecordedFile] = field(default_factory=dict)
    #: Repository-relative path -> sha256 of each file under the checkout, outside its ``sub_paths``
    #: and ``files``, when the record was first written (a file an earlier, wider tree left): the
    #: baseline an out-of-scope edit is told by (#10144). Carried forward by :func:`advance`,
    #: :func:`grown` and a rebase, so no record rewrite hides an edit. ``None`` on a record written
    #: before it: the record's write time is the fallback.
    outside_baseline: Mapping[str, str] | None = None
    #: The assignment run that wrote the tree, "" in a conversation's workspace or on an older record: a clean
    #: checkout of an ended run is reclaimed at the file limit (``eviction.py``). Carried by every move.
    run_id: str = ""
    #: The commit a ``from_checkout`` call is about to make (#12377): ``{"branch", "parent", "files":
    #: {path: {"blob", "sha256", "size"}}, "removed": [...]}``, written before the commit and cleared by
    #: :func:`advance`. A call cut after the commit landed leaves it, and the next ``from_checkout``
    #: adopts the landed commit (:func:`adopt_pending`) instead of merging the workspace against its own lines.
    pending: Mapping[str, Any] | None = None
    #: The commit the branch's own change counts from: the commit the tree was checked out (or rebased) at,
    #: kept when :func:`advance` moves ``commit`` to a commit made from the checkout. "" = ``commit``.
    base_commit: str = ""
    #: Repository path -> ``A`` (added), ``M`` (modified) or ``D`` (deleted) by the commits made from the
    #: checkout since ``base_commit``: what a check on the branch's change still counts once committed.
    branch: Mapping[str, str] = field(default_factory=dict)

    @property
    def baseline(self) -> str:
        """The commit the branch's change counts from: ``base_commit``, else ``commit``."""
        return self.base_commit or self.commit

    def branch_paths(self) -> dict[str, str]:
        """The paths the commits made from the checkout added (``A``) or modified (``M``) since the baseline."""
        return {p: mark for p, mark in self.branch.items() if mark in ("A", "M")}

    @property
    def record_path(self) -> str:
        return f"{self.path}/{RECORD_NAME}"

    @property
    def legacy_record_path(self) -> str:
        """Where a record written before the rename sits: removed by every write of the record."""
        return f"{self.path}/{LEGACY_RECORD_NAME}"

    def workspace_path(self, repo_path: str) -> str:
        return f"{self.path}/{repo_path}"

    def repo_path(self, workspace_path: str) -> str | None:
        """*workspace_path* relative to the repository root, or ``None`` outside the checkout."""
        prefix = self.path + "/"
        if not workspace_path.startswith(prefix):
            return None
        return workspace_path[len(prefix) :]

    def covers(self, repo_path: str) -> bool:
        """Whether *repo_path* is under one of the recorded sub_paths (every path when none)."""
        if not self.sub_paths:
            return True
        return any(is_under(repo_path, sub) for sub in self.sub_paths)

    def outside(self, workspace_paths: Iterable[str]) -> list[str]:
        """The repository paths of *workspace_paths* (under the checkout) that its sub_paths and record do not cover."""
        out: list[str] = []
        for path in workspace_paths:
            repo_path = self.repo_path(path)
            if repo_path and repo_path not in RECORD_NAMES and not self.tracks(repo_path):
                out.append(repo_path)
        return sorted(set(out))

    def tracks(self, repo_path: str) -> bool:
        """Whether a change of *repo_path* belongs to the checkout: under its sub_paths, or recorded.

        A file committed from the checkout outside its ``sub_paths`` (named
        in ``paths``) is recorded by :func:`advance`, so its later edits
        are changes, as git tracks a committed file.
        """
        return repo_path in self.files or self.covers(repo_path)

    def to_json(self) -> bytes:
        payload = {
            "version": RECORD_VERSION,
            "provider": self.provider,
            "repository": self.repository,
            "ref": self.ref,
            "commit": self.commit,
            "path": self.path,
            "sub_paths": list(self.sub_paths),
            "files": {p: {"sha256": f.sha256, "size": f.size} for p, f in sorted(self.files.items())},
        }
        if self.outside_baseline is not None:
            payload["outside"] = dict(sorted(self.outside_baseline.items()))
        if self.run_id:
            payload["run_id"] = self.run_id
        if self.pending:
            payload["pending"] = dict(self.pending)
        if self.base_commit:
            payload["base"] = {"commit": self.base_commit, "paths": dict(sorted(self.branch.items()))}
        return (json.dumps(payload, indent=1) + "\n").encode("utf-8")

    @classmethod
    def from_json(cls, data: bytes, *, path: str) -> CheckoutRecord:
        where = f"{path}/{RECORD_NAME}"
        try:
            payload = json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise CheckoutRecordError(f"{where} is not a checkout record ({exc}).") from exc
        if not isinstance(payload, dict):
            raise CheckoutRecordError(f"{where} is not a checkout record.")
        if payload.get("version") != RECORD_VERSION:
            raise CheckoutRecordError(
                f"{where} is a checkout record of version {payload.get('version')!r}; this platform reads "
                f"version {RECORD_VERSION}: check the repository out again."
            )
        raw_files = payload.get("files")
        if not isinstance(raw_files, dict):
            raise CheckoutRecordError(f"{where} has no file list.")
        files: dict[str, RecordedFile] = {}
        for repo_path, meta in raw_files.items():
            if not isinstance(meta, dict) or not isinstance(meta.get("sha256"), str):
                raise CheckoutRecordError(f"{where}: entry {str(repo_path)[:80]!r} is malformed.")
            files[str(repo_path)] = RecordedFile(sha256=meta["sha256"], size=int(meta.get("size") or 0))
        raw_outside = payload.get("outside")
        outside = (
            {str(p): str(sha) for p, sha in raw_outside.items() if isinstance(sha, str)}
            if isinstance(raw_outside, dict)
            else None
        )
        raw_base = payload.get("base")
        base = raw_base if isinstance(raw_base, dict) else {}
        raw_paths = base.get("paths")
        branch = (
            {str(p): str(m) for p, m in raw_paths.items() if m in ("A", "M", "D")}
            if isinstance(raw_paths, dict)
            else {}
        )
        return cls(
            provider=str(payload.get("provider") or ""),
            repository=str(payload.get("repository") or ""),
            ref=str(payload.get("ref") or ""),
            commit=str(payload.get("commit") or ""),
            path=path,
            sub_paths=tuple(str(s) for s in payload.get("sub_paths") or []),
            files=files,
            outside_baseline=outside,
            run_id=str(payload.get("run_id") or ""),
            pending=payload["pending"] if isinstance(payload.get("pending"), dict) else None,
            base_commit=str(base.get("commit") or ""),
            branch=branch,
        )

    def carried(
        self,
        *,
        files: Mapping[str, RecordedFile],
        ref: str | None = None,
        commit: str | None = None,
        sub_paths: tuple[str, ...] | None = None,
    ) -> CheckoutRecord:
        """This record moved (a commit, a grow, a rebase); the out-of-scope baseline keeps the paths still outside."""
        moved = dataclasses.replace(
            self,
            files=files,
            ref=self.ref if ref is None else ref,
            commit=self.commit if commit is None else commit,
            sub_paths=self.sub_paths if sub_paths is None else sub_paths,
        )
        if moved.outside_baseline is None:
            return moved
        kept = {p: sha for p, sha in moved.outside_baseline.items() if not moved.tracks(p)}
        return dataclasses.replace(moved, outside_baseline=kept)


def outside_baseline(
    path: str, sub_paths: Iterable[str], recorded: Iterable[str], entries: Mapping[str, object]
) -> dict[str, str]:
    """The sha256 of each file the workspace holds under checkout *path*, outside *sub_paths* and *recorded*.

    *entries* is the workspace manifest before the tree is written: what an
    earlier, wider tree left, the baseline :func:`changes_since` names an
    out-of-scope edit against (#10144). Empty for a whole-tree checkout.
    """
    subs = [normalize(s) for s in sub_paths]
    if not subs:
        return {}
    known = set(recorded)
    prefix = normalize(path) + "/"
    out: dict[str, str] = {}
    for workspace_path, entry in entries.items():
        text = str(workspace_path)
        if not text.startswith(prefix):
            continue
        repo_path = text[len(prefix) :]
        if repo_path in RECORD_NAMES or repo_path in known or any(is_under(repo_path, s) for s in subs):
            continue
        if is_generated(repo_path):
            continue
        out[repo_path] = str(getattr(entry, "sha256", "") or "")
    return out


def outside_record_note(record: CheckoutRecord, workspace_paths: Iterable[str], *, limit: int = 12) -> str:
    """The sentence warning that a write landed outside the checkout's record and sub_paths, or ``""``.

    Such a file is never a change of the checkout (:meth:`CheckoutRecord.tracks`): status says clean and
    ``from_checkout`` leaves it out, so the editing tool says it at once instead of letting the edit go
    unseen until the commit.
    """
    out = [p for p in record.outside(workspace_paths) if not is_generated(p)]
    if not out:
        return ""
    return (
        f" Warning: {len(out)} written file(s) lie outside the checkout record of {record.path}/ "
        f"(sub_paths {', '.join(record.sub_paths) or 'none'}): {_shown(out, limit)}. Status, diff and "
        f'create_github_commit(from_checkout="{record.path}") do not count them: grow the checkout with '
        f'materialize_repository(path="{record.path}", add_paths=[...]), or name them in the commit\'s paths '
        "together with the checkout's sub_paths, else the edit is not committed."
    )


def build_record(
    *,
    provider: str,
    repository: str,
    ref: str,
    commit: str,
    path: str,
    sub_paths: Iterable[str],
    files: Iterable[tuple[str, bytes]],
    outside: Mapping[str, str] | None = None,
    run_id: str = "",
) -> CheckoutRecord:
    """The record of a tree about to be written at *path*; *files* are repository-relative.

    *outside* is the out-of-scope baseline (:func:`outside_baseline`); *run_id* the run writing it.
    """
    return CheckoutRecord(
        provider=provider,
        repository=repository,
        ref=ref,
        commit=commit,
        path=normalize(path),
        sub_paths=tuple(sub_paths),
        files={p: RecordedFile(sha256=hashlib.sha256(data).hexdigest(), size=len(data)) for p, data in files},
        outside_baseline=dict(outside) if outside is not None else {},
        run_id=str(run_id or ""),
    )


@dataclass(frozen=True)
class CheckoutChanges:
    """What the workspace holds that the record does not, as repository-relative paths, sorted."""

    modified: tuple[str, ...] = ()
    added: tuple[str, ...] = ()
    removed: tuple[str, ...] = ()
    #: The ``paths`` asked for under which nothing changed: said in the result, never dropped silently.
    unmatched: tuple[str, ...] = ()
    #: Files outside the record and its ``sub_paths`` changed, added or removed against the
    #: record's out-of-scope baseline (#10144): never a change, but named so an edit there is
    #: never silently left out of a commit.
    untracked: tuple[str, ...] = ()
    #: Untracked files the checkout's ``.gitignore`` files exclude (a build's output): never a change, counted.
    ignored: int = 0
    #: Directories under the checkout that hold their own checkout record (another repository checked
    #: out inside this one): never a change, whole, named once.
    nested: tuple[str, ...] = ()
    #: Files outside the record and its ``sub_paths`` still as an earlier, wider tree left them (the
    #: out-of-scope baseline): not at the record's commit, so a check reading one may read an old
    #: version. Never a change; named so the leftover is never silently stale.
    leftover: tuple[str, ...] = ()

    @property
    def empty(self) -> bool:
        return not (self.modified or self.added or self.removed)

    @property
    def count(self) -> int:
        return len(self.modified) + len(self.added) + len(self.removed)

    def describe(self, *, limit: int = 12) -> str:
        parts: list[str] = []
        for label, paths in (("modified", self.modified), ("added", self.added), ("removed", self.removed)):
            if paths:
                parts.append(f"{len(paths)} {label} ({_shown(paths, limit)})")
        text = "; ".join(parts) if parts else "no change"
        if self.unmatched:
            text += f"; nothing changed under {', '.join(self.unmatched[:limit])}"
        return text

    def ignored_note(self) -> str:
        """The sentences counting the untracked files ``.gitignore`` leaves out and naming the nested checkouts, or ``""``."""
        text = ""
        if self.ignored:
            text += f" {self.ignored} ignored file(s) not listed (matched by the checkout's .gitignore files)."
        if self.nested:
            text += (
                f" {len(self.nested)} nested checkout(s) excluded whole, each with its own {RECORD_NAME} "
                f"({_shown(self.nested, 12)}): read or commit one with its own checkout path."
            )
        return text

    def leftover_note(self, record_path: str, *, limit: int = 12) -> str:
        """The sentence naming the files an earlier tree left outside the checkout's record, or ``""``."""
        if not self.leftover:
            return ""
        paths = self.leftover
        return (
            f" {len(paths)} file(s) under the checkout lie outside its record, as an earlier, wider tree left "
            f"them, not at the record's commit ({_shown(paths, limit)}): a check that reads one may read an old "
            f'version; grow the checkout with materialize_repository(path="{record_path}", add_paths=[...]) to '
            "bring them to the record's commit."
        )

    def untracked_note(self, record_path: str, *, limit: int = 12) -> str:
        """The sentence naming the files written outside the checkout's scope, or ``""``."""
        if not self.untracked:
            return ""
        paths = self.untracked
        return (
            f" {len(paths)} file(s) changed since the checkout lie outside its record and sub_paths and are not "
            f"counted ({_shown(paths, limit)}): to commit them, name them in paths together with the checkout's "
            f"sub_paths (paths replaces the selection, so the sub_paths left out are not committed), or grow the "
            f'checkout with materialize_repository(path="{record_path}", add_paths=[...]).'
        )


def changes_since(
    record: CheckoutRecord,
    entries: Mapping[str, object],
    *,
    paths: Iterable[str] | None = None,
    since: float | None = None,
    ignore: GitIgnore | None = None,
) -> CheckoutChanges:
    """The files of the checkout that differ from *record*.

    *entries* is the workspace manifest (workspace path -> an object with a
    ``sha256``), *paths* an optional filter of repository-relative files or
    directories. The record itself is never a change. Without *paths*, a file
    outside the recorded ``sub_paths`` is never a change: the tree never held
    it, so nothing tells an edit from a stray file. A file under a path named
    in *paths* is one wherever it is (a new test beside a narrow checkout):
    the caller named it, unless it is still byte for byte what the record's
    out-of-scope baseline holds (a file an earlier, wider tree left that
    nobody edited): that one is a ``leftover``, never an added file (#237).
    A named path under which nothing changed is listed in ``unmatched``. A
    Python bytecode cache the record does not hold (what an import from
    code_interpreter writes) is never a change (#9518).
    Without *paths*, a file outside the record and its sub_paths is listed in
    ``untracked`` when it differs from the record's out-of-scope baseline
    (added, edited or removed since the first checkout; carried through every
    record rewrite), or, on a record written before the baseline existed, when
    it was written at or after *since* (the record's write time). A check's
    output (``.xtm/runs``) and an installed or cache directory are never
    named: not a change, but named, so an edit there is never silently left
    out (#10144). A nested checkout (a directory under the checkout holding
    its own record: another repository checked out inside this one) is
    skipped whole, with or without *paths*, its files never a change nor
    untracked, and named once in ``nested``. A file the record does not hold that
    *ignore* (the checkout's ``.gitignore`` files) excludes, a build's output,
    is never a change nor named, only counted in ``ignored``; a recorded
    (tracked) file stays a change whatever ``.gitignore`` says, as in git.
    """
    wanted = [normalize(p) for p in paths or [] if str(p).strip().strip("/")]

    def selected(repo_path: str) -> bool:
        if wanted:
            return any(is_under(repo_path, w) for w in wanted)
        return record.tracks(repo_path)

    nested = sorted(
        {root for rel in (record.repo_path(str(p)) for p in entries) if rel is not None and (root := record_root(rel))}
    )

    def in_nested(repo_path: str) -> bool:
        return any(is_under(repo_path, root) for root in nested)

    baseline = record.outside_baseline
    current: dict[str, str] = {}
    outside_now: dict[str, str] = {}
    untracked: list[str] = []
    leftover: list[str] = []
    ignored = 0
    for workspace_path, entry in entries.items():
        repo_path = record.repo_path(str(workspace_path))
        if repo_path is None or repo_path in RECORD_NAMES:
            continue
        try:
            repo_path = normalize(repo_path)
        except WorkspacePathError:
            continue
        if in_nested(repo_path):
            continue
        if repo_path not in record.files and is_bytecode_cache(repo_path):
            continue
        if ignore and repo_path not in record.files and ignore.ignores(repo_path):
            ignored += 1
            continue
        sha = str(getattr(entry, "sha256", "") or "")
        if selected(repo_path):
            if wanted and baseline and sha and repo_path not in record.files and baseline.get(repo_path) == sha:
                # Named by paths but still as an earlier, wider tree left it: a leftover, never added (#237).
                leftover.append(repo_path)
            else:
                current[repo_path] = sha
            continue
        if wanted or is_generated(repo_path):
            continue
        if baseline is not None:
            outside_now[repo_path] = sha
            if baseline.get(repo_path) != sha:
                untracked.append(repo_path)
            else:
                leftover.append(repo_path)
        elif since is not None and float(getattr(entry, "mtime", 0) or 0) >= since:
            untracked.append(repo_path)
    if baseline is not None and not wanted:
        untracked.extend(p for p in baseline if p not in outside_now and not record.tracks(p) and not in_nested(p))
    modified = tuple(sorted(p for p, sha in current.items() if p in record.files and record.files[p].sha256 != sha))
    added = tuple(sorted(p for p in current if p not in record.files))
    removed = tuple(sorted(p for p in record.files if p not in current and selected(p) and not in_nested(p)))
    changed = (*modified, *added, *removed)
    unmatched = tuple(w for w in wanted if not any(is_under(p, w) for p in changed))
    return CheckoutChanges(
        modified=modified,
        added=added,
        removed=removed,
        unmatched=unmatched,
        untracked=tuple(sorted(untracked)),
        ignored=ignored,
        nested=tuple(nested),
        leftover=tuple(sorted(leftover)),
    )


def advance(
    record: CheckoutRecord,
    *,
    commit: str,
    ref: str,
    written: Mapping[str, bytes],
    removed: Iterable[str],
) -> CheckoutRecord:
    """*record* moved to *commit* on *ref*: what a commit made from the checkout leaves behind.

    *written* maps each repository-relative path the commit wrote to the
    bytes it wrote, *removed* names the paths it deleted. Every other file
    keeps its recorded hash: the commit did not touch it, so an edit the
    sandbox made to it since stays a change. The ``sub_paths`` are kept; a
    written file outside them is recorded, so it is tracked from then on.

    The baseline stays where the tree was checked out and each path the commit
    wrote or deleted is marked in ``branch``: a check on the branch's change
    (``affected = "files" | "lines"``) still counts the committed lines as the
    branch's own, never as pre-existing.
    """
    files = dict(record.files)
    branch = dict(record.branch)
    for repo_path in removed:
        path = normalize(repo_path)
        if branch.get(path) == "A":
            branch.pop(path)
        else:
            branch[path] = "D"
        files.pop(path, None)
    for repo_path, data in written.items():
        path = normalize(repo_path)
        mark = branch.get(path)
        if mark is None:
            branch[path] = "M" if path in record.files else "A"
        elif mark == "D":
            branch[path] = "M"
        files[path] = RecordedFile(sha256=hashlib.sha256(data).hexdigest(), size=len(data))
    moved = record.carried(ref=ref or record.ref, commit=commit or record.commit, files=files)
    return dataclasses.replace(moved, base_commit=record.baseline, branch=branch, pending=None)


def git_blob_sha(data: bytes) -> str:
    """The object id git gives *data* as a blob: what a tree or a compare lists for the file."""
    return hashlib.sha1(b"blob %d\0" % len(data) + data, usedforsecurity=False).hexdigest()


def with_pending(
    record: CheckoutRecord, *, branch: str, parent: str, written: Mapping[str, bytes], removed: Iterable[str]
) -> CheckoutRecord:
    """*record* noting the commit a ``from_checkout`` call is about to make on *branch* over *parent* (#12377)."""
    return dataclasses.replace(
        record,
        pending={
            "branch": branch,
            "parent": parent,
            "files": {
                normalize(path): {
                    "blob": git_blob_sha(data),
                    "sha256": hashlib.sha256(data).hexdigest(),
                    "size": len(data),
                }
                for path, data in sorted(written.items())
            },
            "removed": sorted(normalize(path) for path in removed),
        },
    )


def pending_matches(record: CheckoutRecord, *, parent: str, changed: Mapping[str, str | None]) -> bool:
    """Whether a commit over *parent* that *changed* these paths is the one the record's pending note describes.

    *changed* maps each path the commit changed against its parent to its blob id, ``None`` for
    a removal. It matches only when the parent is the pending parent (the branch tip the call
    read) and the paths and blobs are exactly the pending ones: the commit is then the commit
    the call made, so the record moves to it as the call would have moved it.
    """
    pending = record.pending
    if not pending or not parent or pending.get("parent") != parent:
        return False
    files = pending.get("files")
    removed = pending.get("removed")
    if not isinstance(files, dict) or not isinstance(removed, list):
        return False
    expected: dict[str, str | None] = {
        str(path): str(meta.get("blob") or "") for path, meta in files.items() if isinstance(meta, dict)
    }
    expected.update({str(path): None for path in removed})
    return bool(expected) and dict(changed) == expected


def adopt_pending(record: CheckoutRecord, *, commit: str) -> CheckoutRecord:
    """*record* moved to *commit*, the landed commit its pending note describes (:func:`pending_matches`).

    The same move :func:`advance` makes after a commit that answered: the files, the branch marks
    and the baseline as the cut call would have left them.
    """
    pending = record.pending or {}
    files = dict(record.files)
    branch = dict(record.branch)
    for path in pending.get("removed") or []:
        key = str(path)
        if branch.get(key) == "A":
            branch.pop(key)
        else:
            branch[key] = "D"
        files.pop(key, None)
    for path, meta in (pending.get("files") or {}).items():
        if not isinstance(meta, dict):
            continue
        key = str(path)
        mark = branch.get(key)
        if mark is None:
            branch[key] = "M" if key in record.files else "A"
        elif mark == "D":
            branch[key] = "M"
        files[key] = RecordedFile(sha256=str(meta.get("sha256") or ""), size=int(meta.get("size") or 0))
    moved = record.carried(ref=str(pending.get("branch") or record.ref), commit=commit, files=files)
    return dataclasses.replace(moved, base_commit=record.baseline, branch=branch, pending=None)


def advance_files(
    record: CheckoutRecord,
    *,
    repository: str,
    branch: str,
    parent: str,
    commit: str,
    written: Mapping[str, tuple[str, bytes]],
    committed: Iterable[str] = (),
) -> CheckoutRecord | None:
    """*record* moved to *commit*, made with ``files[]`` from its own files, or ``None`` when it cannot move.

    *written* maps each workspace path a ``workspace_path`` write read to the
    repository path it was committed at and its bytes; *committed* names every
    path the commit wrote or deleted. The record moves only when it is a
    checkout of *repository* at *parent* (the tip the commit was built on), so
    the new commit is the record's tree plus these writes and nothing else;
    each file of the checkout is committed at its own repository path; and no
    other path of the commit is one the checkout tracks (the record would then
    call a file unchanged that the branch changed). Otherwise the record stays
    where it is and the drift reconcile of the next ``from_checkout`` merges.
    """
    if not record.commit or record.commit != parent or not commit:
        return None
    if record.repository and record.repository.casefold() != repository.strip().casefold():
        return None
    mine: dict[str, bytes] = {}
    for workspace_path, (repo_path, data) in written.items():
        own = record.repo_path(workspace_path)
        if own is None:
            continue
        try:
            if normalize(own) != normalize(repo_path):
                return None
        except WorkspacePathError:
            return None
        mine[own] = data
    if not mine:
        return None
    for repo_path in committed:
        if repo_path not in mine and (record.tracks(repo_path) or repo_path in record.files):
            return None
    return advance(record, commit=commit, ref=branch, written=mine, removed=())


def stale_on_grow(record: CheckoutRecord, repo_path: str, entry: object, *, since: float | None = None) -> bool:
    """Whether the workspace file *entry* at *repo_path*, outside *record*, is a stale leftover rather than an edit.

    An ``add_paths`` grow reaches files an earlier, wider checkout at the same
    path left behind: the tree of an older commit, not an edit. One is stale
    when the record's out-of-scope baseline holds it with the hash it still
    has (untouched since the record was first written, #10144); a file absent
    from the baseline or changed since is an edit. On a record written before
    the baseline existed, a file written before the record (*since*, its
    manifest mtime) is stale. Without either, nothing is stale (#10998).
    """
    sha = str(getattr(entry, "sha256", "") or "")
    baseline = record.outside_baseline
    if baseline is not None:
        return bool(sha) and baseline.get(repo_path) == sha
    mtime = getattr(entry, "mtime", None)
    return since is not None and mtime is not None and float(mtime) < float(since)


def grown(record: CheckoutRecord, *, sub_paths: Iterable[str], files: Iterable[tuple[str, bytes]]) -> CheckoutRecord:
    """*record* grown by *sub_paths* and the *files* read for them at its commit.

    The sub_paths become the union (a path under another one is dropped),
    and each file the record does not hold yet is recorded with the hash it
    has at the record's commit: its base, so a later edit is a change and a
    rebase merges it. A file the record holds keeps its recorded hash.
    """
    merged = list(record.sub_paths)
    for sub in sub_paths:
        clean = normalize(sub)
        if clean not in merged:
            merged.append(clean)
    kept = tuple(s for s in merged if not any(s != o and is_under(s, o) for o in merged))
    recorded = dict(record.files)
    for repo_path, data in files:
        rel = normalize(repo_path)
        if rel not in recorded:
            recorded[rel] = RecordedFile(sha256=hashlib.sha256(data).hexdigest(), size=len(data))
    return record.carried(sub_paths=kept, files=recorded)


# -- git status / git diff of a checkout --------------------------------

#: Lines ``sandbox_checkout_status`` lists before counting the rest.
MAX_STATUS_LINES = 500
#: Files ``sandbox_checkout_diff`` diffs in one call; the others are named.
MAX_DIFF_FILES = 40
#: Characters of one file's diff before it is cut.
MAX_FILE_DIFF_CHARS = 20_000
#: Characters of a whole ``sandbox_checkout_diff`` answer.
MAX_DIFF_RESULT_CHARS = 60_000
#: Context lines around each hunk: the default and the ceiling.
DEFAULT_CONTEXT_LINES = 3
MAX_CONTEXT_LINES = 10


def origin(record: CheckoutRecord) -> str:
    """``owner/name@ref (commit abc123)``: where the checkout came from."""
    text = f"{record.repository}@{record.ref or 'default branch'}"
    return text + (f" (commit {record.commit[:12]})" if record.commit else "")


def baseline_origin(record: CheckoutRecord) -> str:
    """Where the branch's change counts from: :func:`origin`, or the checkout's commit and the commits made from it."""
    if not record.base_commit or record.base_commit == record.commit:
        return origin(record)
    return (
        f"{record.repository}@{record.base_commit[:12]}, the commit checked out, with the commits made from it "
        f"up to {record.ref or 'the branch'} ({record.commit[:12]})"
    )


def marked(changes: CheckoutChanges) -> list[tuple[str, str]]:
    """``(path, mark)`` per changed file, ``M`` / ``A`` / ``D``, sorted by path."""
    pairs = [(p, "M") for p in changes.modified]
    pairs.extend((p, "A") for p in changes.added)
    pairs.extend((p, "D") for p in changes.removed)
    return sorted(pairs)


def status_lines(changes: CheckoutChanges) -> list[str]:
    """One ``M`` / ``A`` / ``D`` line per changed file, sorted by path as ``git status --short`` does."""
    return [f"{mark} {path}" for path, mark in marked(changes)]


def format_status(record: CheckoutRecord, changes: CheckoutChanges, *, shown_as: str, limit: int = MAX_STATUS_LINES) -> str:
    """What ``sandbox_checkout_status`` answers: the origin, the counts, one line per file, bounded."""
    unmatched = f" Nothing changed under {', '.join(changes.unmatched)}." if changes.unmatched else ""
    untracked = changes.untracked_note(record.path) + changes.ignored_note() + changes.leftover_note(record.path)
    if changes.empty:
        return f"{shown_as}/ is clean: nothing changed since its checkout of {origin(record)}.{unmatched}{untracked}"
    lines = status_lines(changes)
    head = (
        f"{shown_as}/: checkout of {origin(record)}; {changes.count} file(s) changed: "
        f"{len(changes.modified)} modified, {len(changes.added)} added, {len(changes.removed)} removed."
        f"{unmatched} create_github_commit(from_checkout=\"{record.path}\") commits exactly these.{untracked}"
    )
    body = lines[:limit]
    if len(lines) > limit:
        body.append(f"[{len(lines) - limit} more file(s) not listed: narrow with paths]")
    return "\n".join([head, *body])


def clamp_diff_context(value: object) -> int:
    try:
        return max(0, min(int(value), MAX_CONTEXT_LINES))
    except (TypeError, ValueError):
        return DEFAULT_CONTEXT_LINES


def file_diff(
    repo_path: str,
    before: bytes | None,
    after: bytes | None,
    *,
    context: int = DEFAULT_CONTEXT_LINES,
    max_chars: int = MAX_FILE_DIFF_CHARS,
) -> str:
    """The unified diff of one file, ``None`` meaning the file does not exist on that side.

    A binary file is named, never shown; a diff past *max_chars* is cut and says so.
    """
    if (before is not None and not is_text(before)) or (after is not None and not is_text(after)):
        sizes = f"{len(before) if before is not None else 0:,} -> {len(after) if after is not None else 0:,} bytes"
        return f"Binary file {repo_path} differs ({sizes}).\n"
    old = decode(before).splitlines(keepends=True) if before is not None else []
    new = decode(after).splitlines(keepends=True) if after is not None else []
    lines = difflib.unified_diff(
        old,
        new,
        fromfile=f"a/{repo_path}" if before is not None else "/dev/null",
        tofile=f"b/{repo_path}" if after is not None else "/dev/null",
        n=context,
    )
    text = "".join(line if line.endswith("\n") else line + "\n\\ No newline at end of file\n" for line in lines)
    if not text:
        return f"{repo_path}: same content as checked out.\n"
    if len(text) > max_chars:
        return text[:max_chars] + f"\n... [diff of {repo_path} cut: {len(text) - max_chars:,} more characters]\n"
    return text
