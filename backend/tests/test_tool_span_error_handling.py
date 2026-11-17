"""Regression tests for the tool-execution Langfuse spans.

Pin the round-6 fix that addressed the two final Copilot threads:

* **Exception-object preservation** — every error-close site (``TimeoutError``,
  ``CancelledError``, generic ``Exception``) now passes the exception
  instance to ``_lf_end_tool_span(error=...)`` rather than ``str(exc)``.
  Without it, ``str(CancelledError())`` is empty (Langfuse renders a
  blank status message), and the exception type is lost for any
  exception with no message — silently corrupting trace fidelity.

* **Explicit ``asyncio.CancelledError`` handler** — both the single-tool
  and parallel-tool paths now close their tool span on cancellation
  before re-raising. Previously the single-tool path caught only
  ``Exception`` (so ``CancelledError`` bypassed it and left the tool
  span open) and the parallel path caught ``BaseException`` (CodeQL
  ``py/catch-base-exception`` violation that the round-5 pattern
  already resolved elsewhere in the file).

* **CodeQL compliance** — the parallel path no longer catches
  ``BaseException``. ``SystemExit`` / ``KeyboardInterrupt`` are
  handled at the outer ``asyncio.gather`` site (the existing
  ``isinstance(result, (KeyboardInterrupt, SystemExit)): raise``
  check). The single-tool path matches the same convention.

* **Single-tool path contextvar reset is DRY** — moved into a
  ``finally`` block so a future regression that adds a new ``except``
  branch and forgets to reset the contextvar cannot silently leak
  the tool's parent context into the next tool call.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import MagicMock

import pytest

from app.llm.base import (
    BaseLLMProvider,
    ChatMessage,
    ChatResponse,
)
from app.tools import agent_loop as al
from app.tools.base import (
    CallableToolExecutor,
    CompositeToolExecutor,
    ToolCall,
    ToolDefinition,
    ToolResult,
)

# ---------------------------------------------------------------------------
# Raising executor — bypasses ``CallableToolExecutor`` which would
# otherwise catch ``Exception`` at line 606 of ``app/tools/base.py`` and
# wrap it into a ``ToolResult(is_error=True)``, so generic exceptions
# would never reach the ``except Exception`` branch in ``_exec_with_span``.
# These tests need the exception to bubble up to the agent-loop's
# tool-span handler so we can verify it's passed to ``_lf_end_tool_span``
# as the exception object (not ``str(exc)``).
# ---------------------------------------------------------------------------


class _RaisingExecutor:
    """Test-only executor that raises a configured exception on every
    invocation, bypassing ``CallableToolExecutor``'s built-in
    ``except Exception`` wrap. Used to drive ``run_agent_loop``'s
    tool-span error handlers directly."""

    def __init__(self, exc: BaseException) -> None:
        self._exc = exc

    async def execute_tool(self, call: ToolCall) -> ToolResult:
        raise self._exc


class _PerNameRaisingExecutor:
    """Variant for the parallel-tool path — raises a different
    exception per tool name so we can exercise mixed success / failure
    inside ``asyncio.gather``."""

    def __init__(self, exc_per_name: dict[str, BaseException | None]) -> None:
        self._exc_per_name = exc_per_name

    async def execute_tool(self, call: ToolCall) -> ToolResult:
        exc = self._exc_per_name.get(call.name)
        if exc is not None:
            raise exc
        return ToolResult(tool_call_id=call.id, content="ok")


# ---------------------------------------------------------------------------
# Stubs.
# ---------------------------------------------------------------------------


class _ToolCallingLLM(BaseLLMProvider):
    """Returns a ``tool_calls`` response on the first call and a plain
    text response on the second so the agent loop exits cleanly after
    one round of tool execution.

    ``tool_calls`` is the canonical list the test wants to drive
    through ``run_agent_loop`` — set ``["one"]`` for the single-tool
    code path or ``["a", "b"]`` for the parallel path.
    """

    provider_name = "stub"
    context_window = 10_000

    def __init__(self, tool_names: list[str]) -> None:
        self._tool_names = tool_names
        self.call_count = 0

    async def chat(self, messages, **kwargs) -> ChatResponse:
        self.call_count += 1
        if self.call_count == 1:
            return ChatResponse(
                content="",
                model="stub",
                stop_reason="tool_use",
                tool_calls=[
                    ToolCall(id=f"call_{i}", name=name, arguments={})
                    for i, name in enumerate(self._tool_names)
                ],
            )
        return ChatResponse(content="done", model="stub", stop_reason="end_turn")

    async def chat_stream(self, messages, **kwargs) -> AsyncIterator[str]:
        yield "done"

    async def embed(self, text, model=None):
        return [0.0]

    async def list_models(self):
        return []


def _make_executor(tool_name: str, *, raises: BaseException | None = None):
    """Build a ``CompositeToolExecutor`` with one tool.

    When ``raises`` is set, uses ``_RaisingExecutor`` so the exception
    actually bubbles up to ``_exec_with_span``'s handlers (the default
    ``CallableToolExecutor`` would swallow ``Exception`` into a
    ``ToolResult(is_error=True)`` at line 606 of ``app/tools/base.py``
    and we'd never hit the tool-span error path under test).

    When ``raises`` is ``None``, uses ``CallableToolExecutor`` as
    normal so the success path runs through the regular code path.
    """
    composite = CompositeToolExecutor()
    if raises is not None:
        inner: Any = _RaisingExecutor(raises)
    else:
        inner = CallableToolExecutor()

        async def _ok(**_kwargs: Any) -> str:
            return "ok"

        inner.register(tool_name, _ok)
    composite.add_tools(
        [
            ToolDefinition(
                name=tool_name,
                description=f"Tool {tool_name}",
                input_schema={"type": "object", "properties": {}},
            )
        ],
        inner,
    )
    return composite


def _capture_tool_span_calls(monkeypatch) -> dict[str, list[dict]]:
    """Patch the Langfuse facade calls so we can assert on what
    ``_lf_end_tool_span`` received per call. Returns a dict with two
    lists: ``"started"`` (one entry per ``start_tool_span``) and
    ``"ended"`` (one entry per ``end_tool_span``)."""

    log: dict[str, list[dict]] = {"started": [], "ended": [], "ctx_reset": 0}

    def _fake_start_tool_span(**kwargs):
        # Return a sentinel ctx so callers' ``if _tool_ctx:`` checks
        # succeed AND the contextvar push happens.
        ctx = {"trace": MagicMock(), "tool_name": kwargs.get("tool_name")}
        log["started"].append({**kwargs, "_ctx": ctx})
        return ctx

    def _fake_end_tool_span(ctx, **kwargs):
        log["ended"].append({**kwargs, "_ctx": ctx})

    def _fake_set_ctx(ctx):
        return object()  # sentinel token

    def _fake_reset_ctx(_token):
        log["ctx_reset"] += 1

    monkeypatch.setattr(al, "_lf_start_tool_span", _fake_start_tool_span)
    monkeypatch.setattr(al, "_lf_end_tool_span", _fake_end_tool_span)
    monkeypatch.setattr(al, "_lf_set_ctx", _fake_set_ctx)
    monkeypatch.setattr(al, "_lf_reset_ctx", _fake_reset_ctx)

    # Keep the per-iteration spans as no-ops so the test focuses on
    # tool-span behaviour only.
    monkeypatch.setattr(al, "_lf_start_iter_span", lambda **kw: None)
    monkeypatch.setattr(al, "_lf_end_span", lambda *a, **kw: None)
    monkeypatch.setattr(al, "_lf_start_llm_gen", lambda *a, **kw: None)
    monkeypatch.setattr(al, "_lf_end_generation", lambda *a, **kw: None)

    return log


# ---------------------------------------------------------------------------
# Single-tool path.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_single_tool_exception_passes_exception_object(monkeypatch):
    """Generic tool exceptions must reach ``_lf_end_tool_span`` as the
    exception OBJECT, not ``str(exc)``. The facade then renders
    ``f'{type(exc).__name__}: {exc}'`` so the trace status message
    keeps the type info — important for exception classes whose
    ``str()`` is empty (e.g. ``CancelledError``) and for triage."""
    log = _capture_tool_span_calls(monkeypatch)
    composite = _make_executor("tool_a", raises=RuntimeError("boom"))
    llm = _ToolCallingLLM(["tool_a"])

    await al.run_agent_loop(
        llm=llm,
        messages=[ChatMessage(role="user", content="go")],
        tools=composite.all_tools,
        executor=composite,
        max_iterations=2,
    )

    # The error close must have happened with the exception OBJECT.
    error_closes = [c for c in log["ended"] if c.get("is_error")]
    assert len(error_closes) == 1, error_closes
    err = error_closes[0]["error"]
    assert isinstance(err, RuntimeError), (
        f"expected exception object, got {type(err).__name__}: {err!r}"
    )
    assert str(err) == "boom"
    assert error_closes[0]["error_kind"] == "exception"


@pytest.mark.asyncio
async def test_single_tool_cancellederror_closes_span_and_propagates(monkeypatch):
    """``asyncio.CancelledError`` raised inside a single-tool execution
    MUST close the Langfuse tool span with the cancellation recorded
    AND propagate so the asyncio scheduler sees it. The earlier shape
    (``except Exception``) silently let ``CancelledError`` skip the
    close path entirely, leaving the span open and the trace looking
    like a clean success."""
    log = _capture_tool_span_calls(monkeypatch)
    composite = _make_executor("tool_a", raises=asyncio.CancelledError())
    llm = _ToolCallingLLM(["tool_a"])

    with pytest.raises(asyncio.CancelledError):
        await al.run_agent_loop(
            llm=llm,
            messages=[ChatMessage(role="user", content="go")],
            tools=composite.all_tools,
            executor=composite,
            max_iterations=2,
        )

    # The tool span was closed with the CancelledError instance and the
    # explicit ``cancelled`` error_kind so it can be filtered in Langfuse.
    cancelled_closes = [
        c for c in log["ended"] if c.get("error_kind") == "cancelled"
    ]
    assert len(cancelled_closes) == 1, (
        f"expected exactly one cancelled close, got {log['ended']}"
    )
    err = cancelled_closes[0]["error"]
    assert isinstance(err, asyncio.CancelledError), (
        f"error annotation must be the CancelledError instance so the "
        f"facade renders 'CancelledError: ...'; got {err!r}"
    )
    assert cancelled_closes[0]["is_error"] is True


@pytest.mark.asyncio
async def test_single_tool_timeout_passes_timeout_exception_object(monkeypatch):
    """``TimeoutError`` must also flow as the exception object so the
    Langfuse-rendered status label is consistent with the other close
    sites in the file (``'TimeoutError: ...'`` instead of the previous
    raw ``f'timeout after Ns'`` string)."""
    log = _capture_tool_span_calls(monkeypatch)

    async def _slow(**_kwargs):
        await asyncio.sleep(10)
        return "never"

    composite = CompositeToolExecutor()
    inner = CallableToolExecutor()
    inner.register("slow_tool", _slow)
    composite.add_tools(
        [
            ToolDefinition(
                name="slow_tool",
                description="Slow",
                input_schema={"type": "object", "properties": {}},
            )
        ],
        inner,
    )
    # Force a near-zero timeout so the wait_for fires before _slow returns.
    monkeypatch.setattr(al, "_TOOL_EXECUTION_TIMEOUT", 0.01)
    llm = _ToolCallingLLM(["slow_tool"])

    await al.run_agent_loop(
        llm=llm,
        messages=[ChatMessage(role="user", content="go")],
        tools=composite.all_tools,
        executor=composite,
        max_iterations=2,
    )

    timeout_closes = [c for c in log["ended"] if c.get("error_kind") == "timeout"]
    assert len(timeout_closes) == 1, log["ended"]
    err = timeout_closes[0]["error"]
    assert isinstance(err, TimeoutError), (
        f"error must be a TimeoutError instance (not a string) so the "
        f"facade renders 'TimeoutError: ...'; got {type(err).__name__}: {err!r}"
    )


@pytest.mark.asyncio
async def test_single_tool_contextvar_reset_runs_on_every_path(monkeypatch):
    """The single-tool path moved the contextvar reset into a
    ``finally`` block. Verify it fires regardless of which branch
    exited — a future regression that adds a new ``except`` branch
    and forgets to reset would let the parent contextvar leak into
    the next tool call."""
    log = _capture_tool_span_calls(monkeypatch)

    # Success branch.
    composite = _make_executor("ok_tool")
    llm = _ToolCallingLLM(["ok_tool"])
    await al.run_agent_loop(
        llm=llm,
        messages=[ChatMessage(role="user", content="go")],
        tools=composite.all_tools,
        executor=composite,
        max_iterations=2,
    )
    assert log["ctx_reset"] == 1, "success path must reset the contextvar"

    # Error branch.
    log["ctx_reset"] = 0
    composite = _make_executor("err_tool", raises=ValueError("nope"))
    llm = _ToolCallingLLM(["err_tool"])
    await al.run_agent_loop(
        llm=llm,
        messages=[ChatMessage(role="user", content="go")],
        tools=composite.all_tools,
        executor=composite,
        max_iterations=2,
    )
    assert log["ctx_reset"] == 1, "exception path must reset the contextvar"

    # Cancellation branch.
    log["ctx_reset"] = 0
    composite = _make_executor("cancel_tool", raises=asyncio.CancelledError())
    llm = _ToolCallingLLM(["cancel_tool"])
    with pytest.raises(asyncio.CancelledError):
        await al.run_agent_loop(
            llm=llm,
            messages=[ChatMessage(role="user", content="go")],
            tools=composite.all_tools,
            executor=composite,
            max_iterations=2,
        )
    assert log["ctx_reset"] == 1, "cancellation path must reset the contextvar"


# ---------------------------------------------------------------------------
# Parallel-tool path.
# ---------------------------------------------------------------------------


def _make_two_tool_executor(*, a_raises=None, b_raises=None):
    """Parallel-path executor with two tools.

    When either ``a_raises`` or ``b_raises`` is set, the corresponding
    tool is wired through ``_PerNameRaisingExecutor`` so the exception
    propagates to ``_exec_with_span`` instead of being wrapped by
    ``CallableToolExecutor``. When both are ``None``, uses the regular
    callable path for success-flow tests.
    """
    composite = CompositeToolExecutor()
    if a_raises is not None or b_raises is not None:
        inner: Any = _PerNameRaisingExecutor(
            {"tool_a": a_raises, "tool_b": b_raises}
        )
    else:
        inner = CallableToolExecutor()

        async def _ok_a(**_kwargs):
            return "ok_a"

        async def _ok_b(**_kwargs):
            return "ok_b"

        inner.register("tool_a", _ok_a)
        inner.register("tool_b", _ok_b)
    composite.add_tools(
        [
            ToolDefinition(
                name="tool_a",
                description="A",
                input_schema={"type": "object", "properties": {}},
            ),
            ToolDefinition(
                name="tool_b",
                description="B",
                input_schema={"type": "object", "properties": {}},
            ),
        ],
        inner,
    )
    return composite


@pytest.mark.asyncio
async def test_parallel_tool_exception_passes_exception_object(monkeypatch):
    """Parallel branch: a tool raising a generic ``Exception`` must
    reach ``_lf_end_tool_span`` as the exception OBJECT, not as
    ``str(exc) or type(exc).__name__``. The previous fallback string
    silently lost the exception type when ``str(exc)`` was non-empty
    (so Langfuse showed ``'boom'`` instead of ``'RuntimeError: boom'``)."""
    log = _capture_tool_span_calls(monkeypatch)
    composite = _make_two_tool_executor(
        a_raises=RuntimeError("boom_a"), b_raises=None
    )
    llm = _ToolCallingLLM(["tool_a", "tool_b"])

    await al.run_agent_loop(
        llm=llm,
        messages=[ChatMessage(role="user", content="go")],
        tools=composite.all_tools,
        executor=composite,
        max_iterations=2,
    )

    error_closes = [c for c in log["ended"] if c.get("error_kind") == "exception"]
    assert len(error_closes) == 1, error_closes
    err = error_closes[0]["error"]
    assert isinstance(err, RuntimeError), (
        f"expected exception object, got {type(err).__name__}: {err!r}"
    )
    assert str(err) == "boom_a"


@pytest.mark.asyncio
async def test_parallel_tool_cancellederror_closes_span_with_exception_object(
    monkeypatch,
):
    """Parallel branch: ``asyncio.CancelledError`` must close the tool
    span with the cancellation INSTANCE recorded. The previous
    ``except BaseException`` shape tripped CodeQL
    ``py/catch-base-exception`` and used ``str(exc) or type(exc).__name__``
    which evaluated to ``'CancelledError'`` (lost the type-prefix
    format). Splitting into explicit ``except asyncio.CancelledError``
    + ``except Exception`` matches the round-5 pattern already adopted
    by the continuation and empty-response-retry rescue blocks."""
    log = _capture_tool_span_calls(monkeypatch)
    composite = _make_two_tool_executor(
        a_raises=asyncio.CancelledError(), b_raises=None
    )
    llm = _ToolCallingLLM(["tool_a", "tool_b"])

    # The CancelledError surfaces through ``asyncio.gather(...,
    # return_exceptions=True)`` and the loop's ``isinstance(result,
    # BaseException)`` branch converts it to a tool-error message; the
    # important assertion is the tool span got closed with the right
    # error data, not that the cancellation propagates out of the loop
    # (the outer loop treats CancelledError as "the tool failed").
    await al.run_agent_loop(
        llm=llm,
        messages=[ChatMessage(role="user", content="go")],
        tools=composite.all_tools,
        executor=composite,
        max_iterations=2,
    )

    cancelled_closes = [
        c for c in log["ended"] if c.get("error_kind") == "cancelled"
    ]
    assert len(cancelled_closes) == 1, (
        f"expected one cancelled close from the parallel branch, got "
        f"{log['ended']}"
    )
    err = cancelled_closes[0]["error"]
    assert isinstance(err, asyncio.CancelledError), (
        f"error annotation must be the CancelledError instance so the "
        f"facade renders 'CancelledError: ...'; got {err!r}"
    )


@pytest.mark.asyncio
async def test_parallel_tool_timeout_passes_timeout_exception_object(monkeypatch):
    """Parallel branch: ``TimeoutError`` must flow as the exception
    object for the same trace-fidelity reason as the single-tool
    branch."""
    log = _capture_tool_span_calls(monkeypatch)

    async def _slow(**_kwargs):
        await asyncio.sleep(10)
        return "never"

    async def _fast(**_kwargs):
        return "ok"

    composite = CompositeToolExecutor()
    inner = CallableToolExecutor()
    inner.register("slow_tool", _slow)
    inner.register("fast_tool", _fast)
    composite.add_tools(
        [
            ToolDefinition(
                name="slow_tool",
                description="Slow",
                input_schema={"type": "object", "properties": {}},
            ),
            ToolDefinition(
                name="fast_tool",
                description="Fast",
                input_schema={"type": "object", "properties": {}},
            ),
        ],
        inner,
    )
    monkeypatch.setattr(al, "_TOOL_EXECUTION_TIMEOUT", 0.01)
    llm = _ToolCallingLLM(["slow_tool", "fast_tool"])

    await al.run_agent_loop(
        llm=llm,
        messages=[ChatMessage(role="user", content="go")],
        tools=composite.all_tools,
        executor=composite,
        max_iterations=2,
    )

    timeout_closes = [c for c in log["ended"] if c.get("error_kind") == "timeout"]
    assert len(timeout_closes) == 1, log["ended"]
    err = timeout_closes[0]["error"]
    assert isinstance(err, TimeoutError), (
        f"error must be a TimeoutError instance (not a string); "
        f"got {type(err).__name__}: {err!r}"
    )


def test_parallel_tool_path_has_no_baseexception_catch():
    """Pin the CodeQL ``py/catch-base-exception`` invariant statically:
    the parallel-tool ``_exec_with_span`` helper must not contain
    ``except BaseException`` (which would silently re-intercept
    ``SystemExit`` / ``KeyboardInterrupt`` and let the round-4 CodeQL
    failure regress). The runtime semantics for ``CancelledError`` are
    pinned by ``test_parallel_tool_cancellederror_closes_span_with_exception_object``
    above; this test is the textual guard."""
    import inspect

    source = inspect.getsource(al.run_agent_loop)
    # Find the _exec_with_span helper body and scan it for the
    # forbidden pattern. We grab from ``async def _exec_with_span`` up
    # to the matching closing line ``return result`` of the helper.
    start = source.find("async def _exec_with_span")
    assert start != -1, "_exec_with_span helper not found in run_agent_loop"
    after_start = source[start:]
    end = after_start.find("return result")
    assert end != -1, "expected `return result` terminator inside _exec_with_span"
    helper_body = after_start[: end + len("return result")]

    assert "except BaseException" not in helper_body, (
        "_exec_with_span must NOT catch BaseException — it trips CodeQL "
        "py/catch-base-exception and silently re-intercepts SystemExit / "
        "KeyboardInterrupt. Split into `except asyncio.CancelledError` "
        "(close + re-raise) and `except Exception` (close + re-raise) "
        "instead, matching the round-5 convention adopted by the "
        "continuation and empty-response-retry rescue blocks."
    )
    # Belt-and-braces: confirm the two explicit handlers ARE present so
    # this guard catches a regression that also removes them.
    assert "except asyncio.CancelledError" in helper_body, (
        "_exec_with_span must include an explicit `except "
        "asyncio.CancelledError` handler that closes the tool span "
        "with the exception object before re-raising."
    )
    assert "except Exception" in helper_body, (
        "_exec_with_span must include an `except Exception` handler "
        "for ordinary tool failures."
    )
