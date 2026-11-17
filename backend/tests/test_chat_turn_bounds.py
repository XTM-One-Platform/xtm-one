"""Tests for the Phase-1 chat-turn resilience bounds.

Covers:

* P1.1 — in-loop LLM calls are capped to the remaining wall-clock budget
  (``wall_clock_deadline``) instead of the full 900 s ``_LLM_CALL_TIMEOUT``,
  so a turn near its budget fails cleanly INSIDE the loop rather than being
  cancelled mid-call by the caller's outer ``asyncio.wait_for``; tool calls
  are capped the same way.
* P1.2 — the streaming idle-chunk watchdog aborts a stalled SSE stream
  (no text chunk AND no thinking delta) after ``idle_timeout`` seconds,
  while reasoning-only activity keeps the stream alive.
* P1.3 — the setup-path persistent-memory query embedding is capped and
  degrades to the recency-based load on timeout; a chat turn passes a much
  shorter budget (``CHAT_TURN_EMBED_BUDGET_SECS``).
"""

from __future__ import annotations

import asyncio
import time
import uuid
from datetime import UTC, datetime, timedelta

import pytest

import app.tools.agent_loop as al
from app.llm.base import BaseLLMProvider, ChatMessage, ChatResponse
from app.models.agent_memory_entry import AgentMemoryEntry
from app.services.persistent_memory import load_persistent_context
from app.tools.base import (
    CallableToolExecutor,
    CompositeToolExecutor,
    ToolCall,
    ToolDefinition,
)

# ---------------------------------------------------------------------------
# P1.2 — streaming idle-chunk watchdog
# ---------------------------------------------------------------------------


class _StallingStreamLLM(BaseLLMProvider):
    """Yields one chunk, then stalls forever (provider accepted, then died)."""

    provider_name = "stub"
    context_window = 100_000

    async def chat(self, messages, **kwargs):
        return ChatResponse(content="unused", model="stub")

    async def chat_stream(self, messages, **kwargs):
        yield "hello "
        await asyncio.sleep(3600)
        yield "never"

    async def embed(self, text, model=None):
        return [0.0]

    async def list_models(self):
        return []


class _FakeMonotonic:
    """Stands in for ``agent_loop``'s ``time`` module with a settable clock.

    Only ``monotonic`` is simulated; every other attribute is the real
    ``time`` module's. The watchdog measures idleness on this clock, so its
    verdict depends on the simulated silence alone, never on how late the
    event loop of a loaded xdist worker wakes up.
    """

    def __init__(self) -> None:
        self.now = 1_000.0

    def monotonic(self) -> float:
        return self.now

    def __getattr__(self, name: str) -> object:
        return getattr(time, name)


@pytest.fixture
def fake_clock(monkeypatch) -> _FakeMonotonic:
    clock = _FakeMonotonic()
    monkeypatch.setattr(al, "time", clock)
    return clock


@pytest.fixture
def memory_clock(monkeypatch) -> _FakeMonotonic:
    """Freezes the clock ``load_persistent_context`` reads its deadline on.

    The embed window is then exactly ``embed_deadline - clock.now`` whatever
    the probe, recency and resolve steps cost on a loaded xdist worker; the
    embed's own wait still runs on the event loop's real clock.
    """
    clock = _FakeMonotonic()
    monkeypatch.setattr("app.services.persistent_memory.time", clock)
    return clock


class _ThinkingOnlyLLM(BaseLLMProvider):
    """Emits thinking deltas (no text chunks) for a while, then the answer.

    Models a reasoning model that produces no text for longer than the idle
    window while thinking tokens flow through ``on_thinking`` — the watchdog
    must treat that as activity and NOT abort.

    Silence is simulated on *clock*: each beat advances it by 0.3 s before
    the delta, then yields the loop for a short real pause so the watchdog's
    wait can time out and re-evaluate mid-silence.
    """

    provider_name = "stub"
    context_window = 100_000

    def __init__(self, clock: _FakeMonotonic) -> None:
        self._clock = clock

    async def chat(self, messages, **kwargs):
        return ChatResponse(content="unused", model="stub")

    async def chat_stream(self, messages, on_thinking=None, **kwargs):
        # 6 thinking beats spaced 0.3 s apart on the simulated clock: total
        # silence on the text channel is 1.8 s, far above the 0.5 s idle
        # window used in the test — but each beat re-arms the watchdog.
        for i in range(6):
            self._clock.now += 0.3
            await asyncio.sleep(0.1)
            if on_thinking is not None:
                await on_thinking(f"thinking-{i} ")
        yield "final answer"

    async def embed(self, text, model=None):
        return [0.0]

    async def list_models(self):
        return []


@pytest.mark.asyncio
async def test_idle_watchdog_aborts_stalled_stream():
    """A stream that stops producing chunks must be aborted after
    ``idle_timeout`` seconds with a ``TimeoutError`` (P1.2)."""
    llm = _StallingStreamLLM()
    started = time.monotonic()
    with pytest.raises(TimeoutError):
        await al._streaming_chat(
            llm,
            [ChatMessage(role="user", content="hi")],
            None,
            model="stub",
            max_tokens=128,
            idle_timeout=0.5,
        )
    elapsed = time.monotonic() - started
    assert elapsed < 5.0, f"watchdog should fire at ~0.5s, took {elapsed:.1f}s"


@pytest.mark.asyncio
async def test_idle_watchdog_spared_by_thinking_activity(fake_clock):
    """Reasoning deltas re-arm the idle window: a stream that thinks for
    longer than ``idle_timeout`` without emitting text must NOT be aborted
    (P1.2 — reasoning models legitimately emit no text for minutes)."""
    llm = _ThinkingOnlyLLM(fake_clock)
    thinking_chunks: list[str] = []

    async def _on_thinking(text: str) -> None:
        thinking_chunks.append(text)

    response = await al._streaming_chat(
        llm,
        [ChatMessage(role="user", content="hi")],
        None,
        model="stub",
        max_tokens=128,
        on_thinking=_on_thinking,
        idle_timeout=0.5,
    )
    assert response.content == "final answer"
    assert len(thinking_chunks) == 6


@pytest.mark.asyncio
async def test_idle_watchdog_spared_by_thinking_without_consumer(fake_clock):
    """Even with NO caller-side ``on_thinking`` consumer, thinking deltas
    must count as liveness: ``_streaming_chat`` injects an activity-only
    sink so a reasoning model that emits no text is never falsely aborted
    (P1.2 review hardening)."""
    llm = _ThinkingOnlyLLM(fake_clock)
    response = await al._streaming_chat(
        llm,
        [ChatMessage(role="user", content="hi")],
        None,
        model="stub",
        max_tokens=128,
        # No on_thinking passed — the sink must still observe the deltas.
        idle_timeout=0.5,
    )
    assert response.content == "final answer"


@pytest.mark.asyncio
async def test_idle_watchdog_disabled_with_none():
    """``idle_timeout=None`` disables the watchdog — only the overall
    per-call ``timeout`` bounds the stream (legacy behaviour)."""
    llm = _StallingStreamLLM()
    started = time.monotonic()
    with pytest.raises(TimeoutError):
        await al._streaming_chat(
            llm,
            [ChatMessage(role="user", content="hi")],
            None,
            model="stub",
            max_tokens=128,
            idle_timeout=None,
            timeout=0.8,
        )
    elapsed = time.monotonic() - started
    # The overall timeout (0.8 s) fired, not an idle watchdog.
    assert 0.5 < elapsed < 5.0


# ---------------------------------------------------------------------------
# P1.1 — in-loop LLM calls capped to the remaining wall-clock budget
# ---------------------------------------------------------------------------


class _SlowToolCallLLM(BaseLLMProvider):
    """First (with-tools) call sleeps far longer than the remaining budget."""

    provider_name = "stub"
    context_window = 100_000

    def __init__(self) -> None:
        self.chat_calls = 0

    async def chat(self, messages, tools=None, model=None, **kwargs):
        self.chat_calls += 1
        await asyncio.sleep(30.0)
        return ChatResponse(content="too late", model=model or "stub")

    async def chat_stream(self, messages, **kwargs):
        yield ""

    async def embed(self, text, model=None):
        return [0.0]

    async def list_models(self):
        return []


@pytest.mark.asyncio
async def test_in_loop_llm_call_capped_to_wall_clock_budget(monkeypatch):
    """With ``wall_clock_deadline`` only a few seconds away, the in-loop
    (with-tools) LLM call must be capped to the remaining budget and time
    out INSIDE the loop — returning the graceful terminal response well
    before the 900 s ``_LLM_CALL_TIMEOUT`` (P1.1).

    ``_IN_LOOP_MIN_REMAINING_SECS`` is patched to 1 s so the 3-second
    budget still reaches the LLM call (rather than breaking to the backstop
    via the guard).  The call itself is capped to ≈ 1 s by
    ``_remaining_llm_timeout()`` and fails before the LLM's 30 s sleep.
    """
    monkeypatch.setattr(al, "_IN_LOOP_MIN_REMAINING_SECS", 1)

    async def _echo() -> str:
        return "ok"

    callable_exec = CallableToolExecutor()
    callable_exec.register("echo_tool", _echo)
    definitions = [ToolDefinition(name="echo_tool", description="echo")]
    composite = CompositeToolExecutor()
    composite.add_tools(definitions, callable_exec)

    llm = _SlowToolCallLLM()
    started = time.monotonic()
    response = await al.run_agent_loop(
        llm=llm,
        messages=[ChatMessage(role="user", content="do the thing")],
        tools=definitions,
        executor=composite,
        max_iterations=3,
        progressive_tools=False,
        # ~3 s of budget: the in-loop call cap is remaining - reserve,
        # floored at 1 s — far below the LLM's 30 s sleep.
        wall_clock_deadline=time.monotonic() + 3.0,
    )
    elapsed = time.monotonic() - started

    assert llm.chat_calls >= 1
    assert "took too long" in (response.content or "")
    assert elapsed < 15.0, (
        f"in-loop call should have been capped to the remaining budget, "
        f"took {elapsed:.1f}s"
    )


@pytest.mark.asyncio
async def test_no_deadline_keeps_full_llm_timeout():
    """Without ``wall_clock_deadline`` the per-call timeout must stay at the
    full ``_LLM_CALL_TIMEOUT`` (zero behaviour change for legacy callers)."""
    # Exercise the helper contract indirectly: a fast LLM under no deadline
    # completes normally (the loop never times its calls out).

    class _FastLLM(_SlowToolCallLLM):
        async def chat(self, messages, tools=None, model=None, **kwargs):
            self.chat_calls += 1
            return ChatResponse(content="all done", model=model or "stub")

    llm = _FastLLM()
    response = await al.run_agent_loop(
        llm=llm,
        messages=[ChatMessage(role="user", content="hi")],
        tools=None,
        executor=None,
        max_iterations=2,
        progressive_tools=False,
    )
    assert response.content == "all done"


class _SlowToolsLLM(BaseLLMProvider):
    """First call asks for ``calls`` slow tool calls at once, then answers."""

    provider_name = "stub"
    context_window = 100_000

    def __init__(self, calls: int) -> None:
        self.calls = calls
        self.chat_calls = 0

    async def chat(self, messages, tools=None, model=None, **kwargs):
        self.chat_calls += 1
        if tools and self.chat_calls == 1:
            return ChatResponse(
                content="",
                model=model or "stub",
                tool_calls=[
                    ToolCall(id=f"tc-{i}", name="slow_tool", arguments={"i": i})
                    for i in range(self.calls)
                ],
                stop_reason="tool_use",
            )
        return ChatResponse(content="answered with what it had", model=model or "stub")

    async def chat_stream(self, messages, **kwargs):
        yield ""

    async def embed(self, text, model=None):
        return [0.0]

    async def list_models(self):
        return []


def test_tool_timeout_capped_to_remaining_budget():
    """Each tier keeps its own timeout while the budget allows it and is capped
    to what remains of it (minus the in-loop reserve, floored at 1 s) near the
    deadline; without a deadline nothing changes."""
    far = time.monotonic() + 10_000
    assert al._tool_timeout_for("slow_tool", 300) == 300
    assert al._tool_timeout_for("slow_tool", 300, far) == 300
    assert al._tool_timeout_for("consult_agent", 300, far) == al._AGENT_TOOL_TIMEOUT
    near = time.monotonic() + al._IN_LOOP_DEADLINE_RESERVE_SECS + 60.5
    assert al._tool_timeout_for("slow_tool", 300, near) in (59, 60)
    assert al._tool_timeout_for("consult_agent", 300, near) in (59, 60)
    assert al._tool_timeout_for("slow_tool", 300, time.monotonic() - 1) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("calls", [1, 2], ids=["sequential", "parallel"])
async def test_tool_call_capped_to_wall_clock_budget(monkeypatch, calls):
    """A tool call still running at the deadline is cancelled with the whole
    run by the caller's outer ``asyncio.wait_for``; capped to the remaining
    budget it times out INSIDE the loop instead, reports the cap it ran under,
    and the loop still answers."""
    monkeypatch.setattr(al, "_IN_LOOP_MIN_REMAINING_SECS", 1)

    async def _slow(i: int = 0) -> str:
        await asyncio.sleep(30.0)
        return "too late"

    callable_exec = CallableToolExecutor()
    callable_exec.register("slow_tool", _slow)
    definitions = [ToolDefinition(name="slow_tool", description="slow")]
    composite = CompositeToolExecutor()
    composite.add_tools(definitions, callable_exec)

    messages = [ChatMessage(role="user", content="do the thing")]
    started = time.monotonic()
    response = await al.run_agent_loop(
        llm=_SlowToolsLLM(calls),
        messages=messages,
        tools=definitions,
        executor=composite,
        max_iterations=3,
        progressive_tools=False,
        # ~3 s of budget: remaining - reserve is below the 1 s floor.
        wall_clock_deadline=time.monotonic() + 3.0,
    )
    elapsed = time.monotonic() - started

    timed_out = [
        m for m in messages if m.role == "tool" and "timed out after 1s" in (m.content or "")
    ]
    assert len(timed_out) == calls
    assert response.content
    assert elapsed < 15.0, f"the 30 s tool should have been capped, took {elapsed:.1f}s"


# ---------------------------------------------------------------------------
# P1.3 — setup-path embedding cap
# ---------------------------------------------------------------------------

_AGENT_ID = "00000000-0000-0000-0000-000000000001"


def _entry(content: str, *, age_minutes: int = 0) -> AgentMemoryEntry:
    return AgentMemoryEntry(
        id=uuid.uuid4(),
        agent_id=uuid.UUID(_AGENT_ID),
        user_id=None,
        content=content,
        memory_type="fact",
        source="compaction",
        embedding=None,
        created_at=datetime.now(UTC) - timedelta(minutes=age_minutes),
    )


class _FakeResult:
    def __init__(self, *, scalar=None, entries=None):
        self._scalar = scalar
        self._entries = entries if entries is not None else []

    def scalar_one_or_none(self):
        return self._scalar

    def scalars(self):
        return self

    def all(self):
        return list(self._entries)


class _FakeDB:
    """Fake AsyncSession dispatching on the shape of each SELECT (see
    ``test_persistent_context_relevance.py`` for the classification)."""

    def __init__(self, *, probe_id=None, recent=None):
        self.statements: list = []
        self._probe_id = probe_id
        self._recent = recent or []

    async def execute(self, stmt):
        self.statements.append(stmt)
        descs = stmt.column_descriptions
        if len(descs) == 1 and descs[0]["name"] == "id":
            # More entries than the limit, so the embedding path runs.
            ids = [uuid.uuid4() for _ in range(1_000)] if self._probe_id else []
            return _FakeResult(scalar=self._probe_id, entries=ids)
        if "ORDER BY agent_memory_entries.created_at DESC" in str(stmt):
            return _FakeResult(entries=self._recent)
        return _FakeResult(entries=[])


@pytest.mark.asyncio
async def test_setup_embed_timeout_falls_back_to_recency(monkeypatch):
    """A hung embedding call in the setup path must be capped and degrade
    to the recency-based load instead of stalling the turn (P1.3)."""

    async def _hung_embed(db, text, *, _provider_model=None, **_kwargs):
        await asyncio.sleep(3600)

    # String targets keep the module imported one way only (the top-level
    # ``from app.services.persistent_memory import ...``) — code-quality
    # flags mixing ``import x as y`` with ``from x import y``.
    monkeypatch.setattr(
        "app.services.persistent_memory.embed_text", _hung_embed
    )
    monkeypatch.setattr(
        "app.services.persistent_memory._SETUP_EMBED_TIMEOUT_SECS", 0.2
    )

    recent = [_entry("Currency prefix is EUR")]
    db = _FakeDB(probe_id=uuid.uuid4(), recent=recent)

    started = time.monotonic()
    result = await load_persistent_context(
        db,
        agent_id=_AGENT_ID,
        user_id=None,
        query="what currency prefix should I use?",
    )
    elapsed = time.monotonic() - started

    assert "Currency prefix is EUR" in result
    assert elapsed < 5.0, f"embed cap should fire at ~0.2s, took {elapsed:.1f}s"


@pytest.mark.asyncio
async def test_chat_turn_embed_budget_overrides_setup_cap(monkeypatch):
    """A chat turn's short embed budget wins over the 30 s setup cap: a slow
    embedding degrades to the recency load within the budget, so the user's
    first token is not held by the memory stage (dev:chat-reactivity R2)."""

    async def _slow_embed(db, text, *, _provider_model=None, **_kwargs):
        await asyncio.sleep(3600)

    monkeypatch.setattr("app.services.persistent_memory.embed_text", _slow_embed)

    recent = [_entry("Currency prefix is EUR")]
    db = _FakeDB(probe_id=uuid.uuid4(), recent=recent)

    started = time.monotonic()
    result = await load_persistent_context(
        db,
        agent_id=_AGENT_ID,
        user_id=None,
        query="what currency prefix should I use?",
        embed_timeout_secs=0.05,
    )
    elapsed = time.monotonic() - started

    assert "Currency prefix is EUR" in result
    assert elapsed < 2.0, f"chat budget should fire at ~0.05s, took {elapsed:.1f}s"


def test_chat_turn_passes_its_embed_budget():
    """The chat turn hands the short budget to the persistent-memory loader;
    other callers keep the default setup cap."""
    import importlib
    import inspect as _inspect

    from app.services.agent_chat import run_agent_chat

    # importlib keeps the module imported one way only (see above).
    persistent_memory = importlib.import_module("app.services.persistent_memory")

    assert persistent_memory.CHAT_TURN_EMBED_BUDGET_SECS < 1.0
    assert persistent_memory._SETUP_EMBED_TIMEOUT_SECS == 30.0
    sig = _inspect.signature(persistent_memory.load_persistent_entries)
    assert sig.parameters["embed_timeout_secs"].default is None

    src = _inspect.getsource(run_agent_chat)
    call_pos = src.find("load_persistent_context(")
    assert call_pos != -1
    assert "embed_timeout_secs=CHAT_TURN_EMBED_BUDGET_SECS" in src[call_pos : call_pos + 800]
    assert "embed_deadline=_embed_deadline" in src[call_pos : call_pos + 800]
    assert "timings=_persistent_timings" in src[call_pos : call_pos + 800]


@pytest.mark.asyncio
async def test_embed_deadline_covers_a_slow_provider_resolution(monkeypatch, memory_clock):
    """A provider resolution that eats the deadline leaves no window for the
    embed: the embed is skipped and the recency load answers within the
    deadline, not deadline + embed budget (#8757).

    The deadline is read on the frozen memory clock and the bound is the
    timeout handed to the resolve wait, not the wall time of the call: a
    loaded xdist worker stalled the event loop 43 s past a 0.1 s timer on
    main (CI 42568875674), which a wall-clock bound reads as a regression."""
    embed_calls: list[str] = []
    wait_timeouts: list[float | None] = []
    real_wait_for = asyncio.wait_for

    async def _spy_wait_for(aw, timeout):
        wait_timeouts.append(timeout)
        return await real_wait_for(aw, timeout)

    async def _embed(db, text, *, _provider_model=None, **_kwargs):
        embed_calls.append(text)
        await asyncio.sleep(3600)

    async def _slow_resolve():
        await asyncio.sleep(3600)

    monkeypatch.setattr("app.services.persistent_memory.embed_text", _embed)
    monkeypatch.setattr(asyncio, "wait_for", _spy_wait_for)

    recent = [_entry("Currency prefix is EUR")]
    db = _FakeDB(probe_id=uuid.uuid4(), recent=recent)
    timings: dict = {}
    task = asyncio.create_task(_slow_resolve())
    try:
        result = await load_persistent_context(
            db,
            agent_id=_AGENT_ID,
            user_id=None,
            query="what currency prefix should I use?",
            provider_model_task=task,
            embed_timeout_secs=5.0,
            embed_deadline=memory_clock.now + 0.1,
            timings=timings,
        )
    finally:
        task.cancel()

    assert "Currency prefix is EUR" in result
    assert embed_calls == []
    assert timings.get("embed_skipped") is True
    assert {"probe_ms", "resolve_ms", "recency_ms"} <= timings.keys()
    assert "embed_ms" not in timings
    # The resolve wait is the only wait: bounded by the 0.1 s left of the
    # deadline, never by the 5 s embed budget.
    assert wait_timeouts == [pytest.approx(0.1)]


@pytest.mark.asyncio
async def test_embed_gets_what_is_left_of_the_deadline(monkeypatch, memory_clock):
    """A quick provider resolution leaves the rest of the deadline to the
    embed; a slow embed times out there and the step timings say so.

    The deadline is read on a frozen clock, so the window the embed gets does
    not shrink with the scheduling delays of a loaded worker."""

    async def _embed(db, text, *, _provider_model=None, **_kwargs):
        assert _provider_model == ("provider", "model")
        await asyncio.sleep(3600)

    async def _resolve():
        return ("provider", "model")

    monkeypatch.setattr("app.services.persistent_memory.embed_text", _embed)

    recent = [_entry("Currency prefix is EUR")]
    db = _FakeDB(probe_id=uuid.uuid4(), recent=recent)
    timings: dict = {}
    task = asyncio.create_task(_resolve())
    # Resolved before the call: the wait on it runs on the loop's real clock,
    # and a task still pending there races its 0.2 s timer on a loaded worker
    # (#9084).
    await task
    started = time.monotonic()
    result = await load_persistent_context(
        db,
        agent_id=_AGENT_ID,
        user_id=None,
        query="what currency prefix should I use?",
        provider_model_task=task,
        embed_timeout_secs=5.0,
        embed_deadline=memory_clock.now + 0.2,
        timings=timings,
    )
    elapsed = time.monotonic() - started

    assert "Currency prefix is EUR" in result
    assert timings.get("embed_timed_out") is True
    assert timings.get("embed_window_ms") == 200.0
    assert {"probe_ms", "resolve_ms", "embed_ms", "recency_ms"} <= timings.keys()
    assert elapsed < 2.0, f"embed should stop at the ~0.2s deadline, took {elapsed:.1f}s"


@pytest.mark.asyncio
async def test_embedded_query_records_vector_step(monkeypatch, memory_clock):
    """A successful embed records the embed and vector steps, no skip or
    timeout flag. The deadline is read on the frozen memory clock, so a slow
    probe on a loaded xdist worker never eats the embed window (#9556)."""

    async def _embed(db, text, *, _provider_model=None, **_kwargs):
        return [0.1, 0.2, 0.3]

    monkeypatch.setattr("app.services.persistent_memory.embed_text", _embed)

    recent = [_entry("Currency prefix is EUR")]
    db = _FakeDB(probe_id=uuid.uuid4(), recent=recent)
    timings: dict = {}
    result = await load_persistent_context(
        db,
        agent_id=_AGENT_ID,
        user_id=None,
        query="what currency prefix should I use?",
        _provider_model=("provider", "model"),
        embed_timeout_secs=5.0,
        embed_deadline=memory_clock.now + 5.0,
        timings=timings,
    )

    assert "Currency prefix is EUR" in result
    assert {"probe_ms", "embed_ms", "vector_ms", "recency_ms"} <= timings.keys()
    assert "embed_timed_out" not in timings
    assert "embed_skipped" not in timings
    assert "resolve_ms" not in timings


@pytest.mark.asyncio
async def test_timed_out_embed_is_not_waited_for_while_it_unwinds(monkeypatch, memory_clock):
    """A cancelled embedding that takes its time to unwind (an HTTP client
    closing its request) does not hold the tier: the load answers at the
    deadline and the call finishes on its own (#8801)."""
    unwinding = asyncio.Event()
    release = asyncio.Event()
    finished = asyncio.Event()

    async def _embed(db, text, *, _provider_model=None, **_kwargs):
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            unwinding.set()
            await release.wait()
            finished.set()
            raise

    monkeypatch.setattr("app.services.persistent_memory.embed_text", _embed)

    recent = [_entry("Currency prefix is EUR")]
    db = _FakeDB(probe_id=uuid.uuid4(), recent=recent)
    timings: dict = {}
    result = await load_persistent_context(
        db,
        agent_id=_AGENT_ID,
        user_id=None,
        query="what currency prefix should I use?",
        _provider_model=("provider", "model"),
        embed_timeout_secs=5.0,
        embed_deadline=memory_clock.now + 0.1,
        timings=timings,
    )

    # Answered while the cancelled embed is still unwinding.
    assert "Currency prefix is EUR" in result
    assert timings.get("embed_timed_out") is True
    assert "embed_window_ms" in timings
    await asyncio.wait_for(unwinding.wait(), timeout=5.0)
    assert not finished.is_set()
    release.set()
    await asyncio.wait_for(finished.wait(), timeout=5.0)


class _OrderedDB(_FakeDB):
    """Records when the recency query starts, to prove it overlaps the embed."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.recency_started = asyncio.Event()

    async def execute(self, stmt):
        if "ORDER BY agent_memory_entries.created_at DESC" in str(stmt):
            self.recency_started.set()
        return await super().execute(stmt)


@pytest.mark.asyncio
async def test_recency_query_runs_while_the_embed_is_in_flight(monkeypatch, memory_clock):
    """With a deadline the recency rows are read during the embed, so the
    recency query does not add to the embed wait (#8801). The deadline is on
    the frozen memory clock so a slow probe never skips the embed (#9556)."""
    db = _OrderedDB(probe_id=uuid.uuid4(), recent=[_entry("Currency prefix is EUR")])
    seen_during_embed: list[bool] = []

    async def _embed(_db, text, *, _provider_model=None, **_kwargs):
        await asyncio.wait_for(db.recency_started.wait(), timeout=5.0)
        seen_during_embed.append(db.recency_started.is_set())
        return None

    monkeypatch.setattr("app.services.persistent_memory.embed_text", _embed)

    timings: dict = {}
    result = await load_persistent_context(
        db,
        agent_id=_AGENT_ID,
        user_id=None,
        query="what currency prefix should I use?",
        _provider_model=("provider", "model"),
        embed_timeout_secs=5.0,
        embed_deadline=memory_clock.now + 5.0,
        timings=timings,
    )

    assert seen_during_embed == [True]
    assert "Currency prefix is EUR" in result
    # One recency read only: the top-up reuses the rows read during the embed.
    recency = [s for s in db.statements if "created_at DESC" in str(s)]
    assert len(recency) == 1
    assert {"probe_ms", "embed_ms", "recency_ms"} <= timings.keys()


@pytest.mark.asyncio
async def test_without_a_deadline_recency_waits_for_the_embed(monkeypatch):
    """Assignments (no deadline) keep the sequential path: no recency read
    while the embedding is in flight."""
    db = _OrderedDB(probe_id=uuid.uuid4(), recent=[_entry("Currency prefix is EUR")])
    seen_during_embed: list[bool] = []

    async def _embed(_db, text, *, _provider_model=None, **_kwargs):
        await asyncio.sleep(0)
        seen_during_embed.append(db.recency_started.is_set())
        return None

    monkeypatch.setattr("app.services.persistent_memory.embed_text", _embed)

    result = await load_persistent_context(
        db,
        agent_id=_AGENT_ID,
        user_id=None,
        query="what currency prefix should I use?",
        _provider_model=("provider", "model"),
    )

    assert seen_during_embed == [False]
    assert "Currency prefix is EUR" in result
