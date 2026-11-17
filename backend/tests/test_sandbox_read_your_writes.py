"""Read-your-writes on the sandbox workspace within one tool round (#11714).

The agent loop runs the calls of one response under ``asyncio.gather``. Every
workspace reader waits, bounded, for the writes of its round: the file tools,
the checkout status and the ``workspace_path`` readers (``WorkspaceIngestor``),
a ``code_interpreter`` call before it mounts, and a ``to_workspace_path`` ingest
is itself counted as a write. A read made inside a writing tool never waits for
its own write.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import uuid
from collections.abc import Iterator
from typing import Any, cast

import pytest

from app.services.sandbox_workspace.ingest import WorkspaceIngestor
from app.services.sandbox_workspace.manifest import Limits
from app.services.sandbox_workspace.session import WorkspaceSession
from app.services.sandbox_workspace.store import MemoryWorkspaceStore, WorkspaceScope


@pytest.fixture(autouse=True)
def _workspace_on(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.core.config import settings

    monkeypatch.setattr(settings, "code_interpreter_workspace_enabled", True)


def _session() -> WorkspaceSession:
    scope = WorkspaceScope(user_id=uuid.uuid4(), conversation_id=uuid.uuid4())
    return WorkspaceSession(scope, store=MemoryWorkspaceStore(), limits=Limits(quota_bytes=1 << 22, max_files=500))


async def _edit(session: WorkspaceSession, path: str, data: bytes) -> None:
    """A writing tool as the loop dispatches it: started a moment after the reader, slow to commit."""
    await asyncio.sleep(0)
    with session.writing():
        async with session.write_turn():
            await asyncio.sleep(0.01)
            await session.put_bytes(path, data)


async def test_a_read_inside_a_writing_tool_does_not_wait_for_itself() -> None:
    session = _session()
    with session.writing():
        waited, left = await asyncio.wait_for(session.settled(timeout=5), 1)
    assert (waited, left) == (0.0, 0)


async def test_a_code_interpreter_call_waits_for_the_tools_but_not_for_another_call() -> None:
    session = _session()
    with session.writing(run=True):
        assert session.writes_in_flight == 1
        # Another code_interpreter call of the round: not held by this one.
        assert await session.settled(timeout=1, grace=0, tools_only=True) == (0.0, 0)
        # Any other reader of the round waits for it.
        _waited, left = await session.settled(timeout=0.02, grace=0)
        assert left == 1
    assert session.writes_in_flight == 0

    order: list[str] = []

    async def run() -> None:
        with session.writing(run=True):
            _waited, left = await session.settled(timeout=5, tools_only=True)
            got = (await session.get_bytes("a.py"))[1]
            order.append(f"run:{got.decode()}:{left}")

    await session.put_bytes("a.py", b"old")
    await asyncio.gather(run(), _edit(session, "a.py", b"new"))
    assert order == ["run:new:0"]


async def test_ingestor_read_file_and_checkout_status_see_the_edit_of_their_round() -> None:
    session = _session()
    record = {
        "repository": "o/r",
        "ref": "main",
        "commit": "a" * 40,
        "files": {"src/a.py": "0" * 64},
    }
    await session.put_bytes("co/.xtm-checkout.json", json.dumps(record).encode())
    await session.put_bytes("co/src/a.py", b"old\n")
    ingestor = WorkspaceIngestor(session)
    seen: list[str | bytes] = []

    async def read() -> None:
        _rel, _name, data = await ingestor.read_file("co/src/a.py", tool="create_github_commit")
        seen.append(data)

    await asyncio.gather(read(), _edit(session, "co/src/a.py", b"new\n"))
    assert seen == [b"new\n"]

    async def status() -> None:
        # A record the reader refuses still shows the order, which is what is checked.
        with contextlib.suppress(ValueError):
            _record, _changes = await ingestor.checkout_changes("co", None)
        entries = await session.entries(refresh=True)
        seen.append(entries["co/src/b.py"].sha256 if "co/src/b.py" in entries else b"missing")

    await asyncio.gather(status(), _edit(session, "co/src/b.py", b"b\n"))
    assert seen[-1] != b"missing"


async def test_a_to_workspace_path_ingest_is_counted_as_a_write() -> None:
    session = _session()
    ingestor = WorkspaceIngestor(session)
    order: list[str] = []

    async def fetch_to_workspace() -> None:
        # What the dispatcher does around a to_workspace_path call.
        with ingestor.writing():
            await asyncio.sleep(0.02)
            await session.put_bytes("work/file.json", b"{}")
            order.append("ingest")

    async def read() -> None:
        await asyncio.sleep(0.005)
        _rel, _name, data = await ingestor.read_file("work/file.json", tool="sandbox_read_file")
        order.append(f"read:{data.decode()}")

    await asyncio.gather(fetch_to_workspace(), read())
    assert order == ["ingest", "read:{}"]


async def test_the_dispatcher_counts_a_to_workspace_path_call(monkeypatch: pytest.MonkeyPatch) -> None:
    """``CompositeToolExecutor._dispatch_with_workspace`` enters the sink's ``writing`` around the tool."""
    from app.tools import base

    entered: list[str] = []

    class Sink:
        def check_path(self, path: object) -> str | None:
            return None

        @contextlib.contextmanager
        def writing(self) -> Iterator[None]:
            entered.append("in")
            yield
            entered.append("out")

        async def ingest(self, *, call: Any, result: Any, captured: Any, path: Any, file_context: Any) -> Any:
            entered.append("ingest")
            return result

    class Inner:
        async def execute_tool(self, call: Any) -> Any:
            entered.append("tool")
            return base.ToolResult(tool_call_id=call.id, content="ok")

    definition = base.ToolDefinition(
        name="fetch", description="d", input_schema={"type": "object", "properties": {}}, workspace_ingest=True
    )
    composite = base.CompositeToolExecutor.__new__(base.CompositeToolExecutor)
    monkeypatch.setattr(composite, "get_tool_definition", lambda name: definition, raising=False)
    composite.file_context = None
    call = base.ToolCall(id="c1", name="fetch", arguments={base.WORKSPACE_PATH_ARGUMENT: "work/x.txt"})
    await composite._dispatch_with_workspace(call, cast(Any, Inner()), cast(Any, Sink()))
    assert entered == ["in", "tool", "ingest", "out"]


async def test_one_hundred_rounds_of_write_then_read_see_no_stale_read() -> None:
    """Acceptance criterion 1: edit, then get_file, checkout read and a code_interpreter mount in one round, 100 of 100."""
    session = _session()
    ingestor = WorkspaceIngestor(session)
    await session.put_bytes("w/a.txt", b"0")
    stale = 0

    async def get_file(got: list[bytes]) -> None:
        await session.settled(grace=0.001)
        got.append((await session.get_bytes("w/a.txt"))[1])

    async def tool_read(got: list[bytes]) -> None:
        got.append((await ingestor.read_file("w/a.txt", tool="create_github_commit"))[2])

    async def interpreter(got: list[bytes]) -> None:
        with session.writing(run=True):
            await session.settled(tools_only=True, grace=0.001)
            got.append((await session.get_bytes("w/a.txt"))[1])

    for i in range(1, 101):
        want = str(i).encode()
        got: list[bytes] = []
        await asyncio.gather(get_file(got), tool_read(got), interpreter(got), _edit(session, "w/a.txt", want))
        stale += sum(1 for data in got if data != want)
    assert stale == 0
    assert session.writes_in_flight == 0
