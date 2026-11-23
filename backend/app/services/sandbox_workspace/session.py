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

"""One turn's handle on its sandbox workspace: every read and write goes through it.

Built by the code-interpreter builder for its turn (or run) and shared by the
tools that touch the workspace: ``code_interpreter`` itself, the
``sandbox_*`` file tools, ``materialize_repository``, the package installs and
the ``to_workspace_path`` ingestion of other tools (``ingest.py``). It opens
the workspace lazily (a turn that never touches it creates no row), keeps the
manifest it read, and turns every write into one commit: the new contents
uploaded first (a content the workspace already holds is not uploaded again),
then the manifest changed under the workspace's row lock, held to its limits.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
import time
from collections.abc import AsyncIterator, Callable, Iterable, Iterator
from contextvars import ContextVar
from dataclasses import dataclass

from app.core.utils import format_exception_message
from app.services.sandbox_workspace import metrics
from app.services.sandbox_workspace.manifest import (
    CommitPlan,
    Limits,
    ManifestDiff,
    ManifestEntry,
    QuotaExceededError,
    format_manifest,
)
from app.services.sandbox_workspace.paths import is_under, normalize
from app.services.sandbox_workspace.store import (
    BlobReleasedError,
    MemoryWorkspaceStore,
    WorkspaceAccessError,
    WorkspaceRecord,
    WorkspaceScope,
    WorkspaceStore,
    default_store,
)

logger = logging.getLogger(__name__)

#: How long building the tool description may wait for the manifest.
PREPARE_TIMEOUT_SECONDS = 5.0
#: Object uploads in flight at once in one commit (a checkout's thousands of files).
UPLOAD_CONCURRENCY = 16
#: The named lock every read-modify-write of ``kernel_state`` holds (:meth:`WorkspaceSession.update_kernel_state`).
KERNEL_STATE_LOCK = "kernel_state"
#: How long a command or a lab waits for the workspace writes of its own tool round.
SETTLE_TIMEOUT_SECONDS = 60.0
#: How long it first lets the other calls of its round start: each goes through the loop's dispatch
#: (span, cancellation race, timeout) before its handler counts its write.
SETTLE_GRACE_SECONDS = 0.05
#: The sessions the current task writes through (``WorkspaceSession.writing``): a read made by a
#: write of the round (an edit reading the file it changes, a ``code_interpreter`` mounting what it
#: runs on) never waits for itself in :meth:`WorkspaceSession.settled`.
_WRITING: ContextVar[frozenset[int]] = ContextVar("workspace_session_writing", default=frozenset())


@dataclass(frozen=True)
class PendingFile:
    """A file a commit writes: its manifest entry and where its bytes are."""

    entry: ManifestEntry
    data: bytes | None = None
    local_path: str = ""


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def workspace_enabled() -> bool:
    from app.core.config import settings

    return bool(getattr(settings, "code_interpreter_workspace_enabled", True))


class WorkspaceUnavailableError(RuntimeError):
    """The turn has no workspace (no conversation nor run, the feature off, another person's)."""


class WorkspaceFileUnreadableError(RuntimeError):
    """A file the manifest names whose content is missing from storage, even after the manifest was read again.

    ``str()`` names the workspace path, never the storage key, for the model.
    """

    def __init__(self, path: str) -> None:
        self.path = path
        super().__init__(f"workspace/{path} could not be read: its content is missing from storage; write it again")


async def manifest_for_resume(
    conversation_id: object, user_id: object, *, assignment_run_id: object = None
) -> str | None:
    """The manifest a resumed turn's note shows; ``None`` when the conversation (or run) has no workspace.

    The sandbox commands still running in the workspace's containers follow
    it with their run_ids: the cut segment started them, and the resumed turn
    waits on each instead of starting it again (which the container refuses).
    """
    scope = (
        WorkspaceScope.build(user_id=user_id, conversation_id=conversation_id, assignment_run_id=assignment_run_id)
        if workspace_enabled()
        else None
    )
    if scope is None:
        return None
    session = WorkspaceSession(scope)
    try:
        record = await asyncio.wait_for(session.record(create=False), PREPARE_TIMEOUT_SECONDS)
    except Exception:
        return None
    if record is None:
        return None
    await session.prepare()
    return with_commands_in_flight(session.described, record.kernel_state)


def with_commands_in_flight(described: str | None, kernel_state: dict | None) -> str | None:
    """*described* followed by the sandbox commands *kernel_state* holds as still running, when there are any."""
    from app.services.sandbox_workspace.commands import in_flight_lines

    lines = in_flight_lines(kernel_state)
    if not lines:
        return described
    running = (
        f"{len(lines)} sandbox command(s) started before the interruption are still running: "
        + "; ".join(lines)
        + ". Wait for each with sandbox_run_command(run_id=...) instead of starting it again."
    )
    return f"{described}\n{running}" if described else running


class WorkspaceSession:
    def __init__(
        self,
        scope: WorkspaceScope | None,
        *,
        store: WorkspaceStore | MemoryWorkspaceStore | None = None,
        limits: Limits | None = None,
        manifest_limit: int | None = None,
    ) -> None:
        self.scope = scope
        self.store = store if store is not None else default_store()
        self._limits = limits
        self._manifest_limit = manifest_limit
        self._record: WorkspaceRecord | None = None
        self._entries: dict[str, ManifestEntry] | None = None
        self._refusal: str | None = None
        self._lock = asyncio.Lock()
        #: Workspace writes in flight (``writing``) and the event set when none is.
        self._writes_in_flight = 0
        self._settled_event = asyncio.Event()
        self._settled_event.set()
        #: The same for the writing tools alone, a ``code_interpreter`` call left out (``writing(run=True)``):
        #: what a ``code_interpreter`` call waits for before it mounts, so two of one round still run side by side.
        self._tool_writes_in_flight = 0
        self._tools_settled_event = asyncio.Event()
        self._tools_settled_event.set()
        #: One workspace-writing tool at a time (``write_turn``): each reads the
        #: files it changes, computes, and commits them whole, so two of one tool
        #: round on the same file would otherwise both succeed and the later
        #: commit drop the earlier change (a lost update).
        self._write_turn = asyncio.Lock()
        #: The manifest the tool description was written from (``prepare``).
        self.described: str | None = None
        #: Whether the workspace held installed packages when last read, or
        #: got some since (``packages.install``); ``False`` until read.
        self._has_packages = False

    # -- state ---------------------------------------------------------------

    @property
    def limits(self) -> Limits:
        if self._limits is None:
            self._limits = Limits.from_settings()
        return self._limits

    @property
    def manifest_limit(self) -> int:
        if self._manifest_limit is None:
            from app.core.config import settings

            self._manifest_limit = max(1, int(settings.code_interpreter_workspace_manifest_entries))
        return self._manifest_limit

    @property
    def available(self) -> bool:
        return self.scope is not None and workspace_enabled() and self._refusal is None

    @property
    def unavailable_reason(self) -> str:
        if self._refusal:
            return self._refusal
        if not workspace_enabled():
            return "the sandbox workspace is disabled on this platform"
        if self.scope is None:
            return "this session has no conversation or run to keep a workspace for"
        return ""

    @property
    def workspace_id(self):
        return self._record.id if self._record is not None else None

    async def record(self, *, create: bool) -> WorkspaceRecord | None:
        """The workspace row, opened once; created only when *create* (a write)."""
        if not self.available:
            raise WorkspaceUnavailableError(self.unavailable_reason)
        if self._record is not None:
            return self._record
        try:
            self._record = await self.store.open(self.scope, create=create)
        except WorkspaceAccessError as exc:
            self._refusal = str(exc)
            raise WorkspaceUnavailableError(self._refusal) from exc
        return self._record

    async def entries(self, *, refresh: bool = False) -> dict[str, ManifestEntry]:
        """The manifest; empty for a workspace never written."""
        if self._entries is not None and not refresh:
            return dict(self._entries)
        record = await self.record(create=False)
        self._entries = {} if record is None else await self.store.entries(record.id)
        self._has_packages = any(path.startswith(".venv/") for path in self._entries)
        return dict(self._entries)

    @property
    def may_hold_packages(self) -> bool:
        """Whether a call that does not name the workspace may still import from it."""
        return self._has_packages

    def note_packages(self) -> None:
        self._has_packages = True

    async def prepare(self) -> None:
        """Read the manifest the tool description shows; bounded and never raising."""
        if not self.available:
            self.described = None
            return
        try:
            entries = await asyncio.wait_for(self.entries(refresh=True), PREPARE_TIMEOUT_SECONDS)
        except WorkspaceUnavailableError:
            self.described = None
            return
        except Exception as exc:
            logger.warning(
                "Could not read the sandbox workspace manifest of %s: %s",
                self.scope.label if self.scope else "?",
                format_exception_message(exc),
            )
            self.described = "(its manifest could not be read just now: list it with sandbox_list_files)"
            return
        self.described = format_manifest(entries, limit=self.manifest_limit, quota_bytes=self.limits.quota_bytes)

    # -- writes --------------------------------------------------------------

    async def commit(
        self,
        files: Iterable[PendingFile],
        removals: Iterable[str] = (),
        *,
        bytes_read: int = 0,
        operation: str = "write",
    ) -> CommitPlan:
        """Upload what is new, then apply the change set; :class:`QuotaExceededError` refuses it whole."""
        files = list(files)
        removals = [path for path in removals]
        async with self._lock:
            record = await self.record(create=True)
            current = await self.entries(refresh=True)
            held = {entry.sha256 for entry in current.values()}
            # An early, optimistic check: a change set past the quota is
            # refused before anything is uploaded. The commit checks again
            # under the row lock.
            from app.services.sandbox_workspace.manifest import plan_commit

            plan_commit(current, [f.entry for f in files], removals, self.limits)
            uploaded: set[str] = set()
            written = 0
            started = time.monotonic()
            # One upload per distinct content, UPLOAD_CONCURRENCY at a time:
            # a repository checkout (materialize_repository) brings thousands
            # of small files, and one object-store round trip each, in
            # sequence, ran past the tool's time limit. What was
            # uploaded before a failure is dropped as it is after a refusal.
            to_upload: list[PendingFile] = []
            # Contents skipped because the workspace held them: the commit
            # checks under its row lock that they are still held (#1496).
            reused: dict[str, PendingFile] = {}
            for pending in files:
                sha = pending.entry.sha256
                if sha in uploaded:
                    continue
                if sha in held:
                    if sha not in reused or (reused[sha].data is None and not reused[sha].local_path):
                        reused[sha] = pending
                    continue
                if pending.data is None and not pending.local_path:
                    # A re-pointed entry (a move) whose blob a concurrent
                    # write released: nothing to upload it from.
                    raise FileNotFoundError(pending.entry.path)
                uploaded.add(sha)
                to_upload.append(pending)
            gate = asyncio.Semaphore(UPLOAD_CONCURRENCY)

            async def upload(pending: PendingFile) -> None:
                nonlocal written
                async with gate:
                    await self.store.upload_blob(record.id, pending.entry.sha256, pending.data, path=pending.local_path)
                written += pending.entry.size

            if record is None:  # a write creates the row (create=True): never None in practice
                raise WorkspaceUnavailableError(self.unavailable_reason or "The sandbox workspace could not be opened.")
            workspace_id = record.id

            async def upload_all(batch: list[PendingFile]) -> None:
                if batch:
                    # A task group: one upload failing cancels the others
                    # before the failure is raised.
                    async with asyncio.TaskGroup() as uploads:
                        for pending in batch:
                            uploads.create_task(upload(pending))

            try:
                await upload_all(to_upload)
                try:
                    plan = await self.store.commit(
                        workspace_id,
                        [f.entry for f in files],
                        removals,
                        self.limits,
                        bytes_read=bytes_read,
                        bytes_written=written,
                        reused=list(reused),
                    )
                except BlobReleasedError as released:
                    # A concurrent commit released a content this change set
                    # reused and deleted its blob: upload it from the bytes we
                    # have and commit once more, never a path without content.
                    metrics.operation(operation, "reused_blob_released")
                    again: list[PendingFile] = []
                    for sha in sorted(released.shas):
                        pending = reused[sha]
                        if pending.data is None and not pending.local_path:
                            raise FileNotFoundError(pending.entry.path) from released
                        uploaded.add(sha)
                        again.append(pending)
                    await upload_all(again)
                    plan = await self.store.commit(
                        workspace_id,
                        [f.entry for f in files],
                        removals,
                        self.limits,
                        bytes_read=bytes_read,
                        bytes_written=written,
                        reused=[sha for sha in reused if sha not in released.shas],
                    )
            except QuotaExceededError:
                metrics.quota_refused(operation)
                await self.store.drop_blobs(record.id, uploaded - held)
                raise
            except BaseException:
                # An upload that failed (a task group raises an ExceptionGroup):
                # the others may have landed, and nothing of the change set is
                # kept, so nothing of it stays in the store.
                await self.store.drop_blobs(record.id, uploaded - held)
                raise
            finally:
                metrics.observe_sync("upload", time.monotonic() - started)
            metrics.bytes_moved("written", written)
            metrics.bytes_moved("read", bytes_read)
            self._entries = None
            return plan

    async def put_bytes(self, path: str, data: bytes, *, mtime: float | None = None, operation: str = "put") -> tuple[ManifestEntry, CommitPlan]:
        rel = normalize(path)
        entry = ManifestEntry(path=rel, size=len(data), sha256=sha256_bytes(data), mtime=mtime or time.time())
        return entry, await self.commit([PendingFile(entry, data=data)], operation=operation)

    async def put_tree(
        self,
        files: Iterable[tuple[str, bytes]],
        *,
        under: str = "",
        replace: Iterable[str] = (),
        remove: Iterable[str] = (),
        operation: str = "put",
    ) -> CommitPlan:
        """Write *files* under *under*.

        *replace* names directories (relative to *under*, ``""`` for *under*
        itself) whose files the tree replaces: what they held and the tree
        does not is removed in the same commit. *remove* names workspace
        files (full paths) removed in the same commit too: one change set,
        checked against the quota after the removals, applied whole or not
        at all.
        """
        prefix = normalize(under, allow_root=True)
        now = time.time()
        pending: dict[str, PendingFile] = {}
        for path, data in files:
            rel = normalize(f"{prefix}/{normalize(path)}" if prefix else path)
            pending[rel] = PendingFile(
                ManifestEntry(path=rel, size=len(data), sha256=sha256_bytes(data), mtime=now), data=data
            )
        removals: list[str] = []
        scopes = [
            normalize(f"{prefix}/{normalize(sub)}" if prefix and sub else (sub or prefix), allow_root=True)
            for sub in replace
        ]
        dropped = [normalize(path) for path in remove]
        if scopes or dropped:
            current = await self.entries(refresh=True)
            removals = [p for p in current if p not in pending and any(is_under(p, scope) for scope in scopes)]
            removals += [p for p in dropped if p in current and p not in pending and p not in removals]
        return await self.commit(pending.values(), removals, operation=operation)

    async def relink(self, entries: Iterable[ManifestEntry], removals: Iterable[str], *, operation: str = "move") -> CommitPlan:
        """Point *entries* at blobs the workspace already holds and drop *removals*, in one commit; nothing is uploaded.

        A move: the moved files keep their content, so the change set is only
        manifest work. :class:`FileNotFoundError` when a blob is no longer held.
        """
        return await self.commit([PendingFile(entry) for entry in entries], removals, operation=operation)

    async def remove(self, paths: Iterable[str]) -> tuple[list[str], CommitPlan]:
        """Remove each path, or everything under a directory path; returns what went."""
        current = await self.entries(refresh=True)
        gone: list[str] = []
        for raw in paths:
            prefix = normalize(raw, allow_root=True)
            gone.extend(p for p in current if is_under(p, prefix) and p not in gone)
        if not gone:
            return [], CommitPlan(
                size_bytes=sum(e.size for e in current.values()),
                file_count=len(current),
                diff=ManifestDiff(),
                orphaned=frozenset(),
            )
        return gone, await self.commit([], gone, operation="remove")

    # -- reads ---------------------------------------------------------------

    async def get_bytes(self, path: str) -> tuple[ManifestEntry, bytes]:
        rel = normalize(path)
        entries = await self.entries(refresh=True)
        entry = entries.get(rel)
        if entry is None:
            raise FileNotFoundError(rel)
        entry, data = await self.read_entry(entry)
        metrics.bytes_moved("read", len(data))
        return entry, data

    async def not_found(self, path: str) -> str:
        """The refusal for *path*, absent from this workspace; it says where the file went when another one holds it.

        Each conversation and each assignment run has its own workspace: a
        run started by another (a work item's next run, a background copy, a
        listener) never sees the files the first one wrote. When a workspace
        of the same person holds *path*, the answer says so and names the
        supported hand-over; a path no workspace holds keeps the plain answer.
        Best effort: a failed lookup answers the plain refusal.
        """
        from app.services.sandbox_workspace.paths import display

        rel = normalize(path)
        plain = f"{display(rel)} is not in the workspace; list it with sandbox_list_files."
        if self.scope is None:
            return plain
        holders = getattr(self.store, "holders", None)
        if holders is None:
            return plain
        try:
            count, other = await holders(self.scope.user_id, rel, exclude=self.workspace_id)
        except Exception as exc:
            logger.debug("Could not look %s up in the other workspaces: %s", rel, format_exception_message(exc))
            return plain
        if not count or other is None:
            return plain
        where = WorkspaceScope(
            user_id=other.user_id, conversation_id=other.conversation_id, assignment_run_id=other.assignment_run_id
        ).label
        more = f" (and {count - 1} other workspace(s))" if count > 1 else ""
        return (
            f"{display(rel)} is not in this workspace, the one of {self.scope.label}: the workspace of {where}{more} "
            "holds it. Workspaces are not shared between conversations or runs (a background copy or a later run "
            "starts with its own): hand a file over as a conversation file (sandbox_get_file, then its file_id) or a "
            "work item attachment, or inline in the task text."
        )

    async def read_entry(self, entry: ManifestEntry) -> tuple[ManifestEntry, bytes]:
        """The content of *entry*, read through the current manifest when its blob is gone.

        A commit moves the manifest, then drops the blobs it orphaned: a read
        that took the manifest just before (a call of the same tool round, a
        resumed turn on another worker) names a blob that no longer exists.
        The manifest is then read again once and the path's current blob
        read: :class:`FileNotFoundError` when the path is gone,
        :class:`WorkspaceFileUnreadableError` when its blob is still missing.
        Returns the entry actually read and its bytes.
        """
        from app.services.file_storage import FileNotFoundInStorageError

        record = await self.record(create=False)
        if record is None:
            raise FileNotFoundError(entry.path)
        try:
            return entry, await self.store.read_blob(record.id, entry.sha256)
        except FileNotFoundInStorageError:
            pass
        current = (await self.entries(refresh=True)).get(entry.path)
        if current is None:
            raise FileNotFoundError(entry.path)
        try:
            return current, await self.store.read_blob(record.id, current.sha256)
        except FileNotFoundInStorageError:
            metrics.operation("read", "missing_blob")
            raise WorkspaceFileUnreadableError(entry.path) from None

    async def note_usage(self, *, bytes_read: int = 0, bytes_written: int = 0) -> None:
        if self._record is not None:
            await self.store.note_usage(self._record.id, bytes_read=bytes_read, bytes_written=bytes_written)
            if bytes_read:
                metrics.bytes_moved("read", bytes_read)

    # -- ordering within a tool round --------------------------------

    @contextlib.contextmanager
    def writing(self, *, run: bool = False) -> Iterator[None]:
        """Count one workspace write in flight, from before its first await to its end.

        The agent loop runs the calls of one response together: a read
        started next to an edit must not read the manifest before the edit
        committed (:meth:`settled`). Entered synchronously, so a write is
        counted the moment its task starts. *run*: a ``code_interpreter``
        call, which writes back what its code changed; it is waited for by
        every reader but not by another ``code_interpreter`` call
        (``settled(tools_only=True)``), so two of one round still run side
        by side. A writing tool marks its task (``_WRITING``): a read it makes
        itself never waits for its own write.
        """
        self._writes_in_flight += 1
        self._settled_event.clear()
        token = None
        if not run:
            self._tool_writes_in_flight += 1
            self._tools_settled_event.clear()
            token = _WRITING.set(_WRITING.get() | {id(self)})
        try:
            yield
        finally:
            if token is not None:
                with contextlib.suppress(ValueError):
                    _WRITING.reset(token)
                self._tool_writes_in_flight -= 1
                if self._tool_writes_in_flight <= 0:
                    self._tool_writes_in_flight = 0
                    self._tools_settled_event.set()
            self._writes_in_flight -= 1
            if self._writes_in_flight <= 0:
                self._writes_in_flight = 0
                self._settled_event.set()

    @contextlib.asynccontextmanager
    async def write_turn(self) -> AsyncIterator[None]:
        """Hold the session's write turn: workspace-writing tools run one after another.

        A writing tool reads the files it changes, computes the new content,
        then commits it whole; :meth:`commit` serializes only the last step,
        so two writes of one tool round on the same file each read the old
        content and the later commit silently drops the earlier change. The
        turn covers the whole read-compute-commit: the second write starts
        from what the first committed, in the order the calls were issued
        (``asyncio.Lock`` wakes its waiters first in, first out). Not
        reentrant: a writing tool never runs another registered one.
        """
        async with self._write_turn:
            yield

    @property
    def writes_in_flight(self) -> int:
        return self._writes_in_flight

    def _in_flight(self, tools_only: bool) -> int:
        return self._tool_writes_in_flight if tools_only else self._writes_in_flight

    async def settled(
        self,
        timeout: float = SETTLE_TIMEOUT_SECONDS,
        *,
        grace: float = SETTLE_GRACE_SECONDS,
        tools_only: bool = False,
    ) -> tuple[float, int]:
        """Wait until no workspace write of this session is in flight, at most *timeout* seconds.

        Sleeps *grace* first, so a write requested in the same tool round as
        the caller (started after it by ``asyncio.gather``) has reached its
        handler and is counted before the look. Returns (seconds waited for
        writes, writes still in flight at the end: 0 unless the bound was
        reached). A read made inside a writing tool's own task answers at
        once: it would wait for itself, and the write turn already orders it
        after the writes issued before it. *tools_only*: the writing tools
        alone, not the ``code_interpreter`` calls (what a ``code_interpreter``
        call waits for before it mounts the workspace).
        """
        if not tools_only and id(self) in _WRITING.get():
            return 0.0, 0
        await asyncio.sleep(max(0.0, grace))
        if self._in_flight(tools_only) <= 0:
            return 0.0, 0
        event = self._tools_settled_event if tools_only else self._settled_event
        started = time.monotonic()
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(event.wait(), timeout)
        return time.monotonic() - started, max(0, self._in_flight(tools_only))

    @contextlib.asynccontextmanager
    async def lock(self, name: str) -> AsyncIterator[None]:
        """Hold the workspace's lock named *name*: one holder at a time, across API replicas."""
        record = await self.record(create=True)
        if record is None:
            yield
            return
        async with self.store.lock(record.id, name):
            yield

    async def latest_kernel_state(self) -> dict | None:
        """The kernel state as stored now (another call may have written it since this session opened)."""
        record = await self.record(create=False)
        if record is None:
            return None
        latest = await self.store.get_record(record.id)
        return (latest or record).kernel_state

    async def update_kernel_state(self, change: Callable[[dict], dict | None], *, restarted: bool = False) -> None:
        """Apply *change* to the kernel state as stored now and write its answer, one writer at a time.

        ``kernel_state`` holds several records (the kernel's keys, ``labs``, ``services``, ``commands``) written by
        calls that may run in parallel (two services of one checkout started at once, a resumed run): a read then
        write of the whole object outside one lock let a writer put back another's older record (the lab's
        ``preview_url`` lost). *change* gets a copy and answers the state to write, or ``None`` to write nothing.
        """
        if await self.record(create=True) is None:
            return
        async with self.lock(KERNEL_STATE_LOCK):
            current = dict(await self.latest_kernel_state() or {})
            changed = change(current)
            if changed is not None:
                await self.record_kernel(changed, restarted=restarted)

    async def record_kernel(self, state: dict | None, *, restarted: bool) -> None:
        record = await self.record(create=True)
        await self.store.record_kernel(record.id, state, restarted=restarted)
        if self._record is not None:
            self._record.kernel_state = state
            self._record.kernel_restarts += int(restarted)
