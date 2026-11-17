"""A mounted conversation file is downloaded ONCE per turn.

The conversation files ride into the code-interpreter sandbox under
``attachments/``, read through the turn's ``FileContext``. Every call
used to download every file again and hold its own copy: a parallel batch of
N calls meant N downloads per file and N copies in memory at once. The turn's
code-interpreter tool now reads each file once and hands the same bytes to
every call of the turn - single-flight, shielded from a cancelled caller,
never keeping a failure, bounded by the mount budget, keyed on the file's
storage identity so a changed file is read again - while the loader still
runs only past the sandbox's gates and every output stays byte-for-byte the
same, for the local subprocess sandbox and OpenSandbox alike.
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.llm.base import FileRef
from app.services import code_interpreter
from app.services.builtin_tools import media
from app.services.file_storage import FileNotFoundInStorageError, file_storage
from app.tools.base import FileContext, ToolCall

_ID_A = "0f3c9a1b-2222-4333-8444-555566667777"
_ID_B = "7d0e11aa-2222-4333-8444-555566667777"
_CSV = ("a.csv", "chat/c/0f3c.csv", b"a,b\n1,2\n")
_HTML = ("Model TD.html", "chat/c/7d0e.html", b"<html>model</html>")
_OPENS_ATTACHMENTS = "print(open('attachments/index.json').read())"


class _Storage:
    """Object storage as the mount meets it: counts every download, and can
    hold downloads at a gate, fail one once, or miss an object."""

    def __init__(self, objects: dict[str, bytes]) -> None:
        self.objects = dict(objects)
        self.downloads: list[str] = []
        self.gate: asyncio.Event | None = None
        self.fail_once: dict[str, Exception] = {}

    async def download(self, object_name: str) -> bytes:
        self.downloads.append(object_name)
        if self.gate is not None:
            await self.gate.wait()
        failure = self.fail_once.pop(object_name, None)
        if failure is not None:
            raise failure
        if object_name not in self.objects:
            raise FileNotFoundInStorageError(object_name)
        return self.objects[object_name]

    def count(self, object_name: str) -> int:
        return self.downloads.count(object_name)


@pytest.fixture
def storage(monkeypatch) -> _Storage:
    fake = _Storage({_CSV[1]: _CSV[2], _HTML[1]: _HTML[2]})
    monkeypatch.setattr(file_storage, "download", fake.download)
    return fake


def _stored(filename: str, object_name: str, data: bytes, file_id: str, content_type: str = "text/plain") -> FileRef:
    """A conversation file as ``FileRef.from_attachment`` builds it: metadata only, the bytes in object storage."""
    return FileRef(
        file_id=file_id, filename=filename, content_type=content_type, size=len(data), s3_object_name=object_name
    )


def _csv() -> FileRef:
    return _stored(*_CSV, _ID_A, "text/csv")


def _html() -> FileRef:
    return _stored(*_HTML, _ID_B, "text/html")


def _turn(*refs: FileRef):
    """One turn: the code-interpreter tool every entry point builds per turn, and the turn's FileContext."""
    _defs, executor = media.build_code_interpreter_tool(user_id=str(uuid.uuid4()))
    executor.file_context = FileContext.from_file_refs(list(refs))
    return executor


def _call(n: int, code: str = _OPENS_ATTACHMENTS) -> ToolCall:
    return ToolCall(id=f"c{n}", name="code_interpreter", arguments={"code": code})


def _sandbox(monkeypatch) -> list[list[tuple[str, bytes]]]:
    """``execute_python`` as both sandboxes behave past their gates: it awaits
    the loader and reports the names it wrote. Returns what each run mounted."""
    mounts: list[list[tuple[str, bytes]]] = []

    async def _execute_python(code, **kwargs):
        loader = kwargs.get("input_files")
        inputs = (await loader()) if callable(loader) else list(loader or [])
        mounts.append(inputs)
        return {
            "stdout": "ok",
            "stderr": "",
            "error": None,
            "images": [],
            "output_files": [],
            "mounted": [name for name, _ in inputs],
        }

    monkeypatch.setattr(code_interpreter, "execute_python", _execute_python)
    return mounts


async def _settle(done, rounds: int = 200) -> None:
    """Run the event loop until *done()* holds - every call parked where the test needs it."""
    for _ in range(rounds):
        if done():
            return
        await asyncio.sleep(0)
    raise AssertionError("the calls never got there")


def _in_flight() -> list[asyncio.Task]:
    """The shared mount reads still running (the cache names each ``sandbox-mount:<file id>``)."""
    return [task for task in asyncio.all_tasks() if task.get_name().startswith("sandbox-mount:") and not task.done()]


# -- One turn, one read per file -----------------------------------------------


async def test_a_parallel_batch_downloads_each_file_once_and_mounts_the_same_bytes(monkeypatch, storage):
    """Three calls of one batch download every file once, not three times,
    and never hold three copies of it at once."""
    expected, _planned, _skipped = await media.collect_sandbox_inputs(FileContext.from_file_refs([_csv(), _html()]))
    storage.downloads.clear()
    mounts = _sandbox(monkeypatch)
    executor = _turn(_csv(), _html())

    storage.gate = asyncio.Event()
    batch = asyncio.gather(*(executor.execute_tool(_call(n, f"print({n}); {_OPENS_ATTACHMENTS}")) for n in range(3)))
    await _settle(lambda: storage.downloads)
    for _ in range(10):
        await asyncio.sleep(0)  # the other calls reach the mount while the first read is in flight
    storage.gate.set()
    results = await batch

    assert storage.count(_CSV[1]) == 1 and storage.count(_HTML[1]) == 1
    assert mounts == [expected] * 3, "every call mounts byte-for-byte what a call reading on its own does"
    lines = {result.content.split("\n\n", 1)[0] for result in results}
    assert len(lines) == 1 and next(iter(lines)).startswith("[Conversation files mounted for this run: attachments/")


async def test_sequential_calls_of_one_turn_download_each_file_once(monkeypatch, storage):
    mounts = _sandbox(monkeypatch)
    executor = _turn(_csv(), _html())
    first = await executor.execute_tool(_call(1))
    second = await executor.execute_tool(_call(2, "import json\nprint(json.load(open('attachments/index.json')))"))
    assert storage.count(_CSV[1]) == 1 and storage.count(_HTML[1]) == 1
    assert mounts[0] == mounts[1]
    assert first.content.split("\n\n", 1)[0] == second.content.split("\n\n", 1)[0], "the same mount line"


async def test_a_failed_or_missing_read_is_never_kept_and_the_next_call_reads_again(monkeypatch, storage):
    _sandbox(monkeypatch)
    executor = _turn(_csv(), _html())
    storage.fail_once[_CSV[1]] = RuntimeError("storage down")
    html = storage.objects.pop(_HTML[1])

    first = await executor.execute_tool(_call(1))
    assert "not mounted (too many, too large or failed to load): a.csv, Model TD.html" in first.content

    storage.objects[_HTML[1]] = html
    second = await executor.execute_tool(_call(2))
    assert "not mounted" not in second.content
    assert storage.count(_CSV[1]) == 2 and storage.count(_HTML[1]) == 2, "a failure and a miss are read again"

    third = await executor.execute_tool(_call(3))
    assert "not mounted" not in third.content
    assert storage.count(_CSV[1]) == 2 and storage.count(_HTML[1]) == 2, "a success is kept for the turn"


async def test_a_batch_sharing_a_failed_read_makes_one_attempt_and_the_next_call_reads_again(monkeypatch, storage):
    """Single flight holds on the failure path: the calls of a batch wait on
    ONE read per file, and when it fails (or misses) every one of them says so
    - no call retries on its own while the others wait. The failure is not
    kept: the next call reads again, and keeps what it gets."""
    _sandbox(monkeypatch)
    executor = _turn(_csv(), _html())
    storage.fail_once[_CSV[1]] = RuntimeError("storage down")
    html = storage.objects.pop(_HTML[1])

    storage.gate = asyncio.Event()
    batch = asyncio.gather(*(executor.execute_tool(_call(n)) for n in range(3)))
    await _settle(lambda: storage.downloads)
    for _ in range(10):
        await asyncio.sleep(0)  # every call of the batch waits on the one read in flight
    storage.gate.set()
    results = await batch

    assert storage.count(_CSV[1]) == 1 and storage.count(_HTML[1]) == 1, "one attempt per file for the whole batch"
    assert all(
        "not mounted (too many, too large or failed to load): a.csv, Model TD.html" in result.content
        for result in results
    )

    storage.gate = None
    storage.objects[_HTML[1]] = html
    retry = await executor.execute_tool(_call(4))
    kept = await executor.execute_tool(_call(5))
    assert "not mounted" not in retry.content and "not mounted" not in kept.content
    assert storage.count(_CSV[1]) == 2 and storage.count(_HTML[1]) == 2, "read again once, then kept"


async def test_a_cancelled_call_never_cancels_the_read_the_other_calls_share(monkeypatch, storage):
    """The load is shielded: the call that started it can be cancelled (a
    user cancel, a timeout) without failing the batch's other calls."""
    _sandbox(monkeypatch)
    executor = _turn(_csv())
    storage.gate = asyncio.Event()
    first = asyncio.ensure_future(executor.execute_tool(_call(1)))
    second = asyncio.ensure_future(executor.execute_tool(_call(2)))
    await _settle(lambda: storage.downloads)
    for _ in range(10):
        await asyncio.sleep(0)
    first.cancel()
    await _settle(first.done)
    storage.gate.set()

    result = await second
    assert first.cancelled()
    mount = code_interpreter.safe_mount_name(_CSV[0], _ID_A)
    assert result.content.startswith(f"[Conversation files mounted for this run: attachments/{mount} (8 B)")
    third = await executor.execute_tool(_call(3))
    assert "not mounted" not in third.content
    assert storage.count(_CSV[1]) == 1, "the read the cancelled call started completed, and was kept, for the others"


async def test_a_read_every_call_gave_up_on_still_completes_and_nothing_is_left_in_flight(monkeypatch, storage):
    """A user cancel stops the whole batch, so EVERY call waiting on a read is
    cancelled - never the read itself: it completes on its own, is kept for
    the rest of the turn, and nothing is left in flight. A read that fails
    once every call has left is not kept: the next call reads again."""
    _sandbox(monkeypatch)

    async def _cancel_every_call_mid_read(executor) -> None:
        started = len(storage.downloads)
        storage.gate = asyncio.Event()
        calls = [asyncio.ensure_future(executor.execute_tool(_call(n))) for n in range(3)]
        await _settle(lambda: len(storage.downloads) > started)
        for _ in range(10):
            await asyncio.sleep(0)
        for call in calls:
            call.cancel()
        await _settle(lambda: all(call.done() for call in calls))
        assert all(call.cancelled() for call in calls)
        assert len(_in_flight()) == 1, "the read outlives every call that waited on it"
        storage.gate.set()
        await _settle(lambda: not _in_flight())
        storage.gate = None

    executor = _turn(_csv())
    await _cancel_every_call_mid_read(executor)
    after = await executor.execute_tool(_call(4))
    assert "not mounted" not in after.content
    assert storage.count(_CSV[1]) == 1, "the read nobody waited for any more was kept for the turn"

    executor = _turn(_html())
    storage.fail_once[_HTML[1]] = RuntimeError("storage down")
    await _cancel_every_call_mid_read(executor)
    retry = await executor.execute_tool(_call(5))
    assert "not mounted" not in retry.content
    assert storage.count(_HTML[1]) == 2, "a read that failed after every call left is not kept"


# -- The bound, the turn, the freshness ------------------------------------------


async def test_the_kept_bytes_never_exceed_the_mount_budget(monkeypatch, storage):
    """Past :data:`MAX_INPUT_TOTAL_BYTES` a file is read per call, as before."""
    monkeypatch.setattr(code_interpreter, "MAX_INPUT_TOTAL_BYTES", 30)
    storage.objects = {"x/1": b"1" * 20, "x/2": b"2" * 15, "x/3": b"3" * 10}
    refs = [_stored(f"f{i}.bin", f"x/{i}", storage.objects[f"x/{i}"], str(uuid.uuid4())) for i in (1, 2, 3)]
    cache = media.SandboxInputCache(FileContext.from_file_refs(refs))

    assert [await cache.read(ref) for ref in refs] == [b"1" * 20, b"2" * 15, b"3" * 10]
    assert cache.held_bytes == 30, "20 + 10 kept; the 15 did not fit after the 20"
    assert [await cache.read(ref) for ref in refs] == [b"1" * 20, b"2" * 15, b"3" * 10]
    assert (storage.count("x/1"), storage.count("x/2"), storage.count("x/3")) == (1, 2, 1)
    assert cache.held_bytes <= code_interpreter.MAX_INPUT_TOTAL_BYTES


async def test_two_turns_never_share_a_read(monkeypatch, storage):
    _sandbox(monkeypatch)
    await _turn(_csv()).execute_tool(_call(1))
    await _turn(_csv()).execute_tool(_call(2))
    assert storage.count(_CSV[1]) == 2, "one read per turn, never one for the whole conversation"

    # A tool instance handed another context never serves it the first one's reads.
    executor = _turn(_csv())
    await executor.execute_tool(_call(3))
    executor.file_context = FileContext.from_file_refs([_csv()])
    await executor.execute_tool(_call(4))
    assert storage.count(_CSV[1]) == 4


async def test_a_file_whose_bytes_changed_is_read_again_never_served_stale(monkeypatch, storage):
    """Keyed on the ref's storage identity: a template asset replaced in place
    keeps its id and gets a new object key; a revision (``edit_file_content``)
    is a new id; a file carried in memory is read from its ref every time."""
    mounts = _sandbox(monkeypatch)
    storage.objects["assets/v1.png"] = b"v1"
    executor = _turn(_stored("logo.png", "assets/v1.png", b"v1", _ID_A, "image/png"))
    await executor.execute_tool(_call(1))

    storage.objects["assets/v2.png"] = b"v2-bytes"
    executor.file_context.add(_stored("logo.png", "assets/v2.png", b"v2-bytes", _ID_A, "image/png"))
    await executor.execute_tool(_call(2))
    assert [dict(run)[code_interpreter.safe_mount_name("logo.png", _ID_A)] for run in mounts] == [b"v1", b"v2-bytes"]
    assert storage.count("assets/v1.png") == 1 and storage.count("assets/v2.png") == 1

    executor.file_context.add(FileRef.from_bytes(b"one", "note.txt", "text/plain", file_id=_ID_B))
    await executor.execute_tool(_call(3))
    executor.file_context.add(FileRef.from_bytes(b"two", "note.txt", "text/plain", file_id=_ID_B))
    await executor.execute_tool(_call(4))
    note = code_interpreter.safe_mount_name("note.txt", _ID_B)
    assert [dict(run)[note] for run in mounts[2:]] == [b"one", b"two"]


async def test_a_file_edited_in_the_turn_is_mounted_from_its_revision_never_from_the_kept_bytes(monkeypatch, storage):
    """Write then run, in ONE turn: ``edit_file_content`` saves a revision - a
    new id under a new object key - on the turn's context, so the next run
    mounts the edited bytes, read once, next to the original, which comes from
    the kept read."""
    mounts = _sandbox(monkeypatch)

    async def _persist(**kwargs):
        # ``_persist_generated_file`` as the edit meets it: a new row under a new random key.
        key = f"chat/c/{uuid.uuid4().hex}.csv"
        storage.objects[key] = kwargs["raw_bytes"]
        return SimpleNamespace(
            id=uuid.uuid4(),
            filename=kwargs["filename"],
            content_type=kwargs["content_type"],
            size=len(kwargs["raw_bytes"]),
            s3_object_name=key,
            source=kwargs["source"],
        )

    monkeypatch.setattr("app.services.builtin_tools._persist_generated_file", _persist)
    executor = _turn(_csv())
    _defs, files = media.build_generate_file_tool(None, str(uuid.uuid4()), str(uuid.uuid4()))
    files.file_context = executor.file_context  # the turn's tools share its one context

    await executor.execute_tool(_call(1))
    edit = {"file_id": _ID_A, "edits": [{"find": "1,2", "replace": "3,4"}]}
    edited = await files.execute_tool(ToolCall(id="e1", name="edit_file_content", arguments=edit))
    assert not edited.is_error, edited.content
    (revision,) = edited.files
    await executor.execute_tool(_call(2))

    original = code_interpreter.safe_mount_name(_CSV[0], _ID_A)
    edited_mount = code_interpreter.safe_mount_name(revision.filename, revision.file_id)
    assert dict(mounts[1])[edited_mount] == b"a,b\n3,4\n", "the run after the edit mounts the edited bytes"
    assert dict(mounts[1])[original] == dict(mounts[0])[original] == _CSV[2], "the original is unchanged"
    assert storage.count(revision.s3_object_name) == 1
    # The first mount and the edit's own read of the source (``edit_file_content``
    # reads through the context, not through the mount's reads); the second run
    # mounts the original from the kept read.
    assert storage.count(_CSV[1]) == 2


# -- Both sandboxes ----------------------------------------------------------------


async def test_the_local_sandbox_writes_the_shared_bytes_for_every_call(monkeypatch, storage):
    from app.core.config import settings

    monkeypatch.setattr(settings, "custom_tool_sandbox", "subprocess")
    monkeypatch.setattr(settings, "disable_code_interpreter", False)
    seen: list[dict[str, bytes]] = []

    class _Process:
        returncode = 0

        def __init__(self, cwd: str) -> None:
            self.cwd = cwd

        async def communicate(self):
            attach = os.path.join(self.cwd, code_interpreter.ATTACHMENTS_DIRNAME)
            files: dict[str, bytes] = {}
            for name in sorted(os.listdir(attach)):
                with open(os.path.join(attach, name), "rb") as fh:
                    files[name] = fh.read()
            seen.append(files)
            return json.dumps({"stdout": "ok", "stderr": "", "error": None, "images": []}).encode(), b""

    async def _spawn(*_args, cwd=None, **_kwargs):
        return _Process(cwd)

    monkeypatch.setattr(code_interpreter.asyncio, "create_subprocess_exec", _spawn)
    executor = _turn(_csv(), _html())
    results = await asyncio.gather(*(executor.execute_tool(_call(n)) for n in range(3)))

    assert storage.count(_CSV[1]) == 1 and storage.count(_HTML[1]) == 1
    csv_mount = code_interpreter.safe_mount_name(_CSV[0], _ID_A)
    html_mount = code_interpreter.safe_mount_name(_HTML[0], _ID_B)
    assert [(files[csv_mount], files[html_mount]) for files in seen] == [(_CSV[2], _HTML[2])] * 3
    assert all(f"attachments/{csv_mount} (8 B)" in result.content for result in results)


@pytest.fixture
def opensandbox(monkeypatch) -> SimpleNamespace:
    """The OpenSandbox path for real up to the container: its probe, its quota gate, its creation."""
    from app.core.config import settings
    from app.services import _sandbox_selector, quota

    monkeypatch.setattr(settings, "custom_tool_sandbox", "opensandbox")
    monkeypatch.setattr(settings, "disable_code_interpreter", False)
    monkeypatch.setattr(settings, "opensandbox_domain", "localhost:8090")
    monkeypatch.setattr(settings, "opensandbox_protocol", "http")
    monkeypatch.setattr(settings, "opensandbox_api_key", "test-key")
    monkeypatch.setattr(settings, "opensandbox_network_allow", "")
    monkeypatch.setattr(quota, "enforce_combined_sandbox_quota", AsyncMock(return_value=None))
    monkeypatch.setattr(quota, "increment_sandbox_usage", AsyncMock(return_value=None))
    gates = SimpleNamespace(reachable=True)

    async def _probe() -> bool:
        return gates.reachable

    monkeypatch.setattr(_sandbox_selector, "_probe_opensandbox_available", _probe)
    return gates


def _container() -> tuple[AsyncMock, list[tuple[str, bytes]]]:
    """An OpenSandbox container that records what the mount wrote into it."""
    written: list[tuple[str, bytes]] = []
    run = MagicMock(exit_code=0, complete=SimpleNamespace(execution_time_in_millis=5))
    run.logs.stdout = [MagicMock(text='{"stdout": "ok", "error": null}')]
    run.logs.stderr = []
    sandbox = AsyncMock()
    sandbox.__aenter__ = AsyncMock(return_value=sandbox)
    sandbox.__aexit__ = AsyncMock(return_value=False)
    sandbox.files.create_directories = AsyncMock()

    async def _write(path: str, data: bytes) -> None:
        written.append((path, data))

    sandbox.files.write_file = AsyncMock(side_effect=_write)
    sandbox.files.list_directory = AsyncMock(return_value=[])
    sandbox.commands.run = AsyncMock(return_value=run)
    sandbox.get_metrics = AsyncMock(
        return_value=SimpleNamespace(
            cpu_used_percentage=1.0, cpu_count=1.0, memory_used_in_mib=1.0, memory_total_in_mib=2.0
        )
    )
    return sandbox, written


async def test_a_refused_run_reads_nothing_and_the_runs_past_the_gates_share_one_read(
    monkeypatch, storage, opensandbox
):
    from app.services import quota

    executor = _turn(_csv(), _html())
    opensandbox.reachable = False
    unreachable = await executor.execute_tool(_call(1))
    assert "not reachable" in unreachable.content

    opensandbox.reachable = True
    monkeypatch.setattr(
        quota,
        "enforce_combined_sandbox_quota",
        AsyncMock(side_effect=quota.QuotaExceededError("sandbox", 5, 5, "monthly")),
    )
    spent = await executor.execute_tool(_call(2))
    assert quota.QUOTA_EXCEEDED_MESSAGE in spent.content

    monkeypatch.setattr(quota, "enforce_combined_sandbox_quota", AsyncMock(return_value=None))
    with patch("opensandbox.Sandbox") as sdk:
        sdk.create = AsyncMock(side_effect=RuntimeError("no capacity"))
        uncreated = await executor.execute_tool(_call(3))
    assert "Failed to create sandbox" in uncreated.content
    assert storage.downloads == [], "no gate passed, nothing read"

    containers = [_container() for _ in range(3)]
    with patch("opensandbox.Sandbox") as sdk:
        sdk.create = AsyncMock(side_effect=[sandbox for sandbox, _ in containers])
        results = await asyncio.gather(*(executor.execute_tool(_call(n)) for n in range(4, 7)))

    assert storage.count(_CSV[1]) == 1 and storage.count(_HTML[1]) == 1
    attach = f"/tmp/{code_interpreter.ATTACHMENTS_DIRNAME}"  # noqa: S108 - the container's working directory
    csv_mount = code_interpreter.safe_mount_name(_CSV[0], _ID_A)
    html_mount = code_interpreter.safe_mount_name(_HTML[0], _ID_B)
    for _sandbox_mock, written in containers:
        files = dict(written)
        assert (files[f"{attach}/{csv_mount}"], files[f"{attach}/{html_mount}"]) == (_CSV[2], _HTML[2])
    assert all(f"attachments/{csv_mount} (8 B)" in result.content for result in results)
