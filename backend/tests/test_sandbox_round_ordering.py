"""A command started in the same tool round as a workspace write sees it.

The agent loop runs the calls of one response under ``asyncio.gather``: a
``sandbox_run_command`` next to a ``sandbox_edit_file`` read the manifest before
the edit committed and ran the old file. The session counts the writes in
flight (``WorkspaceSession.writing``) and a command waits, bounded, for none to
be left (``WorkspaceSession.settled``).
"""

from __future__ import annotations

import asyncio
import uuid

import pytest

from app.services.builtin_tools import sandbox_workspace as tools_module
from app.services.sandbox_workspace.manifest import Limits
from app.services.sandbox_workspace.session import WorkspaceSession
from app.services.sandbox_workspace.store import MemoryWorkspaceStore, WorkspaceScope


@pytest.fixture(autouse=True)
def _workspace_on(monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "code_interpreter_workspace_enabled", True)


def _session() -> WorkspaceSession:
    scope = WorkspaceScope(user_id=uuid.uuid4(), conversation_id=uuid.uuid4())
    return WorkspaceSession(scope, store=MemoryWorkspaceStore(), limits=Limits(quota_bytes=1 << 20, max_files=100))


async def test_settled_answers_at_once_when_nothing_is_written():
    session = _session()
    waited, left = await session.settled(timeout=1, grace=0)
    assert (waited, left) == (0.0, 0)


async def test_a_command_of_the_round_waits_for_the_write_that_started_after_it():
    session = _session()
    order: list[str] = []

    async def edit() -> None:
        # The loop dispatches the write a moment after the command's task started.
        await asyncio.sleep(0)
        with session.writing():
            await asyncio.sleep(0.05)
            await session.put_bytes("src/a.py", b"new\n")
            order.append("edit")

    async def run() -> None:
        _waited, left = await session.settled(timeout=5)
        entries = await session.entries(refresh=True)
        order.append(f"run:{entries['src/a.py'].size if 'src/a.py' in entries else 'missing'}:{left}")

    await asyncio.gather(run(), edit())
    assert order == ["edit", "run:4:0"]


async def test_a_write_that_never_ends_holds_a_command_only_for_the_bound():
    session = _session()
    release = asyncio.Event()

    async def stuck() -> None:
        with session.writing():
            await release.wait()

    task = asyncio.create_task(stuck())
    await asyncio.sleep(0)
    waited, left = await session.settled(timeout=0.05, grace=0)
    assert left == 1 and waited >= 0.04
    release.set()
    await task
    assert session.writes_in_flight == 0
    assert await session.settled(timeout=1, grace=0) == (0.0, 0)


async def test_a_failed_write_is_no_longer_counted():
    session = _session()
    with pytest.raises(RuntimeError), session.writing():
        assert session.writes_in_flight == 1
        raise RuntimeError("boom")
    assert session.writes_in_flight == 0


def test_every_workspace_writing_tool_is_counted_and_no_reader_is():
    assert set(tools_module.SANDBOX_TOOL_NAMES) >= tools_module.WORKSPACE_WRITE_TOOLS
    expected = {
        "sandbox_put_file",
        "sandbox_remove_files",
        "sandbox_move_files",
        "sandbox_edit_file",
        "sandbox_replace_in_files",
        "sandbox_apply_patch",
        "sandbox_write_file",
        "sandbox_json_keys",
        "sandbox_convert_images",
        "materialize_repository",
        "sandbox_checkout_rebase",
        "sandbox_diff_files",
    }
    assert expected == tools_module.WORKSPACE_WRITE_TOOLS
    assert not tools_module.WORKSPACE_WRITE_TOOLS & {"sandbox_run_command", "sandbox_start_service", "sandbox_read_file"}


async def test_the_registered_write_handler_is_counted_while_it_runs():
    session = _session()
    seen: list[int] = []

    async def handler(path: str) -> str:
        seen.append(session.writes_in_flight)
        return path

    counted = tools_module._counted_write(session, handler)
    assert await counted(path="x") == "x"
    assert seen == [1] and session.writes_in_flight == 0


async def test_writes_of_one_round_take_turns_in_issue_order_and_are_all_counted():
    """A read-compute-commit write never interleaves with another (no lost update)."""
    session = _session()
    events: list[str] = []
    peak: list[int] = []

    async def handler(name: str) -> str:
        events.append(f"start:{name}")
        await asyncio.sleep(0.02)
        peak.append(session.writes_in_flight)
        events.append(f"end:{name}")
        return name

    counted = tools_module._counted_write(session, handler)
    assert await asyncio.gather(counted(name="a"), counted(name="b")) == ["a", "b"]
    # Run one after another, in issue order; the waiting write is counted (a command of the round waits for it).
    assert events == ["start:a", "end:a", "start:b", "end:b"]
    assert peak == [2, 1] and session.writes_in_flight == 0


async def test_a_failed_write_releases_the_turn():
    session = _session()

    async def boom() -> None:
        raise RuntimeError("boom")

    async def ok() -> str:
        return "ok"

    results = await asyncio.gather(
        tools_module._counted_write(session, boom)(), tools_module._counted_write(session, ok)(), return_exceptions=True
    )
    assert isinstance(results[0], RuntimeError) and results[1] == "ok"
    assert session.writes_in_flight == 0
