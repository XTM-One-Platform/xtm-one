"""``session=true`` on the OpenSandbox backend, against a fake container.

The fake keeps a container's ``/tmp`` in a local directory, runs the real
kernel (the code interpreter's wrapper in its directory mode) as a background
process and the real workspace helper, so the request / result files, the
workspace reconcile and read-back, the outputs a cell writes and the kernel
being found again by id are what is tested - everything but the transport.
"""

from __future__ import annotations

import os
import shlex
import subprocess
import sys
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.services import code_interpreter
from app.services.sandbox_workspace import kernel as kernel_module
from app.services.sandbox_workspace.manifest import Limits
from app.services.sandbox_workspace.session import WorkspaceSession
from app.services.sandbox_workspace.store import MemoryWorkspaceStore, WorkspaceScope

#: The container's own ``/tmp``, kept in a local directory by the fake.
_CONTAINER_TMP = "/tmp"  # noqa: S108


class _Container:
    def __init__(self, root: Path) -> None:
        self.id = f"sbx-{uuid.uuid4().hex[:8]}"
        self.root = root
        self.files = self
        self.commands = self
        self.processes: list[subprocess.Popen] = []
        self.killed = False
        root.mkdir(parents=True, exist_ok=True)

    def local(self, path: str) -> Path:
        assert path.startswith(_CONTAINER_TMP), path
        return self.root / path[len(_CONTAINER_TMP) :].lstrip("/")

    def _map(self, text: str) -> str:
        return text.replace(_CONTAINER_TMP, str(self.root).replace("\\", "/"))

    async def create_directories(self, entries) -> None:
        for entry in entries:
            self.local(entry.path).mkdir(parents=True, exist_ok=True)

    async def write_file(self, path: str, data: bytes) -> None:
        target = self.local(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)

    async def read_bytes(self, path: str) -> bytes:
        return self.local(path).read_bytes()

    async def run(self, command: str, opts=None):
        argv = [self._map(a) for a in shlex.split(command)]
        assert argv[0] == "python"
        env = {**os.environ, "WORKSPACE_DIR": str(self.local(f"{_CONTAINER_TMP}/workspace"))}
        if opts is not None and getattr(opts, "background", False):
            self.processes.append(subprocess.Popen([sys.executable, *argv[1:]], cwd=self.root, env=env))  # noqa: S603
            return SimpleNamespace(exit_code=None, logs=SimpleNamespace(stdout=[], stderr=[]))
        done = subprocess.run([sys.executable, *argv[1:]], cwd=self.root, env=env, capture_output=True, text=True, check=False)  # noqa: S603
        return SimpleNamespace(
            exit_code=done.returncode,
            logs=SimpleNamespace(stdout=[], stderr=[SimpleNamespace(text=line) for line in done.stderr.splitlines()]),
        )

    async def renew(self, _timeout) -> None:
        return None

    async def kill(self) -> None:
        self.killed = True
        for process in self.processes:
            process.kill()
            process.wait(timeout=10)

    async def close(self) -> None:
        return None


@pytest.fixture
def api(monkeypatch, tmp_path):
    from app.core.config import settings

    monkeypatch.setattr(settings, "custom_tool_sandbox", "opensandbox")
    monkeypatch.setattr(settings, "disable_code_interpreter", False)
    monkeypatch.setattr(settings, "code_interpreter_kernel_idle_seconds", 120)

    async def _no_quota(*_a, **_k):
        return None

    monkeypatch.setattr("app.services.quota.enforce_combined_sandbox_quota", _no_quota)
    monkeypatch.setattr("app.services.quota.increment_sandbox_usage", _no_quota)
    containers: dict[str, _Container] = {}

    async def create(_lifetime):
        container = _Container(tmp_path / f"c{len(containers)}")
        containers[container.id] = container
        return container

    async def connect(sandbox_id: str):
        container = containers.get(sandbox_id)
        if container is None or container.killed:
            raise RuntimeError("no such sandbox")
        return container

    yield SimpleNamespace(create=create, connect=connect, containers=containers)
    for container in containers.values():
        for process in container.processes:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=10)


def _workspace(store: MemoryWorkspaceStore) -> WorkspaceSession:
    scope = WorkspaceScope(user_id=uuid.UUID(int=7), conversation_id=uuid.uuid4())
    return WorkspaceSession(scope, store=store, limits=Limits(quota_bytes=8 * 1024 * 1024, max_files=100))


async def _cell(code: str, workspace: WorkspaceSession, api, **kwargs) -> dict:
    return await kernel_module.execute_in_remote_kernel(code, timeout=kwargs.pop("timeout", 60), workspace=workspace, sandbox_api=api, **kwargs)


async def test_the_container_kernel_keeps_state_files_and_outputs(api):
    store = MemoryWorkspaceStore()
    workspace = _workspace(store)
    first = await _cell("import json\nanswer = 41\nopen('workspace/w.txt','w').write('kept')", workspace, api)
    assert first["error"] is None, first
    assert first["workspace"].kernel == "new" and first["workspace"].diff.added == ["w.txt"]
    second = await _cell("print(json.dumps(answer + 1))\n_ = open('report.csv','w').write('a\\n')", workspace, api)
    assert second["stdout"].strip() == "42"
    assert second["workspace"].kernel == "reused"
    assert [f["filename"] for f in second["output_files"]] == ["report.csv"]
    assert len(api.containers) == 1, "found again by id, not created anew"
    text = code_interpreter.format_text_result(second)
    assert text.startswith("[Kernel (session=true): the conversation's kernel")
    _entry, data = await workspace.get_bytes("w.txt")
    assert data == b"kept"


async def test_a_gone_container_is_a_restarted_kernel_and_reset_a_new_one(api):
    store = MemoryWorkspaceStore()
    workspace = _workspace(store)
    await _cell("x = 1", workspace, api)
    (container,) = api.containers.values()
    await container.kill()
    restarted = await _cell("print(x)", workspace, api)
    assert restarted["workspace"].kernel == "restarted" and "NameError" in (restarted["error"] or "")
    record = await workspace.record(create=False)
    assert (await store.get_record(record.id)).kernel_restarts == 1
    await _cell("y = 2", workspace, api)
    reset = await _cell("print(y)", workspace, api, reset=True)
    assert reset["workspace"].kernel == "reset" and "NameError" in (reset["error"] or "")
