"""A content a commit reuses without uploading it stays readable when a concurrent commit releases it (#1496).

The session skips the upload of a content the manifest held when it looked.
A concurrent commit that releases that content deletes its blob; the reusing
commit used to point its path at the deleted blob, and the file was listed
with its bytes gone (a resumed checkout: 171 files unreadable). The store now
checks the reused contents under its lock and the session uploads the
released ones before committing again.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from app.services.sandbox_workspace.manifest import CommitPlan, Limits
from app.services.sandbox_workspace.session import WorkspaceSession
from app.services.sandbox_workspace.store import BlobReleasedError, MemoryWorkspaceStore, WorkspaceScope

MB = 1024 * 1024


def _session(store: MemoryWorkspaceStore, user: uuid.UUID, conversation: uuid.UUID) -> WorkspaceSession:
    scope = WorkspaceScope(user_id=user, conversation_id=conversation)
    return WorkspaceSession(scope, store=store, limits=Limits(quota_bytes=8 * MB, max_files=100))


class _RacingStore(MemoryWorkspaceStore):
    """Its first commit is preceded by a concurrent one removing *path*, the only holder of its content."""

    def __init__(self) -> None:
        super().__init__()
        self.race_path = ""

    async def commit(
        self, workspace_id: uuid.UUID, upserts: Any, removals: Any, limits: Limits, **kwargs: Any
    ) -> CommitPlan:
        if self.race_path:
            path, self.race_path = self.race_path, ""
            await super().commit(workspace_id, [], [path], limits)
        return await super().commit(workspace_id, upserts, removals, limits, **kwargs)


class _CountingStore(MemoryWorkspaceStore):
    def __init__(self) -> None:
        super().__init__()
        self.uploads = 0

    async def upload_blob(
        self, workspace_id: uuid.UUID, sha256: str, data: bytes | None = None, *, path: str = ""
    ) -> None:
        self.uploads += 1
        await super().upload_blob(workspace_id, sha256, data, path=path)


async def test_a_reused_content_released_meanwhile_is_uploaded_again_and_stays_readable() -> None:
    store = _RacingStore()
    user, conversation = uuid.uuid4(), uuid.uuid4()
    await _session(store, user, conversation).put_bytes("old/a.py", b"shared = 1\n")
    other = _session(store, user, conversation)
    store.race_path = "old/a.py"
    await other.put_bytes("new/a.py", b"shared = 1\n")

    _entry, data = await _session(store, user, conversation).get_bytes("new/a.py")
    assert data == b"shared = 1\n"
    assert "old/a.py" not in await other.entries(refresh=True)


async def test_the_store_refuses_a_reused_content_no_path_holds() -> None:
    store = MemoryWorkspaceStore()
    session = _session(store, uuid.uuid4(), uuid.uuid4())
    await session.put_bytes("a.txt", b"one")
    record = await session.record(create=False)
    assert record is not None
    entry = (await session.entries(refresh=True))["a.txt"]
    await store.commit(record.id, [], ["a.txt"], session.limits)
    with pytest.raises(BlobReleasedError) as refused:
        await store.commit(record.id, [entry], [], session.limits, reused=[entry.sha256])
    assert refused.value.shas == {entry.sha256}
    assert await store.entries(record.id) == {}


async def test_a_reused_content_still_held_commits_without_an_upload() -> None:
    store = _CountingStore()
    session = _session(store, uuid.uuid4(), uuid.uuid4())
    await session.put_bytes("a.txt", b"same")
    store.uploads = 0
    await session.put_bytes("b.txt", b"same")
    assert store.uploads == 0
    _entry, data = await session.get_bytes("b.txt")
    assert data == b"same"
