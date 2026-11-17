"""The sandbox workspace store and its sweep on real PostgreSQL.

Rows in a scratch database, objects in memory (``MemoryBlobs``): the row
created once per conversation, the change set held to the quota under the row
lock, a released content deleted with ``last_gc_at`` stamped, another person
refused, and the sweep deleting the workspace of an archived, a deleted and an
idle conversation, and the objects of a prefix no row names. Skips without
PostgreSQL, like every ``integration`` test here.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import delete, select, update

import app.models  # noqa: F401 - registers every table on Base.metadata
from app.models.chat import Conversation
from app.models.sandbox_workspace import SandboxWorkspace, SandboxWorkspaceFile
from app.models.user import User
from app.services.sandbox_workspace import gc
from app.services.sandbox_workspace.manifest import Limits, ManifestEntry, QuotaExceededError
from app.services.sandbox_workspace.store import (
    OBJECT_PREFIX,
    MemoryBlobs,
    WorkspaceAccessError,
    WorkspaceScope,
    WorkspaceStore,
    blob_key,
)
from tests.scratch_database import ALL_TABLES, scratch_database

pytestmark = pytest.mark.integration


def _entry(path: str, data: bytes) -> ManifestEntry:
    import hashlib

    return ManifestEntry(path=path, size=len(data), sha256=hashlib.sha256(data).hexdigest(), mtime=1.0)


async def _conversation(scratch, email: str) -> tuple[uuid.UUID, uuid.UUID]:
    async with scratch.sessions() as db:
        user = User(email=email, full_name=email)
        db.add(user)
        await db.flush()
        conv = Conversation(user_id=user.id, channel="web")
        db.add(conv)
        await db.commit()
        return user.id, conv.id


async def test_the_store_keeps_one_workspace_per_conversation_and_holds_it_to_its_limits():
    async with scratch_database("sandbox_ws", tables=ALL_TABLES, extensions=("vector",), skip_without_extensions=True) as scratch:
        user_id, conv_id = await _conversation(scratch, "ws@example.com")
        blobs = MemoryBlobs()
        store = WorkspaceStore(session_factory=scratch.sessions, blobs=blobs)
        scope = WorkspaceScope(user_id=user_id, conversation_id=conv_id)
        first = await store.open(scope, create=True)
        again = await store.open(scope, create=True)
        assert first.id == again.id

        a, b = _entry("a.txt", b"aaa"), _entry("b.txt", b"bbbb")
        for entry, data in ((a, b"aaa"), (b, b"bbbb")):
            await store.upload_blob(first.id, entry.sha256, data)
        plan = await store.commit(first.id, [a, b], [], Limits(quota_bytes=100, max_files=10), bytes_written=7)
        assert plan.size_bytes == 7 and sorted(await store.entries(first.id)) == ["a.txt", "b.txt"]

        with pytest.raises(QuotaExceededError):
            await store.commit(first.id, [_entry("c.txt", b"c" * 200)], [], Limits(quota_bytes=100, max_files=10))
        assert sorted(await store.entries(first.id)) == ["a.txt", "b.txt"]

        a2 = _entry("a.txt", b"AAA")
        await store.upload_blob(first.id, a2.sha256, b"AAA")
        await store.commit(first.id, [a2], ["b.txt"], Limits(quota_bytes=100, max_files=10))
        assert blob_key(first.id, a.sha256) not in blobs.objects
        assert blob_key(first.id, b.sha256) not in blobs.objects
        record = await store.get_record(first.id)
        assert record.size_bytes == 3 and record.file_count == 1 and record.last_gc_at is not None
        assert record.bytes_written == 7

        await store.record_kernel(first.id, {"kernel_id": "k1"}, restarted=False)
        await store.record_kernel(first.id, {"kernel_id": "k2"}, restarted=True)
        assert (await store.get_record(first.id)).kernel_restarts == 1

        intruder = WorkspaceScope(user_id=uuid.uuid4(), conversation_id=conv_id)
        with pytest.raises(WorkspaceAccessError):
            await store.open(intruder, create=True)


async def test_the_sweep_deletes_archived_deleted_idle_and_orphaned_workspaces():
    async with scratch_database("sandbox_gc", tables=ALL_TABLES, extensions=("vector",), skip_without_extensions=True) as scratch:
        blobs = MemoryBlobs()
        store = WorkspaceStore(session_factory=scratch.sessions, blobs=blobs)
        ids = {}
        for name in ("archived", "deleted", "idle", "live"):
            user_id, conv_id = await _conversation(scratch, f"{name}@example.com")
            record = await store.open(WorkspaceScope(user_id=user_id, conversation_id=conv_id), create=True)
            entry = _entry("f.txt", name.encode())
            await store.upload_blob(record.id, entry.sha256, name.encode())
            await store.commit(record.id, [entry], [], Limits(quota_bytes=1000, max_files=10))
            ids[name] = (record.id, conv_id)
        orphan = uuid.uuid4()
        await blobs.put(f"{OBJECT_PREFIX}{orphan}/deadbeef", b"left behind")

        async with scratch.sessions() as db:
            await db.execute(update(Conversation).where(Conversation.id == ids["archived"][1]).values(is_archived=True))
            await db.execute(delete(Conversation).where(Conversation.id == ids["deleted"][1]))
            await db.execute(
                update(SandboxWorkspace)
                .where(SandboxWorkspace.id == ids["idle"][0])
                .values(last_used_at=datetime.now(UTC) - gc.idle_ttl() - timedelta(hours=1))
            )
            await db.commit()

        report = await gc.sweep(store=store, session_factory=scratch.sessions)
        assert report.deleted == {"conversation_archived": 1, "conversation_deleted": 1, "idle": 1}
        assert report.orphan_prefixes == 1

        async with scratch.sessions() as db:
            left = set((await db.execute(select(SandboxWorkspace.id))).scalars().all())
            files = set((await db.execute(select(SandboxWorkspaceFile.workspace_id))).scalars().all())
        assert left == {ids["live"][0]} and files == {ids["live"][0]}
        assert all(key.startswith(f"{OBJECT_PREFIX}{ids['live'][0]}/") for key in blobs.objects)


async def _run_workspace(scratch, store, status: str, *, used_hours_ago: float = 0) -> tuple[uuid.UUID, uuid.UUID]:
    """A run in *status* holding a one-file workspace (8 bytes), last used *used_hours_ago*."""
    from app.models.agent import Agent
    from app.models.assignment import AssignmentRun

    async with scratch.sessions() as db:
        user = User(email=f"run-{uuid.uuid4().hex[:8]}@example.com", full_name="Run")
        db.add(user)
        await db.flush()
        agent = Agent(id=uuid.uuid4(), name="Runner", user_id=user.id)
        db.add(agent)
        await db.flush()
        run = AssignmentRun(id=uuid.uuid4(), assignment_id=None, agent_id=agent.id, user_id=user.id, status=status)
        db.add(run)
        await db.commit()
        user_id, run_id = user.id, run.id
    record = await store.open(WorkspaceScope(user_id=user_id, assignment_run_id=run_id), create=True)
    entry = _entry("repo/a.py", b"12345678")
    await store.upload_blob(record.id, entry.sha256, b"12345678")
    await store.commit(record.id, [entry], [], Limits(quota_bytes=1000, max_files=10))
    if used_hours_ago:
        async with scratch.sessions() as db:
            await db.execute(
                update(SandboxWorkspace)
                .where(SandboxWorkspace.id == record.id)
                .values(last_used_at=datetime.now(UTC) - timedelta(hours=used_hours_ago))
            )
            await db.commit()
    return record.id, run_id


async def test_the_sweep_deletes_the_workspace_of_every_ended_run_and_says_what_it_freed(monkeypatch):
    """A terminal run's workspace goes (the backlog), a live run's after the run idle TTL only."""
    freed: list[gc.SweepReport] = []
    swept: list[gc.SweepReport] = []

    async def _record(report):
        freed.append(report)

    async def _sweep_done(report, *, finished_at):
        swept.append(report)

    async def _renew():
        return None

    monkeypatch.setattr(gc, "_record_freed", _record)
    monkeypatch.setattr(gc, "_record_sweep", _sweep_done)
    monkeypatch.setattr(gc, "_renew_lease", _renew)
    monkeypatch.setattr(gc, "SWEEP_BATCH", 2)
    async with scratch_database("sandbox_gc_runs", tables=ALL_TABLES, extensions=("vector",), skip_without_extensions=True) as scratch:
        blobs = MemoryBlobs()
        store = WorkspaceStore(session_factory=scratch.sessions, blobs=blobs)
        ended = [await _run_workspace(scratch, store, status) for status in ("completed", "failed", "cancelled", "skipped")]
        running, _ = await _run_workspace(scratch, store, "running")
        stale, _ = await _run_workspace(scratch, store, "running", used_hours_ago=gc.run_idle_ttl().total_seconds() / 3600 + 1)
        waiting, _ = await _run_workspace(scratch, store, "awaiting_human", used_hours_ago=gc.run_idle_ttl().total_seconds() / 3600 + 1)

        report = await gc.sweep(store=store, session_factory=scratch.sessions)
        # Four ended runs over batches of two: the passes collect the whole backlog in one sweep.
        assert report.deleted == {"run_ended": 4, "idle": 1}
        assert report.bytes_freed == {"run_ended": 32, "idle": 8}
        # What each pass freed is summed as it goes, the sweep recorded once at its end.
        assert len(freed) >= 2 and swept == [report]
        assert sum(step.deleted.get("run_ended", 0) for step in freed) == 4
        assert sum(step.bytes_freed.get("run_ended", 0) for step in freed) == 32

        async with scratch.sessions() as db:
            left = set((await db.execute(select(SandboxWorkspace.id))).scalars().all())
        assert left == {running, waiting}
        assert stale not in left and not any(wid in left for wid, _ in ended)
        assert all(key.split("/")[1] in {str(running), str(waiting)} for key in blobs.objects)


async def test_collect_runs_deletes_an_ended_run_workspace_and_keeps_a_live_one(monkeypatch):
    """The run-end path removes the workspace at once; a run read back not terminal keeps it."""

    async def _record(report):
        return None

    monkeypatch.setattr(gc, "_record_freed", _record)
    async with scratch_database("sandbox_gc_collect", tables=ALL_TABLES, extensions=("vector",), skip_without_extensions=True) as scratch:
        blobs = MemoryBlobs()
        store = WorkspaceStore(session_factory=scratch.sessions, blobs=blobs)
        done_ws, done_run = await _run_workspace(scratch, store, "completed")
        live_ws, live_run = await _run_workspace(scratch, store, "running")

        report = await gc.collect_runs([done_run, live_run], session_factory=scratch.sessions, store=store)
        assert report.deleted == {"run_ended": 1} and report.bytes_freed == {"run_ended": 8}
        async with scratch.sessions() as db:
            left = set((await db.execute(select(SandboxWorkspace.id))).scalars().all())
        assert left == {live_ws}
        assert done_ws not in left
        assert all(key.startswith(f"{OBJECT_PREFIX}{live_ws}/") for key in blobs.objects)


async def _item_run(scratch, *, user_id, agent_id, item_id, status: str = "running") -> uuid.UUID:
    """A run the inbox dispatched for *item_id* (server context and payload agree)."""
    from app.models.assignment import AssignmentRun

    async with scratch.sessions() as db:
        run = AssignmentRun(
            id=uuid.uuid4(),
            assignment_id=None,
            agent_id=agent_id,
            user_id=user_id,
            status=status,
            trigger_context={"type": "work_item", "work_item_id": str(item_id)},
            trigger_payload={"trigger_type": "work_item", "work_item_id": str(item_id)},
        )
        db.add(run)
        await db.commit()
        return run.id


async def _set_run_status(scratch, run_id: uuid.UUID, status: str) -> None:
    from app.models.assignment import AssignmentRun

    async with scratch.sessions() as db:
        await db.execute(update(AssignmentRun).where(AssignmentRun.id == run_id).values(status=status))
        await db.commit()


async def test_the_next_run_of_a_work_item_adopts_the_workspace_its_earlier_run_left(monkeypatch):
    """#9868: run A of item X writes work/foo.py and ends; run B of X finds it, another item's run does not."""
    from app.models.agent import Agent
    from app.models.work_item import WorkItem

    async def _record(report):
        return None

    async def _sweep_done(report, *, finished_at):
        return None

    async def _renew():
        return None

    monkeypatch.setattr(gc, "_record_freed", _record)
    monkeypatch.setattr(gc, "_record_sweep", _sweep_done)
    monkeypatch.setattr(gc, "_renew_lease", _renew)
    async with scratch_database("sandbox_ws_item", tables=ALL_TABLES, extensions=("vector",), skip_without_extensions=True) as scratch:
        async with scratch.sessions() as db:
            user = User(email="item-ws@example.com", full_name="Item")
            other_user = User(email="item-ws-other@example.com", full_name="Other")
            db.add_all([user, other_user])
            await db.flush()
            agent = Agent(id=uuid.uuid4(), name="Dev", user_id=user.id)
            other_agent = Agent(id=uuid.uuid4(), name="Other", user_id=user.id)
            db.add_all([agent, other_agent])
            await db.flush()
            item = WorkItem(title="X", user_id=user.id, status="in_progress")
            other_item = WorkItem(title="Y", user_id=user.id, status="in_progress")
            db.add_all([item, other_item])
            await db.commit()
            user_id, other_user_id, agent_id, other_agent_id = user.id, other_user.id, agent.id, other_agent.id
            item_id, other_item_id = item.id, other_item.id
        blobs = MemoryBlobs()
        store = WorkspaceStore(session_factory=scratch.sessions, blobs=blobs)
        limits = Limits(quota_bytes=1000, max_files=10)

        run_a = await _item_run(scratch, user_id=user_id, agent_id=agent_id, item_id=item_id)
        ws_a = await store.open(WorkspaceScope(user_id=user_id, assignment_run_id=run_a), create=True)
        entry = _entry("work/foo.py", b"print(1)")
        await store.upload_blob(ws_a.id, entry.sha256, b"print(1)")
        await store.commit(ws_a.id, [entry], [], limits)
        async with scratch.sessions() as db:
            assert (await db.get(SandboxWorkspace, ws_a.id)).work_item_id == item_id

        # A live run's workspace is never taken.
        run_live = await _item_run(scratch, user_id=user_id, agent_id=agent_id, item_id=item_id)
        assert await store.open(WorkspaceScope(user_id=user_id, assignment_run_id=run_live), create=False) is None

        # Run A ends while X is open: the run-end collect keeps its workspace.
        await _set_run_status(scratch, run_a, "completed")
        report = await gc.collect_runs([run_a], session_factory=scratch.sessions, store=store)
        assert report.deleted == {}
        assert (await gc.sweep(store=store, session_factory=scratch.sessions)).deleted.get("run_ended", 0) == 0

        # Another item's run, another agent's, another person's: nothing adopted.
        for scope_user, scope_agent, scope_item in (
            (user_id, agent_id, other_item_id),
            (user_id, other_agent_id, item_id),
            (other_user_id, agent_id, item_id),
        ):
            run = await _item_run(scratch, user_id=scope_user, agent_id=scope_agent, item_id=scope_item)
            assert await store.open(WorkspaceScope(user_id=scope_user, assignment_run_id=run), create=False) is None

        # A run whose payload names X without the dispatcher's own record adopts nothing.
        from app.models.assignment import AssignmentRun

        async with scratch.sessions() as db:
            forged = AssignmentRun(
                id=uuid.uuid4(),
                assignment_id=None,
                agent_id=agent_id,
                user_id=user_id,
                status="running",
                trigger_context={"type": "manual"},
                trigger_payload={"trigger_type": "work_item", "work_item_id": str(item_id)},
            )
            db.add(forged)
            await db.commit()
            forged_id = forged.id
        assert await store.open(WorkspaceScope(user_id=user_id, assignment_run_id=forged_id), create=False) is None

        # Run B of X adopts it, with work/foo.py.
        run_b = await _item_run(scratch, user_id=user_id, agent_id=agent_id, item_id=item_id)
        ws_b = await store.open(WorkspaceScope(user_id=user_id, assignment_run_id=run_b), create=False)
        assert ws_b is not None and ws_b.id == ws_a.id and ws_b.assignment_run_id == run_b
        assert "work/foo.py" in await store.entries(ws_b.id)
        assert await store.read_blob(ws_b.id, entry.sha256) == b"print(1)"

        # X done: the next run end deletes it.
        await _set_run_status(scratch, run_b, "completed")
        async with scratch.sessions() as db:
            await db.execute(update(WorkItem).where(WorkItem.id == item_id).values(status="done"))
            await db.commit()
        report = await gc.collect_runs([run_b], session_factory=scratch.sessions, store=store)
        assert report.deleted == {"run_ended": 1}
        async with scratch.sessions() as db:
            assert await db.get(SandboxWorkspace, ws_a.id) is None
        assert not any(key.startswith(f"{OBJECT_PREFIX}{ws_a.id}/") for key in blobs.objects)
