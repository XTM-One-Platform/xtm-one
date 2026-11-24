"""A tool that changes an integration reloads the turn's tools at the end of its step (#1874)."""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

import pytest

from app.llm.base import BaseLLMProvider, ChatMessage, ChatResponse
from app.services.tool_surface_refresh import IntegrationSurfaceRefresher
from app.tools import agent_loop as al
from app.tools.base import (
    CallableToolExecutor,
    CompositeToolExecutor,
    ScopedToolExecutor,
    ToolCall,
    ToolDefinition,
    ToolExecutor,
)

_SCHEMA = {"type": "object", "properties": {}}


class _RecordingLLM(BaseLLMProvider):
    provider_name = "stub"
    context_window = 100_000

    def __init__(self, script: list[ChatResponse]) -> None:
        self._script = script
        self.offered: list[list[str]] = []
        self.seen: list[list[ChatMessage]] = []

    async def chat(
        self,
        messages: list[ChatMessage],
        model: str | None = None,
        temperature: float = 0.7,
        max_tokens: int = 4096,
        tools: list[ToolDefinition] | None = None,
        **kwargs: Any,
    ) -> ChatResponse:
        self.offered.append([t.name for t in tools or []])
        self.seen.append(list(messages))
        return self._script[len(self.offered) - 1]

    async def chat_stream(
        self,
        messages: list[ChatMessage],
        model: str | None = None,
        temperature: float = 0.7,
        max_tokens: int = 4096,
        tools: list[ToolDefinition] | None = None,
        **kwargs: Any,
    ) -> AsyncIterator[str]:
        yield ""

    async def embed(self, text: str, model: str | None = None) -> list[float]:
        return [0.0]

    async def list_models(self) -> list[str]:
        return []


@pytest.fixture
def _quiet_langfuse(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(al, "_lf_start_llm_gen", lambda *a, **kw: None)
    monkeypatch.setattr(al, "_lf_end_generation", lambda *a, **kw: None)
    monkeypatch.setattr(al, "_lf_end_span", lambda *a, **kw: None)
    monkeypatch.setattr(al, "_lf_end_tool_span", lambda *a, **kw: None)
    monkeypatch.setattr(al, "_lf_start_iter_span", lambda **kw: None)
    monkeypatch.setattr(al, "_lf_start_tool_span", lambda **kw: None)


def _call(name: str, call_id: str) -> ChatResponse:
    return ChatResponse(
        content="",
        model="stub",
        stop_reason="tool_calls",
        tool_calls=[ToolCall(id=call_id, name=name, arguments={})],
    )


def _answer() -> ChatResponse:
    return ChatResponse(content="done", model="stub", stop_reason="end_turn")


def _builder(
    *, changes_surface: bool, outcome: str = "Integration updated."
) -> tuple[CompositeToolExecutor, list[str]]:
    calls: list[str] = []
    builtin = CallableToolExecutor()

    async def _update_integration() -> str:
        calls.append("update_integration")
        return outcome

    builtin.register("update_integration", _update_integration)
    composite = CompositeToolExecutor()
    composite.add_tools(
        [
            ToolDefinition(
                name="update_integration",
                description="Update an integration",
                input_schema=_SCHEMA,
                source="builtin:platform",
                changes_tool_surface=changes_surface,
            ),
        ],
        builtin,
    )
    composite.add_tools(
        [
            ToolDefinition(
                name="list_dags",
                description="List the DAGs",
                input_schema=_SCHEMA,
                source="integration:airflow",
            ),
        ],
        _integration_executor(calls, ["list_dags"]),
    )
    return composite, calls


def _integration_executor(calls: list[str], names: list[str]) -> ToolExecutor:
    executor = CallableToolExecutor()
    for name in names:

        async def _run(_name: str = name) -> str:
            calls.append(_name)
            return f"{_name} ok"

        executor.register(name, _run)
    return executor


def _new_build(calls: list[str]) -> Callable[[], Awaitable[tuple[list[ToolDefinition], ToolExecutor]]]:
    async def _rebuild() -> tuple[list[ToolDefinition], ToolExecutor]:
        definitions = [
            ToolDefinition(name=name, description=name, input_schema=_SCHEMA, source="integration:airflow")
            for name in ("list_dags", "trigger_dag")
        ]
        return definitions, _integration_executor(calls, ["list_dags", "trigger_dag"])

    return _rebuild


async def _run(
    composite: CompositeToolExecutor, llm: _RecordingLLM, calls: list[str], *, refresher: bool = True
) -> Any:
    scoped = ScopedToolExecutor(composite, composite.all_tools)
    return await al.run_agent_loop(
        llm=llm,
        messages=[ChatMessage(role="user", content="add trigger_dag and run it")],
        tools=composite.all_tools,
        executor=scoped,
        max_iterations=6,
        progressive_tools=False,
        tool_surface_refresher=(
            IntegrationSurfaceRefresher(composite, scoped, _new_build(calls), lambda tools: tools)
            if refresher
            else None
        ),
    )


@pytest.mark.asyncio
async def test_a_new_operation_is_offered_and_callable_from_the_next_step(_quiet_langfuse: None) -> None:
    composite, calls = _builder(changes_surface=True)
    llm = _RecordingLLM([_call("update_integration", "c1"), _call("trigger_dag", "c2"), _answer()])

    response = await _run(composite, llm, calls)

    assert response.content == "done"
    assert "trigger_dag" not in llm.offered[0]
    assert "trigger_dag" in llm.offered[1]
    assert calls == ["update_integration", "trigger_dag"]
    notes = [m.content for m in llm.seen[1] if m.role == "system" and "were reloaded" in (m.content or "")]
    assert len(notes) == 1
    assert "added trigger_dag" in notes[0]


@pytest.mark.asyncio
async def test_a_tool_that_does_not_declare_the_change_leaves_the_surface(_quiet_langfuse: None) -> None:
    composite, calls = _builder(changes_surface=False)
    llm = _RecordingLLM([_call("update_integration", "c1"), _answer()])

    await _run(composite, llm, calls)

    assert llm.offered[0] == llm.offered[1]
    assert "trigger_dag" not in llm.offered[1]


@pytest.mark.asyncio
async def test_no_refresher_keeps_the_surface(_quiet_langfuse: None) -> None:
    composite, calls = _builder(changes_surface=True)
    llm = _RecordingLLM([_call("update_integration", "c1"), _answer()])

    await _run(composite, llm, calls, refresher=False)

    assert "trigger_dag" not in llm.offered[1]


@pytest.mark.asyncio
async def test_a_failed_refresh_keeps_the_surface_and_says_so(_quiet_langfuse: None) -> None:
    composite, _calls = _builder(changes_surface=True)
    llm = _RecordingLLM([_call("update_integration", "c1"), _answer()])
    scoped = ScopedToolExecutor(composite, composite.all_tools)

    async def _broken() -> tuple[list[ToolDefinition], ToolExecutor]:
        raise RuntimeError("database unavailable")

    await al.run_agent_loop(
        llm=llm,
        messages=[ChatMessage(role="user", content="update it")],
        tools=composite.all_tools,
        executor=scoped,
        max_iterations=4,
        progressive_tools=False,
        tool_surface_refresher=IntegrationSurfaceRefresher(composite, scoped, _broken, lambda tools: tools),
    )

    assert llm.offered[0] == llm.offered[1]
    assert any(m.role == "system" and "could not be reloaded" in (m.content or "") for m in llm.seen[1])


def test_the_note_names_the_change_and_caps_the_list() -> None:
    names = [f"tool_{i:02d}" for i in range(25)]
    note = al._surface_refresh_note(names, ["old_tool"], gated=True)
    assert "added tool_00" in note
    assert "and 5 more" in note
    assert "removed old_tool" in note
    assert "load_tools" in note
    assert "load_tools" not in al._surface_refresh_note([], ["old_tool"], gated=True)


def test_update_integration_declares_the_change() -> None:
    import uuid
    from types import SimpleNamespace

    from app.services.builtin_tools.agent_builder import build_agent_builder_tool

    definitions, _executor = build_agent_builder_tool(SimpleNamespace(), str(uuid.uuid4()))  # type: ignore[arg-type]
    by_name = {t.name: t for t in definitions}
    assert by_name["update_integration"].changes_tool_surface is True
    assert by_name["delete_integration"].changes_tool_surface is False
