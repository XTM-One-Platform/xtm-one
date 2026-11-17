"""Slow-turn watchdog logging.

Long-conversation chat turns can legitimately run for tens of minutes
(huge context -> multi-minute LLM calls + tool rounds), but all of the
loop's per-iteration progress logging is INFO-level.  Production log
captures (WARNING+) therefore showed NOTHING for the whole turn and it
looked like a silent death.

These tests pin the watchdog contract:

1. Once a run exceeds ``_LOOP_PROGRESS_WARN_AFTER`` seconds, the loop
   emits a WARNING progress heartbeat at the next iteration boundary.
2. Fast runs (below the threshold) emit no watchdog WARNING at all —
   healthy turns must not add log noise.
3. The chat services' completion line closes a heartbeat series at WARNING
   and is INFO when the loop emitted none: a turn that crossed the
   threshold between two iteration boundaries completed normally and has no
   series to close.
"""

from __future__ import annotations

import ast
import inspect
import logging
import textwrap

import pytest

import app.tools.agent_loop as agent_loop_mod
from app.llm.base import BaseLLMProvider, ChatMessage
from app.services import agent_chat, general_chat


class _StubLLM(BaseLLMProvider):
    """Minimal streaming stub for the no-tools loop branch."""

    provider_name = "stub"
    context_window = 50_000

    def __init__(self) -> None:
        self.last_stop_reason: str | None = None
        self.last_usage: dict = {}

    async def chat(self, messages, **kwargs):  # pragma: no cover - unused
        raise AssertionError("chat() should not be called in these tests")

    async def chat_stream(self, messages, **kwargs):
        yield "ok"
        self.last_stop_reason = "end_turn"
        self.last_usage = {}

    async def embed(self, text, model=None):  # pragma: no cover - unused
        return [0.0]

    async def list_models(self):  # pragma: no cover - unused
        return []

    async def list_embedding_models(self):  # pragma: no cover - unused
        return []

    async def list_image_models(self):  # pragma: no cover - unused
        return []


async def _noop_token(_: str) -> None:
    return None


@pytest.mark.asyncio
async def test_slow_turn_emits_warning_heartbeat(monkeypatch, caplog):
    """A run past the slow-turn threshold logs a WARNING progress heartbeat."""
    # Threshold 0 -> the very first iteration boundary is already "slow".
    monkeypatch.setattr(agent_loop_mod, "_LOOP_PROGRESS_WARN_AFTER", 0.0)
    monkeypatch.setattr(agent_loop_mod, "_LOOP_PROGRESS_WARN_EVERY", 0.0)

    with caplog.at_level(logging.WARNING, logger=agent_loop_mod.__name__):
        response = await agent_loop_mod.run_agent_loop(
            llm=_StubLLM(),
            messages=[ChatMessage(role="user", content="hello")],
            tools=None,
            executor=None,
            max_iterations=3,
            model="stub-model",
            on_token=_noop_token,
        )

    assert response.content == "ok"
    heartbeats = [
        r for r in caplog.records if "Agent loop still running" in r.getMessage()
    ]
    assert heartbeats, "expected a slow-turn WARNING heartbeat"
    msg = heartbeats[0].getMessage()
    assert "iteration 1/3" in msg
    assert "elapsed" in msg


@pytest.mark.asyncio
async def test_fast_turn_emits_no_watchdog_warning(caplog):
    """Healthy fast turns stay silent at WARNING level (no log noise)."""
    with caplog.at_level(logging.WARNING, logger=agent_loop_mod.__name__):
        response = await agent_loop_mod.run_agent_loop(
            llm=_StubLLM(),
            messages=[ChatMessage(role="user", content="hello")],
            tools=None,
            executor=None,
            max_iterations=3,
            model="stub-model",
            on_token=_noop_token,
        )

    assert response.content == "ok"
    assert not [
        r for r in caplog.records if "Agent loop still running" in r.getMessage()
    ]


async def _run_loop() -> None:
    await agent_loop_mod.run_agent_loop(
        llm=_StubLLM(),
        messages=[ChatMessage(role="user", content="hello")],
        tools=None,
        executor=None,
        max_iterations=3,
        model="stub-model",
        on_token=_noop_token,
    )


@pytest.mark.asyncio
async def test_a_watched_turn_records_every_heartbeat_the_loop_emits(monkeypatch, caplog):
    monkeypatch.setattr(agent_loop_mod, "_LOOP_PROGRESS_WARN_AFTER", 0.0)
    monkeypatch.setattr(agent_loop_mod, "_LOOP_PROGRESS_WARN_EVERY", 0.0)

    with (
        caplog.at_level(logging.WARNING, logger=agent_loop_mod.__name__),
        agent_loop_mod.watch_slow_turn() as heartbeats,
    ):
        await _run_loop()

    emitted = [r for r in caplog.records if "Agent loop still running" in r.getMessage()]
    assert emitted
    assert len(heartbeats) == len(emitted)
    assert agent_loop_mod._slow_turn_heartbeats.get() is None, "the record ends with the block"


@pytest.mark.asyncio
async def test_a_fast_watched_turn_records_no_heartbeat():
    with agent_loop_mod.watch_slow_turn() as heartbeats:
        await _run_loop()
    assert heartbeats == []


@pytest.mark.asyncio
async def test_an_unwatched_loop_records_nothing(monkeypatch):
    monkeypatch.setattr(agent_loop_mod, "_LOOP_PROGRESS_WARN_AFTER", 0.0)
    await _run_loop()
    assert agent_loop_mod._slow_turn_heartbeats.get() is None


def _completion_call(module, opening: str) -> ast.Call:
    tree = ast.parse(textwrap.dedent(inspect.getsource(module)))
    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and any(isinstance(arg, ast.Constant) and str(arg.value).startswith(opening) for arg in node.args)
    ]
    assert len(calls) == 1, f"one completion line opening with {opening!r}"
    return calls[0]


def _level_name(node: ast.AST) -> str:
    assert isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == "logging"
    return node.attr


@pytest.mark.parametrize(
    ("module", "opening"),
    [(agent_chat, "Agent chat turn completed after"), (general_chat, "General chat turn finished after")],
    ids=["agent_chat", "general_chat"],
)
def test_the_completion_line_is_a_warning_only_when_it_closes_a_heartbeat_series(module, opening):
    call = _completion_call(module, opening)
    assert isinstance(call.func, ast.Attribute) and call.func.attr == "log"
    level = call.args[0]
    assert isinstance(level, ast.IfExp)
    assert isinstance(level.test, ast.Name) and level.test.id == "_slow_turn_heartbeats"
    assert (_level_name(level.body), _level_name(level.orelse)) == ("WARNING", "INFO")

    tree = ast.parse(textwrap.dedent(inspect.getsource(module)))
    watched = [
        item
        for node in ast.walk(tree)
        if isinstance(node, (ast.With, ast.AsyncWith))
        for item in node.items
        if isinstance(item.context_expr, ast.Call)
        and getattr(item.context_expr.func, "id", None) == "watch_slow_turn"
    ]
    assert len(watched) == 1
    assert isinstance(watched[0].optional_vars, ast.Name)
    assert watched[0].optional_vars.id == "_slow_turn_heartbeats"
