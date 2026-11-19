"""The workspace in an OpenSandbox container, against a fake container.

The fake keeps the container's ``/tmp`` in a local directory and runs the
real helper script (``remote.TOOL_SRC``) with the local interpreter, so what
is tested is the archive in, the listing, the archive out and the hashes the
server computes - everything but the transport.
"""

from __future__ import annotations

import asyncio
import json
import os
import shlex
import subprocess
import sys
import time
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.services.sandbox_workspace import remote
from app.services.sandbox_workspace.manifest import Limits, QuotaExceededError
from app.services.sandbox_workspace.session import WorkspaceSession
from app.services.sandbox_workspace.store import MemoryWorkspaceStore, WorkspaceScope

#: The container's own ``/tmp``, kept in a local directory by the fake.
_CONTAINER_TMP = "/tmp"  # noqa: S108


class _FakeContainer:
    def __init__(self, root: Path) -> None:
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
        # The platform's own helper script, run by the test's interpreter, off the event loop so two
        # commands of one container run at once as they do in a real one.
        done = await asyncio.to_thread(
            subprocess.run,
            [sys.executable, *args],
            capture_output=True,
            text=True,
            check=False,
        )
        return SimpleNamespace(
            exit_code=done.returncode,
            logs=SimpleNamespace(stdout=[], stderr=[SimpleNamespace(text=line) for line in done.stderr.splitlines()]),
        )


@pytest.fixture
def container(tmp_path) -> _FakeContainer:
    return _FakeContainer(tmp_path / "container-tmp")


def _session(store: MemoryWorkspaceStore, quota: int = 1_000_000) -> WorkspaceSession:
    scope = WorkspaceScope(user_id=uuid.uuid4(), conversation_id=uuid.uuid4())
    return WorkspaceSession(scope, store=store, limits=Limits(quota_bytes=quota, max_files=100))


async def test_the_container_gets_the_manifest_and_gives_back_what_changed(container):
    store = MemoryWorkspaceStore()
    workspace = _session(store)
    await workspace.put_tree([("keep.txt", b"keep"), ("edit.txt", b"v1"), ("drop.txt", b"bye")])
    await remote.install_tool(container)
    state = await remote.reconcile(container, workspace, await workspace.entries(refresh=True))
    ws = container.local(remote.REMOTE_WORKSPACE)
    assert sorted(p.name for p in ws.iterdir()) == ["drop.txt", "edit.txt", "keep.txt"]
    assert state.bytes_read == len(b"keepv1bye")

    # The code runs: one file edited, one removed, one added in a subdirectory.
    (ws / "edit.txt").write_bytes(b"v2 longer")
    (ws / "drop.txt").unlink()
    (ws / "out").mkdir()
    (ws / "out" / "new.csv").write_bytes(b"a,b\n")
    scanned = await remote.collect(container, workspace, state)
    assert sorted(p.entry.path for p in scanned.upserts) == ["edit.txt", "out/new.csv"]
    assert scanned.removals == ["drop.txt"]
    plan = await workspace.commit(scanned.upserts, scanned.removals)
    assert plan.diff.as_dict() == {"added": ["out/new.csv"], "changed": ["edit.txt"], "removed": ["drop.txt"]}
    _entry, data = await workspace.get_bytes("edit.txt")
    assert data == b"v2 longer"


async def test_reconcile_removes_what_the_manifest_no_longer_lists(container):
    store = MemoryWorkspaceStore()
    workspace = _session(store)
    await workspace.put_tree([("a.txt", b"a")])
    await remote.install_tool(container)
    await remote.reconcile(container, workspace, await workspace.entries(refresh=True))
    stray = container.local(remote.REMOTE_WORKSPACE) / "refused.bin"
    stray.write_bytes(b"left by a refused commit")
    await remote.reconcile(container, workspace, await workspace.entries(refresh=True))
    assert not stray.exists()


async def test_a_large_stage_is_sent_in_parts_and_removals_go_with_the_first(container, monkeypatch):
    store = MemoryWorkspaceStore()
    workspace = _session(store)
    await workspace.put_tree([("a.txt", b"a" * 10), ("b.txt", b"b" * 10), ("c.txt", b"c" * 10)])
    await remote.install_tool(container)
    ws = container.local(remote.REMOTE_WORKSPACE)
    ws.mkdir(parents=True, exist_ok=True)
    stray = ws / "stray.bin"
    stray.write_bytes(b"left over")
    monkeypatch.setattr(remote, "UPLOAD_PART_BYTES", 15)
    sent: list[bytes] = []
    real_upload = remote._upload

    async def counting_upload(sandbox, archive, removals):
        sent.append(removals)
        await real_upload(sandbox, archive, removals)

    monkeypatch.setattr(remote, "_upload", counting_upload)
    state = await remote.reconcile(container, workspace, await workspace.entries(refresh=True))
    assert sorted(p.name for p in ws.iterdir()) == ["a.txt", "b.txt", "c.txt"]
    assert state.bytes_read == 30
    assert len(sent) == 3
    assert json.loads(sent[0]) == ["stray.bin"]
    assert [json.loads(r) for r in sent[1:]] == [[], []]


async def test_a_part_that_failed_leaves_the_parts_sent_and_the_next_call_sends_the_rest(container, monkeypatch):
    store = MemoryWorkspaceStore()
    workspace = _session(store)
    await workspace.put_tree([("a.txt", b"a" * 10), ("b.txt", b"b" * 10)])
    await remote.install_tool(container)
    monkeypatch.setattr(remote, "UPLOAD_PART_BYTES", 10)
    real_upload = remote._upload
    calls = {"n": 0}

    async def second_part_fails_once(sandbox, archive, removals):
        calls["n"] += 1
        if calls["n"] == 2:
            raise remote.UploadError("cut")
        await real_upload(sandbox, archive, removals)

    monkeypatch.setattr(remote, "_upload", second_part_fails_once)
    entries = await workspace.entries(refresh=True)
    with pytest.raises(remote.UploadError):
        await remote.reconcile(container, workspace, entries)
    ws = container.local(remote.REMOTE_WORKSPACE)
    assert sorted(p.name for p in ws.iterdir()) == ["a.txt"]
    state = await remote.reconcile(container, workspace, entries)
    assert sorted(p.name for p in ws.iterdir()) == ["a.txt", "b.txt"]
    assert state.bytes_read == 10


def test_upload_parts_keep_order_and_put_an_oversized_file_alone(monkeypatch):
    monkeypatch.setattr(remote, "UPLOAD_PART_BYTES", 10)
    entries = [SimpleNamespace(path=name, sha256=name) for name in ("s1", "big", "s2", "s3")]
    blobs = {"s1": b"x" * 4, "big": b"x" * 25, "s2": b"x" * 4, "s3": b"x" * 4}
    parts = remote._upload_parts(entries, blobs)
    assert [[e.path for e in part] for part in parts] == [["s1"], ["big"], ["s2", "s3"]]
    assert remote._upload_parts([], {}) == [[]]


async def test_a_change_past_the_quota_is_refused_before_anything_is_fetched(container):
    store = MemoryWorkspaceStore()
    workspace = _session(store, quota=10)
    await remote.install_tool(container)
    state = await remote.reconcile(container, workspace, {})
    (container.local(remote.REMOTE_WORKSPACE) / "big.bin").write_bytes(b"0" * 100)
    with pytest.raises(QuotaExceededError):
        await remote.collect(container, workspace, state)
    assert not container.local(f"{remote.TOOL_DIR}/out.tar").exists()


def _helper_namespace() -> dict:
    namespace: dict = {"__name__": "ws_tool"}
    exec(compile(remote.TOOL_SRC, "ws_tool.py", "exec"), namespace)  # noqa: S102 - the platform's own helper source
    return namespace


@pytest.mark.parametrize(
    ("path", "ignored"),
    [
        (".xtm/runs/abc/stdout.txt", True),
        ("co/.xtm/lint.sarif", True),
        (".venv/lib/python3.14/site-packages/x.py", True),
        ("co/frontend/node_modules/react/index.js", True),
        ("co/backend/app/__pycache__/x.cpython-314.pyc", True),
        ("co/backend/app/x.pyc", True),
        ("co/backend/app/x.py", False),
        ("co/xtm/runs.txt", False),
        ("co/node_modules_list.txt", False),
        (".venv", False),
    ],
)
def test_a_directory_pattern_ignores_every_path_under_it(path, ignored):
    patterns = [".xtm/", "*/.xtm/", "__pycache__/", "*/__pycache__/", "*.pyc", ".venv/", "*/.venv/", "node_modules/", "*/node_modules/"]
    assert _helper_namespace()["_ignored"](path, patterns) is ignored


async def test_a_manifest_file_under_an_ignored_directory_is_neither_collected_nor_removed(container):
    store = MemoryWorkspaceStore()
    workspace = _session(store)
    ignore = [".xtm/", "*/.xtm/", "node_modules/", "*/node_modules/"]
    kept = [".xtm/runs/first/stdout.txt", "co/.xtm/runs/first/run.json", "co/node_modules/pkg/index.js"]
    await workspace.put_tree([("co/app.py", b"v1"), *[(path, b"saved by an earlier run") for path in kept]])
    await remote.install_tool(container)
    state = await remote.reconcile(container, workspace, await workspace.entries(refresh=True), ignore=ignore)
    ws = container.local(remote.REMOTE_WORKSPACE)
    # Under an ignored directory, the manifest's files are left to the container,
    # which does not hold them; the command then writes into those directories.
    assert not (ws / ".xtm").exists() and (ws / "co" / "app.py").read_bytes() == b"v1"
    (ws / ".xtm" / "runs" / "second").mkdir(parents=True)
    (ws / ".xtm" / "runs" / "second" / "stdout.txt").write_bytes(b"built in the container")
    (ws / "co" / "node_modules" / "pkg").mkdir(parents=True)
    (ws / "co" / "node_modules" / "pkg" / "index.js").write_bytes(b"linked")
    (ws / "co" / "app.py").write_bytes(b"v2, another size")
    scanned = await remote.collect(container, workspace, state, ignore=ignore)
    assert [p.entry.path for p in scanned.upserts] == ["co/app.py"]
    assert scanned.removals == []
    await workspace.commit(scanned.upserts, scanned.removals)
    assert set(kept) <= set(await workspace.entries(refresh=True))


async def test_a_workspace_replaced_by_a_link_is_not_read(container, tmp_path):
    if sys.platform == "win32":
        pytest.skip("symbolic links need privileges on Windows")
    store = MemoryWorkspaceStore()
    workspace = _session(store)
    await remote.install_tool(container)
    state = await remote.reconcile(container, workspace, {})
    ws = container.local(remote.REMOTE_WORKSPACE)
    ws.rmdir()
    ws.symlink_to(tmp_path)
    scanned = await remote.collect(container, workspace, state)
    assert scanned.unreadable and scanned.upserts == []


async def test_two_lists_at_once_on_one_container_each_read_their_own_listing(container):
    """Two commands of one checkout in one round list the same container side by side, never each other's file."""
    store = MemoryWorkspaceStore()
    workspace = _session(store)
    await workspace.put_tree([(f"f{i}.txt", b"v1") for i in range(20)])
    await remote.install_tool(container)
    entries = await workspace.entries(refresh=True)
    await remote.reconcile(container, workspace, entries)
    (container.local(remote.REMOTE_WORKSPACE) / "f0.txt").write_bytes(b"v2, another size")
    half = {path: entry for path, entry in entries.items() if path != "f7.txt"}
    for _round in range(5):
        full_listing, half_listing = await asyncio.gather(
            remote._listing(container, entries, 100, missing_ok=False),
            remote._listing(container, half, 100, missing_ok=False),
        )
        assert [row[0] for row in full_listing["changed"]] == ["f0.txt"] and full_listing["removed"] == []
        assert sorted(row[0] for row in half_listing["changed"]) == ["f0.txt", "f7.txt"]


def _run_list(args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, *args], capture_output=True, text=True, check=False)  # noqa: S603


async def test_two_list_helpers_writing_one_listing_path_at_once_both_succeed(tmp_path):
    """The helper's aside file is its own: two runs onto one output path never rename each other's file."""
    tool = tmp_path / "ws_tool.py"
    tool.write_text(remote.TOOL_SRC, encoding="utf-8")
    root = tmp_path / "ws"
    root.mkdir()
    for i in range(200):
        (root / f"f{i}.txt").write_bytes(b"x")
    snapshot = tmp_path / "snapshot.json"
    snapshot.write_text("{}", encoding="utf-8")
    out = tmp_path / "out" / "listing.json"
    args = [str(tool), "list", str(root), str(snapshot), str(out), "1000", "0"]
    runs = await asyncio.gather(*(asyncio.to_thread(_run_list, args) for _ in range(6)))
    assert [run.returncode for run in runs] == [0] * 6, [run.stderr for run in runs]
    assert len(json.loads(out.read_text(encoding="utf-8"))["changed"]) == 200
    assert sorted(p.name for p in out.parent.iterdir()) == ["listing.json"]


def test_the_helper_sweeps_list_files_no_call_touched_for_an_hour(tmp_path):
    helper = _helper_namespace()
    old = tmp_path / "list-0123456789abcdef-snapshot.json"
    fresh = tmp_path / "list-fedcba9876543210-listing.json"
    other = tmp_path / "ws_tool.py"
    for path in (old, fresh, other):
        path.write_text("{}", encoding="utf-8")
    stale = time.time() - 7200
    os.utime(old, (stale, stale))
    os.utime(other, (stale, stale))
    helper["_sweep_lists"](str(tmp_path))
    assert sorted(p.name for p in tmp_path.iterdir()) == [fresh.name, other.name]


def _archive(files: dict[str, bytes]) -> bytes:
    import io
    import tarfile

    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w", format=tarfile.PAX_FORMAT) as tf:
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


async def test_two_uploads_at_once_on_one_container_each_unpack_their_own_archive(container):
    """Two commands of one container upload side by side: neither unpacks the other's archive, none is left behind."""
    await remote.install_tool(container)
    first = _archive({f"a/f{i}.txt": b"a" * 4000 for i in range(30)})
    second = _archive({f"b/f{i}.txt": b"b" * 4000 for i in range(30)})
    for _round in range(3):
        await asyncio.gather(remote._upload(container, first, b"[]"), remote._upload(container, second, b"[]"))
    ws = container.local(remote.REMOTE_WORKSPACE)
    assert len(list((ws / "a").iterdir())) == 30 and len(list((ws / "b").iterdir())) == 30
    assert not [p.name for p in container.local(remote.TOOL_DIR).iterdir() if p.name.startswith("stage-")]


async def test_a_truncated_archive_is_refused_by_the_helper_then_sent_again(container, monkeypatch):
    """An archive cut short on its way is never extracted: the helper refuses it, the second send lands."""
    await remote.install_tool(container)
    original = container.write_file
    cut: list[str] = []

    async def cutting_write(path: str, data: bytes) -> None:
        if path.endswith("-in.tar") and not cut:
            cut.append(path)
            data = data[: len(data) // 2]
        await original(path, data)

    monkeypatch.setattr(container, "write_file", cutting_write)
    await remote._upload(container, _archive({"x.txt": b"x" * 5000}), b"[]")
    assert cut
    assert (container.local(remote.REMOTE_WORKSPACE) / "x.txt").read_bytes() == b"x" * 5000


async def test_an_upload_that_keeps_failing_names_its_size_and_each_cause(container, monkeypatch):
    await remote.install_tool(container)
    original = container.write_file

    async def broken_write(path: str, data: bytes) -> None:
        if path.endswith("-in.tar"):
            raise ConnectionError("stream reset")
        await original(path, data)

    monkeypatch.setattr(container, "write_file", broken_write)
    with pytest.raises(remote.UploadError) as caught:
        await remote._upload(container, _archive({"x.txt": b"x" * 5000}), b"[]")
    message = str(caught.value)
    assert "MB workspace archive could not be unpacked in the container after 2 attempts" in message
    assert "attempt 1: " in message and "attempt 2: " in message and "stream reset" in message


def test_the_helper_refuses_an_archive_of_another_size_or_digest(tmp_path):
    helper = _helper_namespace()
    archive = tmp_path / "in.tar"
    archive.write_bytes(_archive({"x.txt": b"x"}))
    removals = tmp_path / "remove.json"
    removals.write_text("[]", encoding="utf-8")
    with pytest.raises(SystemExit) as caught:
        helper["_unpack"](str(tmp_path / "ws"), str(archive), str(removals), archive.stat().st_size + 1, "0" * 64)
    assert caught.value.code == 3
    assert not (tmp_path / "ws").exists()


def test_each_list_call_names_its_own_files():
    first, second = remote._list_files(), remote._list_files()
    assert set(first.values()).isdisjoint(second.values())
    assert all(path.startswith(f"{remote.TOOL_DIR}/list-") for path in first.values())
