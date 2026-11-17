"""Regression coverage for langfuse tracing local-span + dispatcher flow."""

from __future__ import annotations

import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services import langfuse_tracing as lft


@pytest.fixture
def stub_langfuse_module(monkeypatch):
    fake_propagate = MagicMock()
    fake_propagate.return_value.__enter__ = MagicMock(return_value=None)
    fake_propagate.return_value.__exit__ = MagicMock(return_value=False)
    monkeypatch.setitem(
        sys.modules,
        "langfuse",
        MagicMock(propagate_attributes=fake_propagate),
    )
    return fake_propagate


def _make_span(span_id="span-1", trace_id="trace-1"):
    span = MagicMock()
    span.id = span_id
    span.trace_id = trace_id
    span.start_observation = MagicMock(return_value=MagicMock(id="child-1", trace_id=trace_id))
    return span


@pytest.mark.asyncio
async def test_trace_llm_call_returns_local_span_context(stub_langfuse_module, monkeypatch):
    client = MagicMock()
    root_span = _make_span()
    generation = _make_span(span_id="gen-1", trace_id="trace-1")
    root_span.start_observation = MagicMock(return_value=generation)
    client.start_observation = MagicMock(return_value=root_span)
    monkeypatch.setattr(lft, "get_langfuse_client", AsyncMock(return_value=client))

    db = MagicMock()
    ctx = await lft.trace_llm_call(db, name="test", user_id="u1", source="test")

    assert ctx is not None
    assert "trace" in ctx
    assert "generation" in ctx
    assert "local_spans" in ctx
    assert isinstance(ctx["local_spans"], list)
    assert ctx["local_spans"] == []


@pytest.mark.asyncio
async def test_trace_llm_call_returns_local_context_when_langfuse_unavailable(monkeypatch):
    monkeypatch.setattr(lft, "get_langfuse_client", AsyncMock(return_value=None))
    monkeypatch.setattr(lft, "_resolve_user_group_ids", AsyncMock(return_value=[]))

    db = MagicMock()
    ctx = await lft.trace_llm_call(
        db,
        name="agent-chat:test-agent",
        user_id=str(lft.uuid.uuid4()),
        source="agent-chat",
    )

    assert ctx is not None
    assert ctx["trace"] is None
    assert ctx["generation"] is None
    assert ctx["client"] is None
    assert isinstance(ctx.get("local_trace_id"), lft.uuid.UUID)
    assert ctx["local_source"] == "agent-chat"
    assert isinstance(ctx.get("local_spans"), list)


def test_close_observation_appends_local_span_record():
    root = {
        "local_trace_id": lft.uuid.uuid4(),
        "local_spans": [],
        "local_span_counter": 0,
    }
    span = _make_span()
    ctx = {
        "trace": span,
        "metadata": {"observation_kind": "tool"},
        "parent_ctx": root,
        "local_span_id": "00000000-0000-0000-0000-000000000001",
        "local_span_type": "TOOL",
        "local_name": "tool:web_fetch",
        "local_started_at": lft.datetime.now(lft.UTC),
        "local_input": {"tool": "web_fetch"},
        "local_metadata": {"observation_kind": "tool"},
        "local_position": 1,
    }

    lft._close_observation(ctx, output="ok")

    span.end.assert_called()
    assert len(root["local_spans"]) == 1
    assert root["local_spans"][0]["name"] == "tool:web_fetch"
    assert root["local_spans"][0]["type"] == "TOOL"


def test_start_helpers_attach_local_context_chain():
    root_span = _make_span()
    root_ctx = {
        "trace": root_span,
        "local_trace_id": lft.uuid.uuid4(),
        "local_spans": [],
        "local_span_counter": 0,
    }

    iter_ctx = lft.start_iteration_span(iteration=1, parent_ctx=root_ctx)
    assert iter_ctx is not None
    assert "local_span_id" in iter_ctx

    gen_ctx = lft.start_llm_generation(iter_ctx, model="gpt-4")
    assert gen_ctx is not None
    assert gen_ctx.get("local_parent_span_id") == iter_ctx.get("local_span_id")

    tool_ctx = lft.start_tool_span(tool_name="web_fetch", parent_ctx=iter_ctx)
    assert tool_ctx is not None
    assert tool_ctx.get("local_parent_span_id") == iter_ctx.get("local_span_id")


def test_start_helpers_attach_local_context_chain_without_remote_span():
    root_ctx = {
        "trace": None,
        "local_trace_id": lft.uuid.uuid4(),
        "local_spans": [],
        "local_span_counter": 0,
    }

    iter_ctx = lft.start_iteration_span(iteration=1, parent_ctx=root_ctx)
    assert iter_ctx is not None
    assert iter_ctx.get("trace") is None

    gen_ctx = lft.start_llm_generation(iter_ctx, model="gpt-4")
    assert gen_ctx is not None
    assert gen_ctx.get("trace") is None
    assert gen_ctx.get("local_parent_span_id") == iter_ctx.get("local_span_id")

    tool_ctx = lft.start_tool_span(tool_name="web_fetch", parent_ctx=iter_ctx)
    assert tool_ctx is not None
    assert tool_ctx.get("trace") is None
    assert tool_ctx.get("local_parent_span_id") == iter_ctx.get("local_span_id")


def test_close_observation_appends_local_span_when_remote_span_missing():
    root = {
        "local_trace_id": lft.uuid.uuid4(),
        "local_spans": [],
        "local_span_counter": 0,
    }
    ctx = {
        "trace": None,
        "metadata": {"observation_kind": "tool"},
        "parent_ctx": root,
        "local_span_id": "00000000-0000-0000-0000-000000000002",
        "local_span_type": "TOOL",
        "local_name": "tool:web_fetch",
        "local_started_at": lft.datetime.now(lft.UTC),
        "local_input": {"tool": "web_fetch"},
        "local_metadata": {"observation_kind": "tool"},
        "local_position": 1,
    }

    lft._close_observation(ctx, output="ok")

    assert len(root["local_spans"]) == 1
    assert root["local_spans"][0]["name"] == "tool:web_fetch"
    assert root["local_spans"][0]["type"] == "TOOL"


def test_end_trace_submits_local_spans_without_remote_span():
    root_ctx = {
        "trace": None,
        "generation": None,
        "client": None,
        "parent_ctx": None,
        "metadata": {"source": "test"},
        "input_data": {"messages": []},
        "local_spans": [],
        "local_trace_id": lft.uuid.uuid4(),
        "local_source": "agent_chat",
        "local_name": "trace",
        "local_started_at": lft.datetime.now(lft.UTC),
        "local_metadata": {"source": "test"},
        "local_tags": [],
    }
    iter_ctx = lft.start_iteration_span(iteration=1, parent_ctx=root_ctx)
    assert iter_ctx is not None
    gen_ctx = lft.start_llm_generation(iter_ctx, model="gpt-4")
    assert gen_ctx is not None
    lft.end_generation(gen_ctx, output="ok", usage={"input_tokens": 3, "output_tokens": 2}, model="gpt-4")
    lft.end_span(iter_ctx, output="iteration done")

    with patch("app.services.tracing.dispatcher.submit_trace_for_flush") as submit_mock:
        lft.end_trace(root_ctx, output="done", usage={"input_tokens": 5, "output_tokens": 4}, model="gpt-4")

    submit_mock.assert_called_once()
    _trace_data, spans = submit_mock.call_args.args
    assert len(spans) >= 2


def test_end_trace_updates_span_and_submits_dispatch():
    span = _make_span()
    span.set_trace_io = MagicMock()
    ctx = {
        "trace": span,
        "generation": None,
        "client": MagicMock(),
        "parent_ctx": None,
        "metadata": {"source": "test"},
        "input_data": None,
        "local_spans": [],
        "local_trace_id": lft.uuid.uuid4(),
        "local_source": "agent_chat",
        "local_name": "trace",
        "local_started_at": lft.datetime.now(lft.UTC),
        "local_metadata": {"source": "test"},
        "local_tags": [],
    }

    with patch("app.services.tracing.dispatcher.submit_trace_for_flush") as submit_mock:
        lft.end_trace(ctx, output="done", usage={"input_tokens": 3, "output_tokens": 2}, model="gpt-4")

    span.update.assert_called()
    span.end.assert_called()
    submit_mock.assert_called_once()
    trace_data, spans = submit_mock.call_args.args
    assert trace_data.source == "agent_chat"
    assert spans == []


def test_start_memory_span_attaches_local_context_chain_without_remote_span():
    """Langfuse disabled → the MEMORY span still lands in the durable store,
    parented to the chat turn's root trace."""
    root_ctx = {
        "trace": None,
        "local_trace_id": lft.uuid.uuid4(),
        "local_spans": [],
        "local_span_counter": 0,
    }

    mem_ctx = lft.start_memory_span(
        short_term_enabled=True,
        medium_term_enabled=False,
        persistent_enabled=True,
        user_enabled=False,
        parent_ctx=root_ctx,
    )
    assert mem_ctx is not None
    assert mem_ctx.get("trace") is None
    assert mem_ctx.get("local_span_type") == "MEMORY"
    assert mem_ctx.get("local_root_trace_id") == root_ctx["local_trace_id"]

    lft.end_span(mem_ctx, output={"agent_memory_chars": 12})

    assert len(root_ctx["local_spans"]) == 1
    assert root_ctx["local_spans"][0]["type"] == "MEMORY"
    assert root_ctx["local_spans"][0]["name"] == "memory"


def test_update_trace_input_tools_patches_local_input_without_remote_span():
    """Progressive disclosure must reach the local-only trace too.

    Without this, an install running the durable trace store with Langfuse
    disabled kept the pre-gating catalogue on its root card forever — the
    misleading "N tools were sent" state this function exists to prevent.
    """
    ctx = {
        "trace": None,
        "local_trace_id": lft.uuid.uuid4(),
        "local_spans": [],
        "local_span_counter": 0,
        "input_data": {
            "system_prompt": "sp",
            "tools": [{"name": "pre_gating_catalogue", "description": "x"}],
        },
    }

    lft.update_trace_input_tools(ctx, [{"name": "load_tools", "description": "reveal"}])

    assert [t["name"] for t in ctx["input_data"]["tools"]] == ["load_tools"]
    assert [t["name"] for t in ctx["local_input"]["tools"]] == ["load_tools"]
    # Untouched fields survive the swap.
    assert ctx["local_input"]["system_prompt"] == "sp"


def test_update_trace_input_tools_no_op_without_span_or_local_root():
    """A ctx that is neither live nor local-backed stays a silent no-op."""
    ctx = {"trace": None, "input_data": {"tools": [{"name": "keep"}]}}
    lft.update_trace_input_tools(ctx, [{"name": "load_tools"}])
    assert [t["name"] for t in ctx["input_data"]["tools"]] == ["keep"]
    assert "local_input" not in ctx
