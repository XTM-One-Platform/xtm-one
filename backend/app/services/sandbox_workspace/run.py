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

"""What one ``code_interpreter`` call did with the workspace, and how the result says it.

Every result of a call that had a workspace carries a :class:`WorkspaceCall`
(``result["workspace"]``): the kernel mode that ran, whether the workspace
was mounted, the bytes it moved and the manifest diff, or why nothing was
saved. :func:`format_call` is the one line the model reads.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass, field

from app.core.utils import format_exception_message
from app.services.sandbox_workspace import metrics
from app.services.sandbox_workspace.local import ScanResult, Snapshot
from app.services.sandbox_workspace.manifest import (
    ManifestDiff,
    ManifestEntry,
    QuotaExceededError,
    format_diff,
    format_size,
)
from app.services.sandbox_workspace.session import WorkspaceSession, WorkspaceUnavailableError

logger = logging.getLogger(__name__)

#: The code names the workspace (``workspace/x``, ``"workspace"``,
#: ``WORKSPACE_DIR``): the case worth a mount. Matched as a word, so a
#: variable named ``workspaces`` does not pay for one.
_WORKSPACE_REFERENCE_RE = re.compile(r"(?<![A-Za-z0-9_])workspace(?![A-Za-z0-9_])|WORKSPACE_DIR", re.IGNORECASE)

#: A literal path under the workspace inside a string: the opening quote right
#: before ``workspace/``, the closing quote right after the path, and nothing
#: after it that builds a longer path (``+``, ``%``, ``.format``, an adjacent
#: string). ``f"workspace/{name}"`` or ``"workspace/" + name`` do not match.
_WORKSPACE_LITERAL_RE = re.compile(
    r"""(?<=["'])workspace/([A-Za-z0-9_\-./*?\[\]]+)(["'])(?!\s*[+%.\["'])"""
)
_GLOB_CHARS = frozenset("*?[]")
#: Where the workspace's packages live: mounted whenever the workspace is, since an import may need them.
_PACKAGES_PREFIX = ".venv"
#: How far a scoped mount widens: a named file brings its directory, recursively (the sibling module an
#: import reaches, the file a join or an exec opens), while what the widening adds stays under both bounds.
WIDEN_BUDGET_BYTES = 32 * 1024 * 1024
WIDEN_BUDGET_FILES = 2_000

_Entries = dict[str, ManifestEntry]

STATELESS = "stateless"
SESSION = "session"


def code_names_workspace(code: str) -> bool:
    return _WORKSPACE_REFERENCE_RE.search(code or "") is not None


def named_workspace_paths(code: str) -> list[str] | None:
    """The workspace paths *code* names, when it names the workspace ONLY through literal paths.

    ``None`` means the whole workspace: the code names it bare (``"workspace"``,
    ``WORKSPACE_DIR``, ``os.listdir("workspace")``), builds a path under it
    (``f"workspace/{x}"``, ``"workspace/a" + b``), climbs out of it (``..``) or
    names its root. A glob keeps the directories before its first wildcard
    (``workspace/data/*.csv`` names ``data``). An empty list means the code does
    not name the workspace at all.
    """
    text = code or ""
    references = [m.start() for m in _WORKSPACE_REFERENCE_RE.finditer(text)]
    if not references:
        return []
    literals = {m.start(): m.group(1) for m in _WORKSPACE_LITERAL_RE.finditer(text)}
    if any(start not in literals for start in references):
        return None
    prefixes: set[str] = set()
    for raw in literals.values():
        parts: list[str] = []
        for segment in raw.split("/"):
            if _GLOB_CHARS.intersection(segment):
                break
            if segment == "..":
                return None
            if segment in {"", "."}:
                continue
            parts.append(segment)
        if not parts:
            return None
        prefixes.add("/".join(parts))
    return sorted(prefixes)


def _under(path: str, prefixes: list[str]) -> bool:
    return any(prefix == "" or path == prefix or path.startswith(prefix + "/") for prefix in prefixes)


def _directories_of(named: list[str], entries: _Entries) -> list[str]:
    """The directory trees a scoped mount widens to: a named directory itself, else the named path's directory."""
    trees: set[str] = set()
    for path in named:
        is_directory = any(entry.startswith(path + "/") for entry in entries)
        trees.add(path if is_directory else path.rpartition("/")[0])
    return sorted(trees)


@dataclass(frozen=True)
class LeftOut:
    """A directory tree a scoped mount did not widen to: over :data:`WIDEN_BUDGET_BYTES` or :data:`WIDEN_BUDGET_FILES`."""

    directory: str
    files: int
    size: int

    def describe(self) -> str:
        where = f"workspace/{self.directory}/" if self.directory else "workspace/"
        return f"{where} ({self.files:,} files, {format_size(self.size)})"


def plan_mount(code: str, entries: _Entries) -> tuple[_Entries | None, list[LeftOut]]:
    """The manifest entries a stateless run mounts, and the directory trees left out of a scoped mount.

    ``None`` means no mount (the code does not name the workspace and it holds
    no packages). A bare or built reference mounts everything. A run that names
    only literal paths gets those paths, the packages (``.venv/``) and the
    directory tree of every named path (a named file brings its directory,
    recursively), so an imported sibling module, a path joined at run time or a
    file an ``exec`` opens is there; the trees are added smallest first while the
    widening stays under :data:`WIDEN_BUDGET_BYTES` and :data:`WIDEN_BUDGET_FILES`,
    and a tree that does not fit is returned as left out (its named files are
    still mounted). What the run did not get is never reported as removed: the
    read-back compares against what was mounted.
    """
    named = named_workspace_paths(code)
    has_packages = any(path.startswith(_PACKAGES_PREFIX + "/") for path in entries)
    if named is None:
        return entries, []
    if not named and not has_packages:
        return None, []
    prefixes = [*named, _PACKAGES_PREFIX] if has_packages else named
    chosen = {path: entry for path, entry in entries.items() if _under(path, prefixes)}
    size = files = 0
    trees: list[tuple[int, str, _Entries]] = []
    for directory in _directories_of(named, entries):
        extra = {path: entry for path, entry in entries.items() if path not in chosen and _under(path, [directory])}
        if extra:
            trees.append((sum(entry.size for entry in extra.values()), directory, extra))
    left_out: list[LeftOut] = []
    for _size, directory, extra in sorted(trees, key=lambda tree: (tree[0], tree[1])):
        new = {path: entry for path, entry in extra.items() if path not in chosen}
        added = sum(entry.size for entry in new.values())
        if not new:
            continue
        if size + added <= WIDEN_BUDGET_BYTES and files + len(new) <= WIDEN_BUDGET_FILES:
            chosen.update(new)
            size += added
            files += len(new)
        else:
            left_out.append(LeftOut(directory=directory, files=len(new), size=added))
    return chosen, left_out


def mount_entries(code: str, entries: _Entries) -> _Entries | None:
    """The manifest entries a stateless run mounts (:func:`plan_mount` without what it left out)."""
    return plan_mount(code, entries)[0]


@dataclass
class WorkspaceCall:
    mode: str = STATELESS
    mounted: bool = False
    unavailable: str = ""
    bytes_read: int = 0
    bytes_written: int = 0
    diff: ManifestDiff = field(default_factory=ManifestDiff)
    #: Why nothing the call wrote under workspace/ was saved.
    refused: str = ""
    skipped: list[str] = field(default_factory=list)
    #: ``new`` / ``reused`` / ``restarted`` / ``reset`` (session mode only).
    kernel: str = ""
    kernel_note: str = ""
    size_bytes: int | None = None
    quota_bytes: int | None = None
    notes: list[str] = field(default_factory=list)
    #: ``(files mounted, files in the workspace)`` when the run mounted only the paths its code names.
    partial: tuple[int, int] | None = None
    #: The workspace files a scoped run was not given: never overwritten by what it wrote.
    unmounted: frozenset[str] = frozenset()
    #: The directory trees a scoped mount did not widen to (over the widening budget).
    left_out: list[LeftOut] = field(default_factory=list)
    #: Lines the result ends with: the dedicated tool for what the run wrote (an i18n catalog: sandbox_json_keys).
    hints: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "mode": self.mode,
            "mounted": self.mounted,
            "unavailable": self.unavailable,
            "bytes_read": self.bytes_read,
            "bytes_written": self.bytes_written,
            "diff": self.diff.as_dict(),
            "refused": self.refused,
            "skipped": list(self.skipped),
            "kernel": self.kernel,
            "size_bytes": self.size_bytes,
        }


_KERNEL_LINES = {
    "new": "a new kernel started for this conversation",
    "reused": "the conversation's kernel: variables and imports from its earlier session calls are defined",
    "restarted": (
        "a NEW kernel: the previous one was gone (idle timeout, killed after a timeout, or another server "
        "process), so variables and imports from earlier calls are NOT defined; files under workspace/ are"
    ),
    "reset": "a new kernel, as reset=true asked",
}


def format_call(call: WorkspaceCall) -> str:
    """The bracketed line a ``code_interpreter`` result starts with."""
    parts: list[str] = []
    if call.mode == SESSION:
        if call.kernel:
            parts.append(f"Kernel (session=true): {_KERNEL_LINES.get(call.kernel, call.kernel)}.")
        if call.kernel_note:
            parts.append(call.kernel_note)
    else:
        parts.append("Fresh process (session=false): no variable or import carries over to the next call.")
    if call.unavailable:
        parts.append(f"No sandbox workspace: {call.unavailable}.")
    elif call.mounted:
        moved = f"read {format_size(call.bytes_read)}, wrote {format_size(call.bytes_written)}"
        if call.partial is not None:
            moved = (
                f"only the paths the code names and their directories, {call.partial[0]:,} of {call.partial[1]:,} files; "
                f"{moved}"
            )
        if call.refused:
            parts.append(f"workspace/ was mounted ({moved}). {call.refused}")
        else:
            size = (
                f" Workspace now {format_size(call.size_bytes)} of {format_size(call.quota_bytes)}."
                if call.size_bytes is not None and call.quota_bytes
                else ""
            )
            parts.append(f"workspace/ was mounted ({moved}). {format_diff(call.diff)}{size}")
        if call.skipped:
            shown = ", ".join(call.skipped[:6]) + (f", +{len(call.skipped) - 6} more" if len(call.skipped) > 6 else "")
            parts.append(f"Not saved: {shown}.")
        if call.left_out:
            trees = ", ".join(tree.describe() for tree in call.left_out[:6])
            more = f", +{len(call.left_out) - 6} more" if len(call.left_out) > 6 else ""
            parts.append(
                f"Left out, over the {format_size(WIDEN_BUDGET_BYTES)} / {WIDEN_BUDGET_FILES:,}-file mount budget: "
                f"{trees}{more}. A file there is mounted only when the code names it as a literal "
                "'workspace/...' path."
            )
    parts.extend(call.notes)
    return "[" + " ".join(parts) + "]"


async def commit_scan(
    session: WorkspaceSession,
    scanned: ScanResult,
    call: WorkspaceCall,
    *,
    bytes_read: int,
    count_source_writes: bool = False,
) -> None:
    """Save what a run changed under workspace/, recording the outcome on *call*.

    *count_source_writes* (a code_interpreter run): a file it added or changed
    inside a ``materialize_repository`` checkout is recorded as a friction of
    the run (``frictions.note_code_interpreter_source_writes``); a check run by
    ``sandbox_run_command`` (a formatter, a build) leaves it off.
    """
    started = time.monotonic()
    call.bytes_read = bytes_read
    call.skipped = scanned.skipped
    if call.unmounted:
        # A path derived at run time (a string method, a join, a parent) can reach a file the run was not
        # given: it saw an empty place there, so what it wrote would replace the file without its content.
        kept = [p for p in scanned.upserts if p.entry.path not in call.unmounted]
        held = [p.entry.path for p in scanned.upserts if p.entry.path in call.unmounted]
        if held:
            scanned.upserts = kept
            call.skipped = [
                *call.skipped,
                *(f"{path} (exists but was not mounted: name it as a literal 'workspace/...' path)" for path in held),
            ]
    if scanned.unreadable:
        call.refused = scanned.unreadable + "."
        await session.note_usage(bytes_read=bytes_read)
        return
    if scanned.overflow:
        call.refused = (
            f"Workspace file limit exceeded: workspace/ holds more than {session.limits.max_files:,} files. "
            "Nothing it wrote under workspace/ was saved; remove files or pack them into an archive."
        )
        metrics.quota_refused("code_interpreter")
        await session.note_usage(bytes_read=bytes_read)
        return
    if not scanned.upserts and not scanned.removals:
        await session.note_usage(bytes_read=bytes_read)
        entries = await session.entries()
        call.size_bytes = sum(entry.size for entry in entries.values())
        call.quota_bytes = session.limits.quota_bytes
        return
    try:
        plan = await session.commit(scanned.upserts, scanned.removals, bytes_read=bytes_read, operation="code_interpreter")
    except QuotaExceededError as exc:
        call.refused = str(exc)
        await session.note_usage(bytes_read=bytes_read)
        return
    except WorkspaceUnavailableError as exc:
        call.refused = f"Nothing was saved: {exc}."
        return
    finally:
        metrics.observe_sync("commit", time.monotonic() - started)
    call.diff = plan.diff
    call.bytes_written = sum(p.entry.size for p in scanned.upserts)
    call.size_bytes = plan.size_bytes
    call.quota_bytes = session.limits.quota_bytes
    if count_source_writes and (plan.diff.added or plan.diff.changed):
        await _note_source_writes(session, [*plan.diff.added, *plan.diff.changed], call)


async def _note_source_writes(session: WorkspaceSession, written: list[str], call: WorkspaceCall) -> None:
    """Record the checkout files a code_interpreter run wrote as a friction, and its hints on *call*; never raises.

    A write to an i18n catalog of a checkout ends the result with the
    sandbox_json_keys call to use (``frictions.catalog_write_hint``), with or
    without a friction scope; the ledger records it only under one.
    """
    from app.services import frictions

    try:
        entries = await session.entries()
        call.hints.extend(
            frictions.code_interpreter_write_hints(frictions.checkout_source_writes(written, entries.keys()))
        )
        if frictions.current_friction_scope() is not None:
            frictions.note_code_interpreter_source_writes(written, entries.keys())
    except Exception as exc:  # best-effort: the ledger never changes the call
        logger.warning("Source-write friction not recorded: %s", format_exception_message(exc))


async def scan_in_thread(workspace_dir: str, snapshot, *, max_files: int) -> ScanResult:
    from app.services.sandbox_workspace.local import scan

    started = time.monotonic()
    try:
        return await asyncio.to_thread(scan, workspace_dir, snapshot, max_files=max_files)
    finally:
        metrics.observe_sync("scan", time.monotonic() - started)


@dataclass
class LocalMount:
    """The workspace of one local run: where it is mounted and what was put there."""

    session: WorkspaceSession
    call: WorkspaceCall
    workspace_dir: str
    snapshot: Snapshot | None = None


def _scoped(call: WorkspaceCall, mounted: _Entries, entries: _Entries, left_out: list[LeftOut] | None = None) -> None:
    if len(mounted) < len(entries):
        call.partial = (len(mounted), len(entries))
        call.unmounted = frozenset(path for path in entries if path not in mounted)
        call.left_out = list(left_out or [])


async def mount_local(
    session: WorkspaceSession,
    code: str,
    workspace_dir: str,
    *,
    mode: str = STATELESS,
    force: bool = False,
) -> LocalMount:
    """Put the workspace under *workspace_dir* when the run needs it; never raises."""
    from app.services.sandbox_workspace.local import materialize

    mount = LocalMount(session=session, call=WorkspaceCall(mode=mode), workspace_dir=workspace_dir)
    if not session.available:
        mount.call.unavailable = session.unavailable_reason
        return mount
    try:
        entries = await session.entries(refresh=True)
        wanted, left_out = (entries, []) if force else plan_mount(code, entries)
        if wanted is None:
            return mount
        mount.snapshot = await materialize(session, workspace_dir, entries=wanted)
        _scoped(mount.call, wanted, entries, left_out)
    except WorkspaceUnavailableError as exc:
        mount.call.unavailable = str(exc)
        return mount
    except Exception as exc:
        logger.warning("Could not mount the sandbox workspace of %s: %s", session.scope.label, format_exception_message(exc))
        mount.call.unavailable = f"its files could not be read just now ({format_exception_message(exc)})"
        mount.snapshot = None
        return mount
    mount.call.mounted = True
    return mount


@dataclass
class RemoteMount:
    """The workspace of one OpenSandbox run: brought in before the code, read back after it."""

    session: WorkspaceSession
    call: WorkspaceCall
    #: The manifest to bring in; ``None`` when the run does not mount it.
    entries: dict | None = None
    state: object | None = None
    env: dict = field(default_factory=dict)

    async def push(self, sandbox: object) -> None:
        if self.entries is None:
            return
        from app.services.sandbox_workspace.remote import install_tool, reconcile

        try:
            await install_tool(sandbox)
            self.state = await reconcile(sandbox, self.session, self.entries)
        except Exception as exc:
            logger.warning(
                "Could not put the sandbox workspace of %s in the container: %s",
                self.session.scope.label,
                format_exception_message(exc),
            )
            self.call.unavailable = f"its files could not be put in the sandbox ({format_exception_message(exc)})"
            self.state = None
            return
        self.call.mounted = True

    async def pull(self, sandbox: object) -> None:
        if self.state is None:
            return
        from app.services.sandbox_workspace.remote import collect

        try:
            scanned = await collect(sandbox, self.session, self.state)
        except QuotaExceededError as exc:
            metrics.quota_refused("code_interpreter")
            self.call.refused = str(exc)
            self.call.bytes_read = getattr(self.state, "bytes_read", 0)
            return
        except Exception as exc:
            logger.warning(
                "Could not read the sandbox workspace of %s back: %s", self.session.scope.label, format_exception_message(exc)
            )
            self.call.refused = f"Nothing it wrote under workspace/ was saved: {format_exception_message(exc)}."
            return
        await commit_scan(
            self.session, scanned, self.call, bytes_read=getattr(self.state, "bytes_read", 0), count_source_writes=True
        )

    def finish(self) -> WorkspaceCall:
        metrics.call(self.call.mode, "unavailable" if self.call.unavailable else "mounted" if self.call.mounted else "unmounted")
        return self.call


async def prepare_remote(session: WorkspaceSession, code: str) -> RemoteMount:
    """Decide, before the container is created, whether the run mounts the workspace; never raises."""
    from app.services.sandbox_workspace.paths import WORKSPACE_ENV
    from app.services.sandbox_workspace.remote import REMOTE_WORKSPACE

    mount = RemoteMount(session=session, call=WorkspaceCall(mode=STATELESS))
    if not session.available:
        mount.call.unavailable = session.unavailable_reason
        return mount
    try:
        entries = await session.entries(refresh=True)
    except WorkspaceUnavailableError as exc:
        mount.call.unavailable = str(exc)
        return mount
    except Exception as exc:
        mount.call.unavailable = f"its files could not be read just now ({format_exception_message(exc)})"
        return mount
    wanted, left_out = plan_mount(code, entries)
    if wanted is not None:
        mount.entries = wanted
        mount.env = {WORKSPACE_ENV: REMOTE_WORKSPACE}
        _scoped(mount.call, wanted, entries, left_out)
    return mount


async def finish_local(mount: LocalMount) -> WorkspaceCall:
    """Save what the run changed under its mounted workspace; the call's report."""
    if mount.snapshot is not None:
        try:
            scanned = await scan_in_thread(mount.workspace_dir, mount.snapshot, max_files=mount.session.limits.max_files)
            await commit_scan(
                mount.session, scanned, mount.call, bytes_read=mount.snapshot.bytes_read, count_source_writes=True
            )
        except Exception as exc:
            logger.warning(
                "Could not save the sandbox workspace of %s: %s", mount.session.scope.label, format_exception_message(exc)
            )
            mount.call.refused = f"Nothing it wrote under workspace/ was saved: {format_exception_message(exc)}."
    metrics.call(mount.call.mode, "unavailable" if mount.call.unavailable else "mounted" if mount.call.mounted else "unmounted")
    return mount.call
