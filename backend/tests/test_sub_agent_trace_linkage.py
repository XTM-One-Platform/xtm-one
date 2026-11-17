"""Tests for background sub-agent Langfuse trace linkage (finding OBS-2).

Background delegations run on the worker via the orchestration reconciler
(a separate asyncio task / process), so the parent's ContextVar-based trace
context is not inherited and delegation trees used to appear as disconnected
root traces in Langfuse.  The fix:

1. ``capture_trace_context()`` snapshots the active trace as a JSON-safe
   ``{"trace_id": ..., "parent_span_id": ...}`` dict.
2. ``create_sub_agent_task`` persists that snapshot in the orchestration
   execution payload (``parent_trace_context``).
3. The ``SubAgentAdapter`` forwards it to ``_execute_sub_agent_task``, which
   rebuilds it and passes ``parent_trace_ctx`` to ``run_agent_chat``.
4. ``trace_llm_call`` accepts the serialized form and attaches the sub-agent
   root span to the parent's trace via the Langfuse v4 ``trace_context``
   kwarg on ``start_observation``.
"""

from __future__ import annotations

import sys
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services import langfuse_tracing as lft
from tests.agentic_gate_stub import stub_agentic_gate


@pytest.fixture(autouse=True)
def _agentic_ai_available(monkeypatch):
    """The task body asks the background gate first; these tests are
    about the trace context it hands the agent turn once it may run."""
    stub_agentic_gate(monkeypatch)


@pytest.fixture
def stub_langfuse_module(monkeypatch):
    """Patch ``sys.modules['langfuse']`` with a stub whose
    ``propagate_attributes`` returns a no-op context manager (mirrors
    ``test_langfuse_hierarchical_tracing.py``)."""
    fake_propagate = MagicMock()
    fake_propagate.return_value.__enter__ = MagicMock(return_value=None)
    fake_propagate.return_value.__exit__ = MagicMock(return_value=False)
    monkeypatch.setitem(
        sys.modules,
        "langfuse",
        MagicMock(propagate_attributes=fake_propagate),
    )
    return fake_propagate


# ---------------------------------------------------------------------------
# capture_trace_context
# ---------------------------------------------------------------------------


def test_capture_trace_context_returns_none_without_active_trace():
    import contextvars

    ctx = contextvars.copy_context()
    assert ctx.run(lft.capture_trace_context) is None


def test_capture_trace_context_serializes_live_span():
    span = MagicMock()
    span.trace_id = "trace-abc"
    span.id = "span-1"
    out = lft.capture_trace_context({"trace": span})
    assert out == {"trace_id": "trace-abc", "parent_span_id": "span-1"}


def test_capture_trace_context_reads_contextvar_fallback():
    span = MagicMock()
    span.trace_id = "trace-ctxvar"
    span.id = "span-ctxvar"
    token = lft.set_current_trace_ctx({"trace": span})
    try:
        out = lft.capture_trace_context()
    finally:
        lft.reset_current_trace_ctx(token)
    assert out == {"trace_id": "trace-ctxvar", "parent_span_id": "span-ctxvar"}


def test_capture_trace_context_passes_through_serialized_form():
    """An already-serialized remote context (no live span) round-trips."""
    out = lft.capture_trace_context(
        {"trace_id": "trace-remote", "parent_span_id": "span-remote"}
    )
    assert out == {"trace_id": "trace-remote", "parent_span_id": "span-remote"}


def test_capture_trace_context_explicit_empty_ctx_does_not_fall_back():
    """An explicit (but empty/cleared) ctx means "no parent" — it must NOT
    silently fall back to the contextvar, which could link the wrong trace."""
    span = MagicMock()
    span.trace_id = "trace-ctxvar"
    span.id = "span-ctxvar"
    token = lft.set_current_trace_ctx({"trace": span})
    try:
        assert lft.capture_trace_context({}) is None
    finally:
        lft.reset_current_trace_ctx(token)


def test_capture_trace_context_returns_none_when_span_has_no_trace_id():
    span = MagicMock(spec=[])  # no trace_id / id attributes at all
    assert lft.capture_trace_context({"trace": span}) is None


def test_capture_trace_context_omits_missing_parent_span_id():
    span = MagicMock(spec=["trace_id"])
    span.trace_id = "trace-only"
    out = lft.capture_trace_context({"trace": span})
    assert out == {"trace_id": "trace-only"}


# ---------------------------------------------------------------------------
# trace_llm_call: serialized remote parent → trace_context kwarg
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_trace_llm_call_links_serialized_parent_via_trace_context(
    monkeypatch, stub_langfuse_module
):
    """A serialized parent (captured in another process) must be attached
    via ``client.start_observation(trace_context={...})`` — proving the
    delegation nests inside the orchestrator's trace across the worker
    boundary instead of starting a disconnected root trace."""
    root_span = MagicMock()
    root_span.id = "child-root"
    root_span.trace_id = "trace-parent"
    client = MagicMock()
    client.start_observation = MagicMock(return_value=root_span)

    async def fake_get_client(_db):
        return client

    monkeypatch.setattr(lft, "get_langfuse_client", fake_get_client)
    monkeypatch.setattr(lft, "_resolve_user_group_ids", AsyncMock(return_value=[]))

    serialized = {"trace_id": "trace-parent", "parent_span_id": "span-parent"}
    ctx = await lft.trace_llm_call(
        db=MagicMock(),
        name="agent-chat:worker",
        user_id="u",
        source="test",
        agent_slug="worker",
        parent_trace_ctx=serialized,
    )

    assert ctx is not None
    client.start_observation.assert_called_once()
    kwargs = client.start_observation.call_args.kwargs
    assert kwargs["trace_context"] == {
        "trace_id": "trace-parent",
        "parent_span_id": "span-parent",
    }
    # Sub-agent naming semantics: compact agent label, no trace_name
    # override (the trace card belongs to the orchestrator).
    assert kwargs["name"] == "agent:worker"
    attrs_kwargs = stub_langfuse_module.call_args.kwargs
    assert "trace_name" not in attrs_kwargs
    # Correlation metadata is stamped for filtering/debugging.
    assert kwargs["metadata"]["parent_trace_id"] == "trace-parent"
    # ``end_trace`` relies on parent_ctx to apply sub-agent output
    # semantics (output on the span, not on the trace card).
    assert ctx["parent_ctx"] is serialized


@pytest.mark.asyncio
async def test_trace_llm_call_serialized_parent_without_span_id(
    monkeypatch, stub_langfuse_module
):
    """``parent_span_id`` is optional — linkage degrades to trace-level
    attachment rather than dropping the parent relationship."""
    root_span = MagicMock()
    root_span.id = "child-root"
    root_span.trace_id = "trace-parent"
    client = MagicMock()
    client.start_observation = MagicMock(return_value=root_span)

    async def fake_get_client(_db):
        return client

    monkeypatch.setattr(lft, "get_langfuse_client", fake_get_client)
    monkeypatch.setattr(lft, "_resolve_user_group_ids", AsyncMock(return_value=[]))

    ctx = await lft.trace_llm_call(
        db=MagicMock(),
        name="agent-chat:worker",
        user_id="u",
        source="test",
        parent_trace_ctx={"trace_id": "trace-parent"},
    )

    assert ctx is not None
    kwargs = client.start_observation.call_args.kwargs
    assert kwargs["trace_context"] == {"trace_id": "trace-parent"}


@pytest.mark.asyncio
async def test_trace_llm_call_live_parent_span_still_wins(
    monkeypatch, stub_langfuse_module
):
    """A live parent span (same-process consult/transfer) keeps using the
    span-nesting path — the serialized branch only fires when no live span
    is available."""
    child_span = MagicMock()
    child_span.id = "child"
    child_span.trace_id = "trace-live"
    parent_root = MagicMock()
    parent_root.trace_id = "trace-live"
    parent_root.start_observation = MagicMock(return_value=child_span)
    client = MagicMock()

    async def fake_get_client(_db):
        return client

    monkeypatch.setattr(lft, "get_langfuse_client", fake_get_client)
    monkeypatch.setattr(lft, "_resolve_user_group_ids", AsyncMock(return_value=[]))

    ctx = await lft.trace_llm_call(
        db=MagicMock(),
        name="agent-chat:child",
        user_id="u",
        source="test",
        parent_trace_ctx={"trace": parent_root},
    )

    assert ctx is not None
    parent_root.start_observation.assert_called_once()
    client.start_observation.assert_not_called()


@pytest.mark.asyncio
async def test_trace_llm_call_explicit_empty_ctx_does_not_fall_back(
    monkeypatch, stub_langfuse_module
):
    """An explicit (but empty/cleared) ``parent_trace_ctx`` means "no
    parent" — it must NOT fall back to the contextvar (which could link
    the wrong trace) and must start a plain top-level trace."""
    root_span = MagicMock()
    root_span.id = "root"
    root_span.trace_id = "trace-root"
    client = MagicMock()
    client.start_observation = MagicMock(return_value=root_span)

    async def fake_get_client(_db):
        return client

    monkeypatch.setattr(lft, "get_langfuse_client", fake_get_client)
    monkeypatch.setattr(lft, "_resolve_user_group_ids", AsyncMock(return_value=[]))

    contextvar_parent = MagicMock()
    contextvar_parent.trace_id = "trace-ctxvar"
    token = lft.set_current_trace_ctx({"trace": contextvar_parent})
    try:
        ctx = await lft.trace_llm_call(
            db=MagicMock(),
            name="agent-chat:isolated",
            user_id="u",
            source="test",
            parent_trace_ctx={},
        )
    finally:
        lft.reset_current_trace_ctx(token)

    assert ctx is not None
    # The contextvar parent must not have been used for span nesting.
    contextvar_parent.start_observation.assert_not_called()
    # Top-level trace semantics: root span on the client, no trace_context
    # kwarg, and the descriptive trace_name override is applied.
    client.start_observation.assert_called_once()
    kwargs = client.start_observation.call_args.kwargs
    assert "trace_context" not in kwargs
    assert kwargs["name"] == "agent_loop"
    attrs_kwargs = stub_langfuse_module.call_args.kwargs
    assert attrs_kwargs["trace_name"] == "agent-chat:isolated"
    # Normalized to None so ``end_trace`` applies top-level output
    # semantics (trace card I/O, not span output).
    assert ctx["parent_ctx"] is None


# ---------------------------------------------------------------------------
# create_sub_agent_task: trace context captured into the execution payload
# ---------------------------------------------------------------------------


class _FakeSessionCM:
    def __init__(self, db):
        self._db = db

    async def __aenter__(self):
        return self._db

    async def __aexit__(self, *exc):
        return False


def _make_own_db() -> MagicMock:
    db = MagicMock()
    db.add = MagicMock()
    db.flush = AsyncMock()
    db.commit = AsyncMock()
    db.refresh = AsyncMock()
    db.execute = AsyncMock()
    return db


@pytest.mark.asyncio
async def test_create_sub_agent_task_persists_parent_trace_context():
    """The parent's trace context active at spawn time must land in the
    orchestration execution payload so the worker can rebuild it."""
    from app.services.sub_agent import create_sub_agent_task

    db = _make_own_db()
    create_execution = AsyncMock()
    span = MagicMock()
    span.trace_id = "trace-parent"
    span.id = "span-parent"

    token = lft.set_current_trace_ctx({"trace": span})
    try:
        with (
            patch("app.core.database.async_session", lambda: _FakeSessionCM(db)),
            patch(
                "app.services.orchestration_engine.create_execution",
                new=create_execution,
            ),
            patch(
                "app.services.orchestration_engine.notify_orchestration_wake",
                new=AsyncMock(),
            ),
            patch("app.services.delegation.log_delegation", new=AsyncMock()),
        ):
            task = await create_sub_agent_task(
                MagicMock(),
                user_id=str(uuid.uuid4()),
                parent_agent_id=str(uuid.uuid4()),
                target_agent_id=str(uuid.uuid4()),
                task_description="research the topic",
            )
    finally:
        lft.reset_current_trace_ctx(token)

    assert task is not None
    create_execution.assert_awaited_once()
    payload = create_execution.await_args.kwargs["payload"]
    assert payload["parent_trace_context"] == {
        "trace_id": "trace-parent",
        "parent_span_id": "span-parent",
    }


@pytest.mark.asyncio
async def test_create_sub_agent_task_payload_none_without_active_trace():
    """No active trace (Langfuse disabled / untraced path) → the payload
    carries ``parent_trace_context: None`` and creation succeeds."""
    from app.services.sub_agent import create_sub_agent_task

    db = _make_own_db()
    create_execution = AsyncMock()

    with (
        patch("app.core.database.async_session", lambda: _FakeSessionCM(db)),
        patch(
            "app.services.orchestration_engine.create_execution",
            new=create_execution,
        ),
        patch(
            "app.services.orchestration_engine.notify_orchestration_wake",
            new=AsyncMock(),
        ),
        patch("app.services.delegation.log_delegation", new=AsyncMock()),
    ):
        task = await create_sub_agent_task(
            MagicMock(),
            user_id=str(uuid.uuid4()),
            parent_agent_id=str(uuid.uuid4()),
            target_agent_id=str(uuid.uuid4()),
            task_description="untraced task",
        )

    assert task is not None
    payload = create_execution.await_args.kwargs["payload"]
    assert payload["parent_trace_context"] is None


# ---------------------------------------------------------------------------
# SubAgentAdapter: payload field forwarded to _execute_sub_agent_task
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_adapter_forwards_parent_trace_context_from_payload():
    from app.services.orchestration_adapters.sub_agent import SubAgentAdapter
    from app.services.sub_agent import SubAgentTaskOutcome

    serialized = {"trace_id": "trace-parent", "parent_span_id": "span-parent"}
    execution = MagicMock()
    execution.payload = {
        "task_id": str(uuid.uuid4()),
        "user_id": str(uuid.uuid4()),
        "target_agent_id": str(uuid.uuid4()),
        "task_description": "do the work",
        "parent_trace_context": serialized,
    }
    # The adapter's terminal-status guard queries the SubAgentTask before
    # running the body — answer with "no row" so execution proceeds.
    guard_result = MagicMock()
    guard_result.scalar_one_or_none = MagicMock(return_value=None)
    ctx = SimpleNamespace(
        db=MagicMock(
            execute=AsyncMock(return_value=guard_result), commit=AsyncMock()
        ),
        cancel_event=None,
    )

    execute_mock = AsyncMock(return_value=SubAgentTaskOutcome(status="completed"))
    with patch(
        "app.services.sub_agent._execute_sub_agent_task", new=execute_mock
    ):
        result = await SubAgentAdapter().execute(ctx, execution)

    assert result.kind == "completed"
    execute_mock.assert_awaited_once()
    assert execute_mock.await_args.kwargs["parent_trace_context"] == serialized


# ---------------------------------------------------------------------------
# _execute_sub_agent_task: rebuilt context passed to run_agent_chat
# ---------------------------------------------------------------------------


def _make_exec_db(task: MagicMock, agent: MagicMock) -> MagicMock:
    """Session mock whose sequential ``execute`` calls resolve the task, the
    caller's group memberships (three-tier visibility), the target agent, then
    the task again (result persistence).

    ``_update_task_delegation`` must be patched to a no-op alongside this
    mock — ``update_delegation`` issues its own ``db.execute`` calls, which
    would shift this side_effect queue, exhaust it before the result
    persistence query, and silently flip the outcome to ``failed``."""

    def _result(obj):
        res = MagicMock()
        res.scalar_one_or_none = MagicMock(return_value=obj)
        return res

    def _group_result():
        # ``get_user_group_ids`` reads ``result.scalars().all()``.
        res = MagicMock()
        res.scalars = MagicMock(return_value=MagicMock(all=MagicMock(return_value=[])))
        return res

    db = MagicMock()
    db.execute = AsyncMock(
        side_effect=[_result(task), _group_result(), _result(agent), _result(task)]
    )
    db.commit = AsyncMock()
    return db


def _make_task_and_agent() -> tuple[MagicMock, MagicMock]:
    task = MagicMock()
    task.conversation_id = None  # skip proactive delivery
    agent = MagicMock()
    agent.handover_agent_ids = []
    agent.disable_chat = True
    # Concrete int so ``resolve_chat_sub_agent_timeout`` does not issue an
    # extra GA lookup that would shift ``_make_exec_db``'s execute queue.
    agent.max_iterations = 25
    return task, agent


@pytest.mark.asyncio
async def test_execute_sub_agent_task_passes_parent_trace_ctx_to_run_agent_chat():
    from app.services.sub_agent import _execute_sub_agent_task

    task, agent = _make_task_and_agent()
    db = _make_exec_db(task, agent)
    run_agent_chat = AsyncMock(return_value=("all done", None))
    serialized = {"trace_id": "trace-parent", "parent_span_id": "span-parent"}

    with (
        patch("app.core.database.async_session", lambda: _FakeSessionCM(db)),
        patch("app.services.agent_chat.run_agent_chat", new=run_agent_chat),
        patch("app.services.sub_agent._update_task_delegation", new=AsyncMock()),
    ):
        outcome = await _execute_sub_agent_task(
            task_id=str(uuid.uuid4()),
            user_id=str(uuid.uuid4()),
            target_agent_id=str(uuid.uuid4()),
            task_description="background work",
            parent_trace_context=serialized,
        )

    # The happy path must genuinely complete — without this assertion the
    # test would still pass if the function errored after run_agent_chat
    # (its broad exception handler swallows and returns a failed outcome).
    assert outcome.status == "completed"
    run_agent_chat.assert_awaited_once()
    assert run_agent_chat.await_args.kwargs["parent_trace_ctx"] == serialized


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bad_context",
    [None, {}, {"parent_span_id": "span-only"}, "not-a-dict"],
)
async def test_execute_sub_agent_task_ignores_unusable_trace_context(bad_context):
    """Missing / malformed payload values degrade to ``parent_trace_ctx=None``
    (fresh root trace) instead of crashing the delegation."""
    from app.services.sub_agent import _execute_sub_agent_task

    task, agent = _make_task_and_agent()
    db = _make_exec_db(task, agent)
    run_agent_chat = AsyncMock(return_value=("all done", None))

    with (
        patch("app.core.database.async_session", lambda: _FakeSessionCM(db)),
        patch("app.services.agent_chat.run_agent_chat", new=run_agent_chat),
        patch("app.services.sub_agent._update_task_delegation", new=AsyncMock()),
    ):
        outcome = await _execute_sub_agent_task(
            task_id=str(uuid.uuid4()),
            user_id=str(uuid.uuid4()),
            target_agent_id=str(uuid.uuid4()),
            task_description="background work",
            parent_trace_context=bad_context,
        )

    assert outcome.status == "completed"
    run_agent_chat.assert_awaited_once()
    assert run_agent_chat.await_args.kwargs["parent_trace_ctx"] is None


@pytest.mark.asyncio
async def test_execute_sub_agent_task_keeps_a_local_only_trace_context():
    """Langfuse off at spawn time leaves only the local trace id: it is still
    the parent the task's durable trace must name."""
    from app.services.sub_agent import _execute_sub_agent_task

    task, agent = _make_task_and_agent()
    db = _make_exec_db(task, agent)
    run_agent_chat = AsyncMock(return_value=("all done", None))
    serialized = {"local_trace_id": str(uuid.uuid4())}

    with (
        patch("app.core.database.async_session", lambda: _FakeSessionCM(db)),
        patch("app.services.agent_chat.run_agent_chat", new=run_agent_chat),
        patch("app.services.sub_agent._update_task_delegation", new=AsyncMock()),
    ):
        outcome = await _execute_sub_agent_task(
            task_id=str(uuid.uuid4()),
            user_id=str(uuid.uuid4()),
            target_agent_id=str(uuid.uuid4()),
            task_description="background work",
            parent_trace_context=serialized,
        )

    assert outcome.status == "completed"
    run_agent_chat.assert_awaited_once()
    assert run_agent_chat.await_args.kwargs["parent_trace_ctx"] == serialized


# ---------------------------------------------------------------------------
# Durable trace parent: a consult and a delegated task name their caller's
# trace as ``parent_trace_id``
# ---------------------------------------------------------------------------


@pytest.fixture
def local_only_tracing(monkeypatch):
    """Langfuse off: ``trace_llm_call`` returns local-only contexts, and the
    trace ``end_trace`` hands the durable sink is captured instead of flushed."""

    async def no_client(_db):
        return None

    monkeypatch.setattr(lft, "get_langfuse_client", no_client)
    monkeypatch.setattr(lft, "_resolve_user_group_ids", AsyncMock(return_value=[]))
    flushed = MagicMock()
    monkeypatch.setattr(
        "app.services.tracing.dispatcher.submit_trace_for_flush", flushed
    )
    return flushed


async def _open_trace(name: str, parent_trace_ctx: dict | None = None) -> dict:
    ctx = await lft.trace_llm_call(
        db=MagicMock(),
        name=name,
        user_id=str(uuid.uuid4()),
        source="agent-chat",
        parent_trace_ctx=parent_trace_ctx,
    )
    assert ctx is not None
    return ctx


def _flushed_parent(flushed: MagicMock, trace_ctx: dict) -> uuid.UUID | None:
    """The ``parent_trace_id`` of the durable trace flushed for *trace_ctx*."""
    traces = [
        call.args[0]
        for call in flushed.call_args_list
        if call.args[0].id == trace_ctx["local_trace_id"]
    ]
    assert len(traces) == 1
    return traces[0].parent_trace_id


@pytest.mark.asyncio
async def test_consulted_helper_trace_names_the_caller_trace_as_parent(
    local_only_tracing,
):
    """A consult opens the helper's trace while the caller's ``consult_agent``
    tool span is current: that span's context carries no ``local_trace_id``
    of its own, and the parent used to be read as empty."""
    caller = await _open_trace("agent-chat:caller")
    caller_token = lft.set_current_trace_ctx(caller)
    try:
        iteration = lft.start_iteration_span(iteration=1)
        iteration_token = lft.set_current_trace_ctx(iteration)
        try:
            tool = lft.start_tool_span(tool_name="consult_agent")
            tool_token = lft.set_current_trace_ctx(tool)
            try:
                helper = await _open_trace("agent-chat:helper")
                lft.end_trace(helper, output="helper answer")
            finally:
                lft.reset_current_trace_ctx(tool_token)
        finally:
            lft.reset_current_trace_ctx(iteration_token)
    finally:
        lft.reset_current_trace_ctx(caller_token)
    lft.end_trace(caller, output="caller answer")

    assert helper["parent_ctx"] is tool
    assert _flushed_parent(local_only_tracing, helper) == caller["local_trace_id"]
    assert _flushed_parent(local_only_tracing, caller) is None


@pytest.mark.asyncio
async def test_transferred_target_trace_still_names_the_source_trace(
    local_only_tracing,
):
    """A transfer opens its target's trace from the source's trace context
    itself — the case that was already linked must stay linked."""
    source = await _open_trace("agent-chat:source")
    target = await _open_trace("agent-chat:target", parent_trace_ctx=source)
    lft.end_trace(target, output="target answer")

    assert _flushed_parent(local_only_tracing, target) == source["local_trace_id"]


def test_parent_falls_back_to_a_span_root_trace_id_without_its_chain():
    """A span context cut from its parent chain still names its trace."""
    root_id = uuid.uuid4()
    span_ctx = {
        "trace": None,
        "local_span_id": uuid.uuid4(),
        "local_root_trace_id": root_id,
    }
    assert lft._parent_local_trace_id(span_ctx) == root_id
    assert lft._parent_local_trace_id({"trace": None}) is None
    assert lft._parent_local_trace_id(None) is None


@pytest.mark.asyncio
async def test_delegated_task_trace_names_the_spawning_trace_as_parent(
    local_only_tracing,
):
    """A delegated task runs as its own execution: the context captured in
    the delegating tool's span (Langfuse off, so no Langfuse ids at all) must
    carry the spawning run's local trace id, and the task's trace, opened from
    the serialized context on the worker, must name it as its parent."""
    spawner = await _open_trace("agent-chat:spawner")
    spawner_token = lft.set_current_trace_ctx(spawner)
    try:
        tool = lft.start_tool_span(tool_name="delegate_task")
        tool_token = lft.set_current_trace_ctx(tool)
        try:
            captured = lft.capture_trace_context()
        finally:
            lft.reset_current_trace_ctx(tool_token)
    finally:
        lft.reset_current_trace_ctx(spawner_token)

    assert captured == {"local_trace_id": str(spawner["local_trace_id"])}

    task = await _open_trace("agent-chat:worker", parent_trace_ctx=captured)
    lft.end_trace(task, output="task result")

    assert _flushed_parent(local_only_tracing, task) == spawner["local_trace_id"]


def test_capture_trace_context_carries_the_local_trace_id_of_a_live_span():
    """With Langfuse on, the Langfuse ids of the current span ride along with
    the local id of the trace it belongs to."""
    local_id = uuid.uuid4()
    root_span = MagicMock()
    root_span.trace_id = "trace-abc"
    root_span.id = "root"
    tool_span = MagicMock()
    tool_span.trace_id = "trace-abc"
    tool_span.id = "tool"
    root = {"trace": root_span, "local_trace_id": local_id}
    tool = {
        "trace": tool_span,
        "parent_ctx": root,
        "local_span_id": uuid.uuid4(),
        "local_root_trace_id": local_id,
    }
    assert lft.capture_trace_context(tool) == {
        "trace_id": "trace-abc",
        "parent_span_id": "tool",
        "local_trace_id": str(local_id),
    }


def test_capture_trace_context_passes_a_serialized_local_trace_id_through():
    local_id = str(uuid.uuid4())
    serialized = {
        "trace_id": "trace-remote",
        "parent_span_id": "span-remote",
        "local_trace_id": local_id,
    }
    assert lft.capture_trace_context(serialized) == serialized
    assert lft.capture_trace_context({"local_trace_id": "not-a-uuid"}) is None
