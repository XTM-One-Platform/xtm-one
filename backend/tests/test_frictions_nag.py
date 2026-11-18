"""The friction finalisation nag (#8528 slice 3b).

A run that recorded frictions and is about to end with some undisposed is
shown ONE system line naming them and given one more round to call
``dispose_friction``; once per run, never without the tool, never when the
run has none.
"""

from __future__ import annotations

import asyncio
import uuid

import pytest

from app.llm.base import BaseLLMProvider, ChatMessage, ChatResponse
from app.services import frictions
from app.tools.agent_loop import CONTINUATION_COMPOSING_REASONS, run_agent_loop
from app.tools.base import ToolCall, ToolDefinition, ToolResult

AGENT = uuid.uuid4()
USER = uuid.uuid4()
RUN = uuid.uuid4()

_LINE = "[SYSTEM: frictions] 1 friction(s) of this run have no disposition: `x` tool_error on t."


class _ScriptedLLM(BaseLLMProvider):
    provider_name = "stub"
    context_window = 50_000

    def __init__(self, script: list[ChatResponse]) -> None:
        self.script = list(script)
        self.calls: list[list[ChatMessage]] = []

    async def chat(self, messages, model=None, temperature=0.7, max_tokens=4096, tools=None, **kwargs):
        self.calls.append(list(messages))
        return self.script[len(self.calls) - 1]

    async def chat_stream(self, messages, model=None, temperature=0.7, max_tokens=4096, tools=None, **kwargs):
        yield "unused"

    async def embed(self, text, model=None):
        return [0.0]

    async def list_models(self):
        return []


class _Executor:
    def __init__(self) -> None:
        self.called: list[str] = []

    async def execute_tool(self, tool_call):
        self.called.append(tool_call.name)
        return ToolResult(tool_call_id=tool_call.id, content="ok")


def _tool(name: str) -> ToolDefinition:
    return ToolDefinition(name=name, description=name, input_schema={"type": "object", "properties": {}})


def _dispose_call() -> ChatResponse:
    return ChatResponse(
        content="", model="stub", tool_calls=[ToolCall(id="d1", name="dispose_friction", arguments={})]
    )


def _nag_count(call_messages) -> int:
    return sum(1 for m in call_messages if m.role == "system" and m.content == _LINE)


@pytest.fixture
def nag(monkeypatch):
    asked: list[int] = []

    async def fake_nag(scope=None):
        asked.append(1)
        return _LINE

    monkeypatch.setattr(frictions, "finalisation_nag", fake_nag)
    return asked


@pytest.mark.asyncio
async def test_a_run_ending_with_undisposed_frictions_gets_one_disposal_round(nag):
    llm = _ScriptedLLM(
        [
            ChatResponse(content="Done.", model="stub"),
            _dispose_call(),
            ChatResponse(content="Done, frictions disposed of.", model="stub"),
        ]
    )
    executor = _Executor()
    events: list[dict] = []

    async def on_event(event: dict) -> None:
        events.append(event)

    response = await run_agent_loop(
        llm=llm,
        messages=[ChatMessage(role="user", content="go")],
        tools=[_tool("dispose_friction")],
        executor=executor,
        max_iterations=8,
        on_event=on_event,
    )

    assert response.content == "Done, frictions disposed of."
    assert executor.called == ["dispose_friction"]
    assert len(llm.calls) == 3
    assert _nag_count(llm.calls[0]) == 0
    assert _nag_count(llm.calls[1]) == 1
    assert len(nag) == 1
    assert any(e.get("status") == "composing" and e.get("reason") == "frictions_continuation" for e in events)
    assert "frictions_continuation" in CONTINUATION_COMPOSING_REASONS


@pytest.mark.asyncio
async def test_the_nag_fires_at_most_once(nag):
    llm = _ScriptedLLM(
        [
            ChatResponse(content="Done.", model="stub"),
            ChatResponse(content="Still done.", model="stub"),
        ]
    )
    response = await run_agent_loop(
        llm=llm,
        messages=[ChatMessage(role="user", content="go")],
        tools=[_tool("dispose_friction")],
        executor=_Executor(),
        max_iterations=8,
    )
    assert response.content == "Still done."
    assert len(llm.calls) == 2
    assert len(nag) == 1


@pytest.mark.asyncio
async def test_no_nag_without_the_dispose_tool(nag):
    llm = _ScriptedLLM([ChatResponse(content="Done.", model="stub")])
    response = await run_agent_loop(
        llm=llm,
        messages=[ChatMessage(role="user", content="go")],
        tools=[_tool("other")],
        executor=_Executor(),
        max_iterations=8,
    )
    assert response.content == "Done."
    assert len(llm.calls) == 1
    assert nag == []


@pytest.mark.asyncio
async def test_a_run_with_nothing_undisposed_ends_as_before(monkeypatch):
    async def none_left(scope=None):
        return None

    monkeypatch.setattr(frictions, "finalisation_nag", none_left)
    llm = _ScriptedLLM([ChatResponse(content="Done.", model="stub")])
    response = await run_agent_loop(
        llm=llm,
        messages=[ChatMessage(role="user", content="go")],
        tools=[_tool("dispose_friction")],
        executor=_Executor(),
        max_iterations=8,
    )
    assert response.content == "Done."
    assert len(llm.calls) == 1


# ── finalisation_nag itself, on an in-memory ledger ──────────────────────────


class _Row:
    def __init__(self, kind: str, tool: str, count: int = 1, disposed: bool = False) -> None:
        self.id = uuid.uuid4()
        self.run_id = RUN
        self.kind = kind
        self.tool = tool
        self.count = count
        self.disposed_at = object() if disposed else None


class _Rows:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return self

    def all(self):
        return list(self._rows)


class _Session:
    def __init__(self, rows):
        self.rows = rows

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def execute(self, _stmt):
        return _Rows([r for r in self.rows if r.run_id == RUN and r.disposed_at is None])


def _with_rows(monkeypatch, rows) -> None:
    import app.core.database as database

    monkeypatch.setattr(database, "async_session", lambda: _Session(rows))


def test_finalisation_nag_names_the_undisposed_frictions(monkeypatch):
    rows = [_Row("tool_error", "search", count=2), _Row("approval_pause", "send"), _Row("retry", "x", disposed=True)]
    _with_rows(monkeypatch, rows)
    line = asyncio.run(frictions.finalisation_nag(frictions.make_scope(AGENT, user_id=USER, run_id=RUN)))
    assert line is not None
    assert line.startswith("[SYSTEM: frictions] 2 friction(s) of this run have no disposition")
    assert f"`{rows[0].id}` tool_error on search x2" in line
    assert f"`{rows[1].id}` approval_pause on send" in line
    assert str(rows[2].id) not in line
    assert "dispose_friction" in line


def test_finalisation_nag_bounds_the_names(monkeypatch):
    _with_rows(monkeypatch, [_Row("tool_error", f"t{i}") for i in range(frictions.NAG_MAX_NAMED + 3)])
    line = asyncio.run(frictions.finalisation_nag(frictions.make_scope(AGENT, run_id=RUN)))
    assert line is not None and "and 3 more" in line


def test_finalisation_nag_is_none_without_a_run_frictions_or_a_store(monkeypatch):
    _with_rows(monkeypatch, [])
    assert asyncio.run(frictions.finalisation_nag(frictions.make_scope(AGENT, run_id=RUN))) is None
    assert asyncio.run(frictions.finalisation_nag(frictions.make_scope(AGENT))) is None
    assert asyncio.run(frictions.finalisation_nag(None)) is None

    import app.core.database as database

    def broken():
        raise RuntimeError("no database")

    monkeypatch.setattr(database, "async_session", broken)
    assert asyncio.run(frictions.finalisation_nag(frictions.make_scope(AGENT, run_id=RUN))) is None


class _StatusResult:
    def __init__(self, status: str | None) -> None:
        self._status = status

    def scalar_one_or_none(self) -> str | None:
        return self._status


class _ItemSession:
    """The ledger rows on the frictions read, the item's status on the work item read."""

    def __init__(self, kind: str, status: str | None) -> None:
        self.ledger = _Session([_Row(kind, "tool")])  # type: ignore[no-untyped-call]
        self.status = status

    async def __aenter__(self) -> _ItemSession:
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False

    async def execute(self, stmt: object) -> object:
        if "work_items" in str(stmt):
            return _StatusResult(self.status)
        return await self.ledger.execute(stmt)  # type: ignore[no-untyped-call]


@pytest.mark.parametrize("status", ["done", "cancelled"])
def test_finalisation_nag_says_the_run_s_own_item_is_already_finished(
    monkeypatch: pytest.MonkeyPatch, status: str
) -> None:
    import app.core.database as database

    item = uuid.uuid4()
    monkeypatch.setattr(database, "async_session", lambda: _ItemSession("tool_error", status))
    line = asyncio.run(frictions.finalisation_nag(frictions.make_scope(AGENT, run_id=RUN, work_item_id=item)))
    assert line is not None
    assert f"Your work item `{item}` is already {status}: write nothing more on it" in line
    assert line.index("dispose_friction on each") < line.index("Your work item")


@pytest.mark.parametrize("status", ["in_progress", "claimed", None])
def test_finalisation_nag_says_nothing_of_an_open_or_unread_item(
    monkeypatch: pytest.MonkeyPatch, status: str | None
) -> None:
    import app.core.database as database

    monkeypatch.setattr(database, "async_session", lambda: _ItemSession("tool_error", status))
    line = asyncio.run(frictions.finalisation_nag(frictions.make_scope(AGENT, run_id=RUN, work_item_id=uuid.uuid4())))
    assert line is not None and "Your work item" not in line
    # No item on the scope: the line is the one it always was, and the item is never read.
    monkeypatch.setattr(database, "async_session", lambda: _ItemSession("tool_error", "done"))
    line = asyncio.run(frictions.finalisation_nag(frictions.make_scope(AGENT, run_id=RUN)))
    assert line is not None and line.endswith("then give your final answer.")
