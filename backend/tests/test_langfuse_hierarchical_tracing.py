"""Tests for Phase 1 of hierarchical Langfuse tracing.

Covers:
- ``build_trace_input`` payload shape, truncation, and media stripping.
- ``set_current_trace_ctx`` / ``get_current_trace_ctx`` / ``reset_current_trace_ctx``
  contextvar lifecycle (including async-task isolation).
- ``trace_llm_call(parent_trace_ctx=...)`` opens the root span as a child
  observation of the parent span (Gap 1: nested traces for consult/transfer).
- The contextvar fallback path is honoured when ``parent_trace_ctx`` is not
  passed explicitly.
"""

from __future__ import annotations

import asyncio
import json
import sys
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.services import langfuse_tracing as lft


@pytest.fixture
def stub_langfuse_module(monkeypatch):
    """Patch ``sys.modules['langfuse']`` with a stub whose
    ``propagate_attributes`` returns a no-op context manager.

    ``trace_llm_call`` imports the ``langfuse`` module lazily to wrap its
    root-observation block; tests using this fixture exercise that path
    without requiring the real SDK.
    """
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
# build_trace_input
# ---------------------------------------------------------------------------


def test_build_trace_input_empty_returns_empty_dict():
    assert lft.build_trace_input() == {}


def test_build_trace_input_keeps_the_whole_system_prompt():
    """A clipped prompt is not auditable: the trace store is its only copy."""
    long = "x" * 120_000
    out = lft.build_trace_input(system_prompt=long)
    assert out["system_prompt"] == long
    assert "truncated" not in out["system_prompt"]


def test_build_trace_input_serialises_messages_from_chatmessage_objects():
    from app.llm.base import ChatMessage

    msgs = [
        ChatMessage(role="system", content="You are helpful"),
        ChatMessage(role="user", content="hi"),
    ]
    out = lft.build_trace_input(messages=msgs)
    assert out["messages"] == [
        {"role": "system", "content": "You are helpful"},
        {"role": "user", "content": "hi"},
    ]


def test_build_trace_input_accepts_pre_serialised_dicts():
    msgs = [{"role": "user", "content": "hi"}]
    out = lft.build_trace_input(messages=msgs)
    assert out["messages"] == msgs


def test_build_trace_input_keeps_long_message_content_whole():
    long = "y" * (lft._TRACE_MESSAGE_CONTENT_MAX + 50)
    out = lft.build_trace_input(messages=[{"role": "user", "content": long}])
    assert out["messages"][0]["content"] == long


def test_build_trace_input_records_tools_name_and_description():
    tool = MagicMock()
    tool.name = "search_kb"
    tool.description = "Search the knowledge base"
    out = lft.build_trace_input(tools=[tool])
    assert out["tools"] == [
        {"name": "search_kb", "description": "Search the knowledge base"}
    ]


def test_build_trace_input_keeps_long_tool_description_whole():
    """Tool descriptions drive model behaviour, so the trace records them fully."""
    tool = MagicMock()
    tool.name = "huge_tool"
    tool.description = "z" * 4_000
    out = lft.build_trace_input(tools=[tool])
    assert out["tools"][0]["description"] == "z" * 4_000


def test_build_trace_input_handles_openai_function_nested_tool_shape():
    """OpenAI-Chat-Completions tools land via ``/v1/chat/completions``
    (and any provider that mirrors the OpenAI tool schema) as

        {"type": "function", "function": {"name": ..., "description": ...}}

    rather than the top-level-keyed shape. The earlier implementation
    only matched ``t.get("name")`` / ``t.get("description")`` and would
    silently drop the entire catalogue from llm-proxy traces. Pin the
    nested-function lookup so a future refactor that drops it surfaces
    here before it ships.
    """
    tools = [
        {
            "type": "function",
            "function": {
                "name": "search_kb",
                "description": "Search the knowledge base",
                "parameters": {"type": "object"},
            },
        },
        # Confirm the top-level shape still resolves alongside the
        # nested one (existing internal callers must keep working).
        {"name": "list_users", "description": "List the users"},
    ]
    out = lft.build_trace_input(tools=tools)
    by_name = {t["name"]: t for t in out["tools"]}
    assert "search_kb" in by_name, (
        f"OpenAI nested function tool must be extracted; got {out['tools']!r}"
    )
    assert by_name["search_kb"]["description"] == "Search the knowledge base"
    assert "list_users" in by_name
    assert by_name["list_users"]["description"] == "List the users"


def test_build_trace_input_skips_tool_with_no_resolvable_name():
    """Tools with no ``name`` at any of the three lookup sites are
    silently dropped (rather than crashing with ``KeyError`` or
    landing in the trace with an empty name). Catches a regression
    where the nested-function lookup accidentally surfaces non-tool
    entries (e.g. tool-choice constraints, retrieval definitions).
    """
    tools = [
        {"type": "function", "function": {"description": "No name here"}},
        {"description": "Also nameless"},
        {"name": "ok_tool", "description": "Has a name"},
    ]
    out = lft.build_trace_input(tools=tools)
    names = [t["name"] for t in out["tools"]]
    assert names == ["ok_tool"], (
        f"only nameable tools must reach the trace catalogue; got {names!r}"
    )


def test_build_trace_input_extra_merges_without_overriding():
    out = lft.build_trace_input(
        messages=[{"role": "user", "content": "hi"}],
        extra={"agent": "Acme", "messages": "should-not-overwrite"},
    )
    assert out["agent"] == "Acme"
    # ``messages`` key already populated; extra must not overwrite
    assert out["messages"] == [{"role": "user", "content": "hi"}]


def test_build_trace_input_preserves_media_structure_for_downstream_stripping():
    """``build_trace_input`` does NOT strip media — that's the job of
    ``_strip_media_content``, called later by ``trace_llm_call``. This
    test just guards that the structure passes through unchanged AND
    remains compatible with the downstream sanitiser."""
    msgs = [
        {
            "role": "user",
            "content": [{"type": "image_url", "image_url": {"url": "data:..."}}],
        }
    ]
    out = lft.build_trace_input(messages=msgs)
    # Raw structure is preserved (no stripping happens here).
    assert out["messages"][0]["content"][0]["type"] == "image_url"
    # Downstream sanitiser handles it without raising.
    safe = lft._strip_media_content(out)
    assert safe["messages"][0]["content"][0]["type"] == "text"


# ---------------------------------------------------------------------------
# update_trace_input_tools — tools-only swap preserving other input fields
# ---------------------------------------------------------------------------


def test_update_trace_input_tools_swaps_tools_preserving_other_fields():
    """Progressive disclosure rewrites only the ``tools`` entry of the root
    trace card — messages / system_prompt / extra fields must survive so the
    card stays coherent while reflecting what the model was actually offered."""
    span = MagicMock()
    base = {
        "system_prompt": "sp",
        "messages": [{"role": "user", "content": "hi"}],
        "tools": [{"name": "old_full_catalogue", "description": "x"}],
        "agent": "Acme",
    }
    ctx: dict = {"trace": span, "input_data": dict(base)}

    lft.update_trace_input_tools(
        ctx, [{"name": "load_tools", "description": "reveal groups"}]
    )

    span.set_trace_io.assert_called_once()
    sent = span.set_trace_io.call_args.kwargs["input"]
    assert [t["name"] for t in sent["tools"]] == ["load_tools"]
    # Everything else preserved.
    assert sent["system_prompt"] == "sp"
    assert sent["messages"] == [{"role": "user", "content": "hi"}]
    assert sent["agent"] == "Acme"
    # Stash kept in sync so a later reveal swaps on top of this payload.
    assert [t["name"] for t in ctx["input_data"]["tools"]] == ["load_tools"]


def test_update_trace_input_tools_no_op_without_stashed_input():
    """No stashed ``input_data`` (e.g. a trace opened before the stash field
    existed) → silent no-op, no span write (can't safely rebuild the input)."""
    span = MagicMock()
    ctx: dict = {"trace": span}
    lft.update_trace_input_tools(ctx, [{"name": "x"}])
    span.set_trace_io.assert_not_called()


def test_update_trace_input_tools_no_op_paths():
    """Defensive: missing ctx / span are silent no-ops (never raise)."""
    lft.update_trace_input_tools(None, [{"name": "x"}])
    lft.update_trace_input_tools({"trace": None, "input_data": {}}, [{"name": "x"}])


# ---------------------------------------------------------------------------
# enrich_trace_metadata — merge semantics on a v4-replace-style update()
# ---------------------------------------------------------------------------


def test_enrich_trace_metadata_merges_with_stashed_creation_metadata():
    """``enrich_trace_metadata`` MUST merge into the stashed creation
    dict before calling ``span.update`` — Langfuse v4 treats
    ``update(metadata=...)`` as a *replace*, not a merge.

    A regression that calls ``span.update(metadata=metadata)`` with
    only the new keys would wipe the ``source / user_id / session_id /
    instance / agent / assignment`` fields set at trace creation and
    break filterability in the Langfuse UI for every assignment
    workflow (the canonical caller).
    """
    span = MagicMock()
    creation_meta = {
        "source": "assignment",
        "user_id": "u-1",
        "session_id": "s-1",
        "instance": "prod",
        "agent": "Acme",
        "assignment": "Daily digest",
        "observation_kind": "agent_loop",
    }
    ctx: dict = {"trace": span, "metadata": dict(creation_meta)}

    lft.enrich_trace_metadata(
        ctx,
        {"has_tools": True, "tool_count": 4, "max_iterations": 8},
    )

    span.update.assert_called_once()
    _, kwargs = span.update.call_args
    sent = kwargs["metadata"]
    # Creation-time keys MUST survive the enrichment call.
    for key, value in creation_meta.items():
        assert sent[key] == value, (
            f"creation key {key!r} must survive enrich_trace_metadata "
            f"(Langfuse v4 update() replaces, so we have to merge); "
            f"got metadata={sent}"
        )
    # New keys must land on top.
    assert sent["has_tools"] is True
    assert sent["tool_count"] == 4
    assert sent["max_iterations"] == 8


def test_enrich_trace_metadata_updates_stashed_dict_in_place():
    """The ctx-stash MUST be updated so subsequent merges (``end_trace``
    usage rollup, additional enrichment calls) see the cumulative dict
    instead of re-applying creation-only state on top of fresher keys.

    A regression that updates the span but leaves ``ctx['metadata']``
    pointing at the original creation-time dict would silently revert
    enrichment when ``end_trace`` rebuilds the metadata for the usage
    rollup (``end_trace`` reads ``ctx.get('metadata')``).
    """
    ctx: dict = {
        "trace": MagicMock(),
        "metadata": {"source": "assignment", "agent": "Acme"},
    }
    lft.enrich_trace_metadata(ctx, {"tool_count": 3})

    # Stashed dict reflects the merged state.
    assert ctx["metadata"]["source"] == "assignment"
    assert ctx["metadata"]["agent"] == "Acme"
    assert ctx["metadata"]["tool_count"] == 3


def test_enrich_trace_metadata_new_keys_take_precedence_on_conflict():
    """When the enrichment dict and the stash share a key, the new
    value wins.  Enrichment is the canonical source for values only
    known after trace creation (e.g. an updated ``max_iterations``
    resolved against the agent + assignment policy), so a stash-wins
    merge order would silently keep the placeholder.
    """
    ctx: dict = {
        "trace": MagicMock(),
        "metadata": {"max_iterations": -1, "agent": "Acme"},
    }
    lft.enrich_trace_metadata(ctx, {"max_iterations": 8})

    assert ctx["metadata"]["max_iterations"] == 8
    assert ctx["metadata"]["agent"] == "Acme"


def test_enrich_trace_metadata_no_op_paths():
    """Defensive: callers can hand us a missing ctx, a ctx without a
    span, or empty enrichment metadata — each path is a silent no-op
    so the production call site doesn't need to branch around the
    helper.
    """
    # None ctx
    lft.enrich_trace_metadata(None, {"x": 1})
    # Empty ctx
    lft.enrich_trace_metadata({}, {"x": 1})
    # ctx without a trace span
    lft.enrich_trace_metadata({"metadata": {}}, {"x": 1})
    # Empty metadata
    span = MagicMock()
    lft.enrich_trace_metadata({"trace": span, "metadata": {}}, None)
    lft.enrich_trace_metadata({"trace": span, "metadata": {}}, {})
    # span.update must never have been called on the no-op paths.
    span.update.assert_not_called()


# ---------------------------------------------------------------------------
# ContextVar lifecycle
# ---------------------------------------------------------------------------


def test_current_trace_ctx_default_is_none():
    # Run in a fresh context so an unrelated test cannot leak into this one.
    import contextvars

    ctx = contextvars.copy_context()
    assert ctx.run(lft.get_current_trace_ctx) is None


def test_set_and_reset_current_trace_ctx_roundtrip():
    fake = {"trace": "stub-root", "generation": "stub-gen"}
    token = lft.set_current_trace_ctx(fake)
    try:
        assert lft.get_current_trace_ctx() is fake
    finally:
        lft.reset_current_trace_ctx(token)
    assert lft.get_current_trace_ctx() is None


def test_nested_set_restores_parent_on_reset():
    parent = {"label": "parent"}
    child = {"label": "child"}
    t1 = lft.set_current_trace_ctx(parent)
    try:
        assert lft.get_current_trace_ctx() is parent
        t2 = lft.set_current_trace_ctx(child)
        try:
            assert lft.get_current_trace_ctx() is child
        finally:
            lft.reset_current_trace_ctx(t2)
        assert lft.get_current_trace_ctx() is parent
    finally:
        lft.reset_current_trace_ctx(t1)
    assert lft.get_current_trace_ctx() is None


@pytest.mark.asyncio
async def test_contextvar_isolation_across_concurrent_tasks():
    """Each asyncio task gets its own copy of the contextvar."""

    async def child(label: str) -> str | None:
        lft.set_current_trace_ctx({"label": label})
        await asyncio.sleep(0)
        snapshot = lft.get_current_trace_ctx()
        return snapshot["label"] if snapshot else None

    a, b = await asyncio.gather(child("alpha"), child("beta"))
    assert {a, b} == {"alpha", "beta"}
    # Parent task contextvar is untouched
    assert lft.get_current_trace_ctx() is None


# ---------------------------------------------------------------------------
# trace_llm_call: parent_trace_ctx + contextvar fallback
# ---------------------------------------------------------------------------


def _make_fake_client(span_id: str = "root-1", gen_id: str = "gen-1") -> MagicMock:
    """Build a MagicMock that mimics the surface area we touch on Langfuse."""
    root_span = MagicMock()
    root_span.id = span_id
    root_span.trace_id = f"trace-{span_id}"

    generation = MagicMock()
    generation.id = gen_id

    # root_span.start_observation(...) returns the generation
    root_span.start_observation = MagicMock(return_value=generation)

    client = MagicMock()
    client.start_observation = MagicMock(return_value=root_span)
    return client


@pytest.mark.asyncio
async def test_trace_llm_call_uses_client_when_no_parent(monkeypatch, stub_langfuse_module):
    fake_client = _make_fake_client()

    async def fake_get_client(_db):
        return fake_client

    monkeypatch.setattr(lft, "get_langfuse_client", fake_get_client)
    monkeypatch.setattr(lft, "_resolve_user_group_ids", AsyncMock(return_value=[]))

    ctx = await lft.trace_llm_call(
        db=MagicMock(),
        name="agent-chat",
        user_id="u",
        source="test",
    )

    assert ctx is not None
    assert ctx["parent_ctx"] is None
    fake_client.start_observation.assert_called_once()
    # Phase 2: no outer generation is created at trace start —
    # per-iteration generations live inside the agent loop instead.
    assert ctx.get("generation") is None
    ctx["trace"].start_observation.assert_not_called()


@pytest.mark.asyncio
async def test_trace_llm_call_nests_under_explicit_parent(monkeypatch, stub_langfuse_module):
    """When parent_trace_ctx is supplied, root span is opened on the PARENT span,
    not on the client — proving consult/transfer chains nest correctly."""
    parent_root = MagicMock()
    parent_root.id = "parent-root"
    parent_root.trace_id = "trace-parent"
    parent_generation_for_child = MagicMock(id="child-gen")
    parent_child_span = MagicMock()
    parent_child_span.id = "child-root"
    parent_child_span.trace_id = "trace-parent"
    parent_child_span.start_observation = MagicMock(
        return_value=parent_generation_for_child
    )
    parent_root.start_observation = MagicMock(return_value=parent_child_span)

    parent_ctx = {"trace": parent_root, "generation": MagicMock(), "client": MagicMock()}

    fake_client = _make_fake_client()

    async def fake_get_client(_db):
        return fake_client

    monkeypatch.setattr(lft, "get_langfuse_client", fake_get_client)
    monkeypatch.setattr(lft, "_resolve_user_group_ids", AsyncMock(return_value=[]))

    ctx = await lft.trace_llm_call(
        db=MagicMock(),
        name="agent-chat-child",
        user_id="u",
        source="test",
        parent_trace_ctx=parent_ctx,
    )

    assert ctx is not None
    assert ctx["parent_ctx"] is parent_ctx
    # Client.start_observation MUST NOT be called — we nested under the parent.
    fake_client.start_observation.assert_not_called()
    parent_root.start_observation.assert_called_once()
    # Returned root span is the child span we created off the parent root.
    assert ctx["trace"] is parent_child_span
    # The whole point of Phase 1: the child observation lives on the SAME
    # Langfuse trace as the parent so the orchestration chain renders as
    # a single trace in the UI.
    assert ctx["trace"].trace_id == parent_root.trace_id


@pytest.mark.asyncio
async def test_trace_llm_call_picks_up_contextvar_when_no_explicit_parent(monkeypatch, stub_langfuse_module):
    """If parent_trace_ctx is omitted, fall back to the current contextvar."""
    parent_root = MagicMock()
    parent_root.id = "ctxvar-parent"
    parent_root.trace_id = "trace-ctxvar"
    nested_gen = MagicMock(id="nested-gen")
    nested_span = MagicMock()
    nested_span.id = "nested-root"
    nested_span.trace_id = "trace-ctxvar"
    nested_span.start_observation = MagicMock(return_value=nested_gen)
    parent_root.start_observation = MagicMock(return_value=nested_span)

    parent_ctx = {"trace": parent_root, "generation": MagicMock()}

    fake_client = _make_fake_client()

    async def fake_get_client(_db):
        return fake_client

    monkeypatch.setattr(lft, "get_langfuse_client", fake_get_client)
    monkeypatch.setattr(lft, "_resolve_user_group_ids", AsyncMock(return_value=[]))

    token = lft.set_current_trace_ctx(parent_ctx)
    try:
        ctx = await lft.trace_llm_call(
            db=MagicMock(),
            name="agent-chat-child",
            user_id="u",
            source="test",
        )
    finally:
        lft.reset_current_trace_ctx(token)

    assert ctx is not None
    fake_client.start_observation.assert_not_called()
    parent_root.start_observation.assert_called_once()
    # Trace continuity: child observation shares the parent's trace_id.
    assert ctx["trace"].trace_id == parent_root.trace_id


@pytest.mark.asyncio
async def test_trace_llm_call_returns_local_context_when_client_unavailable(monkeypatch):
    async def no_client(_db):
        return None

    monkeypatch.setattr(lft, "get_langfuse_client", no_client)

    ctx = await lft.trace_llm_call(
        db=MagicMock(),
        name="x",
        user_id="u",
        source="s",
    )
    assert ctx is not None
    assert ctx["trace"] is None
    assert ctx["generation"] is None
    assert ctx["client"] is None
    assert ctx["local_trace_id"] is not None
    assert isinstance(ctx["local_spans"], list)
    assert ctx["local_status"] == "ok"


@pytest.mark.asyncio
async def test_trace_llm_call_caches_root_span_id_for_feedback_scoring(
    monkeypatch, stub_langfuse_module
):
    """Phase 2 removed the outer ``*-generation`` observation in favour
    of per-iteration generations created inside ``run_agent_loop``.
    Previously ``cache_chat_trace_ids`` was called with
    ``generation_id=None``, which degraded user feedback scoring
    (``_push_langfuse_feedback_score`` in ``api/v1/chat.py``) from
    ``observation_id``-targeted (granular, attaches to the specific
    observation) to ``trace_id``-targeted (coarse, attaches to the
    entire trace card).

    The fix caches the root span id under the historical
    ``generation_id`` key so feedback stays attached to a specific
    observation. The choice of root-span over per-iteration is
    deliberate: a chat turn has many iterations and picking any single
    one would be arbitrary, but the root span semantically wraps the
    whole turn's work and is what the user is rating when they hit
    👍/👎.

    A regression that re-introduces the ``None`` parameter (or any
    falsy value) trips here; ditto any future refactor that drops the
    root_span.id read.
    """
    fake_client = _make_fake_client(span_id="root-feedback-1")

    async def fake_get_client(_db):
        return fake_client

    cache_calls: list[dict[str, str | None]] = []

    async def fake_cache(session_id, trace_id, generation_id, local_trace_id):
        cache_calls.append(
            {
                "session_id": session_id,
                "trace_id": trace_id,
                "generation_id": generation_id,
                "local_trace_id": local_trace_id,
            }
        )

    monkeypatch.setattr(lft, "get_langfuse_client", fake_get_client)
    monkeypatch.setattr(lft, "_resolve_user_group_ids", AsyncMock(return_value=[]))
    monkeypatch.setattr(lft, "cache_chat_trace_ids", fake_cache)

    ctx = await lft.trace_llm_call(
        db=MagicMock(),
        name="agent-chat",
        user_id="u",
        session_id="conv-42",
        source="test",
    )

    assert ctx is not None
    assert len(cache_calls) == 1
    cached = cache_calls[0]
    assert cached["session_id"] == "conv-42"
    assert cached["trace_id"] == "trace-root-feedback-1"
    # The historical kwarg name is ``generation_id`` but the value
    # MUST be the root span id (not None, not the trace id) so
    # ``_push_langfuse_feedback_score`` can use it as
    # ``observation_id`` for granular feedback scoring.
    assert cached["generation_id"] == "root-feedback-1", (
        "cache_chat_trace_ids MUST receive the root span id (not None) "
        "so feedback scoring can target an observation rather than "
        "degrading to a trace-wide score; got generation_id="
        f"{cached['generation_id']!r}"
    )


@pytest.mark.asyncio
async def test_cache_chat_trace_ids_persists_observation_id_under_generation_id_key(
    monkeypatch,
):
    """Wire-format pin for the historical ``generation_id`` key in the
    Redis cache payload — ``langfuse_tracing.load_latest_langfuse_ids``
    reads exactly that key and writes ``langfuse_generation_id`` into
    message metadata, which the feedback consumer then uses as the
    Langfuse ``observation_id``. A regression that renames the cache
    key (or drops it when the value is truthy) would silently break the
    end-to-end feedback path.
    """
    stored: dict[str, str] = {}

    class _FakeRedis:
        async def set(self, key, value, ex=None):
            stored[key] = value

    async def fake_redis_client():
        return _FakeRedis()

    monkeypatch.setattr("app.core.redis.redis_client", fake_redis_client)

    await lft.cache_chat_trace_ids(
        session_id="conv-99",
        trace_id="trace-abc",
        generation_id="root-span-xyz",
    )

    assert "langfuse:chat:conv-99:latest" in stored
    payload = json.loads(stored["langfuse:chat:conv-99:latest"])
    assert payload["trace_id"] == "trace-abc"
    assert payload["generation_id"] == "root-span-xyz", (
        "cache key MUST be ``generation_id`` for wire-format compatibility "
        "with ``load_latest_langfuse_ids``"
    )


# ---------------------------------------------------------------------------
# Phase 2: per-iteration / per-tool spans
# ---------------------------------------------------------------------------


def _make_iter_ctx_with_span():
    """Return an (iter_ctx, mock_span) pair where mock_span.start_observation
    can be inspected by tests."""
    span = MagicMock()
    span.start_observation = MagicMock(return_value=MagicMock())
    return ({"trace": span, "generation": None, "parent_ctx": None, "client": None}, span)


def test_start_iteration_span_returns_none_without_parent():
    """No parent context (and no contextvar) → helper degrades gracefully."""
    out = lft.start_iteration_span(iteration=1)
    assert out is None


def test_start_iteration_span_opens_child_under_parent_ctx():
    parent_ctx, parent_span = _make_iter_ctx_with_span()
    iter_ctx = lft.start_iteration_span(
        iteration=3,
        available_tools=["search_kb", "fetch_url"],
        message_count=12,
        parent_ctx=parent_ctx,
    )
    assert iter_ctx is not None
    parent_span.start_observation.assert_called_once()
    kwargs = parent_span.start_observation.call_args.kwargs
    assert kwargs["name"] == "iteration-3"
    assert kwargs["as_type"] == "span"
    assert kwargs["input"] == {
        "iteration": 3,
        "available_tools": ["search_kb", "fetch_url"],
        "message_count": 12,
    }
    assert kwargs["metadata"]["iteration"] == 3
    assert kwargs["metadata"]["observation_kind"] == "iteration"
    assert iter_ctx["generation"] is None
    assert iter_ctx["parent_ctx"] is parent_ctx


def test_start_iteration_span_falls_back_to_contextvar():
    parent_ctx, parent_span = _make_iter_ctx_with_span()
    token = lft.set_current_trace_ctx(parent_ctx)
    try:
        iter_ctx = lft.start_iteration_span(iteration=1)
    finally:
        lft.reset_current_trace_ctx(token)
    assert iter_ctx is not None
    parent_span.start_observation.assert_called_once()


def test_start_llm_generation_opens_under_parent_span():
    iter_ctx, iter_span = _make_iter_ctx_with_span()
    # iter_ctx["trace"] is the iteration span itself; reuse its start_observation
    # to capture the generation creation call.
    gen_ctx = lft.start_llm_generation(
        iter_ctx,
        model="gpt-4o",
        messages=[{"role": "user", "content": "hi"}],
    )
    assert gen_ctx is not None
    iter_span.start_observation.assert_called_once()
    kwargs = iter_span.start_observation.call_args.kwargs
    assert kwargs["name"] == "llm_call"
    assert kwargs["as_type"] == "generation"
    assert kwargs["model"] == "gpt-4o"
    assert kwargs["metadata"]["observation_kind"] == "llm_generation"
    assert gen_ctx["trace"] is gen_ctx["generation"]


def test_start_llm_generation_returns_none_when_parent_ctx_missing():
    assert lft.start_llm_generation(None, model="x") is None


def test_start_tool_span_preserves_structured_tool_arguments():
    parent_ctx, parent_span = _make_iter_ctx_with_span()
    huge = {"query": "x" * (lft._TRACE_MESSAGE_CONTENT_MAX + 500)}
    tool_ctx = lft.start_tool_span(
        tool_name="search_kb",
        tool_call_id="call_42",
        arguments=huge,
        parent_ctx=parent_ctx,
    )
    assert tool_ctx is not None
    parent_span.start_observation.assert_called_once()
    kwargs = parent_span.start_observation.call_args.kwargs
    assert kwargs["name"] == "tool:search_kb"
    assert kwargs["as_type"] == "tool"
    # The external Langfuse export stays bounded: its ingestion rejects
    # oversized events, and that sink is not ours to uncap.
    assert kwargs["input"]["tool"] == "search_kb"
    assert kwargs["input"]["tool_call_id"] == "call_42"
    args_str = kwargs["input"]["arguments"]
    assert len(args_str) <= lft._TRACE_MESSAGE_CONTENT_MAX + 100
    assert "truncated" in args_str
    # The durable span payload keeps the object shape *and* the whole value: a
    # truncated tool call cannot be audited.
    local_args = tool_ctx["local_tool_arguments"]
    assert isinstance(local_args, dict)
    assert set(local_args) == {"query"}
    assert local_args["query"] == huge["query"]
    assert "truncated" not in local_args["query"]
    assert kwargs["metadata"]["tool_name"] == "search_kb"
    assert kwargs["metadata"]["tool_call_id"] == "call_42"
    assert kwargs["metadata"]["observation_kind"] == "tool"


def test_truncate_deep_bounds_nested_strings():
    # Generation output now carries a dict (content + OpenAI-schema tool_calls),
    # so truncation must reach every embedded string, not just a top-level one.
    limit = 50
    data = {
        "content": "c" * 200,
        "tool_calls": [
            {
                "id": "call_1",
                "type": "function",
                "function": {"name": "search", "arguments": "a" * 200},
            }
        ],
    }
    out = lft._truncate_deep(data, limit)
    assert out["content"].startswith("c" * limit)
    assert "truncated" in out["content"]
    args = out["tool_calls"][0]["function"]["arguments"]
    assert args.startswith("a" * limit)
    assert "truncated" in args
    # Structure + non-string scalars are preserved.
    assert out["tool_calls"][0]["id"] == "call_1"
    assert out["tool_calls"][0]["type"] == "function"


def test_end_generation_truncates_dict_output():
    # When the generation output is a dict (content + tool_calls), the close
    # path must deeply truncate it before span.update — the regression the
    # review flagged when output switched from a plain string to a dict.
    span = MagicMock()
    ctx = {"trace": span, "generation": span, "metadata": {}}
    big = "z" * (lft._TRACE_MESSAGE_CONTENT_MAX + 500)
    lft.end_generation(
        ctx,
        output={
            "content": big,
            "tool_calls": [
                {"id": "c1", "type": "function", "function": {"arguments": big}}
            ],
        },
    )
    span.update.assert_called_once()
    out = span.update.call_args.kwargs["output"]
    assert len(out["content"]) <= lft._TRACE_MESSAGE_CONTENT_MAX + 100
    assert "truncated" in out["content"]
    assert "truncated" in out["tool_calls"][0]["function"]["arguments"]


def test_start_tool_span_returns_none_without_parent():
    assert lft.start_tool_span(tool_name="x") is None


def test_start_continuation_generation_returns_none_without_parent():
    assert lft.start_continuation_generation(None, n=1, model="gpt-4o") is None
    assert lft.start_continuation_generation({}, n=1, model="gpt-4o") is None
    assert (
        lft.start_continuation_generation({"trace": None}, n=1, model="gpt-4o") is None
    )


def test_start_continuation_generation_opens_generation_under_iter_ctx():
    parent_ctx, parent_span = _make_iter_ctx_with_span()
    cont_ctx = lft.start_continuation_generation(
        parent_ctx,
        n=2,
        model="gpt-4o",
        delta_messages=[
            {"role": "assistant", "content": "partial..."},
            {"role": "user", "content": "continue"},
        ],
        prior_message_count=7,
    )

    assert cont_ctx is not None
    assert cont_ctx["parent_ctx"] is parent_ctx
    parent_span.start_observation.assert_called_once()
    kwargs = parent_span.start_observation.call_args.kwargs
    assert kwargs["name"] == "attempt:continuation:2"
    assert kwargs["as_type"] == "generation"
    assert kwargs["model"] == "gpt-4o"
    assert kwargs["metadata"]["observation_kind"] == "continuation"
    assert kwargs["metadata"]["attempt"] == 2
    assert kwargs["input"]["prior_message_count"] == 7
    assert kwargs["input"]["continuation_attempt"] == 2
    assert len(kwargs["input"]["messages"]) == 2


def test_start_empty_response_retry_generation_returns_none_without_parent():
    assert lft.start_empty_response_retry_generation(None, model="gpt-4o") is None
    assert lft.start_empty_response_retry_generation({}, model="gpt-4o") is None
    assert (
        lft.start_empty_response_retry_generation({"trace": None}, model="gpt-4o")
        is None
    )


def test_start_empty_response_retry_generation_opens_generation_under_iter_ctx():
    parent_ctx, parent_span = _make_iter_ctx_with_span()
    rescue_ctx = lft.start_empty_response_retry_generation(
        parent_ctx,
        model="gpt-4o",
        delta_messages=[{"role": "system", "content": "please respond"}],
        prior_message_count=12,
        metadata={"context_chars": 9001},
    )

    assert rescue_ctx is not None
    assert rescue_ctx["parent_ctx"] is parent_ctx
    parent_span.start_observation.assert_called_once()
    kwargs = parent_span.start_observation.call_args.kwargs
    assert kwargs["name"] == "attempt:empty_response_retry"
    assert kwargs["as_type"] == "generation"
    assert kwargs["model"] == "gpt-4o"
    assert kwargs["metadata"]["observation_kind"] == "empty_response_retry"
    assert kwargs["metadata"]["context_chars"] == 9001
    assert kwargs["input"]["prior_message_count"] == 12
    assert len(kwargs["input"]["messages"]) == 1


def test_start_activation_gate_generation_returns_none_without_parent():
    assert lft.start_activation_gate_generation(None, model="gpt-4o") is None
    assert lft.start_activation_gate_generation({}, model="gpt-4o") is None
    assert (
        lft.start_activation_gate_generation({"trace": None}, model="gpt-4o") is None
    )


def test_start_activation_gate_generation_opens_generation_under_parent():
    parent_ctx, parent_span = _make_iter_ctx_with_span()
    gate_ctx = lft.start_activation_gate_generation(
        parent_ctx,
        model="gpt-4o-mini",
        messages=[
            {"role": "system", "content": "Answer YES or NO."},
            {"role": "user", "content": "Should I run?"},
        ],
        metadata={"assignment": "triage"},
    )

    assert gate_ctx is not None
    assert gate_ctx["parent_ctx"] is parent_ctx
    parent_span.start_observation.assert_called_once()
    kwargs = parent_span.start_observation.call_args.kwargs
    assert kwargs["name"] == "activation-gate"
    assert kwargs["as_type"] == "generation"
    assert kwargs["model"] == "gpt-4o-mini"
    assert kwargs["metadata"]["observation_kind"] == "activation_gate"
    assert kwargs["metadata"]["assignment"] == "triage"
    assert len(kwargs["input"]["messages"]) == 2


def test_end_span_success_path_sets_output_and_usage_and_model():
    inner = MagicMock()
    ctx = {"trace": inner, "generation": None, "parent_ctx": None, "client": None}
    lft.end_span(ctx, output={"is_error": False, "content": "ok"}, usage={"input_tokens": 10}, model="gpt-4o")
    inner.update.assert_called_once()
    update_kwargs = inner.update.call_args.kwargs
    assert update_kwargs["output"] == {"is_error": False, "content": "ok"}
    assert update_kwargs["model"] == "gpt-4o"
    assert "usage_details" in update_kwargs
    inner.end.assert_called_once()


def test_normalize_usage_flattens_openai_token_details_like_langfuse():
    normalized = lft._normalize_usage(
        {
            "prompt_tokens": 5,
            "completion_tokens": 11,
            "total_tokens": 16,
            "prompt_tokens_details": {
                "cached_tokens": 2,
                "audio_tokens": 3,
            },
            "completion_tokens_details": {
                "text_tokens": 3,
                "audio_tokens": 4,
                "reasoning_tokens": 4,
            },
        }
    )

    assert normalized["input"] == 0
    assert normalized["output"] == 0
    assert normalized["total"] == 16
    assert normalized["input_cached_tokens"] == 2
    assert normalized["input_audio_tokens"] == 3
    assert normalized["output_text_tokens"] == 3
    assert normalized["output_audio_tokens"] == 4
    assert normalized["output_reasoning_tokens"] == 4


def test_end_span_error_path_sets_level_and_status_message():
    inner = MagicMock()
    ctx = {"trace": inner, "generation": None, "parent_ctx": None, "client": None}
    lft.end_span(ctx, error="timeout after 30s")
    update_kwargs = inner.update.call_args.kwargs
    assert update_kwargs.get("level") == "ERROR"
    assert "timeout" in update_kwargs.get("status_message", "")
    inner.end.assert_called_once()


# ---------------------------------------------------------------------------
# Facade helpers: end_generation / end_tool_span / _close_observation
# ---------------------------------------------------------------------------


def _make_simple_ctx():
    inner = MagicMock()
    ctx = {"trace": inner, "generation": None, "parent_ctx": None, "client": None}
    return ctx, inner


def test_end_generation_persists_stop_reason_in_metadata():
    ctx, inner = _make_simple_ctx()
    lft.end_generation(ctx, output={"content": "ok"}, model="gpt-4o", stop_reason="end_turn")
    update_kwargs = inner.update.call_args.kwargs
    assert update_kwargs.get("metadata", {}).get("stop_reason") == "end_turn"
    inner.end.assert_called_once()


def test_end_generation_propagates_level():
    ctx, inner = _make_simple_ctx()
    lft.end_generation(ctx, output={"content": ""}, model="gpt-4o", level="WARNING")
    update_kwargs = inner.update.call_args.kwargs
    assert update_kwargs.get("level") == "WARNING"
    inner.end.assert_called_once()


def test_end_tool_span_is_error_routes_through_error_field():
    ctx, inner = _make_simple_ctx()
    lft.end_tool_span(ctx, output="boom", is_error=True)
    update_kwargs = inner.update.call_args.kwargs
    assert update_kwargs.get("level") == "ERROR"
    assert "boom" in update_kwargs.get("status_message", "")
    inner.end.assert_called_once()


def test_end_tool_span_stamps_error_kind_metadata():
    ctx, inner = _make_simple_ctx()
    lft.end_tool_span(ctx, error="timed out", error_kind="timeout")
    update_kwargs = inner.update.call_args.kwargs
    assert update_kwargs.get("metadata", {}).get("error_kind") == "timeout"
    assert update_kwargs.get("level") == "ERROR"
    inner.end.assert_called_once()


def test_end_tool_span_success_path_no_error_no_kind():
    ctx, inner = _make_simple_ctx()
    lft.end_tool_span(ctx, output={"ok": True}, is_error=False)
    update_kwargs = inner.update.call_args.kwargs
    assert update_kwargs.get("output") == {"ok": True}
    assert "level" not in update_kwargs or update_kwargs.get("level") != "ERROR"
    inner.end.assert_called_once()


def test_end_span_coerces_exception_to_status_string():
    """Regression: ``end_span(error=<Exception>)`` used to silently swallow
    the error path because ``Exception[:N]`` raised TypeError inside a
    suppress block."""
    span = MagicMock()
    ctx = {"trace": span, "generation": None, "parent_ctx": None, "client": None}
    exc = ValueError("bad input")

    lft.end_span(ctx, error=exc)

    update_calls = span.update.call_args_list
    assert any(
        c.kwargs.get("level") == "ERROR" for c in update_calls
    )
    assert any(
        "ValueError" in (c.kwargs.get("status_message") or "")
        and "bad input" in (c.kwargs.get("status_message") or "")
        for c in update_calls
    )
    span.end.assert_called_once()


@pytest.mark.parametrize(
    "close_fn", [lft.end_span, lft.end_generation, lft.end_tool_span]
)
def test_all_close_paths_are_no_op_for_none_ctx(close_fn):
    """All three close paths must accept ``ctx=None`` without raising."""
    close_fn(None)
    close_fn(None, output="ignored")


@pytest.mark.parametrize("close_fn", [lft.end_span, lft.end_generation, lft.end_tool_span])
def test_all_close_paths_format_exception_identically(close_fn):
    """All three close paths must route through ``_close_observation`` and
    therefore set the same ERROR level + status_message format."""
    ctx, inner = _make_simple_ctx()
    if close_fn is lft.end_tool_span:
        close_fn(ctx, error="boom")
    else:
        close_fn(ctx, error="boom")
    update_kwargs = inner.update.call_args.kwargs
    assert update_kwargs.get("level") == "ERROR"
    assert "boom" in update_kwargs.get("status_message", "")
    inner.end.assert_called_once()


def test_close_observation_treats_empty_string_error_as_failure():
    """Regression: ``_close_observation`` used to gate the error path on
    ``if error:``, silently treating falsy-but-meaningful error values
    (e.g. ``end_tool_span(is_error=True, output="")`` which coerces the
    empty output into the ``error`` field) as success.  Must now route
    through the ERROR branch whenever ``error is not None``.
    """
    ctx, inner = _make_simple_ctx()
    lft.end_tool_span(ctx, output="", is_error=True)
    update_kwargs = inner.update.call_args.kwargs
    assert update_kwargs.get("level") == "ERROR"
    assert "status_message" in update_kwargs
    inner.end.assert_called_once()


def test_close_observation_merges_extra_metadata_with_creation_metadata():
    """Regression: ``_close_observation`` used to set
    ``update_kwargs["metadata"] = extra_metadata``, which overwrote
    creation-time metadata (e.g. ``observation_kind``, ``tool_name``)
    because the Langfuse SDK treats metadata updates as replace.
    Closing with ``extra_metadata={"error_kind": "..."}`` must merge
    with the metadata captured at ``start_tool_span`` time.
    """
    parent = MagicMock()
    inner = MagicMock()
    parent.start_observation.return_value = inner
    parent_ctx = {
        "trace": parent,
        "generation": None,
        "parent_ctx": None,
        "client": None,
    }

    tool_ctx = lft.start_tool_span(
        tool_name="my_tool",
        tool_call_id="call-1",
        arguments={"x": 1},
        parent_ctx=parent_ctx,
    )
    assert tool_ctx is not None
    # Creation metadata is captured on the ctx so close paths can merge.
    assert tool_ctx["metadata"]["observation_kind"] == "tool"
    assert tool_ctx["metadata"]["tool_name"] == "my_tool"

    # Success close without extra_metadata: no metadata replacement at all.
    lft.end_tool_span(tool_ctx, output={"ok": True})
    update_kwargs = inner.update.call_args.kwargs
    assert "metadata" not in update_kwargs

    # Error close with extra_metadata: merged with creation metadata.
    inner.update.reset_mock()
    inner.end.reset_mock()
    tool_ctx2 = lft.start_tool_span(
        tool_name="my_tool",
        tool_call_id="call-2",
        arguments={"x": 2},
        parent_ctx=parent_ctx,
    )
    assert tool_ctx2 is not None
    lft.end_tool_span(tool_ctx2, error="boom", error_kind="timeout")
    err_kwargs = inner.update.call_args.kwargs
    meta = err_kwargs.get("metadata") or {}
    assert meta.get("error_kind") == "timeout"
    assert meta.get("observation_kind") == "tool"
    assert meta.get("tool_name") == "my_tool"


# ---------------------------------------------------------------------------
# Instance identity stamped on every trace (Round 8)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_trace_llm_call_stamps_instance_identity_on_root_span(
    monkeypatch, stub_langfuse_module
):
    """Every Langfuse trace must carry instance_host / platform_name /
    platform_mode / app_version so traces collected on a shared
    Langfuse workspace (one project, many customer installs) can be
    filtered per deployment."""
    fake_client = _make_fake_client()

    async def fake_get_client(_db):
        return fake_client

    monkeypatch.setattr(lft, "get_langfuse_client", fake_get_client)
    monkeypatch.setattr(
        lft, "_resolve_user_group_ids", AsyncMock(return_value=[])
    )
    monkeypatch.setattr(lft.settings, "frontend_url", "https://copilot.acme.com/app")
    monkeypatch.setattr(lft.settings, "platform_mode", "xtm_one")
    monkeypatch.setattr(lft.settings, "app_version", "1.2.3")

    fake_branding = MagicMock()
    fake_branding.name = "ACME XTM"
    fake_branding.instance_id = "9fc28f0b-aa57-41ff-b52a-a0a45d762fbf"
    monkeypatch.setattr(lft, "get_cached_branding", lambda: fake_branding)

    ctx = await lft.trace_llm_call(
        db=MagicMock(),
        name="agent-chat",
        user_id="u",
        source="test",
    )
    assert ctx is not None

    # metadata on the root span carries the 5 instance keys
    meta = fake_client.start_observation.call_args.kwargs["metadata"]
    assert meta["instance_id"] == "9fc28f0b-aa57-41ff-b52a-a0a45d762fbf"
    assert meta["instance_host"] == "copilot.acme.com"
    assert meta["platform_name"] == "ACME XTM"
    assert meta["platform_mode"] == "xtm_one"
    assert meta["app_version"] == "1.2.3"

    # Tags are intentionally lean — only the canonical instance UUID is
    # promoted to a chip; human-readable labels stay in metadata to avoid
    # cluttering Langfuse's tag filter row.
    tags = stub_langfuse_module.call_args.kwargs["tags"]
    assert "instance:9fc28f0b-aa57-41ff-b52a-a0a45d762fbf" in tags
    assert not any(t.startswith("instance_host:") for t in tags)
    assert not any(t.startswith("platform_name:") for t in tags)
    assert not any(t.startswith("platform:") for t in tags)
    assert not any(t.startswith("version:") for t in tags)


@pytest.mark.asyncio
async def test_trace_llm_call_falls_back_to_unknown_host_on_malformed_url(
    monkeypatch, stub_langfuse_module
):
    """Malformed / empty ``frontend_url`` must not crash trace startup
    — degrade to ``instance_host='unknown'`` instead."""
    fake_client = _make_fake_client()

    async def fake_get_client(_db):
        return fake_client

    monkeypatch.setattr(lft, "get_langfuse_client", fake_get_client)
    monkeypatch.setattr(
        lft, "_resolve_user_group_ids", AsyncMock(return_value=[])
    )
    monkeypatch.setattr(lft.settings, "frontend_url", "")
    monkeypatch.setattr(lft.settings, "platform_mode", "dev")
    monkeypatch.setattr(lft.settings, "app_version", "0.0.0-dev")

    ctx = await lft.trace_llm_call(
        db=MagicMock(),
        name="agent-chat",
        user_id="u",
        source="test",
    )
    assert ctx is not None
    meta = fake_client.start_observation.call_args.kwargs["metadata"]
    assert meta["instance_host"] == "unknown"
    tags = stub_langfuse_module.call_args.kwargs["tags"]
    # instance_host stays in metadata only — tags carry the canonical
    # instance_id UUID, not the host fallback.
    assert not any(t.startswith("instance_host:") for t in tags)


@pytest.mark.asyncio
async def test_end_trace_merges_total_usage_with_creation_metadata(
    monkeypatch, stub_langfuse_module
):
    """``end_trace`` writes ``total_usage`` onto the root span.

    Langfuse v4's ``span.update(metadata=...)`` is a *replace* operation,
    so writing ``{"total_usage": ...}`` directly would wipe ``source`` /
    ``instance_id`` / ``agent_*`` / ``assignment_*`` and break per-deployment
    + per-agent filtering in the Langfuse UI.
    """
    fake_client = _make_fake_client()

    async def fake_get_client(_db):
        return fake_client

    monkeypatch.setattr(lft, "get_langfuse_client", fake_get_client)
    monkeypatch.setattr(
        lft,
        "_resolve_user_group_ids",
        AsyncMock(return_value=["group-a", "group-b"]),
    )
    monkeypatch.setattr(
        lft.settings, "frontend_url", "https://copilot.acme.com/app"
    )
    monkeypatch.setattr(lft.settings, "platform_mode", "xtm_one")
    monkeypatch.setattr(lft.settings, "app_version", "1.2.3")

    fake_branding = MagicMock()
    fake_branding.name = "ACME XTM"
    fake_branding.instance_id = "9fc28f0b-aa57-41ff-b52a-a0a45d762fbf"
    monkeypatch.setattr(lft, "get_cached_branding", lambda: fake_branding)

    ctx = await lft.trace_llm_call(
        db=MagicMock(),
        name="agent-chat",
        user_id="u",
        source="test",
        agent_id="agent-42",
    )
    assert ctx is not None

    # End the trace with usage — should trigger the root-span metadata
    # update with the merged dict.
    lft.end_trace(
        ctx,
        output="hello",
        usage={"prompt_tokens": 10, "completion_tokens": 5},
        model="gpt-4o",
    )

    # Find the metadata-bearing update call (there may be multiple
    # update() calls on root_span for output/status, only one of which
    # carries metadata=).
    root_span = ctx["trace"]
    metadata_updates = [
        call.kwargs["metadata"]
        for call in root_span.update.call_args_list
        if "metadata" in call.kwargs
    ]
    assert len(metadata_updates) == 1, (
        f"expected exactly one metadata update on end_trace, got "
        f"{len(metadata_updates)}: {metadata_updates}"
    )
    merged = metadata_updates[0]

    # total_usage rollup is present
    assert "total_usage" in merged
    assert merged["total_usage"]["input"] == 10
    assert merged["total_usage"]["output"] == 5

    # AND the creation-time keys survive the merge (regression: previously
    # the dict was replaced wholesale, wiping these).
    assert merged["source"] == "test"
    assert merged["instance_id"] == "9fc28f0b-aa57-41ff-b52a-a0a45d762fbf"
    assert merged["instance_host"] == "copilot.acme.com"
    assert merged["platform_name"] == "ACME XTM"
    assert merged["platform_mode"] == "xtm_one"
    assert merged["app_version"] == "1.2.3"
    assert merged["agent_id"] == "agent-42"
    assert merged["user_id"] == "u"
    assert merged["group_ids"] == ["group-a", "group-b"]


# ---------------------------------------------------------------------------
# serialize_file_refs — file provenance on trace payloads
# ---------------------------------------------------------------------------


def test_serialize_file_refs_serializes_metadata_only():
    ref = SimpleNamespace(
        file_id="f-1",
        filename="report.pdf",
        content_type="application/pdf",
        size=1234,
    )
    out = lft.serialize_file_refs([ref])
    assert out == [
        {
            "file_id": "f-1",
            "filename": "report.pdf",
            "content_type": "application/pdf",
            "size": 1234,
        }
    ]


def test_serialize_file_refs_skips_entries_without_id_or_name():
    missing_id = SimpleNamespace(
        file_id="", filename="b.txt", content_type="text/plain", size=1
    )
    missing_name = SimpleNamespace(
        file_id="f-2", filename="", content_type="text/plain", size=1
    )
    good = SimpleNamespace(
        file_id="f-1", filename="a.txt", content_type="text/plain", size=1
    )
    out = lft.serialize_file_refs([missing_id, missing_name, good])
    assert [f["file_id"] for f in out] == ["f-1"]


def test_serialize_file_refs_records_every_ref_and_handles_empty():
    """Dropping the tail would make the trace misreport which files a run touched."""
    many = [
        SimpleNamespace(file_id=f"f-{i}", filename=f"n{i}", content_type="t", size=1)
        for i in range(60)
    ]
    assert len(lft.serialize_file_refs(many)) == 60
    # An explicit limit is still honoured for callers that want a sample.
    assert len(lft.serialize_file_refs(many, limit=10)) == 10
    assert lft.serialize_file_refs(None) == []
    assert lft.serialize_file_refs([]) == []


# ---------------------------------------------------------------------------
# start_iteration_span — platform-action (agentic quota) marker
# ---------------------------------------------------------------------------


def _make_root_ctx_with_metadata(metadata: dict):
    """Root trace ctx (has ``local_trace_id``) whose span records
    ``start_observation`` calls for inspection."""
    span = MagicMock()
    span.start_observation = MagicMock(return_value=MagicMock())
    ctx = {
        "trace": span,
        "generation": None,
        "parent_ctx": None,
        "client": None,
        "local_trace_id": uuid.uuid4(),
        "metadata": metadata,
        "local_spans": [],
        "local_span_counter": 0,
    }
    return ctx, span


def test_start_iteration_span_marks_quota_action_on_first_iteration():
    parent_ctx, parent_span = _make_root_ctx_with_metadata(
        {"quota_actions_consumed": 1, "quota_reason": "charged"}
    )
    iter_ctx = lft.start_iteration_span(iteration=1, parent_ctx=parent_ctx)
    assert iter_ctx is not None
    kwargs = parent_span.start_observation.call_args.kwargs
    assert kwargs["metadata"]["quota_action_consumed"] is True


def test_start_iteration_span_leaves_later_iterations_unmarked():
    parent_ctx, parent_span = _make_root_ctx_with_metadata(
        {"quota_actions_consumed": 1, "quota_reason": "charged"}
    )
    lft.start_iteration_span(iteration=2, parent_ctx=parent_ctx)
    kwargs = parent_span.start_observation.call_args.kwargs
    assert "quota_action_consumed" not in kwargs["metadata"]


def test_start_iteration_span_unmarked_when_no_action_consumed():
    # Nested handover / exempt traces carry quota_actions_consumed = 0.
    parent_ctx, parent_span = _make_root_ctx_with_metadata(
        {"quota_actions_consumed": 0, "quota_reason": "metered_by_parent"}
    )
    lft.start_iteration_span(iteration=1, parent_ctx=parent_ctx)
    kwargs = parent_span.start_observation.call_args.kwargs
    assert "quota_action_consumed" not in kwargs["metadata"]


def test_start_iteration_span_unmarked_without_root_metadata():
    parent_ctx, parent_span = _make_iter_ctx_with_span()
    lft.start_iteration_span(iteration=1, parent_ctx=parent_ctx)
    kwargs = parent_span.start_observation.call_args.kwargs
    assert "quota_action_consumed" not in kwargs["metadata"]


# ---------------------------------------------------------------------------
# MEMORY span (agent-chat memory load)
# ---------------------------------------------------------------------------


def _memory_kwargs(**overrides):
    """All four tiers enabled unless a test turns one off."""
    return {
        "short_term_enabled": True,
        "medium_term_enabled": True,
        "persistent_enabled": True,
        "user_enabled": True,
        **overrides,
    }


def test_start_memory_span_returns_none_without_parent():
    """No parent context (and no contextvar) → helper degrades gracefully."""
    assert lft.start_memory_span(**_memory_kwargs()) is None


def test_start_memory_span_returns_none_when_no_tier_enabled():
    """Evaluation mode zeroes every tier; an all-false input with an all-zero
    output is noise in the span tree, so no span is opened at all."""
    parent_ctx, parent_span = _make_iter_ctx_with_span()
    out = lft.start_memory_span(
        short_term_enabled=False,
        medium_term_enabled=False,
        persistent_enabled=False,
        user_enabled=False,
        parent_ctx=parent_ctx,
    )
    assert out is None
    parent_span.start_observation.assert_not_called()


def test_start_memory_span_opens_child_under_parent_ctx():
    parent_ctx, parent_span = _make_iter_ctx_with_span()
    mem_ctx = lft.start_memory_span(
        **_memory_kwargs(medium_term_enabled=False, user_enabled=False),
        parent_ctx=parent_ctx,
    )
    assert mem_ctx is not None
    parent_span.start_observation.assert_called_once()
    kwargs = parent_span.start_observation.call_args.kwargs
    assert kwargs["name"] == "memory"
    assert kwargs["as_type"] == "span"
    # Which tiers are enabled — never the loaded memory text itself.
    assert kwargs["input"] == {
        "short_term_enabled": True,
        "medium_term_enabled": False,
        "persistent_enabled": True,
        "user_enabled": False,
    }
    assert kwargs["metadata"]["observation_kind"] == "memory"
    assert mem_ctx["generation"] is None
    assert mem_ctx["parent_ctx"] is parent_ctx
    assert mem_ctx["local_span_type"] == "MEMORY"


def test_start_memory_span_falls_back_to_contextvar():
    """The chat turn pushes its root trace onto the contextvar, so the span
    nests without the call site threading ``parent_ctx`` through."""
    parent_ctx, parent_span = _make_iter_ctx_with_span()
    token = lft.set_current_trace_ctx(parent_ctx)
    try:
        mem_ctx = lft.start_memory_span(**_memory_kwargs())
    finally:
        lft.reset_current_trace_ctx(token)
    assert mem_ctx is not None
    parent_span.start_observation.assert_called_once()
    assert mem_ctx["parent_ctx"] is parent_ctx


def test_start_memory_span_survives_sdk_failure_with_local_root():
    """A raising SDK still leaves a durable local span behind."""
    parent_span = MagicMock()
    parent_span.start_observation = MagicMock(side_effect=RuntimeError("boom"))
    parent_ctx = {
        "trace": parent_span,
        "local_trace_id": uuid.uuid4(),
        "local_spans": [],
        "local_span_counter": 0,
    }
    mem_ctx = lft.start_memory_span(**_memory_kwargs(), parent_ctx=parent_ctx)
    assert mem_ctx is not None
    assert mem_ctx["trace"] is None
    assert mem_ctx["local_span_type"] == "MEMORY"


def test_start_memory_span_returns_none_on_sdk_failure_without_local_root():
    parent_span = MagicMock()
    parent_span.start_observation = MagicMock(side_effect=RuntimeError("boom"))
    assert (
        lft.start_memory_span(
            **_memory_kwargs(),
            parent_ctx={"trace": parent_span, "generation": None, "client": None},
        )
        is None
    )


def test_end_span_records_memory_load_sizes():
    """The span's output carries per-tier sizes, so a reader can tell short-
    from medium-term even though the prompt block merges the two."""
    parent_ctx, _parent_span = _make_iter_ctx_with_span()
    mem_ctx = lft.start_memory_span(**_memory_kwargs(), parent_ctx=parent_ctx)
    assert mem_ctx is not None
    output = {
        "agent_memory_loaded": True,
        "agent_memory_chars": 512,
        "short_term_entries": 3,
        "short_term_chars": 210,
        "medium_term_chars": 302,
        "persistent_memory_loaded": False,
        "persistent_memory_chars": 0,
        "user_memory_loaded": True,
        "user_memory_chars": 64,
    }
    lft.end_span(mem_ctx, output=output)
    mem_ctx["trace"].update.assert_called_once()
    assert mem_ctx["trace"].update.call_args.kwargs["output"] == output
    mem_ctx["trace"].end.assert_called_once()


def test_end_span_marks_memory_span_failed_on_error():
    """The memory-load failure path closes the span instead of orphaning it."""
    root_ctx = {
        "trace": None,
        "local_trace_id": uuid.uuid4(),
        "local_spans": [],
        "local_span_counter": 0,
    }
    mem_ctx = lft.start_memory_span(**_memory_kwargs(), parent_ctx=root_ctx)
    assert mem_ctx is not None

    lft.end_span(mem_ctx, error=RuntimeError("redis down"))

    assert len(root_ctx["local_spans"]) == 1
    record = root_ctx["local_spans"][0]
    assert record["type"] == "MEMORY"
    assert record["name"] == "memory"
    assert record["level"] == "ERROR"
    assert "redis down" in record["status_message"]
