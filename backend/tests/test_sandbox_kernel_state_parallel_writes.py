"""Parallel writers of ``kernel_state`` never put back each other's older record.

``kernel_state`` holds the kernel's keys and, beside them, the running labs, service containers and command
containers. Two services of one checkout started in parallel (``lab`` and ``lab-light``), or a run resumed while a
start was in flight, each read the whole object then wrote it back: the slower writer restored the other's older
record, a lab lost its ``preview_url`` and its start answered the bare text ``'preview_url'`` (a ``KeyError``).
"""

from __future__ import annotations

import asyncio
import uuid

from app.services.sandbox_workspace import labs, services
from app.services.sandbox_workspace.manifest import Limits
from app.services.sandbox_workspace.session import WorkspaceSession
from app.services.sandbox_workspace.store import MemoryWorkspaceStore, WorkspaceScope

MB = 1024 * 1024


class _SlowStore(MemoryWorkspaceStore):
    """A store whose reads yield: every reader of a round reads before any of them writes, as across replicas."""

    async def get_record(self, workspace_id):
        await asyncio.sleep(0)
        record = await super().get_record(workspace_id)
        await asyncio.sleep(0)
        return record


def _workspace(store: MemoryWorkspaceStore) -> WorkspaceSession:
    scope = WorkspaceScope(user_id=uuid.UUID(int=1), conversation_id=uuid.uuid4())
    return WorkspaceSession(scope, store=store, limits=Limits(quota_bytes=8 * MB, max_files=100))


async def test_two_labs_recorded_in_parallel_both_keep_their_record():
    workspace = _workspace(_SlowStore())
    await workspace.record(create=True)
    first = {"pod": "lab-aaaaaaaaaaaa", "preview_url": "http://10.42.0.9:3100/"}
    second = {"pod": "lab-bbbbbbbbbbbb", "preview_url": "http://10.42.0.10:3100/"}
    await asyncio.gather(
        labs._remember(workspace, "co:lab", first),
        labs._remember(workspace, "co:lab-light", second),
    )
    state = await workspace.latest_kernel_state()
    assert state["labs"] == {"co:lab": first, "co:lab-light": second}


async def test_a_lab_a_service_and_a_kernel_written_at_once_keep_every_record():
    workspace = _workspace(_SlowStore())
    await workspace.record(create=True)
    lab = {"pod": "lab-aaaaaaaaaaaa", "preview_url": "http://10.42.0.9:3100/"}
    service = {"sandbox_id": "sbx-1", "preview_url": "https://sbx-1.example/"}

    def kernel(current: dict) -> dict:
        kept = {name: current[name] for name in ("labs", "services", "commands") if current.get(name)}
        return {"backend": "local", "kernel_id": "k1", **kept}

    await asyncio.gather(
        labs._remember(workspace, "co:lab", lab),
        services._remember(workspace, "co:api", service),
        workspace.update_kernel_state(kernel),
    )
    state = await workspace.latest_kernel_state()
    assert state["labs"] == {"co:lab": lab}
    assert state["services"] == {"co:api": service}
    assert state["kernel_id"] == "k1"


async def test_a_change_that_answers_none_writes_nothing():
    store = MemoryWorkspaceStore()
    workspace = _workspace(store)
    await workspace.record_kernel({"labs": {"co:lab": {"pod": "p"}}}, restarted=False)
    await labs._remember(workspace, "co:other", None)
    assert (await workspace.latest_kernel_state()) == {"labs": {"co:lab": {"pod": "p"}}}


def test_a_lab_record_without_preview_url_is_described_never_a_bare_key():
    resolved = type("R", (), {"name": "lab"})()
    text = labs._describe(resolved, {"pod": "lab-aaaaaaaaaaaa"}, note="already running")
    assert "service_id: lab-aaaaaaaaaaaa" in text
    assert "preview_url: not recorded yet" in text and "sandbox_service_status" in text
