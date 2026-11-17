"""What screening tool results reads, and how often.

The scan scope is a security option: the loop reads it from the process's
snapshot, ONCE per run, never from the database and never per tool call. The
database edge is a stand-in counting every session opened and statement run,
across runs mixing single calls and parallel batches:

* no tool screened: no session at all, whatever the number of results;
* tools screened, no guard connected: one connection-state read, then none -
  not per result, not for the attachments of the results that follow, and
  not in the next run while "none connected" is known;
* a guard connected: one session per screened result, the evaluation's own,
  and none for the scope.
"""

from __future__ import annotations

import ast
import contextlib
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.core import security_options as so
from app.llm.base import ChatMessage, ChatResponse, FileRef
from app.services import nova_engine
from app.services.tool_result_guard import BLOCK_OPTION, SCOPE_OPTION, drain_background
from app.tools import agent_loop as al
from app.tools.base import CallableToolExecutor, CompositeToolExecutor, ToolCall, ToolDefinition, ToolResult
from tests.test_agent_loop_run_budget import (
    _PADDING,
    _quiet_langfuse,  # noqa: F401 - pytest fixture
    _ScriptedLLM,
)

pytestmark = pytest.mark.usefixtures("_quiet_langfuse")

APP_DIR = Path(__file__).resolve().parents[1] / "app"


class _Database:
    """``app.core.database.async_session``: counts sessions and statements."""

    def __init__(self, *, connected: bool) -> None:
        self.connected = connected
        self.opened = 0
        self.statements = 0

    def __call__(self):
        return self._session()

    @contextlib.asynccontextmanager
    async def _session(self):
        self.opened += 1
        yield SimpleNamespace(execute=self.execute, commit=self.commit)

    async def execute(self, statement, *args, **kwargs):
        self.statements += 1
        row = (uuid.uuid4(),) if self.connected else None
        return SimpleNamespace(first=lambda: row, scalars=lambda: SimpleNamespace(all=lambda: []))

    async def commit(self):
        return None


@pytest.fixture
def database(monkeypatch):
    def install(**kwargs) -> _Database:
        db = _Database(**kwargs)
        monkeypatch.setattr("app.core.database.async_session", db)
        return db

    monkeypatch.setattr(nova_engine, "NOVA_HUNTING_AVAILABLE", False)
    return install


@pytest.fixture
def scope_reads(monkeypatch) -> list[str]:
    """Every read of the two screening options, by key."""
    reads: list[str] = []
    real = so.security_option

    def _counting(key):
        if key in (SCOPE_OPTION, BLOCK_OPTION):
            reads.append(key)
        return real(key)

    monkeypatch.setattr(so, "security_option", _counting)
    return reads


def _composite() -> CompositeToolExecutor:
    callable_exec = CallableToolExecutor()

    async def _fetch(**kwargs):
        q = kwargs.get("q")
        # A file of its own per call: the extraction memo cannot hide a read.
        note = FileRef.from_bytes(f"release notes {q}\n".encode(), f"notes-{q}.txt", "text/plain")
        return ToolResult(tool_call_id="", content=f"page {q}", files=[note])

    async def _lookup(**kwargs):
        return f"row {kwargs.get('q')}"

    callable_exec.register("fetch", _fetch)
    callable_exec.register("lookup", _lookup)
    composite = CompositeToolExecutor()
    schema = {"type": "object", "properties": {"q": {"type": "string"}}}
    composite.add_tools(
        [
            ToolDefinition(name="fetch", description="", input_schema=schema),
            ToolDefinition(name="lookup", description="", input_schema=schema),
        ],
        callable_exec,
    )
    return composite


def _multi_tool_llm() -> _ScriptedLLM:
    """A single call, then a parallel batch of four, then another single call: six results."""

    def _tools(calls):
        return ChatResponse(content="", model="stub-model", stop_reason="tool_use", tool_calls=calls, usage={})

    return _ScriptedLLM(
        [
            _tools([ToolCall(id="c1", name="fetch", arguments={"q": "1"})]),
            _tools(
                [
                    ToolCall(id="c2", name="fetch", arguments={"q": "2"}),
                    ToolCall(id="c3", name="lookup", arguments={"q": "3"}),
                    ToolCall(id="c4", name="fetch", arguments={"q": "4"}),
                    ToolCall(id="c5", name="lookup", arguments={"q": "5"}),
                ]
            ),
            _tools([ToolCall(id="c6", name="lookup", arguments={"q": "6"})]),
            ChatResponse(content="Done." + _PADDING, model="stub-model", usage={}),
        ]
    )


async def _run() -> list[ChatMessage]:
    composite = _composite()
    messages = [ChatMessage(role="user", content="go")]
    await al.run_agent_loop(
        llm=_multi_tool_llm(),
        messages=messages,
        tools=composite.all_tools,
        executor=composite,
        max_iterations=8,
    )
    await drain_background()
    assert [m.tool_call_id for m in messages if m.role == "tool"] == ["c1", "c2", "c3", "c4", "c5", "c6"]
    return messages


async def test_no_tool_screened_is_no_session_and_one_read_of_the_scope(database, scope_reads):
    db = database(connected=True)

    await _run()

    assert (db.opened, db.statements) == (0, 0)
    # The scope, once; the mode is not even read.
    assert scope_reads == [SCOPE_OPTION]


@pytest.mark.parametrize("blocking", [False, True])
async def test_the_scope_is_read_once_per_run_never_per_tool_call(database, scope_reads, blocking):
    database(connected=True)
    so.publish_stored({SCOPE_OPTION: ("fetch", "lookup"), BLOCK_OPTION: blocking})

    await _run()
    await _run()

    assert scope_reads == [SCOPE_OPTION, BLOCK_OPTION, SCOPE_OPTION, BLOCK_OPTION]


@pytest.mark.parametrize("blocking", [False, True])
async def test_with_no_guard_connected_one_read_then_none(database, blocking):
    db = database(connected=False)
    so.publish_stored({SCOPE_OPTION: ("fetch", "lookup"), BLOCK_OPTION: blocking})

    await _run()
    # Six screened results, attachments included: the connection state, once.
    assert (db.opened, db.statements) == (1, 1)

    await _run()
    # "None connected" is known: the next run opens nothing.
    assert (db.opened, db.statements) == (1, 1)


async def test_no_guard_connected_reads_no_attachment_once_that_is_known(database, monkeypatch):
    from app.services import chunking

    database(connected=False)
    so.publish_stored({SCOPE_OPTION: ("fetch", "lookup"), BLOCK_OPTION: True})
    extractions: list[str] = []
    real_extract = chunking.extract_text_from_file

    def _counting(data, filename):
        extractions.append(filename)
        return real_extract(data, filename)

    monkeypatch.setattr(chunking, "extract_text_from_file", _counting)

    await _run()

    # The first fetch's attachment is read before the guard answers "none
    # connected"; the two fetches that follow read none.
    assert extractions == ["notes-1.txt"]


async def test_in_a_batch_the_results_waiting_for_a_slot_read_nothing_once_no_guard_is_known(database, monkeypatch):
    from app.services import chunking, tool_result_guard
    from app.services.tool_result_guard import ToolResultScreen

    db = database(connected=False)
    extractions: list[str] = []
    real_extract = chunking.extract_text_from_file

    def _counting(data, filename):
        extractions.append(filename)
        return real_extract(data, filename)

    monkeypatch.setattr(chunking, "extract_text_from_file", _counting)
    cap = tool_result_guard.MAX_CONCURRENT_EVALUATIONS
    screen = ToolResultScreen(frozenset({"fetch"}), blocking=True)
    results = [
        ToolResult(
            tool_call_id="",
            content=f"page {i}",
            files=[FileRef.from_bytes(f"notes {i}\n".encode(), f"notes-{i}.txt", "text/plain")],
        )
        for i in range(2 * cap)
    ]

    withheld = await screen.screen_batch([(i, "fetch", r) for i, r in enumerate(results)], truncate=lambda t: t)

    assert withheld == {}
    # At most the first slots' worth read their attachment and asked; the
    # others, queued behind them, learned "none connected" and read nothing.
    # A slot holder that enters after the first answer skips too, so fewer
    # than a slots' worth is the scheduler's choice, not a leak.
    assert 1 <= len(extractions) <= cap
    assert db.opened <= cap


async def test_while_no_guard_is_known_a_result_is_not_even_read(database):
    from app.services import prompt_guards
    from app.services.tool_result_guard import ToolResultScreen

    database(connected=False)
    # A first evaluation learns that no guard is connected.
    await prompt_guards.evaluate_text("hello", surface=prompt_guards.SURFACE_TOOL_RESULT)
    read: list[str] = []

    def _truncate(text: str) -> str:
        read.append(text)
        return text

    for blocking in (False, True):
        screen = ToolResultScreen(frozenset({"fetch"}), blocking=blocking)
        result = ToolResult(tool_call_id="", content="page")
        assert await screen.screen("fetch", result, truncate=_truncate) is result
    await drain_background()

    assert read == []


async def test_a_connected_guard_opens_one_session_per_screened_result_and_none_for_the_scope(database):
    db = database(connected=True)
    so.publish_stored({SCOPE_OPTION: ("fetch",), BLOCK_OPTION: True})

    await _run()

    # Three fetch results screened (c1, c2, c4); lookup is not in scope. Each
    # evaluation reads the connection state and the rules on its own session.
    assert (db.opened, db.statements) == (3, 6)


def test_the_loop_reads_the_scope_only_through_the_run_s_screen():
    """No scope read, security option or session in the loop: the screen is built once, before the iterations."""
    source = (APP_DIR / "tools/agent_loop.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "for_run"
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "ToolResultScreen"
    ]
    assert len(calls) == 1
    assert "security_option(" not in source
    assert SCOPE_OPTION not in source and BLOCK_OPTION not in source
    # Built before the iteration loop: never re-read per iteration or per call.
    [loop] = [node for node in ast.walk(tree) if isinstance(node, ast.For) and _iterates_iterations(node)]
    assert calls[0].lineno < loop.lineno


def _iterates_iterations(node: ast.For) -> bool:
    return isinstance(node.target, ast.Name) and node.target.id == "iteration"
