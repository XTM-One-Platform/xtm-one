"""Installed packages (node_modules, .venv) read from the live command container.

``sandbox_grep`` and ``sandbox_read_file`` answer a path under an installed
tree the manifest never records from the workspace's command container,
read-only. The fake container keeps its ``/tmp`` in a local directory and runs
the platform's own helper (``remote.TOOL_SRC``) with the test's interpreter,
as ``test_sandbox_workspace_remote.py`` does.
"""

from __future__ import annotations

import shlex
import subprocess
import sys
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.services.builtin_tools.media import build_code_interpreter_tool
from app.services.sandbox_workspace import installed, local
from app.services.sandbox_workspace import session as session_module
from app.services.sandbox_workspace.local import BlobCache
from app.services.sandbox_workspace.manifest import Limits
from app.services.sandbox_workspace.remote import REMOTE_WORKSPACE
from app.services.sandbox_workspace.session import WorkspaceSession
from app.services.sandbox_workspace.store import MemoryWorkspaceStore, WorkspaceScope
from app.tools.base import CompositeToolExecutor, FileContext, ToolCall

_CONTAINER_TMP = "/tmp"  # noqa: S108 - the container's own /tmp, kept in a local directory
MB = 1024 * 1024


class _Container:
    def __init__(self, root: Path) -> None:
        self.id = "sbx-installed"
        self.root = root
        self.files = self
        self.commands = self

    def local(self, path: str) -> Path:
        assert path.startswith(_CONTAINER_TMP), path
        return self.root / path[len(_CONTAINER_TMP) :].lstrip("/")

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
        argv = shlex.split(command)
        assert argv[0] == "python"
        args = [str(self.local(a)) if a.startswith(_CONTAINER_TMP) else a for a in argv[1:]]
        done = subprocess.run([sys.executable, *args], capture_output=True, text=True, check=False)  # noqa: S603
        return SimpleNamespace(
            exit_code=done.returncode,
            logs=SimpleNamespace(stdout=[], stderr=[SimpleNamespace(text=line) for line in done.stderr.splitlines()]),
        )


@pytest.fixture
def container(tmp_path) -> _Container:
    box = _Container(tmp_path / "container-tmp")
    ws = box.local(REMOTE_WORKSPACE)
    dist = ws / "co/frontend/node_modules/lib/dist"
    dist.mkdir(parents=True)
    (dist / "index.d.ts").write_text("export declare function needle(a: string): number;\nexport const other = 1;\n")
    (dist / "logo.png").write_bytes(b"\x89PNG\x00\x00")
    real = ws / "co/frontend/node_modules/.pnpm/linked@1/node_modules/linked"
    real.mkdir(parents=True)
    (real / "types.d.ts").write_text("export type Needle = string;\n")
    (ws / "co/frontend/node_modules/linked").symlink_to(real)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("needle outside\n")
    (ws / "co/frontend/node_modules/escape").symlink_to(outside)
    package = ws / "co/backend/.venv/lib/site-packages/pkg"
    package.mkdir(parents=True)
    (package / "mod.py").write_text(
        "class Client:\n    def get(self):\n        return 1\n\n\ndef helper():\n    return 2\n"
    )
    return box


@pytest.fixture
def api(container):
    async def connect(sandbox_id: str):
        if sandbox_id != container.id:
            raise RuntimeError("no such sandbox")
        return container

    return SimpleNamespace(connect=connect)


def _workspace() -> WorkspaceSession:
    scope = WorkspaceScope(user_id=uuid.UUID(int=7), conversation_id=uuid.uuid4())
    return WorkspaceSession(scope, store=MemoryWorkspaceStore(), limits=Limits(quota_bytes=8 * MB, max_files=200))


async def _hold(workspace: WorkspaceSession, sandbox_id: str = "sbx-installed") -> None:
    await workspace.record_kernel(
        {"commands": {"node": {"sandbox_id": sandbox_id, "started_at": "2026-11-05T13:00:00+00:00"}}}, restarted=False
    )


def test_a_path_under_an_installed_directory_is_recognised():
    assert installed.installed_path("co/frontend/node_modules/lib/dist")
    assert installed.installed_path("co/backend/.venv/lib/python3.14/site-packages/x.py")
    assert not installed.installed_path("co/frontend/src/node_modules_like.ts")


async def test_the_tree_lists_files_following_links_inside_the_workspace_only(container, api):
    workspace = _workspace()
    await _hold(workspace)
    tree, _sandbox = await installed.list_tree(workspace, "co/frontend/node_modules", sandbox_api=api)
    paths = [p for p, _s in tree.files]
    assert "co/frontend/node_modules/lib/dist/index.d.ts" in paths
    assert "co/frontend/node_modules/linked/types.d.ts" in paths or any(p.endswith("linked/types.d.ts") for p in paths)
    assert not any("secret.txt" in p for p in paths)


async def test_a_prefix_naming_one_installed_file_is_its_own_tree(container, api):
    workspace = _workspace()
    await _hold(workspace)
    tree, _sandbox = await installed.list_tree(workspace, "co/frontend/node_modules/lib/dist/index.d.ts", sandbox_api=api)
    assert tree.exists
    assert [p for p, _s in tree.files] == ["co/frontend/node_modules/lib/dist/index.d.ts"]
    with pytest.raises(installed.InstalledUnavailableError, match="not in the workspace"):
        await installed.list_tree(workspace, "co/frontend/node_modules/escape/secret.txt", sandbox_api=api)


async def test_a_file_is_read_alone_and_nothing_enters_the_manifest(container, api):
    workspace = _workspace()
    await _hold(workspace)
    data = await installed.read_file(workspace, "co/frontend/node_modules/lib/dist/index.d.ts", max_bytes=MB, sandbox_api=api)
    assert data.startswith(b"export declare function needle")
    assert await workspace.entries(refresh=True) == {}
    with pytest.raises(FileNotFoundError):
        await installed.read_file(workspace, "co/frontend/node_modules/escape/secret.txt", max_bytes=MB, sandbox_api=api)


async def test_no_live_container_names_the_install_check(api):
    workspace = _workspace()
    with pytest.raises(installed.NoLiveContainerError, match="install check"):
        await installed.list_tree(workspace, "co/frontend/node_modules", sandbox_api=api)
    await _hold(workspace, sandbox_id="sbx-gone")
    with pytest.raises(installed.NoLiveContainerError, match="ended"):
        await installed.list_tree(workspace, "co/frontend/node_modules", sandbox_api=api)


def test_the_install_directory_is_the_one_holding_the_installed_tree():
    assert installed.install_directory("co/frontend/node_modules/lib/dist", "co") == "frontend"
    assert installed.install_directory("co/node_modules/x", "co") == ""
    assert installed.install_directory("co/backend/.venv/lib/site-packages/a.py", "co") == "backend"


def test_the_install_check_of_a_directory_falls_back_to_the_enclosing_one():
    from app.services.sandbox_workspace.checks import load_checks
    from app.services.sandbox_workspace.commands import install_check_for

    specs = load_checks(
        '[check.install]\nargv = ["yarn"]\n'
        '[check.frontend-install]\nargv = ["yarn"]\ncwd = "frontend"\n'
        '[check.frontend-unit]\nargv = ["vitest"]\ncwd = "frontend"\n'
    )
    assert install_check_for(specs, "frontend") == "frontend-install"
    assert install_check_for(specs, "packages/x") == "install"
    assert install_check_for({}, "frontend") == ""


# -- through the tools -------------------------------------------------------------


@pytest.fixture
def store(monkeypatch, tmp_path, api) -> MemoryWorkspaceStore:
    from app.core.config import settings

    monkeypatch.setattr(settings, "custom_tool_sandbox", "subprocess")
    monkeypatch.setattr(settings, "disable_code_interpreter", False)
    monkeypatch.setattr(settings, "code_interpreter_workspace_enabled", True)
    monkeypatch.setattr(settings, "code_interpreter_package_allowlist", "")
    monkeypatch.setattr(local, "_cache", BlobCache(root=str(tmp_path / "cache"), max_bytes=64 * MB))
    monkeypatch.setattr(installed, "_default_api", lambda _user: api)
    memory = MemoryWorkspaceStore()
    monkeypatch.setattr(session_module, "default_store", lambda: memory)
    return memory


async def _tools() -> tuple[CompositeToolExecutor, WorkspaceSession]:
    composite = CompositeToolExecutor()
    composite.set_file_context(FileContext())
    definitions, executor = build_code_interpreter_tool(user_id=str(uuid.uuid4()), conversation_id=str(uuid.uuid4()))
    composite.add_tools(definitions, executor)
    await _hold(executor.session)
    return composite, executor.session


async def test_grep_on_node_modules_answers_lines_from_the_container(store, container):
    composite, session = await _tools()
    grep = await composite.execute_tool(
        ToolCall(id="g", name="sandbox_grep", arguments={"pattern": "needle", "path_prefix": "co/frontend/node_modules/lib/dist"})
    )
    assert not grep.is_error, grep.content
    assert "workspace/co/frontend/node_modules/lib/dist/index.d.ts:1: export declare function needle" in grep.content
    assert "read from the live command container" in grep.content
    assert await session.entries(refresh=True) == {}


def test_an_installed_search_reads_large_batches() -> None:
    from app.services.sandbox_workspace import reading

    sized = [(f"co/node_modules/p/f{i:04d}.js", 100) for i in range(1000)]
    workspace_batches = reading.plan_grep(sized, reading.GrepResult())
    installed_batches = reading.plan_grep(sized, reading.GrepResult(), batch_files=reading.INSTALLED_READ_FILES)
    assert len(workspace_batches) == 125
    assert [len(b) for b in installed_batches] == [400, 400, 200]


async def test_grep_on_node_modules_stops_at_the_time_budget_naming_what_is_left(
    store: MemoryWorkspaceStore, container: _Container, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.services.sandbox_workspace import commands

    monkeypatch.setattr(commands, "wait_budget", lambda *a, **k: 0.0)
    composite, _session = await _tools()
    grep = await composite.execute_tool(
        ToolCall(
            id="g",
            name="sandbox_grep",
            arguments={"pattern": "needle", "path_prefix": "co/frontend/node_modules/lib/dist"},
        )
    )
    assert not grep.is_error, grep.content
    assert "search stopped early: the call's time budget ran out" in grep.content
    assert "index.d.ts" in grep.content


async def test_grep_on_one_installed_file_answers_its_lines(store, container):
    composite, session = await _tools()
    grep = await composite.execute_tool(
        ToolCall(
            id="g",
            name="sandbox_grep",
            arguments={"pattern": "needle", "path_prefix": "co/frontend/node_modules/lib/dist/index.d.ts"},
        )
    )
    assert not grep.is_error, grep.content
    assert "workspace/co/frontend/node_modules/lib/dist/index.d.ts:1: export declare function needle" in grep.content
    assert await session.entries(refresh=True) == {}


async def test_read_file_on_node_modules_answers_a_window(store, container):
    composite, _session = await _tools()
    read = await composite.execute_tool(
        ToolCall(id="r", name="sandbox_read_file", arguments={"path": "co/frontend/node_modules/lib/dist/index.d.ts", "offset_line": 2})
    )
    assert not read.is_error, read.content
    assert "export const other = 1;" in read.content
    assert "needle(a: string)" not in read.content
    assert read.content.endswith(installed.INSTALLED_READ_LABEL)


async def test_outline_on_site_packages_answers_the_definitions_from_the_container(store, container):
    composite, session = await _tools()
    outline = await composite.execute_tool(
        ToolCall(id="o", name="sandbox_outline", arguments={"path": "co/backend/.venv/lib/site-packages/pkg/mod.py"})
    )
    assert not outline.is_error, outline.content
    assert "class Client" in outline.content and "def helper" in outline.content
    assert outline.content.endswith(installed.INSTALLED_READ_LABEL)
    assert await session.entries(refresh=True) == {}


async def test_read_symbol_on_site_packages_answers_the_definition_from_the_container(store, container):
    composite, _session = await _tools()
    read = await composite.execute_tool(
        ToolCall(
            id="s",
            name="sandbox_read_symbol",
            arguments={"path": "co/backend/.venv/lib/site-packages/pkg/mod.py", "symbol": "Client.get"},
        )
    )
    assert not read.is_error, read.content
    assert "return 1" in read.content and "return 2" not in read.content
    assert read.content.endswith(installed.INSTALLED_READ_LABEL)


async def test_outline_with_no_live_container_answers_the_install_call_not_an_error(store, container):
    composite, _session = await _tools_without_container()
    outline = await composite.execute_tool(
        ToolCall(id="o", name="sandbox_outline", arguments={"path": "co/frontend/node_modules/lib/dist/index.d.ts"})
    )
    assert not outline.is_error, outline.content
    assert 'check="frontend-install"' in outline.content


_CHECKS = b'[check.frontend-install]\nargv = ["yarn", "install"]\ncwd = "frontend"\n'


async def _tools_without_container() -> tuple[CompositeToolExecutor, WorkspaceSession]:
    composite = CompositeToolExecutor()
    composite.set_file_context(FileContext())
    definitions, executor = build_code_interpreter_tool(user_id=str(uuid.uuid4()), conversation_id=str(uuid.uuid4()))
    composite.add_tools(definitions, executor)
    await executor.session.put_tree([("co/.xtm-checkout.json", b"{}"), ("co/.xtm-one/checks.toml", _CHECKS)])
    return composite, executor.session


async def test_grep_with_no_live_container_answers_the_install_call_not_an_error(store, container):
    composite, _session = await _tools_without_container()
    grep = await composite.execute_tool(
        ToolCall(id="g", name="sandbox_grep", arguments={"pattern": "needle", "path_prefix": "co/frontend/node_modules/lib"})
    )
    assert not grep.is_error, grep.content
    assert 'Run first: sandbox_run_command(checkout="co", check="frontend-install")' in grep.content


async def test_read_file_with_no_live_container_answers_the_install_call_not_an_error(store, container):
    composite, _session = await _tools_without_container()
    read = await composite.execute_tool(
        ToolCall(id="r", name="sandbox_read_file", arguments={"path": "co/frontend/node_modules/lib/dist/index.d.ts"})
    )
    assert not read.is_error, read.content
    assert 'check="frontend-install"' in read.content


async def test_a_path_outside_installed_trees_keeps_the_workspace_answer(store, container):
    composite, _session = await _tools()
    read = await composite.execute_tool(ToolCall(id="r", name="sandbox_read_file", arguments={"path": "co/frontend/src/a.ts"}))
    assert read.is_error and "is not in the workspace" in read.content
    outline = await composite.execute_tool(
        ToolCall(id="o", name="sandbox_outline", arguments={"path": "co/frontend/src/a.ts"})
    )
    assert outline.is_error and "is not in the workspace" in outline.content
