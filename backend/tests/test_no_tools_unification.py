"""No-tools chat unification (CORE-11).

``agent_chat``, ``general_chat`` and ``public_chat`` used to bypass
``run_agent_loop`` for conversations without tools, inlining a duplicated
fork built on the loop's private helpers (``_streaming_chat`` +
``_continue_truncated_response``).
The fork skipped the loop's safeguards (empty-response recovery, forced
final answer, compaction, usage accumulation) and had to be kept in sync
with the loop by hand.

These tests pin the unified contract:

1. All three modules route no-tools chats through ``run_agent_loop`` (AST
   inspection: the private-helper fork is gone, the loop entry point and
   the caller-side ``recover_empty_response`` backstop remain — only real
   code references count, comments and docstrings never trip the check).
2. The loop's no-tools branch still forwards streamed tokens and thinking
   text, continues truncated responses, recovers empty responses, and
   accumulates usage — every behavior the inline fork provided.
3. The no-execution boundary: when a chat has no effective tools, the
   services clear the executor too, so a hallucinated tool call can never
   be executed (the loop's dry-run branch returns synthetic results).
"""

from __future__ import annotations

import ast
import inspect

import pytest

from app.llm.base import BaseLLMProvider, ChatMessage, ChatResponse
from app.tools.agent_loop import run_agent_loop
from app.tools.base import ToolCall


class _NoToolsStubLLM(BaseLLMProvider):
    """Scriptable stub for exercising the loop's no-tools branches.

    ``stream_script`` is a list of per-call scripts; each script is a tuple
    ``(chunks, stop_reason)``.  Calls beyond the script replay the last
    entry.  ``chat()`` mirrors the same scripting for non-streaming tests
    and records the kwargs of every call for assertion.
    """

    provider_name = "stub"
    context_window = 50_000

    def __init__(
        self,
        stream_script: list[tuple[list[str], str]] | None = None,
        chat_script: list[ChatResponse] | None = None,
        usage: dict | None = None,
    ) -> None:
        self.stream_script = stream_script or [(["ok"], "end_turn")]
        self.chat_script = chat_script or []
        self.usage = usage or {}
        self.stream_calls = 0
        self.chat_calls = 0
        self.chat_kwargs: list[dict] = []
        self.last_stop_reason: str | None = None
        self.last_usage: dict = {}

    async def chat(self, messages, **kwargs):
        self.chat_calls += 1
        self.chat_kwargs.append(kwargs)
        if not self.chat_script:
            raise AssertionError(
                "_NoToolsStubLLM.chat() was called but no chat_script was "
                "configured — pass chat_script=[...] when a test exercises "
                "a non-streaming code path"
            )
        idx = min(self.chat_calls - 1, len(self.chat_script) - 1)
        return self.chat_script[idx]

    async def chat_stream(self, messages, **kwargs):
        self.stream_calls += 1
        idx = min(self.stream_calls - 1, len(self.stream_script) - 1)
        chunks, stop_reason = self.stream_script[idx]
        on_thinking = kwargs.get("on_thinking")
        if on_thinking:
            await on_thinking("pondering...")
        for chunk in chunks:
            yield chunk
        self.last_stop_reason = stop_reason
        self.last_usage = dict(self.usage)

    async def embed(self, text, model=None):
        return [0.0]

    async def list_models(self):
        return []

    async def list_embedding_models(self):
        return []

    async def list_image_models(self):
        return []


def _collectors():
    tokens: list[str] = []
    events: list[dict] = []

    async def on_token(t: str) -> None:
        tokens.append(t)

    async def on_event(e: dict) -> None:
        events.append(e)

    return tokens, events, on_token, on_event


# ---------------------------------------------------------------------------
# 1. Source inspection: the inline forks are gone, the loop is the only path.
# ---------------------------------------------------------------------------


_UNIFIED_MODULES = [
    "app.services.agent_chat",
    "app.services.general_chat",
    "app.api.v1.public_chat",
]


def _code_identifiers(tree: ast.AST) -> set[str]:
    """Every identifier referenced by actual *code* in the module.

    Collects bare names, attribute accesses, import targets (including
    aliases) and function / class definitions.  Comments and docstrings
    never contribute, so a comment legitimately *mentioning* a loop-private
    helper (as several ``app.llm`` modules do) can never false-fail the
    fork-free assertions below.
    """
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            names.add(node.id)
        elif isinstance(node, ast.Attribute):
            names.add(node.attr)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                names.add(alias.name.split(".")[-1])
                if alias.asname:
                    names.add(alias.asname)
        elif isinstance(
            node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
        ):
            names.add(node.name)
    return names


@pytest.mark.parametrize("module_name", _UNIFIED_MODULES)
def test_services_route_no_tools_chats_through_run_agent_loop(module_name):
    import importlib

    module = importlib.import_module(module_name)
    identifiers = _code_identifiers(ast.parse(inspect.getsource(module)))

    # The duplicated inline fork (built on the loop's private helpers) must
    # not come back: with it gone, ``run_agent_loop`` is the only execution
    # path for both tool-enabled AND no-tools conversations.  Checked
    # against parsed-AST identifiers (not raw source) so only real code
    # references count — a comment mentioning the helper stays legal.
    assert "_streaming_chat" not in identifiers, (
        f"{module_name} must not call the loop-private _streaming_chat "
        "helper directly — no-tools chats go through run_agent_loop"
    )
    assert "_continue_truncated_response" not in identifiers, (
        f"{module_name} must not call the loop-private "
        "_continue_truncated_response helper directly — the loop owns "
        "truncation rescue for no-tools chats"
    )
    # The unified entry point must remain.
    assert "run_agent_loop" in identifiers
    # The caller-side empty-response backstop (post-loop behavior, not part
    # of the fork) must remain in the two chat services; public_chat uses
    # its own inline fallback text instead.
    if module_name.startswith("app.services."):
        assert "recover_empty_response" in identifiers


def _is_none_constant(node: ast.AST) -> bool:
    return isinstance(node, ast.Constant) and node.value is None


def _or_none_gate_name(node: ast.AST) -> str | None:
    """Return the gating name of a ``<name> or None`` normalization."""
    if (
        isinstance(node, ast.BoolOp)
        and isinstance(node.op, ast.Or)
        and len(node.values) == 2
        and isinstance(node.values[0], ast.Name)
        and _is_none_constant(node.values[1])
    ):
        return node.values[0].id
    return None


@pytest.mark.parametrize("module_name", _UNIFIED_MODULES)
def test_services_clear_executor_when_no_tools(module_name):
    """The no-execution boundary the inline fork guaranteed must survive
    unification: when there are no effective tools, the services must NOT
    hand the tool executor to the loop (a hallucinated tool call would
    otherwise be executable — including deny-listed tools, which are only
    filtered from the *advertised* list, not from the executor).

    Pinned structurally via the AST rather than a loose source regex (an
    unrelated ternary elsewhere in the module must never satisfy this
    test).  EVERY call that passes an ``executor=`` keyword — the
    ``run_agent_loop(...)`` call itself or the ``loop_kwargs`` dict it is
    built from — must uphold the full no-tools contract:

    * ``executor`` is a conditional expression falling back to ``None``;
    * ``tools`` is normalized to ``None`` when empty — either inline
      (``tools=<name> or None``) or through a ``<name> = <expr> or None``
      assignment earlier in the module;
    * the SAME name gates both, so the executor can never stay armed for
      a conversation whose advertised tool list collapsed to ``None``.
    """
    import importlib

    module = importlib.import_module(module_name)
    tree = ast.parse(inspect.getsource(module))

    # Names assigned via ``<name> = <expr> or None`` (general_chat binds
    # ``ga_tools = composite.all_tools or None`` before the loop call).
    normalized_names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and (
            isinstance(node.value, ast.BoolOp)
            and isinstance(node.value.op, ast.Or)
            and len(node.value.values) == 2
            and _is_none_constant(node.value.values[1])
        ):
            normalized_names.update(
                target.id for target in node.targets if isinstance(target, ast.Name)
            )

    all_executor_calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and any(kw.arg == "executor" for kw in node.keywords)
    ]

    # The contract below is loop-shaped: it demands a ``tools=`` keyword gated
    # by the same name. Only the loop call (and the kwargs dict built for it)
    # has one, so the scan is narrowed to those — a non-loop consumer of an
    # executor cannot satisfy a rule about a parameter it does not take. The
    # boundary itself is still pinned for *every* consumer, immediately below.
    executor_calls = [
        node
        for node in all_executor_calls
        if (isinstance(node.func, ast.Name) and node.func.id in {"run_agent_loop", "dict"})
        or (isinstance(node.func, ast.Attribute) and node.func.attr == "run_agent_loop")
    ]
    assert executor_calls, (
        f"{module_name} must build a run_agent_loop call (or its kwargs "
        "dict) with an explicit executor= keyword"
    )

    # Every other consumer of an executor in these modules — the approval
    # resume applies one to execute a call a human just approved — must
    # fall back to ``None`` just the same. An approval is not a licence to run a
    # tool the conversation no longer offers, and this is the one property that
    # keeps the no-execution boundary true for callers that are not the loop.
    for call in all_executor_calls:
        if call in executor_calls:
            continue
        value = {kw.arg: kw.value for kw in call.keywords}["executor"]
        assert isinstance(value, ast.IfExp) and _is_none_constant(value.orelse), (
            f"{module_name}: a non-loop executor= consumer must still fall back "
            "to None when the conversation has no effective tools, or an "
            "approved call could execute a tool that is no longer offered"
        )

    for call in executor_calls:
        kwargs = {kw.arg: kw.value for kw in call.keywords}

        executor = kwargs["executor"]
        assert isinstance(executor, ast.IfExp) and _is_none_constant(
            executor.orelse
        ), (
            f"{module_name} must pass executor=<executor> if <tools> else "
            "None to run_agent_loop, so hallucinated tool calls can never "
            "be executed when the chat has no effective tools"
        )
        assert isinstance(executor.test, ast.Name), (
            f"{module_name}: the executor conditional must be gated on the "
            "same name that carries the normalized tool list"
        )

        tools = kwargs.get("tools")
        assert tools is not None, (
            f"{module_name}: the call passing executor= must also pass the "
            "tools= keyword"
        )
        tools_gate = _or_none_gate_name(tools) or (
            tools.id
            if isinstance(tools, ast.Name) and tools.id in normalized_names
            else None
        )
        assert tools_gate is not None, (
            f"{module_name} must normalize the advertised tool list to None "
            "when empty (tools=<name> or None, inline or via a prior "
            "assignment) so the loop's no-tools branch engages"
        )
        assert executor.test.id == tools_gate, (
            f"{module_name}: tools and executor must be gated on the SAME "
            f"name ({tools_gate!r} vs {executor.test.id!r}) — otherwise the "
            "executor could stay armed for a no-tools conversation"
        )


# ---------------------------------------------------------------------------
# 2. Loop parity: streaming tokens + thinking text forwarded with no tools.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_tools_loop_streams_tokens_and_accumulates_usage():
    llm = _NoToolsStubLLM(
        stream_script=[(["Hello ", "world"], "end_turn")],
        usage={"input_tokens": 3, "output_tokens": 5},
    )
    tokens, events, on_token, on_event = _collectors()

    response = await run_agent_loop(
        llm,
        [ChatMessage(role="user", content="hi")],
        tools=None,
        executor=None,
        max_iterations=5,
        model="stub",
        on_token=on_token,
        on_event=on_event,
    )

    assert response.content == "Hello world"
    assert "".join(tokens) == "Hello world"
    assert llm.stream_calls == 1
    # The fork used to discard usage; the loop accumulates it.
    assert response.usage == {"input_tokens": 3, "output_tokens": 5}
    statuses = [e.get("status") for e in events]
    assert "streaming" in statuses
    # No spurious wind-down / tool-strip stages with no tools advertised.
    assert "wind_down" not in statuses
    assert "composing" not in statuses


@pytest.mark.asyncio
async def test_no_tools_loop_with_empty_tool_list_behaves_like_none():
    """Callers pass ``tools=[]`` when an agent has no tools — the loop must
    collapse it to the no-tools branch (single streamed call, no executor
    interaction, no dry-run early return)."""
    llm = _NoToolsStubLLM(stream_script=[(["answer"], "end_turn")])
    tokens, _events, on_token, on_event = _collectors()

    response = await run_agent_loop(
        llm,
        [ChatMessage(role="user", content="hi")],
        tools=[],
        executor=None,
        max_iterations=5,
        model="stub",
        on_token=on_token,
        on_event=on_event,
    )

    assert response.content == "answer"
    assert "".join(tokens) == "answer"
    assert llm.stream_calls == 1
    assert llm.chat_calls == 0


@pytest.mark.asyncio
async def test_no_tools_loop_forwards_thinking_text():
    llm = _NoToolsStubLLM(stream_script=[(["done"], "end_turn")])
    _tokens, events, on_token, on_event = _collectors()

    await run_agent_loop(
        llm,
        [ChatMessage(role="user", content="hi")],
        tools=None,
        executor=None,
        max_iterations=5,
        model="stub",
        on_token=on_token,
        on_event=on_event,
    )

    thinking = [e for e in events if e.get("status") == "thinking_text"]
    assert thinking, "thinking text must be forwarded as thinking_text events"
    # The first chunk of a generation opens a new paragraph.
    assert thinking[0]["content"] == "\n\npondering..."


# ---------------------------------------------------------------------------
# 3. Loop parity: truncated-response continuation still works without tools.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_tools_streaming_truncation_is_continued():
    llm = _NoToolsStubLLM(
        stream_script=[
            (["part1"], "max_tokens"),
            (["part2"], "end_turn"),
        ]
    )
    tokens, _events, on_token, on_event = _collectors()

    response = await run_agent_loop(
        llm,
        [ChatMessage(role="user", content="long answer please")],
        tools=None,
        executor=None,
        max_iterations=5,
        model="stub",
        on_token=on_token,
        on_event=on_event,
    )

    assert response.content == "part1part2"
    assert "".join(tokens) == "part1part2"
    assert llm.stream_calls == 2, "truncation must trigger a continuation call"


@pytest.mark.asyncio
async def test_no_tools_nonstream_truncation_continuation_strips_structured_output():
    """Non-streaming no-tools chats (e.g. assignments, mandatory-transfer
    setups) must continue truncated answers.  The primary call carries the
    structured-output config; the continuation call must NOT
    (a grammar-constrained continuation emits a fresh, complete JSON
    document that cannot be stitched onto the truncated text, so
    ``_continue_truncated_response`` strips the kwarg by design)."""
    structured = {"response_format": {"type": "json_object"}}
    llm = _NoToolsStubLLM(
        chat_script=[
            ChatResponse(content="head", model="stub", stop_reason="max_tokens"),
            ChatResponse(content="tail", model="stub", stop_reason="end_turn"),
        ]
    )

    response = await run_agent_loop(
        llm,
        [ChatMessage(role="user", content="json please")],
        tools=None,
        executor=None,
        max_iterations=5,
        model="stub",
        on_token=None,
        structured_output=structured,
    )

    assert response.content == "headtail"
    assert llm.chat_calls == 2
    # The primary call carries the structured-output config...
    assert llm.chat_kwargs[0].get("structured_output") == structured
    # ...the truncation continuation deliberately does not.
    assert "structured_output" not in llm.chat_kwargs[1]


# ---------------------------------------------------------------------------
# 4. No-execution boundary: hallucinated tool calls without an executor are
#    answered with synthetic dry-run results, never executed.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_tools_hallucinated_tool_call_is_not_executed():
    """With ``tools=None`` and ``executor=None`` (the contract both chat
    services now uphold for no-tools conversations), a hallucinated tool
    call must flow into the loop's dry-run branch: a synthetic result is
    appended and the loop continues to a clean final answer — nothing is
    ever executed."""
    llm = _NoToolsStubLLM(
        chat_script=[
            ChatResponse(
                content="",
                model="stub",
                stop_reason="tool_use",
                tool_calls=[ToolCall(id="t1", name="phantom_tool", arguments={})],
            ),
            ChatResponse(content="clean answer", model="stub", stop_reason="end_turn"),
        ]
    )
    messages = [ChatMessage(role="user", content="hi")]

    response = await run_agent_loop(
        llm,
        messages,
        tools=None,
        executor=None,
        max_iterations=5,
        model="stub",
        on_token=None,
    )

    assert response.content == "clean answer"
    assert llm.chat_calls == 2
    dry_run_results = [
        m for m in messages if m.role == "tool" and "[dry-run]" in (m.content or "")
    ]
    assert dry_run_results, (
        "a hallucinated tool call without an executor must produce a "
        "synthetic [dry-run] tool result, not an execution"
    )


# ---------------------------------------------------------------------------
# 5. Loop parity: empty-response recovery still applies without tools.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_tools_empty_stream_recovers_via_streaming_retry():
    """A single empty no-tools stream is recovered by the one-shot
    streaming retry — the forced-final-answer backstop (and its
    ``composing`` event) must never run for a transient blip."""
    recovered = (
        "Here is the full answer you asked for, synthesised on the recovery "
        "attempt after the first response came back completely empty."
    )
    assert len(recovered) >= 100  # avoid the short-forced-answer retry
    llm = _NoToolsStubLLM(
        stream_script=[
            ([], "end_turn"),  # first call streams nothing
            ([recovered], "end_turn"),  # one-shot streaming retry
        ]
    )
    tokens, events, on_token, on_event = _collectors()

    response = await run_agent_loop(
        llm,
        [ChatMessage(role="user", content="hi")],
        tools=None,
        executor=None,
        max_iterations=5,
        model="stub",
        on_token=on_token,
        on_event=on_event,
    )

    assert response.content == recovered
    assert "".join(tokens) == recovered
    assert llm.stream_calls == 2, (
        "an empty no-tools stream must be retried once before any backstop"
    )
    # The retry recovered, so the backstop (and its composing event) never ran.
    assert "composing" not in [e.get("status") for e in events]


@pytest.mark.asyncio
async def test_no_tools_double_empty_stream_recovers_via_forced_final_answer():
    """When the primary stream AND the one-shot retry both come back empty,
    the loop must still fall through to the forced-final-answer backstop."""
    recovered = (
        "Here is the full answer you asked for, synthesised on the recovery "
        "attempt after both responses came back completely empty."
    )
    assert len(recovered) >= 100  # avoid the short-forced-answer retry
    llm = _NoToolsStubLLM(
        stream_script=[
            ([], "end_turn"),  # primary call streams nothing
            ([], "end_turn"),  # streaming retry also empty
            ([recovered], "end_turn"),  # forced-final-answer backstop
        ]
    )
    tokens, events, on_token, on_event = _collectors()

    response = await run_agent_loop(
        llm,
        [ChatMessage(role="user", content="hi")],
        tools=None,
        executor=None,
        max_iterations=5,
        model="stub",
        on_token=on_token,
        on_event=on_event,
    )

    assert response.content == recovered
    assert "".join(tokens) == recovered
    assert llm.stream_calls == 3, (
        "a double-empty no-tools stream must fall through to the forced-"
        "final-answer backstop instead of returning the empty response"
    )
    # The backstop announces itself via the composing event.
    assert "composing" in [e.get("status") for e in events]
