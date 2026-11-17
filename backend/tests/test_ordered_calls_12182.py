"""A round's calls of a tool declaring ``ordered_calls`` run in the order the model sent them (#12182).

The agent loop runs a round's calls together. The built-in browser keeps one session whose page each call changes:
a navigate and the evaluate sent with it raced, and the evaluate read the page the navigate was leaving (retest
82e944e6, UX run 295e57da: the first evaluate after a navigate returned the previous ``performance.timeOrigin``).
These tests drive the real loop with a stateful stand-in of the browser whose navigate yields before it commits,
as a page load does, and check the calls of the ordered tool never overlap while other tools stay parallel.
"""

from __future__ import annotations

import asyncio
import itertools

import pytest

from app.llm.base import BaseLLMProvider, ChatMessage, ChatResponse
from app.tools import agent_loop as al
from app.tools.base import CallableToolExecutor, CompositeToolExecutor, ToolCall, ToolDefinition


class _ScriptedLLM(BaseLLMProvider):
    provider_name = "stub"
    context_window = 100_000

    def __init__(self, script: list[ChatResponse]) -> None:
        self._script = script
        self._idx = 0

    async def chat(self, messages, model=None, tools=None, **kwargs):
        response = self._script[self._idx]
        self._idx += 1
        return response

    async def chat_stream(self, messages, **kwargs):
        yield ""

    async def embed(self, text, model=None):
        return [0.0]

    async def list_models(self):
        return []


@pytest.fixture
def _quiet_langfuse(monkeypatch):
    monkeypatch.setattr(al, "_lf_start_llm_gen", lambda *a, **kw: None)
    monkeypatch.setattr(al, "_lf_end_generation", lambda *a, **kw: None)
    monkeypatch.setattr(al, "_lf_end_span", lambda *a, **kw: None)
    monkeypatch.setattr(al, "_lf_end_tool_span", lambda *a, **kw: None)
    monkeypatch.setattr(al, "_lf_start_iter_span", lambda **kw: None)
    monkeypatch.setattr(al, "_lf_start_tool_span", lambda **kw: None)


def _round(*calls: ToolCall) -> ChatResponse:
    return ChatResponse(content="", model="stub", stop_reason="tool_calls", tool_calls=list(calls))


def _reply(messages: list[ChatMessage], call_id: str) -> str:
    return next(m.content for m in messages if m.role == "tool" and m.tool_call_id == call_id)


class _Page:
    """A stand-in of the browser session: navigate commits a new document after yielding, as a load does."""

    def __init__(self) -> None:
        self.origin = 0
        self.active = 0
        self.overlaps = 0
        self.clock = itertools.count(1)

    async def act(self, action: str, url: str | None = None, **_kwargs) -> str:
        self.active += 1
        self.overlaps += self.active > 1
        try:
            if action == "navigate":
                await asyncio.sleep(0.002)  # the old document answers until the new one commits
                self.origin = next(self.clock)
                return f"Navigated to {url}"
            await asyncio.sleep(0)
            return f"origin={self.origin}"
        finally:
            self.active -= 1


def _browser(page: _Page, *, ordered: bool = True, extra: dict | None = None) -> CompositeToolExecutor:
    callable_exec = CallableToolExecutor()
    callable_exec.register("browser", page.act)
    definitions = [ToolDefinition(name="browser", description="Browse.", source="builtin:web", ordered_calls=ordered)]
    for name, handler in (extra or {}).items():
        callable_exec.register(name, handler)
        definitions.append(ToolDefinition(name=name, description=name, source="custom:test"))
    composite = CompositeToolExecutor()
    composite.add_tools(definitions, callable_exec)
    return composite


def test_the_browser_tool_declares_ordered_calls():
    from app.services.builtin_tools.web import build_browser_tool

    definitions, _executor = build_browser_tool("agent:conversation")
    assert definitions[0].name == "browser" and definitions[0].ordered_calls


def test_the_chain_links_each_call_of_an_ordered_tool_to_the_one_before_it():
    chain = al._call_order_chain(
        [(0, "browser"), (1, "search"), (2, "browser"), (3, "browser")], frozenset({"browser"})
    )
    assert set(chain) == {0, 2, 3}
    assert chain[0][0] is None
    assert chain[2][0] is chain[0][1] and chain[3][0] is chain[2][1]


@pytest.mark.asyncio
async def test_fifty_navigate_and_evaluate_pairs_never_read_the_previous_document(_quiet_langfuse):
    """The acceptance loop of #12182: 50 navigate + evaluate pairs sent in one round each, across two pages."""
    page = _Page()
    rounds = []
    for i in range(50):
        url = "https://a.example/" if i % 2 == 0 else "https://b.example/"
        rounds.append(
            _round(
                ToolCall(id=f"n{i}", name="browser", arguments={"action": "navigate", "url": url}),
                ToolCall(id=f"e{i}", name="browser", arguments={"action": "evaluate", "expression": f"t{i}"}),
            )
        )
    llm = _ScriptedLLM([*rounds, ChatResponse(content="Done.", model="stub")])
    messages: list[ChatMessage] = [ChatMessage(role="user", content="go")]
    composite = _browser(page)

    await al.run_agent_loop(
        llm=llm, messages=messages, tools=composite.all_tools, executor=composite, max_iterations=60
    )

    origins = [_reply(messages, f"e{i}") for i in range(50)]
    assert all(f"origin={i + 1}" in reply for i, reply in enumerate(origins)), origins
    assert page.overlaps == 0


@pytest.mark.asyncio
async def test_without_the_declaration_the_evaluate_reads_the_document_left(_quiet_langfuse):
    """The premise: the stand-in reproduces the race the loop used to run (one round, no order)."""
    page = _Page()
    llm = _ScriptedLLM(
        [
            _round(
                ToolCall(id="n", name="browser", arguments={"action": "navigate", "url": "https://a.example/"}),
                ToolCall(id="e", name="browser", arguments={"action": "evaluate", "expression": "t"}),
            ),
            ChatResponse(content="Done.", model="stub"),
        ]
    )
    messages: list[ChatMessage] = [ChatMessage(role="user", content="go")]
    composite = _browser(page, ordered=False)

    await al.run_agent_loop(llm=llm, messages=messages, tools=composite.all_tools, executor=composite, max_iterations=5)

    assert "origin=0" in _reply(messages, "e")


@pytest.mark.asyncio
async def test_a_failed_call_releases_the_next_one_and_other_tools_stay_parallel(_quiet_langfuse):
    page = _Page()
    started: list[str] = []
    release = asyncio.Event()

    async def slow_search(**_kwargs) -> str:
        started.append("search")
        await release.wait()
        return "found"

    async def act(action: str, url: str | None = None, **kwargs) -> str:
        started.append(action)
        if action == "click":
            raise RuntimeError("no element matches")
        result = await page.act(action, url, **kwargs)
        release.set()  # the browser ran while the search was still waiting: they run side by side
        return result

    callable_exec = CallableToolExecutor()
    callable_exec.register("browser", act)
    callable_exec.register("slow_search", slow_search)
    composite = CompositeToolExecutor()
    composite.add_tools(
        [
            ToolDefinition(name="browser", description="Browse.", source="builtin:web", ordered_calls=True),
            ToolDefinition(name="slow_search", description="Search.", source="custom:test"),
        ],
        callable_exec,
    )
    llm = _ScriptedLLM(
        [
            _round(
                ToolCall(id="s", name="slow_search", arguments={"q": "x"}),
                ToolCall(id="c", name="browser", arguments={"action": "click", "selector": "#missing"}),
                ToolCall(id="e", name="browser", arguments={"action": "evaluate", "expression": "t"}),
            ),
            ChatResponse(content="Done.", model="stub"),
        ]
    )
    messages: list[ChatMessage] = [ChatMessage(role="user", content="go")]

    await asyncio.wait_for(
        al.run_agent_loop(llm=llm, messages=messages, tools=composite.all_tools, executor=composite, max_iterations=5),
        10,
    )

    assert started.index("click") < started.index("evaluate")
    assert "found" in _reply(messages, "s")
    assert "origin=0" in _reply(messages, "e")
