"""``code_interpreter(session=true)``: the conversation's long-lived kernel.

Real kernel processes (the code interpreter's own wrapper looping over cells),
an in-memory workspace store: state kept from one session call to the next,
``reset`` starting anew, a kernel gone reported as restarted, a timeout
stopping it, and the workspace and the outputs handled as for a fresh process.
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


def _workspace(store: MemoryWorkspaceStore, conversation: uuid.UUID | None = None, user: uuid.UUID | None = None):
    scope = WorkspaceScope(user_id=user or uuid.UUID(int=1), conversation_id=conversation or uuid.uuid4())
    return WorkspaceSession(scope, store=store, limits=Limits(quota_bytes=8 * MB, max_files=100))


async def _session_call(code: str, workspace: WorkspaceSession, *, timeout: int = 60, reset: bool = False) -> dict:
    return await code_interpreter.execute_python(code, timeout=timeout, workspace=workspace, session=True, reset=reset)


async def test_variables_and_imports_persist_between_session_calls():
    store = MemoryWorkspaceStore()
    workspace = _workspace(store)
    first = await _session_call("import json\nanswer = 41", workspace)
    assert first["error"] is None, first
    assert first["workspace"].kernel == "new"
    second = await _session_call("print(json.dumps({'answer': answer + 1}))", workspace)
    assert second["stdout"].strip() == '{"answer": 42}'
    assert second["workspace"].kernel == "reused"
    text = code_interpreter.format_text_result(second)
    assert "variables and imports from its earlier session calls are defined" in text


async def test_reset_starts_a_new_kernel():
    store = MemoryWorkspaceStore()
    workspace = _workspace(store)
    await _session_call("x = 1", workspace)
    result = await _session_call("print(x)", workspace, reset=True)
    assert result["workspace"].kernel == "reset"
    assert "NameError" in (result["error"] or "")


async def test_a_kernel_gone_is_reported_as_restarted_and_counted(_kernels):
    store = MemoryWorkspaceStore()
    workspace = _workspace(store)
    await _session_call("x = 1", workspace)
    # Another server process, or the idle timeout: this process has no kernel.
    record = await workspace.record(create=False)
    await _kernels.drop(str(record.id))
    result = await _session_call("print(x)", workspace)
    assert result["workspace"].kernel == "restarted"
    assert (await store.get_record(record.id)).kernel_restarts == 1
    text = code_interpreter.format_text_result(result)
    assert "a NEW kernel" in text and "NOT defined" in text
    assert "Hint: this call ran in a NEW kernel" in text


async def test_a_timeout_stops_the_kernel_and_keeps_what_it_saved():
    store = MemoryWorkspaceStore()
    workspace = _workspace(store)
    # Booted first: the timeout is the cell's, not the kernel's start.
    await _session_call("booted = True", workspace)
    result = await _session_call(
        "open('workspace/before.txt','w').write('saved')\nimport time\ntime.sleep(30)", workspace, timeout=5
    )
    assert "timed out" in result["error"]
    assert "The kernel was stopped by the timeout." in code_interpreter.format_text_result(result)
    entries = await workspace.entries(refresh=True)
    assert "before.txt" in entries
    after = await _session_call("print(open('workspace/before.txt').read())", workspace)
    assert after["stdout"].strip() == "saved"
    assert after["workspace"].kernel == "restarted"


async def test_session_calls_share_the_workspace_with_stateless_calls():
    store = MemoryWorkspaceStore()
    workspace = _workspace(store)
    await _session_call("open('workspace/k.txt','w').write('from the kernel')", workspace)
    stateless = await code_interpreter.execute_python("print(open('workspace/k.txt').read())", timeout=60, workspace=workspace)
    assert stateless["stdout"].strip() == "from the kernel"
    await workspace.put_bytes("put.txt", b"from a tool")
    back = await _session_call("print(open('workspace/put.txt').read())", workspace)
    assert back["stdout"].strip() == "from a tool"


async def test_a_file_a_session_call_writes_outside_the_workspace_is_that_calls_output_only():
    store = MemoryWorkspaceStore()
    workspace = _workspace(store)
    first = await _session_call("open('report.csv','w').write('a,b\\n1,2\\n')", workspace)
    assert [f["filename"] for f in first["output_files"]] == ["report.csv"]
    second = await _session_call("print('nothing written')", workspace)
    assert second["output_files"] == []


async def test_two_conversations_get_two_kernels():
    store = MemoryWorkspaceStore()
    one, two = _workspace(store), _workspace(store)
    await _session_call("secret = 'one'", one)
    result = await _session_call("print(secret)", two)
    assert "NameError" in (result["error"] or "")


async def test_session_without_a_workspace_runs_in_a_fresh_process():
    result = await code_interpreter.execute_python("print(1)", timeout=60, workspace=None, session=True)
    assert result["stdout"].strip() == "1"
    assert "ran in a fresh process" in code_interpreter.format_text_result(result)
