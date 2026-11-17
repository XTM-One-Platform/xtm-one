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

"""The workspace in the local subprocess sandbox: put in a run's directory, read back after it.

:func:`materialize` writes every file of the manifest under
``<run dir>/workspace/`` with its recorded mtime and returns the
:class:`Snapshot` of what it wrote; :func:`scan` walks the directory once the
code ran and says what changed: a file whose size and mtime are the
snapshot's is unchanged without being read, any other is hashed. A symbolic
link (a file or a directory) is never followed and never saved, nor is
anything but a regular file; a ``workspace`` that is itself a link is not
read at all.

The bytes come from the object store through :class:`BlobCache`, a
per-process cache of the contents this worker downloaded (least recently
used out first, ``CODE_INTERPRETER_WORKSPACE_CACHE_MB``), so a workspace
used call after call is downloaded once per worker. The cache is a cache: the
manifest decides what a run gets.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import shutil
import stat
import tempfile
import time
import uuid
from dataclasses import dataclass, field

from app.services.sandbox_workspace import metrics
from app.services.sandbox_workspace.manifest import ManifestEntry
from app.services.sandbox_workspace.paths import WorkspacePathError, normalize
from app.services.sandbox_workspace.session import PendingFile, WorkspaceSession, sha256_file

logger = logging.getLogger(__name__)

_DOWNLOAD_CONCURRENCY = 8


@dataclass(frozen=True)
class SnapshotEntry:
    size: int
    mtime_ns: int
    sha256: str


@dataclass
class Snapshot:
    """What :func:`materialize` wrote: the baseline :func:`scan` compares against."""

    entries: dict[str, SnapshotEntry] = field(default_factory=dict)
    bytes_read: int = 0


@dataclass
class ScanResult:
    upserts: list[PendingFile] = field(default_factory=list)
    removals: list[str] = field(default_factory=list)
    #: Paths not saved, each with why (a link, an invalid name, not a file).
    skipped: list[str] = field(default_factory=list)
    #: The scan stopped: the directory holds more files than the workspace may.
    overflow: bool = False
    #: The ``workspace`` directory is not there any more (or is a link).
    unreadable: str = ""


class BlobCache:
    """The contents this process downloaded, by workspace and hash."""

    def __init__(self, root: str | None = None, max_bytes: int | None = None) -> None:
        if root is None:
            root = os.path.join(tempfile.gettempdir(), f"xtm-one-sandbox-cache-{os.getpid()}")
        if max_bytes is None:
            from app.core.config import settings

            max_bytes = max(0, int(settings.code_interpreter_workspace_cache_mb)) * 1024 * 1024
        self.root = root
        self.max_bytes = max_bytes
        self._locks: dict[str, asyncio.Lock] = {}

    @property
    def enabled(self) -> bool:
        return self.max_bytes > 0

    def path_for(self, workspace_id: uuid.UUID, sha256: str) -> str:
        return os.path.join(self.root, str(workspace_id), sha256)

    async def ensure(self, session: WorkspaceSession, workspace_id: uuid.UUID, entry: ManifestEntry) -> tuple[str, bool]:
        """The cached file of *entry*'s content, downloaded when missing; ``(path, downloaded)``."""
        path = self.path_for(workspace_id, entry.sha256)
        lock = self._locks.setdefault(path, asyncio.Lock())
        async with lock:
            if os.path.isfile(path) and os.path.getsize(path) == entry.size:
                with contextlib.suppress(OSError):
                    os.utime(path)
                return path, False
            os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
            # A short name: the content's path is already long, and Windows
            # caps a path at 260 characters.
            partial = os.path.join(os.path.dirname(path), f".{uuid.uuid4().hex[:12]}.part")
            try:
                await session.store.read_blob_to_path(workspace_id, entry.sha256, partial)
                os.replace(partial, path)
            finally:
                with contextlib.suppress(OSError):
                    os.remove(partial)
        self._locks.pop(path, None)
        return path, True

    def evict(self, keep: set[str] | None = None) -> int:
        """Remove the least recently used contents until the cache fits; returns bytes freed."""
        if not os.path.isdir(self.root):
            return 0
        files: list[tuple[float, int, str]] = []
        total = 0
        for root, _dirs, names in os.walk(self.root):
            for name in names:
                path = os.path.join(root, name)
                try:
                    st = os.stat(path)
                except OSError:
                    continue
                total += st.st_size
                files.append((st.st_mtime, st.st_size, path))
        freed = 0
        if total <= self.max_bytes:
            return 0
        for _mtime, size, path in sorted(files):
            if keep and path in keep:
                continue
            with contextlib.suppress(OSError):
                os.remove(path)
                total -= size
                freed += size
            if total <= self.max_bytes:
                break
        return freed

    def forget(self, workspace_id: uuid.UUID) -> None:
        shutil.rmtree(os.path.join(self.root, str(workspace_id)), ignore_errors=True)


_cache: BlobCache | None = None


def process_cache() -> BlobCache:
    global _cache
    if _cache is None:
        _cache = BlobCache()
    return _cache


def _target(workspace_dir: str, rel: str) -> str:
    return os.path.join(workspace_dir, *rel.split("/"))


def _place(src: str, dst: str, mtime: float) -> int:
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    shutil.copyfile(src, dst)
    os.utime(dst, (mtime, mtime))
    return os.stat(dst).st_mtime_ns


async def materialize(
    session: WorkspaceSession,
    workspace_dir: str,
    *,
    cache: BlobCache | None = None,
    entries: dict[str, ManifestEntry] | None = None,
    existing: Snapshot | None = None,
) -> Snapshot:
    """Write the manifest's files under *workspace_dir*; return what was written.

    *existing* is what the directory already holds (a kernel's directory,
    kept between calls): a path it holds with the manifest's content is left
    alone, the rest is written and what the manifest no longer lists is
    removed.
    """
    started = time.monotonic()
    os.makedirs(workspace_dir, exist_ok=True)
    if entries is None:
        entries = await session.entries(refresh=True)
    snapshot = Snapshot()
    record = await session.record(create=False) if entries else None
    if existing is not None:
        for rel in list(existing.entries):
            if rel not in entries:
                with contextlib.suppress(OSError):
                    os.remove(_target(workspace_dir, rel))
    todo: list[ManifestEntry] = []
    for entry in entries.values():
        held = existing.entries.get(entry.path) if existing is not None else None
        path = _target(workspace_dir, entry.path)
        if held is not None and held.sha256 == entry.sha256 and os.path.isfile(path):
            try:
                st = os.stat(path)
            except OSError:
                todo.append(entry)
                continue
            if st.st_size == held.size and st.st_mtime_ns == held.mtime_ns:
                snapshot.entries[entry.path] = held
                continue
        todo.append(entry)
    if todo and record is not None:
        cache = cache if cache is not None else process_cache()
        semaphore = asyncio.Semaphore(_DOWNLOAD_CONCURRENCY)

        async def _direct(entry: ManifestEntry, dst: str) -> int:
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            await session.store.read_blob_to_path(record.id, entry.sha256, dst)
            os.utime(dst, (entry.mtime, entry.mtime))
            return os.stat(dst).st_mtime_ns

        async def _one(entry: ManifestEntry) -> None:
            async with semaphore:
                dst = _target(workspace_dir, entry.path)
                downloaded = True
                if cache.enabled:
                    src, downloaded = await cache.ensure(session, record.id, entry)
                    try:
                        mtime_ns = await asyncio.to_thread(_place, src, dst, entry.mtime)
                    except FileNotFoundError:
                        # Evicted by another call between the two steps.
                        mtime_ns = await _direct(entry, dst)
                        downloaded = True
                else:
                    mtime_ns = await _direct(entry, dst)
                snapshot.entries[entry.path] = SnapshotEntry(entry.size, mtime_ns, entry.sha256)
                if downloaded:
                    snapshot.bytes_read += entry.size

        await asyncio.gather(*(_one(entry) for entry in todo))
        if cache.enabled:
            await asyncio.to_thread(
                cache.evict, {cache.path_for(record.id, e.sha256) for e in entries.values()}
            )
    metrics.observe_sync("materialize", time.monotonic() - started)
    return snapshot


def scan(workspace_dir: str, snapshot: Snapshot, *, max_files: int) -> ScanResult:
    """What the run changed under *workspace_dir*, against *snapshot*. Blocking: run it in a thread."""
    result = ScanResult()
    if os.path.islink(workspace_dir):
        result.unreadable = "workspace/ was replaced by a symbolic link: nothing under it was saved"
        return result
    if not os.path.isdir(workspace_dir):
        result.unreadable = "workspace/ was removed or replaced: nothing under it was saved"
        return result
    seen: set[str] = set()
    count = 0
    for root, dirs, names in os.walk(workspace_dir, followlinks=False):
        for name in list(dirs):
            full = os.path.join(root, name)
            if os.path.islink(full):
                dirs.remove(name)
                result.skipped.append(f"{os.path.relpath(full, workspace_dir).replace(os.sep, '/')} (a link)")
        for name in names:
            full = os.path.join(root, name)
            rel_raw = os.path.relpath(full, workspace_dir).replace(os.sep, "/")
            try:
                st = os.lstat(full)
            except OSError:
                continue
            if stat.S_ISLNK(st.st_mode):
                result.skipped.append(f"{rel_raw} (a link)")
                continue
            if not stat.S_ISREG(st.st_mode):
                result.skipped.append(f"{rel_raw} (not a regular file)")
                continue
            try:
                rel = normalize(rel_raw)
            except WorkspacePathError:
                result.skipped.append(f"{rel_raw[:120]} (a name the workspace does not keep)")
                continue
            count += 1
            if count > max_files:
                result.overflow = True
                return result
            seen.add(rel)
            held = snapshot.entries.get(rel)
            if held is not None and held.size == st.st_size and held.mtime_ns == st.st_mtime_ns:
                continue
            sha = sha256_file(full)
            if held is not None and held.sha256 == sha:
                continue
            result.upserts.append(
                PendingFile(
                    ManifestEntry(path=rel, size=st.st_size, sha256=sha, mtime=st.st_mtime),
                    local_path=full,
                )
            )
    result.removals = sorted(path for path in snapshot.entries if path not in seen)
    return result


def snapshot_after(snapshot: Snapshot, scanned: ScanResult, workspace_dir: str) -> Snapshot:
    """The baseline of the next call in a directory that is kept (a kernel's)."""
    entries = dict(snapshot.entries)
    for path in scanned.removals:
        entries.pop(path, None)
    for pending in scanned.upserts:
        try:
            mtime_ns = os.stat(_target(workspace_dir, pending.entry.path)).st_mtime_ns
        except OSError:
            entries.pop(pending.entry.path, None)
            continue
        entries[pending.entry.path] = SnapshotEntry(pending.entry.size, mtime_ns, pending.entry.sha256)
    return Snapshot(entries=entries)
