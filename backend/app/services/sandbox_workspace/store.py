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

"""Where a workspace lives: its row and manifest in PostgreSQL, its bytes in the object store.

One object per distinct content, ``sandbox-workspaces/<workspace id>/<sha256>``:
two paths holding the same bytes share it, a content no path holds any more
is deleted once the commit that released it is in. The sandbox never sees a
key or a credential: the backend reads the objects and hands the sandbox the
files of ITS workspace only (``local.py``, ``remote.py``).

:class:`WorkspaceStore` is the production store, on sessions of its own (a
commit never rides the turn's transaction). :class:`MemoryWorkspaceStore`
keeps the same contract in memory, for the tests and for a run with no
database. Both answer :class:`WorkspaceAccessError` for a workspace that
belongs to another person: the scope's user must be the row's.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import uuid
from collections.abc import AsyncIterator, Callable, Iterable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Any, Protocol

from app.core.utils import format_exception_message
from app.services.sandbox_workspace.manifest import CommitPlan, Limits, ManifestEntry, plan_commit

logger = logging.getLogger(__name__)

#: Every workspace object lives under this prefix of the platform bucket.
OBJECT_PREFIX = "sandbox-workspaces/"
#: First key of the two-key advisory lock of :meth:`WorkspaceStore.lock` ("LABS").
_NAMED_LOCK_NS = 0x4C414253
_UPSERT_BATCH = 500


def blob_key(workspace_id: uuid.UUID, sha256: str) -> str:
    return f"{OBJECT_PREFIX}{workspace_id}/{sha256}"


def commit_statement_timeout_ms() -> int:
    """The statement timeout of one workspace commit transaction, in ms; 0 keeps the pool default."""
    from app.core.config import settings

    seconds = int(getattr(settings, "sandbox_workspace_commit_statement_timeout_secs", 0) or 0)
    return 0 if seconds <= 0 else min(seconds * 1000, 2_147_483_647)


class WorkspaceAccessError(PermissionError):
    """The scope's person is not the workspace's: it is never opened for them."""


class BlobReleasedError(RuntimeError):
    """A content the change set reuses without uploading it is no longer held by the workspace.

    The session skips the upload of a content the manifest held when it read
    it; a concurrent commit (another tool, a ``code_interpreter`` write-back,
    another replica) may release that content and delete its blob before this
    commit takes the row lock. Committing then would point a path at a blob
    that no longer exists: the file is listed but its bytes are gone. The
    commit is refused whole instead and *shas* names the contents to upload
    before trying again.
    """

    def __init__(self, shas: Iterable[str]) -> None:
        self.shas = frozenset(shas)
        super().__init__(f"{len(self.shas)} reused content(s) released by a concurrent commit")


def _released(current: dict[str, ManifestEntry], reused: Iterable[str]) -> frozenset[str]:
    """The contents of *reused* no path of *current* holds any more."""
    wanted = set(reused)
    if not wanted:
        return frozenset()
    return frozenset(wanted - {entry.sha256 for entry in current.values()})


@dataclass(frozen=True)
class WorkspaceScope:
    """Whose workspace: a conversation's, else an assignment run's, and the person it is for."""

    user_id: uuid.UUID | None
    conversation_id: uuid.UUID | None = None
    assignment_run_id: uuid.UUID | None = None

    @classmethod
    def build(
        cls,
        *,
        user_id: object = None,
        conversation_id: object = None,
        assignment_run_id: object = None,
    ) -> WorkspaceScope | None:
        """The scope of a turn or a run, ``None`` when it has neither a conversation nor a run.

        A conversation wins over a run: a chat turn delegated inside a run
        (a consult) works in the conversation the person sees.
        """
        from app.core.utils import safe_uuid

        user = safe_uuid(user_id) if user_id else None
        conversation = safe_uuid(conversation_id) if conversation_id else None
        if conversation is not None:
            return cls(user_id=user, conversation_id=conversation)
        run = safe_uuid(assignment_run_id) if assignment_run_id else None
        if run is not None:
            return cls(user_id=user, assignment_run_id=run)
        return None

    @property
    def label(self) -> str:
        if self.conversation_id is not None:
            return f"conversation {self.conversation_id}"
        return f"assignment run {self.assignment_run_id}"


@dataclass
class WorkspaceRecord:
    id: uuid.UUID
    user_id: uuid.UUID | None
    conversation_id: uuid.UUID | None
    assignment_run_id: uuid.UUID | None
    size_bytes: int = 0
    file_count: int = 0
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    last_used_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    last_gc_at: datetime | None = None
    kernel_restarts: int = 0
    bytes_read: int = 0
    bytes_written: int = 0
    kernel_state: dict | None = None


class BlobStore(Protocol):
    async def put(self, key: str, data: bytes) -> None: ...
    async def put_path(self, key: str, path: str) -> None: ...
    async def get(self, key: str) -> bytes: ...
    async def get_to_path(self, key: str, path: str) -> None: ...
    async def delete(self, keys: Iterable[str]) -> int: ...
    async def list_keys(self, prefix: str) -> list[str]: ...
    async def list_groups(self, prefix: str) -> list[str]: ...


class ObjectStoreBlobs:
    """The platform bucket, through ``file_storage``."""

    async def put(self, key: str, data: bytes) -> None:
        from app.services.file_storage import file_storage

        await file_storage.upload_to(data, key, "application/octet-stream")

    async def put_path(self, key: str, path: str) -> None:
        from app.services.file_storage import file_storage

        await file_storage.upload_path(path, key, "application/octet-stream")

    async def get(self, key: str) -> bytes:
        from app.services.file_storage import file_storage

        return await file_storage.download(key)

    async def get_to_path(self, key: str, path: str) -> None:
        from app.services.file_storage import file_storage

        await file_storage.download_to_path(key, path)

    async def delete(self, keys: Iterable[str]) -> int:
        """Bulk delete (S3 DeleteObjects): a workspace's thousands of objects in a few requests."""
        from app.services.file_storage import file_storage

        return await file_storage.delete_many(keys)

    async def list_keys(self, prefix: str) -> list[str]:
        from app.services.file_storage import file_storage

        return await file_storage.list_object_names(prefix, recursive=True)

    async def list_groups(self, prefix: str) -> list[str]:
        """The sub-prefixes right under *prefix* (``<prefix><id>/``), one listing level."""
        from app.services.file_storage import file_storage

        return [name for name in await file_storage.list_object_names(prefix, recursive=False) if name.endswith("/")]


class MemoryBlobs:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}

    async def put(self, key: str, data: bytes) -> None:
        self.objects[key] = bytes(data)

    async def put_path(self, key: str, path: str) -> None:
        with open(path, "rb") as fh:
            self.objects[key] = fh.read()

    async def get(self, key: str) -> bytes:
        from app.services.file_storage import FileNotFoundInStorageError

        if key not in self.objects:
            raise FileNotFoundInStorageError(key)
        return self.objects[key]

    async def get_to_path(self, key: str, path: str) -> None:
        data = await self.get(key)
        with open(path, "wb") as fh:
            fh.write(data)

    async def delete(self, keys: Iterable[str]) -> int:
        removed = 0
        for key in keys:
            if self.objects.pop(key, None) is not None:
                removed += 1
        return removed

    async def list_keys(self, prefix: str) -> list[str]:
        return sorted(key for key in self.objects if key.startswith(prefix))

    async def list_groups(self, prefix: str) -> list[str]:
        groups: set[str] = set()
        for key in self.objects:
            rest = key[len(prefix) :] if key.startswith(prefix) else ""
            if "/" in rest:
                groups.add(f"{prefix}{rest.split('/', 1)[0]}/")
        return sorted(groups)


class _BaseStore:
    """What both stores share: the blob half and the access rule."""

    blobs: Any

    @staticmethod
    def _check_owner(record: WorkspaceRecord, scope: WorkspaceScope) -> None:
        if record.user_id != scope.user_id:
            raise WorkspaceAccessError(
                f"The sandbox workspace of this {scope.label.split(' ', 1)[0]} belongs to another person: "
                "it is not opened for you."
            )

    async def read_blob(self, workspace_id: uuid.UUID, sha256: str) -> bytes:
        return await self.blobs.get(blob_key(workspace_id, sha256))

    async def read_blob_to_path(self, workspace_id: uuid.UUID, sha256: str, path: str) -> None:
        await self.blobs.get_to_path(blob_key(workspace_id, sha256), path)

    async def upload_blob(self, workspace_id: uuid.UUID, sha256: str, data: bytes | None = None, *, path: str = "") -> None:
        key = blob_key(workspace_id, sha256)
        if data is not None:
            await self.blobs.put(key, data)
        else:
            await self.blobs.put_path(key, path)

    async def drop_blobs(self, workspace_id: uuid.UUID, shas: Iterable[str]) -> int:
        keys = [blob_key(workspace_id, sha) for sha in shas]
        if not keys:
            return 0
        try:
            return await self.blobs.delete(keys)
        except Exception as exc:
            logger.warning(
                "Could not delete %d object(s) of sandbox workspace %s; the sweep retries: %s",
                len(keys),
                workspace_id,
                format_exception_message(exc),
            )
            return 0


class MemoryWorkspaceStore(_BaseStore):
    """The store's contract in memory: the unit tests, a run with no database."""

    def __init__(self, blobs: MemoryBlobs | None = None) -> None:
        self.blobs = blobs or MemoryBlobs()
        self.records: dict[uuid.UUID, WorkspaceRecord] = {}
        self.files: dict[uuid.UUID, dict[str, ManifestEntry]] = {}
        self._lock = asyncio.Lock()
        self._named: dict[tuple[uuid.UUID, str], asyncio.Lock] = {}

    @contextlib.asynccontextmanager
    async def lock(self, workspace_id: uuid.UUID, name: str) -> AsyncIterator[None]:
        """Serialise the callers naming *name* in *workspace_id* (one process: the unit tests)."""
        async with self._named.setdefault((workspace_id, name), asyncio.Lock()):
            yield

    def _find(self, scope: WorkspaceScope) -> WorkspaceRecord | None:
        for record in self.records.values():
            if scope.conversation_id is not None and record.conversation_id == scope.conversation_id:
                return record
            if scope.assignment_run_id is not None and record.assignment_run_id == scope.assignment_run_id:
                return record
        return None

    async def open(self, scope: WorkspaceScope, *, create: bool) -> WorkspaceRecord | None:
        async with self._lock:
            record = self._find(scope)
            if record is None:
                if not create:
                    return None
                record = WorkspaceRecord(
                    id=uuid.uuid4(),
                    user_id=scope.user_id,
                    conversation_id=scope.conversation_id,
                    assignment_run_id=scope.assignment_run_id,
                )
                self.records[record.id] = record
                self.files[record.id] = {}
            self._check_owner(record, scope)
            return replace(record)

    async def entries(self, workspace_id: uuid.UUID) -> dict[str, ManifestEntry]:
        return dict(self.files.get(workspace_id, {}))

    async def commit(
        self,
        workspace_id: uuid.UUID,
        upserts: Iterable[ManifestEntry],
        removals: Iterable[str],
        limits: Limits,
        *,
        bytes_read: int = 0,
        bytes_written: int = 0,
        reused: Iterable[str] = (),
    ) -> CommitPlan:
        async with self._lock:
            current = self.files.setdefault(workspace_id, {})
            released = _released(current, reused)
            if released:
                raise BlobReleasedError(released)
            upserts = list(upserts)
            removals = list(removals)
            plan = plan_commit(current, upserts, removals, limits)
            for entry in upserts:
                current[entry.path] = entry
            for path in removals:
                current.pop(path, None)
            record = self.records[workspace_id]
            record.size_bytes = plan.size_bytes
            record.file_count = plan.file_count
            record.bytes_read += bytes_read
            record.bytes_written += bytes_written
            record.last_used_at = datetime.now(UTC)
            if plan.orphaned:
                record.last_gc_at = record.last_used_at
        await self.drop_blobs(workspace_id, plan.orphaned)
        return plan

    async def note_usage(self, workspace_id: uuid.UUID, *, bytes_read: int = 0, bytes_written: int = 0) -> None:
        record = self.records.get(workspace_id)
        if record is not None:
            record.bytes_read += bytes_read
            record.bytes_written += bytes_written
            record.last_used_at = datetime.now(UTC)

    async def record_kernel(self, workspace_id: uuid.UUID, state: dict | None, *, restarted: bool) -> None:
        record = self.records.get(workspace_id)
        if record is not None:
            record.kernel_state = state
            record.kernel_restarts += int(restarted)

    async def get_record(self, workspace_id: uuid.UUID) -> WorkspaceRecord | None:
        record = self.records.get(workspace_id)
        return replace(record) if record is not None else None

    async def delete_workspace(self, workspace_id: uuid.UUID) -> int:
        self.files.pop(workspace_id, None)
        self.records.pop(workspace_id, None)
        return await self.blobs.delete(await self.blobs.list_keys(f"{OBJECT_PREFIX}{workspace_id}/"))

    async def workspaces(self) -> list[WorkspaceRecord]:
        return [replace(record) for record in self.records.values()]

    async def holders(
        self, user_id: uuid.UUID | None, path: str, *, exclude: uuid.UUID | None
    ) -> tuple[int, WorkspaceRecord | None]:
        """How many workspaces of *user_id* other than *exclude* hold *path*, and the one used last."""
        if user_id is None:
            return 0, None
        found = [
            record
            for record in self.records.values()
            if record.user_id == user_id and record.id != exclude and path in self.files.get(record.id, {})
        ]
        if not found:
            return 0, None
        return len(found), replace(max(found, key=lambda record: record.last_used_at))


class WorkspaceStore(_BaseStore):
    """The production store: PostgreSQL rows, object-store bytes."""

    def __init__(
        self,
        *,
        session_factory: Callable[[], Any] | None = None,
        blobs: BlobStore | None = None,
    ) -> None:
        self._session_factory = session_factory
        self.blobs = blobs or ObjectStoreBlobs()

    def _sessions(self) -> Any:
        if self._session_factory is not None:
            return self._session_factory()
        from app.core.database import async_session

        return async_session()

    @staticmethod
    def _record(row: Any) -> WorkspaceRecord:
        return WorkspaceRecord(
            id=row.id,
            user_id=row.user_id,
            conversation_id=row.conversation_id,
            assignment_run_id=row.assignment_run_id,
            size_bytes=int(row.size_bytes or 0),
            file_count=int(row.file_count or 0),
            created_at=row.created_at,
            last_used_at=row.last_used_at,
            last_gc_at=row.last_gc_at,
            kernel_restarts=int(row.kernel_restarts or 0),
            bytes_read=int(row.bytes_read or 0),
            bytes_written=int(row.bytes_written or 0),
            kernel_state=row.kernel_state,
        )

    @staticmethod
    def _scope_clause(scope: WorkspaceScope) -> Any:
        from app.models.sandbox_workspace import SandboxWorkspace

        if scope.conversation_id is not None:
            return SandboxWorkspace.conversation_id == scope.conversation_id
        return SandboxWorkspace.assignment_run_id == scope.assignment_run_id

    @staticmethod
    async def _run_work_item(db: Any, run_id: uuid.UUID) -> tuple[uuid.UUID | None, Any]:
        """The work item the inbox dispatched *run_id* for, and the run's agent; ``(None, None)`` otherwise."""
        from sqlalchemy import select

        from app.core.utils import safe_uuid
        from app.models.assignment import AssignmentRun
        from app.services.assignment_engine import dispatched_work_item_id

        found = (
            await db.execute(
                select(AssignmentRun.trigger_context, AssignmentRun.trigger_payload, AssignmentRun.agent_id).where(
                    AssignmentRun.id == run_id
                )
            )
        ).first()
        if found is None:
            return None, None
        item = dispatched_work_item_id(found[0], found[1])
        return (safe_uuid(item) if item else None), found[2]

    async def _adopt(self, db: Any, scope: WorkspaceScope, item_id: uuid.UUID, agent_id: Any) -> bool:
        """Hand the workspace an ended run of the same item left over to *scope*'s run (#9868).

        Only a workspace labelled with the same work item, whose run has ended
        (a live run's is never taken), run by the same agent for the same
        person; the newest one used. One conditional UPDATE: two runs racing
        for it, one wins and the other starts empty.
        """
        from sqlalchemy import select, update

        from app.models.assignment import AssignmentRun
        from app.models.sandbox_workspace import SandboxWorkspace as W
        from app.services.assignment_engine import TERMINAL_RUN_STATUSES

        left = (
            await db.execute(
                select(W.id, W.assignment_run_id)
                .join(AssignmentRun, AssignmentRun.id == W.assignment_run_id)
                .where(
                    W.work_item_id == item_id,
                    W.user_id.is_not_distinct_from(scope.user_id),
                    AssignmentRun.agent_id == agent_id,
                    AssignmentRun.status.in_(TERMINAL_RUN_STATUSES),
                )
                .order_by(W.last_used_at.desc())
                .limit(1)
            )
        ).first()
        if left is None:
            return False
        moved = await db.execute(
            update(W)
            .where(W.id == left[0], W.assignment_run_id == left[1])
            .values(assignment_run_id=scope.assignment_run_id, last_used_at=datetime.now(UTC))
        )
        await db.commit()
        if moved.rowcount:
            logger.info(
                "Sandbox workspace %s of work item %s handed over from run %s to run %s",
                left[0],
                item_id,
                left[1],
                scope.assignment_run_id,
            )
        return bool(moved.rowcount)

    async def open(self, scope: WorkspaceScope, *, create: bool) -> WorkspaceRecord | None:
        from sqlalchemy import select
        from sqlalchemy.dialects.postgresql import insert as pg_insert

        from app.models.sandbox_workspace import SandboxWorkspace

        async with self._sessions() as db:
            row = (await db.execute(select(SandboxWorkspace).where(self._scope_clause(scope)))).scalar_one_or_none()
            item_id = None
            if row is None and scope.assignment_run_id is not None:
                item_id, agent_id = await self._run_work_item(db, scope.assignment_run_id)
                if item_id is not None and await self._adopt(db, scope, item_id, agent_id):
                    row = (
                        await db.execute(select(SandboxWorkspace).where(self._scope_clause(scope)))
                    ).scalar_one_or_none()
            if row is None and create:
                now = datetime.now(UTC)
                index = ["conversation_id"] if scope.conversation_id is not None else ["assignment_run_id"]
                await db.execute(
                    pg_insert(SandboxWorkspace)
                    .values(
                        id=uuid.uuid4(),
                        user_id=scope.user_id,
                        conversation_id=scope.conversation_id,
                        assignment_run_id=scope.assignment_run_id,
                        work_item_id=item_id,
                        size_bytes=0,
                        file_count=0,
                        created_at=now,
                        last_used_at=now,
                        kernel_restarts=0,
                        bytes_read=0,
                        bytes_written=0,
                    )
                    .on_conflict_do_nothing(index_elements=index)
                )
                await db.commit()
                row = (
                    await db.execute(select(SandboxWorkspace).where(self._scope_clause(scope)))
                ).scalar_one_or_none()
            if row is None:
                return None
            record = self._record(row)
        self._check_owner(record, scope)
        return record

    async def get_record(self, workspace_id: uuid.UUID) -> WorkspaceRecord | None:
        from app.models.sandbox_workspace import SandboxWorkspace

        async with self._sessions() as db:
            row = await db.get(SandboxWorkspace, workspace_id)
            return self._record(row) if row is not None else None

    async def entries(self, workspace_id: uuid.UUID) -> dict[str, ManifestEntry]:
        from sqlalchemy import select

        from app.models.sandbox_workspace import SandboxWorkspaceFile as F

        async with self._sessions() as db:
            rows = (
                await db.execute(
                    select(F.path, F.size, F.sha256, F.mtime).where(F.workspace_id == workspace_id)
                )
            ).all()
        return {
            path: ManifestEntry(path=path, size=int(size), sha256=sha, mtime=float(mtime))
            for path, size, sha, mtime in rows
        }

    async def commit(
        self,
        workspace_id: uuid.UUID,
        upserts: Iterable[ManifestEntry],
        removals: Iterable[str],
        limits: Limits,
        *,
        bytes_read: int = 0,
        bytes_written: int = 0,
        reused: Iterable[str] = (),
    ) -> CommitPlan:
        """Apply the change set under the workspace's row lock, or refuse it whole.

        The objects of *upserts* are uploaded by the caller first; a content
        the commit releases is deleted once it is in. Concurrent commits of
        one workspace serialize on the row: the later one is planned against
        what the earlier one left, path by path (the last write of a path wins).
        *reused* names the contents the caller did not upload because the
        workspace held them when it looked: one no path holds under the lock
        any more raises :class:`BlobReleasedError`, nothing written.
        """
        from sqlalchemy import delete, select, update
        from sqlalchemy.dialects.postgresql import insert as pg_insert

        from app.models.sandbox_workspace import SandboxWorkspace as W
        from app.models.sandbox_workspace import SandboxWorkspaceFile as F

        upserts = list(upserts)
        removals = list(removals)
        now = datetime.now(UTC)
        async with self._sessions() as db:
            timeout_ms = commit_statement_timeout_ms()
            if timeout_ms:
                # A large change set, or a commit waiting on another one's row
                # lock, outlives the pool's 30 s default: this transaction alone
                # gets the longer bound (SET LOCAL, gone at its commit).
                from sqlalchemy import text

                await db.execute(text("SELECT set_config('statement_timeout', :ms, true)"), {"ms": str(timeout_ms)})
            locked = (
                await db.execute(select(W.id).where(W.id == workspace_id).with_for_update())
            ).scalar_one_or_none()
            if locked is None:
                raise WorkspaceAccessError("The sandbox workspace is gone (deleted meanwhile).")
            rows = (
                await db.execute(select(F.path, F.size, F.sha256, F.mtime).where(F.workspace_id == workspace_id))
            ).all()
            current = {
                path: ManifestEntry(path=path, size=int(size), sha256=sha, mtime=float(mtime))
                for path, size, sha, mtime in rows
            }
            released = _released(current, reused)
            if released:
                raise BlobReleasedError(released)
            plan = plan_commit(current, upserts, removals, limits)
            gone = [path for path in removals if path in current]
            for start in range(0, len(gone), _UPSERT_BATCH):
                await db.execute(
                    delete(F).where(F.workspace_id == workspace_id, F.path.in_(gone[start : start + _UPSERT_BATCH]))
                )
            latest: dict[str, ManifestEntry] = {}
            for entry in upserts:
                latest[entry.path] = entry
            values = [
                {
                    "workspace_id": workspace_id,
                    "path": entry.path,
                    "size": entry.size,
                    "sha256": entry.sha256,
                    "mtime": entry.mtime,
                    "updated_at": now,
                }
                for entry in latest.values()
                if entry.path not in set(removals)
            ]
            for start in range(0, len(values), _UPSERT_BATCH):
                stmt = pg_insert(F).values(values[start : start + _UPSERT_BATCH])
                stmt = stmt.on_conflict_do_update(
                    index_elements=[F.workspace_id, F.path],
                    set_={
                        "size": stmt.excluded.size,
                        "sha256": stmt.excluded.sha256,
                        "mtime": stmt.excluded.mtime,
                        "updated_at": stmt.excluded.updated_at,
                    },
                )
                await db.execute(stmt)
            values: dict = {
                "size_bytes": plan.size_bytes,
                "file_count": plan.file_count,
                "last_used_at": now,
                "bytes_read": W.bytes_read + bytes_read,
                "bytes_written": W.bytes_written + bytes_written,
            }
            if plan.orphaned:
                # The contents this commit released are deleted right after it.
                values["last_gc_at"] = now
            await db.execute(update(W).where(W.id == workspace_id).values(**values))
            await db.commit()
        await self.drop_blobs(workspace_id, plan.orphaned)
        return plan

    async def note_usage(self, workspace_id: uuid.UUID, *, bytes_read: int = 0, bytes_written: int = 0) -> None:
        """Count a call that changed nothing; keeps the workspace from aging out while it is used."""
        from sqlalchemy import update

        from app.models.sandbox_workspace import SandboxWorkspace as W

        with contextlib.suppress(Exception):
            async with self._sessions() as db:
                await db.execute(
                    update(W)
                    .where(W.id == workspace_id)
                    .values(
                        last_used_at=datetime.now(UTC),
                        bytes_read=W.bytes_read + bytes_read,
                        bytes_written=W.bytes_written + bytes_written,
                    )
                )
                await db.commit()

    @contextlib.asynccontextmanager
    async def lock(self, workspace_id: uuid.UUID, name: str) -> AsyncIterator[None]:
        """Serialise the callers naming *name* in *workspace_id* across API replicas.

        A transaction-scoped advisory lock on its own session, released when the
        block ends (the session closes and rolls back); the two-key form keeps it
        apart from the other advisory locks of the database.
        """
        from sqlalchemy import text

        async with self._sessions() as db:
            await db.execute(
                text("SELECT pg_advisory_xact_lock(CAST(:ns AS integer), hashtext(:scope))"),
                {"ns": _NAMED_LOCK_NS, "scope": f"{workspace_id}:{name}"},
            )
            yield

    async def record_kernel(self, workspace_id: uuid.UUID, state: dict | None, *, restarted: bool) -> None:
        from sqlalchemy import update

        from app.models.sandbox_workspace import SandboxWorkspace as W

        try:
            async with self._sessions() as db:
                await db.execute(
                    update(W)
                    .where(W.id == workspace_id)
                    .values(kernel_state=state, kernel_restarts=W.kernel_restarts + int(restarted))
                )
                await db.commit()
        except Exception as exc:
            logger.debug("Could not record the kernel of workspace %s: %s", workspace_id, format_exception_message(exc))

    async def holders(
        self, user_id: uuid.UUID | None, path: str, *, exclude: uuid.UUID | None
    ) -> tuple[int, WorkspaceRecord | None]:
        """How many workspaces of *user_id* other than *exclude* hold *path*, and the one used last.

        Only the same person's workspaces are read (a company-managed run, no
        person, reads none): the answer names where a file went, never its content.
        """
        from sqlalchemy import func, select

        from app.models.sandbox_workspace import SandboxWorkspace as W
        from app.models.sandbox_workspace import SandboxWorkspaceFile as F

        if user_id is None:
            return 0, None
        clauses = [W.user_id == user_id, F.path == path]
        if exclude is not None:
            clauses.append(W.id != exclude)
        held = select(W).join(F, F.workspace_id == W.id).where(*clauses)
        async with self._sessions() as db:
            count = (await db.execute(select(func.count()).select_from(held.subquery()))).scalar_one()
            if not count:
                return 0, None
            row = (await db.execute(held.order_by(W.last_used_at.desc()).limit(1))).scalar_one_or_none()
        return int(count), (self._record(row) if row is not None else None)

    async def delete_workspace(self, workspace_id: uuid.UUID) -> int:
        """Delete the workspace, its manifest and every object under its prefix. Returns objects removed.

        The prefix is listed rather than the manifest read: it also holds what
        a refused or interrupted commit uploaded and no row names. The rows go
        first, so an object is never removed from under a path still listed.
        """
        from sqlalchemy import delete, select

        from app.models.sandbox_workspace import SandboxWorkspace as W
        from app.models.sandbox_workspace import SandboxWorkspaceFile as F

        async with self._sessions() as db:
            shas = set(
                (await db.execute(select(F.sha256).where(F.workspace_id == workspace_id).distinct())).scalars().all()
            )
            await db.execute(delete(F).where(F.workspace_id == workspace_id))
            await db.execute(delete(W).where(W.id == workspace_id))
            await db.commit()
        keys = set(await self.blobs.list_keys(f"{OBJECT_PREFIX}{workspace_id}/"))
        keys |= {blob_key(workspace_id, sha) for sha in shas}
        return await self.blobs.delete(sorted(keys))


_default_store: WorkspaceStore | None = None


def default_store() -> WorkspaceStore:
    global _default_store
    if _default_store is None:
        _default_store = WorkspaceStore()
    return _default_store
