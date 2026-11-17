"""A kernel start keeps the labs, services and commands the workspace record names.

``kernel_state`` holds the kernel last started for the workspace and, beside it, the running labs, service containers
and command containers. A kernel start wrote it whole: the first ``session=true`` call of a run resumed in another
process (no kernel there) wiped them, and the run found its lab gone.
"""

from __future__ import annotations

import uuid

import pytest

from app.services import code_interpreter
from app.services.sandbox_workspace import kernel as kernel_module
from app.services.sandbox_workspace import local
from app.services.sandbox_workspace.kernel import KernelPool
from app.services.sandbox_workspace.local import BlobCache
from app.services.sandbox_workspace.manifest import Limits
from app.services.sandbox_workspace.session import WorkspaceSession
from app.services.sandbox_workspace.store import MemoryWorkspaceStore, WorkspaceScope

MB = 1024 * 1024
LAB = {"pod": "lab-0123456789ab", "port": 3000, "pod_ip": "10.42.3.17"}
OTHERS = {
    "labs": {"co:lab": LAB},
    "services": {"co:api": {"sandbox_id": "sbx-1"}},
    "commands": {"python": {"sandbox_id": "sbx-2"}},
}


@pytest.fixture(autouse=True)
async def _kernels(monkeypatch, tmp_path):
    from app.core.config import settings

    monkeypatch.setattr(settings, "custom_tool_sandbox", "subprocess")
    monkeypatch.setattr(settings, "disable_code_interpreter", False)
    monkeypatch.setattr(settings, "code_interpreter_workspace_enabled", True)
    monkeypatch.setattr(settings, "code_interpreter_kernel_idle_seconds", 120)
    monkeypatch.setattr(local, "_cache", BlobCache(root=str(tmp_path / "cache"), max_bytes=64 * MB))
    pool = KernelPool()
    monkeypatch.setattr(kernel_module, "_pool", pool)
    yield pool
    await pool.shutdown()


def _workspace(store: MemoryWorkspaceStore) -> WorkspaceSession:
    scope = WorkspaceScope(user_id=uuid.UUID(int=1), conversation_id=uuid.uuid4())
    return WorkspaceSession(scope, store=store, limits=Limits(quota_bytes=8 * MB, max_files=100))


async def _session_call(code: str, workspace: WorkspaceSession) -> dict:
    return await code_interpreter.execute_python(code, timeout=60, workspace=workspace, session=True)


async def test_the_first_kernel_keeps_a_lab_started_before_it():
    store = MemoryWorkspaceStore()
    workspace = _workspace(store)
    await workspace.record_kernel(dict(OTHERS), restarted=False)

    result = await _session_call("x = 1", workspace)

    assert result["error"] is None, result
    assert result["workspace"].kernel == "new"  # labs alone are no kernel started before
    state = await workspace.latest_kernel_state()
    assert {name: state[name] for name in OTHERS} == OTHERS
    assert state["backend"] == "local" and state["kernel_id"]


async def test_a_kernel_restarted_in_another_process_keeps_the_lab(_kernels):
    store = MemoryWorkspaceStore()
    workspace = _workspace(store)
    await _session_call("x = 1", workspace)
    state = dict(await workspace.latest_kernel_state())
    await workspace.record_kernel({**state, **OTHERS}, restarted=False)  # a lab started since
    record = await workspace.record(create=False)
    await _kernels.drop(str(record.id))  # the run resumed in a process with no kernel

    result = await _session_call("print(1)", workspace)

    assert result["workspace"].kernel == "restarted"
    after = await workspace.latest_kernel_state()
    assert after["labs"] == OTHERS["labs"]
    assert after["services"] == OTHERS["services"]
    assert after["commands"] == OTHERS["commands"]
    assert after["kernel_id"] != state["kernel_id"]


async def test_the_remote_kernel_record_keeps_the_other_records():
    store = MemoryWorkspaceStore()
    workspace = _workspace(store)
    await workspace.record_kernel(dict(OTHERS), restarted=False)

    await kernel_module._record_kernel(
        workspace, {"backend": "opensandbox", "sandbox_id": "sbx-k", "kernel_id": "k1"}, restarted=False
    )

    state = await workspace.latest_kernel_state()
    assert state["sandbox_id"] == "sbx-k"
    assert {name: state[name] for name in OTHERS} == OTHERS


async def test_a_kernel_record_drops_the_previous_kernel_keys():
    store = MemoryWorkspaceStore()
    workspace = _workspace(store)
    await workspace.record_kernel({"backend": "opensandbox", "sandbox_id": "old", "kernel_id": "k0"}, restarted=False)

    await kernel_module._record_kernel(workspace, {"backend": "local", "kernel_id": "k1"}, restarted=True)

    state = await workspace.latest_kernel_state()
    assert "sandbox_id" not in state and state["kernel_id"] == "k1"
