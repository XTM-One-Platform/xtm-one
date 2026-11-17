"""Tests for identical tool-call deduplication in the agent loop (CORE-6).

The loop keeps a per-run cache keyed by
``(tool_name, sha256(canonical_json(arguments)))`` holding the result of
SUCCESSFUL tool calls.  A repeated identical call is served from the cache
(with a marker + steering nudge) instead of re-executing the tool - bounding
the runaway cost of an LLM stuck calling the same tool with the same
arguments.  Polling tools are exempt and failed calls are never cached so
retries stay possible.  Dedup applies in the single-call sequential path,
the parallel gather path, and within one parallel batch.

Dedup eligibility is read/write aware (reuses the security-graded
``infer_tool_type`` classifier of ``services/tool_classification.py``):

* Only READ-classified, non-polling tools are dedup-eligible.
* WRITE-classified (mutating) tools are ALWAYS executed - an identical-arg
  replay must never be suppressed, or the real mutation is silently skipped
  while the model is told it succeeded.
* When a write tool actually runs, the whole read cache is invalidated - a
  read cached BEFORE the mutation would otherwise be served as a stale
  pre-mutation snapshot AFTER it.
"""

from __future__ import annotations

import pytest

from app.llm.base import BaseLLMProvider, ChatMessage, ChatResponse, FileRef
from app.tools import agent_loop as al
from app.tools.agent_loop import (
    _DEDUP_CACHE_MARKER,
    _DEDUP_CACHE_NUDGE,
    _MAX_NEAR_DUPLICATE_CALLS,
    _NEAR_DUPLICATE_NUDGE,
    DEDUP_EXEMPT_TOOLS,
    _dedup_key,
    _is_dedup_exempt,
    _is_write_tool,
)
from app.tools.base import (
    CallableToolExecutor,
    CompositeToolExecutor,
    ToolCall,
    ToolDefinition,
    ToolResult,
    declared_cosmetic_arguments,
    is_auth_or_permission_error,
    is_rate_limit_error,
)

_TINY_PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01"
    b"\x00\x00\x00\x01\x08\x02\x00\x00\x00\x90wS\xde"
    b"\x00\x00\x00\x0cIDATx\x9cc\xf8\xcf\xc0\x00\x00"
    b"\x03\x01\x01\x00\xc9\xfe\x92\xef\x00\x00\x00\x00IEND\xaeB`\x82"
)


class _ScriptedLLM(BaseLLMProvider):
    """Stub LLM returning a pre-scripted sequence of responses."""

    provider_name = "stub"
    context_window = 100_000

    def __init__(self, script: list[ChatResponse]) -> None:
        self._script = script
        self._idx = 0
        self.offered_tools: list[list[str]] = []

    async def chat(self, messages, model=None, tools=None, **kwargs):
        self.offered_tools.append([t.name for t in (tools or [])])
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
    """Neutralise Langfuse plumbing so the loop runs without real tracing."""
    monkeypatch.setattr(al, "_lf_start_llm_gen", lambda *a, **kw: None)
    monkeypatch.setattr(al, "_lf_end_generation", lambda *a, **kw: None)
    monkeypatch.setattr(al, "_lf_end_span", lambda *a, **kw: None)
    monkeypatch.setattr(al, "_lf_end_tool_span", lambda *a, **kw: None)
    monkeypatch.setattr(al, "_lf_start_iter_span", lambda **kw: None)
    monkeypatch.setattr(al, "_lf_start_tool_span", lambda **kw: None)


def _disable_loop_breaker(monkeypatch) -> None:
    """Switch the loop breaker off so a test can exercise the older,
    steering-only guards in isolation (the breaker refuses the fifth identical
    call by default, which these tests deliberately make)."""
    from app.core.config import settings

    for name in (
        "agent_loop_identical_call_warn_after",
        "agent_loop_identical_call_refuse_after",
        "agent_loop_similar_call_warn_after",
        "agent_loop_similar_call_refuse_after",
    ):
        monkeypatch.setattr(settings, name, 0)


def _executor_with(
    tool_names: list[str], *, failing: set[str] | None = None
) -> tuple[CompositeToolExecutor, list[str]]:
    """Composite executor with the given tools.  Returns the executor and a
    list recording every tool name actually executed."""
    executed: list[str] = []
    failing = failing or set()
    callable_exec = CallableToolExecutor()
    definitions = [
        ToolDefinition(name=n, description=f"{n} desc", source="custom:test")
        for n in tool_names
    ]

    def _make_handler(tool_name: str):
        async def _handler(**kwargs):
            executed.append(tool_name)
            if tool_name in failing:
                raise RuntimeError(f"{tool_name} blew up")
            return f"ran {tool_name} with {sorted(kwargs.items())}"

        return _handler

    for d in definitions:
        callable_exec.register(d.name, _make_handler(d.name))

    composite = CompositeToolExecutor()
    composite.add_tools(definitions, callable_exec)
    return composite, executed


def _tool_call_response(*calls: ToolCall) -> ChatResponse:
    return ChatResponse(
        content="",
        model="stub",
        stop_reason="tool_calls",
        tool_calls=list(calls),
    )


def _tool_messages(messages: list[ChatMessage], tool_call_id: str) -> list:
    return [
        m
        for m in messages
        if getattr(m, "role", None) == "tool"
        and getattr(m, "tool_call_id", None) == tool_call_id
    ]


# ─────────────────────────────────────────────────────────────────────────
# Unit: key + exemption helpers
# ─────────────────────────────────────────────────────────────────────────


def test_dedup_key_is_argument_order_independent():
    assert _dedup_key("t", {"a": 1, "b": 2}) == _dedup_key("t", {"b": 2, "a": 1})
    assert _dedup_key("t", {"a": 1}) != _dedup_key("t", {"a": 2})
    assert _dedup_key("t", None) == _dedup_key("t", {})


def test_dedup_key_ignores_cosmetic_output_naming_arguments():
    """(c) ``download_notion_file(block_id=X, filename="a.png")`` and
    ``(block_id=X, filename="b.png")`` fetch the same bytes, so they share a
    key; a different ``block_id`` does not. Renaming the output was exactly
    how the trace 722375f1 model defeated the cache 28 times in a row."""
    table = declared_cosmetic_arguments(_download_executor()[0].all_tools)
    assert table == {"download_notion_file": frozenset({"filename"})}
    same_a = _dedup_key("download_notion_file", {"block_id": "X", "filename": "a.png"}, table)
    same_b = _dedup_key("download_notion_file", {"block_id": "X", "filename": "b.png"}, table)
    bare = _dedup_key("download_notion_file", {"block_id": "X"}, table)
    other = _dedup_key("download_notion_file", {"block_id": "Y", "filename": "a.png"}, table)
    assert same_a == same_b == bare
    assert other != same_a
    # A multi-instance suffixed name (``tool__<slug>``) shares the base's rule.
    assert _dedup_key("download_notion_file__acme", {"block_id": "X", "filename": "a.png"}, table) == _dedup_key(
        "download_notion_file__acme", {"block_id": "X", "filename": "z.png"}, table
    )
    # Undeclared, the name alone carries no rule: every argument stays in the key.
    assert _dedup_key("download_notion_file", {"block_id": "X", "filename": "a.png"}) != _dedup_key(
        "download_notion_file", {"block_id": "X", "filename": "b.png"}
    )


def test_every_shipped_download_tool_declares_its_output_name_cosmetic():
    """The owners of the shipped download tools declare ``filename`` cosmetic (#5303)."""
    from app.api.v1.integration_catalog import build_dummy_token
    from app.services.integration_tools import PROVIDER_TOOLS

    expected = {
        "notion": {"download_notion_file"},
        "figma": {"download_figma_image"},
        "drata": {"download_drata_file"},
    }
    for provider, names in expected.items():
        definitions = PROVIDER_TOOLS[provider](
            build_dummy_token(provider), CallableToolExecutor(), refresh_fn=lambda: None
        )
        table = declared_cosmetic_arguments(definitions)
        for name in names:
            assert table.get(name) == frozenset({"filename"}), (provider, name)


def test_dedup_key_keeps_every_argument_for_other_tools():
    """The default is unchanged: a tool with no cosmetic-argument entry keys
    on ALL of its arguments, ``filename`` included (for a tool that selects
    the file by name it is data, not decoration)."""
    table = declared_cosmetic_arguments(_download_executor()[0].all_tools)
    assert "search_lookup" not in table
    assert _dedup_key("search_lookup", {"q": "x", "filename": "a"}, table) != _dedup_key(
        "search_lookup", {"q": "x", "filename": "b"}, table
    )


def test_dedup_cache_nudge_points_to_read_file_content_not_new_arguments():
    """(b) The cache nudge must never invite a cosmetic argument change: for
    a download tool "different arguments" means renaming the output, which
    re-downloads the same bytes. It points at ``read_file_content``."""
    assert "read_file_content" in _DEDUP_CACHE_NUDGE
    assert "different arguments" not in _DEDUP_CACHE_NUDGE
    assert "different arguments" not in _NEAR_DUPLICATE_NUDGE
    assert "read_file_content" in _NEAR_DUPLICATE_NUDGE


def test_polling_tools_are_exempt():
    for name in DEDUP_EXEMPT_TOOLS:
        assert _is_dedup_exempt(name)
    # A polling tool declares ``live_state``; its name is never read (#5303).
    assert _is_dedup_exempt("get_build_status", frozenset({"get_build_status"}))
    assert not _is_dedup_exempt("get_build_status"), "an undeclared read is cached like any read"


def test_read_tools_are_dedup_eligible():
    """READ-classified, non-polling tools are the only dedup-eligible tools -
    they are NOT exempt, so identical calls are served from the cache."""
    for name in (
        "search_knowledge",
        "search_openaev_injector_contracts",
        "get_user",
        "list_records",
    ):
        assert not _is_dedup_exempt(name), f"{name} should be dedup-eligible"
        assert not _is_write_tool(name), f"{name} should classify as read"


def test_write_tools_are_never_dedup_exempt():
    """WRITE-classified (mutating) tools MUST be dedup-exempt so an
    identical-argument replay always re-executes the mutation.  Reuses the
    security-graded classifier, which also fails safe to 'write' (and thus
    exempt) for unknown verbs."""
    for name in (
        "github_create_issue",
        "manage_openaev_threat_arsenal",
        "update_user",
        "delete_record",
        "duplicate_scenario",
        # Unknown verb -> classifier fails safe to 'write' -> exempt.
        "frobnicate_widget",
    ):
        assert _is_write_tool(name), f"{name} should classify as write"
        assert _is_dedup_exempt(name), f"{name} must never be deduplicated"


def test_stateful_browser_tool_is_exempt():
    """The built-in browser keeps a persistent session across calls -
    navigate -> click -> get_content legitimately repeats
    ``{"action": "get_content"}`` with identical arguments and must see the
    CURRENT page state, never a stale cached snapshot."""
    assert "browser" in DEDUP_EXEMPT_TOOLS
    assert _is_dedup_exempt("browser")


# ─────────────────────────────────────────────────────────────────────────
# Integration: single-call (sequential) path
# ─────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_identical_successful_call_served_from_cache(_quiet_langfuse):
    """The second identical call must NOT re-execute - the cached result is
    fed back with the marker + nudge."""
    composite, executed = _executor_with(["search_lookup"])
    llm = _ScriptedLLM(
        [
            _tool_call_response(
                ToolCall(id="c1", name="search_lookup", arguments={"q": "x"})
            ),
            _tool_call_response(
                ToolCall(id="c2", name="search_lookup", arguments={"q": "x"})
            ),
            ChatResponse(content="Done.", model="stub"),
        ]
    )
    messages: list[ChatMessage] = [ChatMessage(role="user", content="go")]

    response = await al.run_agent_loop(
        llm=llm,
        messages=messages,
        tools=composite.all_tools,
        executor=composite,
        max_iterations=10,
    )

    assert response.content == "Done."
    assert executed == ["search_lookup"], "identical call must not re-execute"

    first = _tool_messages(messages, "c1")
    second = _tool_messages(messages, "c2")
    assert len(first) == 1 and len(second) == 1
    assert _DEDUP_CACHE_MARKER not in first[0].content
    assert second[0].content.startswith(_DEDUP_CACHE_MARKER)
    assert _DEDUP_CACHE_NUDGE in second[0].content
    # The original result is still available to the LLM.
    assert "ran search_lookup" in second[0].content


@pytest.mark.asyncio
async def test_different_arguments_execute_normally(_quiet_langfuse):
    composite, executed = _executor_with(["search_lookup"])
    llm = _ScriptedLLM(
        [
            _tool_call_response(
                ToolCall(id="c1", name="search_lookup", arguments={"q": "x"})
            ),
            _tool_call_response(
                ToolCall(id="c2", name="search_lookup", arguments={"q": "y"})
            ),
            ChatResponse(content="Done.", model="stub"),
        ]
    )
    messages: list[ChatMessage] = [ChatMessage(role="user", content="go")]

    await al.run_agent_loop(
        llm=llm,
        messages=messages,
        tools=composite.all_tools,
        executor=composite,
        max_iterations=10,
    )

    assert executed == ["search_lookup", "search_lookup"]
    assert _DEDUP_CACHE_MARKER not in _tool_messages(messages, "c2")[0].content


@pytest.mark.asyncio
async def test_exempt_polling_tool_always_executes(_quiet_langfuse):
    composite, executed = _executor_with(["check_task_status"])
    llm = _ScriptedLLM(
        [
            _tool_call_response(
                ToolCall(
                    id="c1", name="check_task_status", arguments={"task_ids": "t1"}
                )
            ),
            _tool_call_response(
                ToolCall(
                    id="c2", name="check_task_status", arguments={"task_ids": "t1"}
                )
            ),
            ChatResponse(content="Done.", model="stub"),
        ]
    )
    messages: list[ChatMessage] = [ChatMessage(role="user", content="go")]

    await al.run_agent_loop(
        llm=llm,
        messages=messages,
        tools=composite.all_tools,
        executor=composite,
        max_iterations=10,
    )

    assert executed == ["check_task_status", "check_task_status"]
    assert _DEDUP_CACHE_MARKER not in _tool_messages(messages, "c2")[0].content


@pytest.mark.asyncio
async def test_a_live_state_tool_answers_every_poll_afresh(_quiet_langfuse):
    """A job poller whose name carries no polling word (``get_render_jobs``,
    like ``get_video_jobs``) is a cacheable read by its name. Its definition
    declares ``live_state``: every identical poll runs and reads what
    the worker wrote since - none is a replay of the first ``queued`` answer -
    and the loop breaker never refuses the poll (it refuses the 5th identical
    call of a counted tool)."""
    answers = iter(["queued", "running 13%", "running 41%", "running 69%", "running 92%", "ready"])
    executed: list[str] = []

    async def poll(**_kwargs):
        executed.append("get_render_jobs")
        return f"render r1: {next(answers)}"

    callable_exec = CallableToolExecutor()
    callable_exec.register("get_render_jobs", poll)
    definition = ToolDefinition(name="get_render_jobs", description="Poll renders.", live_state=True)
    composite = CompositeToolExecutor()
    composite.add_tools([definition], callable_exec)
    assert not al._is_write_tool("get_render_jobs"), "the premise: a read the cache would replay"
    calls = [ToolCall(id=f"c{i}", name="get_render_jobs", arguments={"render_ids": ["r1"]}) for i in range(6)]
    llm = _ScriptedLLM([*(_tool_call_response(call) for call in calls), ChatResponse(content="Done.", model="stub")])
    messages: list[ChatMessage] = [ChatMessage(role="user", content="go")]

    await al.run_agent_loop(
        llm=llm, messages=messages, tools=composite.all_tools, executor=composite, max_iterations=12
    )

    assert len(executed) == 6
    replies = [_tool_messages(messages, call.id)[0].content for call in calls]
    assert replies[0].endswith("queued") and replies[-1].endswith("ready")
    assert not any(_DEDUP_CACHE_MARKER in reply for reply in replies)


@pytest.mark.asyncio
async def test_failed_call_is_not_cached_and_can_retry(_quiet_langfuse):
    """Error results must never be cached - the repeated-failure tracking
    handles broken calls, and a retry of a failed call must execute."""
    composite, executed = _executor_with(["search_flaky"], failing={"search_flaky"})
    llm = _ScriptedLLM(
        [
            _tool_call_response(
                ToolCall(id="c1", name="search_flaky", arguments={"q": "x"})
            ),
            _tool_call_response(
                ToolCall(id="c2", name="search_flaky", arguments={"q": "x"})
            ),
            ChatResponse(content="Done.", model="stub"),
        ]
    )
    messages: list[ChatMessage] = [ChatMessage(role="user", content="go")]

    await al.run_agent_loop(
        llm=llm,
        messages=messages,
        tools=composite.all_tools,
        executor=composite,
        max_iterations=10,
    )

    assert executed == ["search_flaky", "search_flaky"], (
        "failed calls must be retryable"
    )
    assert _DEDUP_CACHE_MARKER not in _tool_messages(messages, "c2")[0].content


@pytest.mark.asyncio
async def test_error_shaped_result_returned_as_normal_string_is_not_cached(
    _quiet_langfuse,
):
    """A read tool that CATCHES an upstream failure and RETURNS it as a normal
    string (``is_error=False``) must NOT be cached.

    Several integrations (OpenAEV's ``_api`` -> ``format_http_error`` among
    them) surface a caught 401/5xx as a normal tool result rather than raising.
    Caching it would freeze the transient failure and re-serve it for the rest
    of the run - the iter-58 ``manage_openaev_settings`` 401 that was cached
    then replayed. The identical retry must therefore re-execute and see the
    recovered result, not the cached error.
    """
    executed: list[str] = []
    callable_exec = CallableToolExecutor()
    definitions = [
        ToolDefinition(
            name="search_lookup", description="search_lookup", source="custom:test"
        )
    ]
    calls = {"n": 0}

    async def _handler(**kwargs):
        executed.append("search_lookup")
        calls["n"] += 1
        if calls["n"] == 1:
            # Caught 401 surfaced as a normal string (no raise -> is_error False).
            return "OpenAEV API error: 401 Unauthorized - token may be expired."
        return "recovered: real result"

    callable_exec.register("search_lookup", _handler)
    composite = CompositeToolExecutor()
    composite.add_tools(definitions, callable_exec)

    llm = _ScriptedLLM(
        [
            _tool_call_response(
                ToolCall(id="c1", name="search_lookup", arguments={"q": "x"})
            ),
            _tool_call_response(
                ToolCall(id="c2", name="search_lookup", arguments={"q": "x"})
            ),
            ChatResponse(content="Done.", model="stub"),
        ]
    )
    messages: list[ChatMessage] = [ChatMessage(role="user", content="go")]

    await al.run_agent_loop(
        llm=llm,
        messages=messages,
        tools=composite.all_tools,
        executor=composite,
        max_iterations=10,
    )

    assert executed == ["search_lookup", "search_lookup"], (
        "an error-shaped result must not be cached; the retry must re-execute"
    )
    second = _tool_messages(messages, "c2")[0]
    assert _DEDUP_CACHE_MARKER not in second.content, (
        "the retry must not be served the cached 401"
    )
    assert "recovered: real result" in second.content


# ─────────────────────────────────────────────────────────────────────────
# Integration: write tools are never deduplicated (mutations always run)
# ─────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_write_tool_never_served_from_cache_sequential(_quiet_langfuse):
    """A write-classified tool replayed with IDENTICAL arguments must always
    re-execute - its side effect must fire every time, never be suppressed by
    a cached 'success'."""
    composite, executed = _executor_with(["github_create_issue"])
    llm = _ScriptedLLM(
        [
            _tool_call_response(
                ToolCall(
                    id="c1", name="github_create_issue", arguments={"title": "x"}
                )
            ),
            _tool_call_response(
                ToolCall(
                    id="c2", name="github_create_issue", arguments={"title": "x"}
                )
            ),
            ChatResponse(content="Done.", model="stub"),
        ]
    )
    messages: list[ChatMessage] = [ChatMessage(role="user", content="go")]

    await al.run_agent_loop(
        llm=llm,
        messages=messages,
        tools=composite.all_tools,
        executor=composite,
        max_iterations=10,
    )

    assert executed == ["github_create_issue", "github_create_issue"], (
        "an identical write replay must re-execute"
    )
    # The second call really ran - no cache marker.
    assert _DEDUP_CACHE_MARKER not in _tool_messages(messages, "c2")[0].content


@pytest.mark.asyncio
async def test_write_execution_invalidates_read_cache_sequential(_quiet_langfuse):
    """A read cached BEFORE a write must NOT be served stale AFTER the write:
    the write invalidates the read cache, so the same read re-executes against
    fresh state.  (Sequential path.)"""
    composite, executed = _executor_with(["search_things", "github_create_issue"])
    llm = _ScriptedLLM(
        [
            # Read -> cached.
            _tool_call_response(
                ToolCall(id="c1", name="search_things", arguments={"q": "x"})
            ),
            # Write -> mutation runs, read cache invalidated.
            _tool_call_response(
                ToolCall(
                    id="c2", name="github_create_issue", arguments={"title": "t"}
                )
            ),
            # Same read as c1 -> must re-execute (cache was cleared), NOT be
            # served the stale pre-mutation snapshot.
            _tool_call_response(
                ToolCall(id="c3", name="search_things", arguments={"q": "x"})
            ),
            ChatResponse(content="Done.", model="stub"),
        ]
    )
    messages: list[ChatMessage] = [ChatMessage(role="user", content="go")]

    await al.run_agent_loop(
        llm=llm,
        messages=messages,
        tools=composite.all_tools,
        executor=composite,
        max_iterations=10,
    )

    assert executed == ["search_things", "github_create_issue", "search_things"], (
        "the post-write read must re-execute, not hit the stale cache"
    )
    # c3 re-executed -> no cache marker.
    assert _DEDUP_CACHE_MARKER not in _tool_messages(messages, "c3")[0].content


# ─────────────────────────────────────────────────────────────────────────
# Integration: parallel (gather) path
# ─────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_intra_batch_dedup_executes_once_and_mirrors_result(_quiet_langfuse):
    """Two identical READ calls inside ONE parallel batch execute once; the
    duplicate mirrors the result with the cache marker.  A distinct call in
    the same batch executes normally."""
    composite, executed = _executor_with(["search_lookup", "get_other"])
    llm = _ScriptedLLM(
        [
            _tool_call_response(
                ToolCall(id="c1", name="search_lookup", arguments={"q": "x"}),
                ToolCall(id="c2", name="search_lookup", arguments={"q": "x"}),
                ToolCall(id="c3", name="get_other", arguments={"q": "z"}),
            ),
            ChatResponse(content="Done.", model="stub"),
        ]
    )
    messages: list[ChatMessage] = [ChatMessage(role="user", content="go")]

    response = await al.run_agent_loop(
        llm=llm,
        messages=messages,
        tools=composite.all_tools,
        executor=composite,
        max_iterations=10,
    )

    assert response.content == "Done."
    assert sorted(executed) == ["get_other", "search_lookup"], (
        "the duplicate must not execute; the distinct call must"
    )
    # Every tool_call id still has exactly one tool-result message
    # (providers require complete pairing).
    for cid in ("c1", "c2", "c3"):
        assert len(_tool_messages(messages, cid)) == 1
    assert _DEDUP_CACHE_MARKER not in _tool_messages(messages, "c1")[0].content
    dup = _tool_messages(messages, "c2")[0]
    assert dup.content.startswith(_DEDUP_CACHE_MARKER)
    assert "ran search_lookup" in dup.content
    assert _DEDUP_CACHE_MARKER not in _tool_messages(messages, "c3")[0].content


@pytest.mark.asyncio
async def test_parallel_batch_uses_cache_from_previous_iteration(_quiet_langfuse):
    """A parallel batch call identical to a SUCCESSFUL read call from an
    earlier iteration is served from the cache without re-execution."""
    composite, executed = _executor_with(["search_lookup", "get_other"])
    llm = _ScriptedLLM(
        [
            _tool_call_response(
                ToolCall(id="c1", name="search_lookup", arguments={"q": "x"})
            ),
            _tool_call_response(
                ToolCall(id="c2", name="search_lookup", arguments={"q": "x"}),
                ToolCall(id="c3", name="get_other", arguments={"q": "z"}),
            ),
            ChatResponse(content="Done.", model="stub"),
        ]
    )
    messages: list[ChatMessage] = [ChatMessage(role="user", content="go")]

    await al.run_agent_loop(
        llm=llm,
        messages=messages,
        tools=composite.all_tools,
        executor=composite,
        max_iterations=10,
    )

    assert sorted(executed) == ["get_other", "search_lookup"]
    assert _tool_messages(messages, "c2")[0].content.startswith(
        _DEDUP_CACHE_MARKER
    )


@pytest.mark.asyncio
async def test_write_tool_never_deduped_in_parallel_batch(_quiet_langfuse):
    """Write tools are never deduplicated in the parallel path either -
    neither within a single batch (two identical writes both execute) nor
    across iterations (an identical write in a later batch re-executes)."""
    composite, executed = _executor_with(["manage_openaev_threat_arsenal", "get_other"])
    llm = _ScriptedLLM(
        [
            # Two IDENTICAL writes in one batch -> both must execute (no
            # intra-batch dedup for writes).
            _tool_call_response(
                ToolCall(
                    id="c1",
                    name="manage_openaev_threat_arsenal",
                    arguments={"action": "duplicate", "id": "1"},
                ),
                ToolCall(
                    id="c2",
                    name="manage_openaev_threat_arsenal",
                    arguments={"action": "duplicate", "id": "1"},
                ),
                ToolCall(id="c3", name="get_other", arguments={"q": "z"}),
            ),
            # Same write again in a later batch -> must execute again.
            _tool_call_response(
                ToolCall(
                    id="c4",
                    name="manage_openaev_threat_arsenal",
                    arguments={"action": "duplicate", "id": "1"},
                ),
                ToolCall(id="c5", name="get_other", arguments={"q": "y"}),
            ),
            ChatResponse(content="Done.", model="stub"),
        ]
    )
    messages: list[ChatMessage] = [ChatMessage(role="user", content="go")]

    await al.run_agent_loop(
        llm=llm,
        messages=messages,
        tools=composite.all_tools,
        executor=composite,
        max_iterations=10,
    )

    assert executed.count("manage_openaev_threat_arsenal") == 3, (
        "every identical write call must execute - no intra-batch or "
        "cross-iteration dedup for writes"
    )
    for cid in ("c1", "c2", "c4"):
        assert _DEDUP_CACHE_MARKER not in _tool_messages(messages, cid)[0].content


@pytest.mark.asyncio
async def test_write_execution_invalidates_read_cache_parallel(_quiet_langfuse):
    """Parallel-path counterpart: a read cached in an earlier batch must not
    be served stale after a write runs in a LATER batch - the write clears the
    cache so the same read re-executes."""
    composite, executed = _executor_with(
        ["search_things", "get_other", "github_create_issue"]
    )
    llm = _ScriptedLLM(
        [
            # Batch 1 (reads only) -> search_things(x) cached.
            _tool_call_response(
                ToolCall(id="c1", name="search_things", arguments={"q": "x"}),
                ToolCall(id="c2", name="get_other", arguments={"q": "a"}),
            ),
            # Batch 2 contains a write -> the whole read cache is invalidated
            # AFTER the batch.
            _tool_call_response(
                ToolCall(
                    id="c3", name="github_create_issue", arguments={"title": "t"}
                ),
                ToolCall(id="c4", name="get_other", arguments={"q": "b"}),
            ),
            # Batch 3: search_things(x) identical to c1 -> must re-execute
            # (cache was cleared by the batch-2 write), not hit the cache.
            _tool_call_response(
                ToolCall(id="c5", name="search_things", arguments={"q": "x"}),
                ToolCall(id="c6", name="get_other", arguments={"q": "c"}),
            ),
            ChatResponse(content="Done.", model="stub"),
        ]
    )
    messages: list[ChatMessage] = [ChatMessage(role="user", content="go")]

    await al.run_agent_loop(
        llm=llm,
        messages=messages,
        tools=composite.all_tools,
        executor=composite,
        max_iterations=10,
    )

    assert executed.count("search_things") == 2, (
        "the post-write read must re-execute, not be served the stale cache"
    )
    # c5 re-executed -> no cache marker (would be present on a cache hit).
    assert _DEDUP_CACHE_MARKER not in _tool_messages(messages, "c5")[0].content


# ─────────────────────────────────────────────────────────────────────────
# Failure-tracking interaction: cached successes clear the consecutive-
# failure counter; mirrored failures are not double-counted
# ─────────────────────────────────────────────────────────────────────────


def _arg_sensitive_executor() -> tuple[CompositeToolExecutor, list[str]]:
    """``search_lookup`` fails when called with ``q="bad"`` and succeeds
    otherwise; ``get_other`` always succeeds.  Both are read-classified so the
    dedup cache applies to them."""
    executed: list[str] = []
    callable_exec = CallableToolExecutor()
    definitions = [
        ToolDefinition(
            name="search_lookup", description="search_lookup", source="custom:test"
        ),
        ToolDefinition(
            name="get_other", description="get_other", source="custom:test"
        ),
    ]

    async def _lookup(**kwargs):
        executed.append("search_lookup")
        if kwargs.get("q") == "bad":
            raise RuntimeError("search_lookup blew up")
        return f"ran search_lookup with {sorted(kwargs.items())}"

    async def _other(**kwargs):
        executed.append("get_other")
        return f"ran get_other with {sorted(kwargs.items())}"

    callable_exec.register("search_lookup", _lookup)
    callable_exec.register("get_other", _other)
    composite = CompositeToolExecutor()
    composite.add_tools(definitions, callable_exec)
    return composite, executed


def _has_repeated_failure_nudge(messages: list[ChatMessage]) -> bool:
    return any(
        getattr(m, "role", None) == "system"
        and "repeated tool failure" in (m.content or "")
        for m in messages
    )


@pytest.mark.asyncio
async def test_sequential_cache_hit_clears_failure_tracking(_quiet_langfuse):
    """A cache hit (always a success) must clear the consecutive-failure
    counter like a real successful execution - interleaved failures with
    DIFFERENT arguments must not accumulate across a cached success and
    trigger the repeated-failure nudge."""
    composite, executed = _arg_sensitive_executor()
    llm = _ScriptedLLM(
        [
            _tool_call_response(
                ToolCall(id="c1", name="search_lookup", arguments={"q": "ok"})
            ),
            _tool_call_response(
                ToolCall(id="c2", name="search_lookup", arguments={"q": "bad"})
            ),
            # Identical to c1 - served from the cache, must clear tracking.
            _tool_call_response(
                ToolCall(id="c3", name="search_lookup", arguments={"q": "ok"})
            ),
            _tool_call_response(
                ToolCall(id="c4", name="search_lookup", arguments={"q": "bad"})
            ),
            ChatResponse(content="Done.", model="stub"),
        ]
    )
    messages: list[ChatMessage] = [ChatMessage(role="user", content="go")]

    response = await al.run_agent_loop(
        llm=llm,
        messages=messages,
        tools=composite.all_tools,
        executor=composite,
        max_iterations=10,
    )

    assert response.content == "Done."
    # c3 was a cache hit; the two failures were never consecutive from the
    # LLM's point of view, so the nudge must NOT fire.
    assert executed == ["search_lookup", "search_lookup", "search_lookup"]
    assert not _has_repeated_failure_nudge(messages)


@pytest.mark.asyncio
async def test_parallel_cache_hit_clears_failure_tracking(_quiet_langfuse):
    """Same invariant in the parallel path: a ``_DedupedCall`` cache hit is
    a success and must clear the consecutive-failure counter."""
    composite, executed = _arg_sensitive_executor()
    llm = _ScriptedLLM(
        [
            _tool_call_response(
                ToolCall(id="c1", name="search_lookup", arguments={"q": "ok"}),
                ToolCall(id="c2", name="get_other", arguments={"q": "a"}),
            ),
            _tool_call_response(
                ToolCall(id="c3", name="search_lookup", arguments={"q": "bad"}),
                ToolCall(id="c4", name="get_other", arguments={"q": "b"}),
            ),
            # c5 identical to c1 - served from the cache in a parallel batch.
            _tool_call_response(
                ToolCall(id="c5", name="search_lookup", arguments={"q": "ok"}),
                ToolCall(id="c6", name="get_other", arguments={"q": "c"}),
            ),
            _tool_call_response(
                ToolCall(id="c7", name="search_lookup", arguments={"q": "bad"}),
                ToolCall(id="c8", name="get_other", arguments={"q": "d"}),
            ),
            ChatResponse(content="Done.", model="stub"),
        ]
    )
    messages: list[ChatMessage] = [ChatMessage(role="user", content="go")]

    response = await al.run_agent_loop(
        llm=llm,
        messages=messages,
        tools=composite.all_tools,
        executor=composite,
        max_iterations=10,
    )

    assert response.content == "Done."
    # c5 was served from the cache - search_lookup executed only on c1/c3/c7.
    assert executed.count("search_lookup") == 3
    assert _tool_messages(messages, "c5")[0].content.startswith(
        _DEDUP_CACHE_MARKER
    )
    assert not _has_repeated_failure_nudge(messages)


@pytest.mark.parametrize(
    "content,expected",
    [
        ("Tool execution failed: GitHub API error: 401 Unauthorized", True),
        # Message starting with the keyword (no leading space, no 401 code).
        ("Unauthorized: token rejected by the upstream API", True),
        # Bare status code at the end of the message / parenthesised.
        ("GitHub API error: 401", True),
        ("Request rejected (401)", True),
        ("Bad credentials", True),
        ("Requires authentication", True),
        ("Validation Failed (422): you do not have permission to view them", True),
        ("Validation Failed (422): missing required parameter 'q'", False),
        # Standalone safety: GitHub's secondary-rate-limit phrasing contains
        # "Permission Denied" but must NOT classify as an auth failure even
        # when this classifier is called without a prior rate-limit check.
        (
            "Permission Denied: You have exceeded a secondary rate limit. "
            "Please wait a few minutes before you try again.",
            False,
        ),
        # HubSpot 403 scope errors: both "access denied" and
        # "insufficient scopes" must be caught so the agent-loop cooldown
        # prevents blind retries on permanent credential failures.
        (
            "Error: HubSpot access denied while trying to list campaigns - "
            "insufficient permissions or scopes. The user should check the "
            "integration permissions. Detail: One or more of the following "
            "scopes are required. Required scopes: content.",
            True,
        ),
        (
            "Error: HubSpot access denied while trying to list campaigns - "
            "insufficient scopes. Required scopes: content.",
            True,
        ),
        # Digit runs containing 401 must not false-positive.
        ("Processed 40123 records successfully", False),
        ("Fetched item id 84012 from the queue", False),
        ("Everything went fine", False),
    ],
)
def test_auth_or_permission_error_classifier(content, expected):
    assert is_auth_or_permission_error(content) is expected


@pytest.mark.parametrize(
    "content,expected",
    [
        ("GitHub API error: 429 Too Many Requests", True),
        ("API rate limit exceeded for installation", True),
        ("Request rejected (429)", True),
        ("upstream returned rate_limit_exceeded", True),
        (
            "Permission Denied: You have exceeded a secondary rate limit. "
            "Please wait a few minutes before you try again.",
            True,
        ),
        # Digit runs containing 429 must not false-positive.
        ("Processed 14290 records successfully", False),
        ("Fetched item id 84291 from the queue", False),
        ("Everything went fine", False),
    ],
)
def test_rate_limit_error_classifier(content, expected):
    assert is_rate_limit_error(content) is expected


@pytest.mark.asyncio
async def test_mirrored_intra_batch_failure_is_not_double_counted(_quiet_langfuse):
    """Two identical FAILING calls in one batch execute once; the mirrored
    duplicate must not increment the failure counter a second time (one
    actual execution = one failure, below the nudge threshold of 2)."""
    composite, executed = _arg_sensitive_executor()
    llm = _ScriptedLLM(
        [
            _tool_call_response(
                ToolCall(id="c1", name="search_lookup", arguments={"q": "bad"}),
                ToolCall(id="c2", name="search_lookup", arguments={"q": "bad"}),
                ToolCall(id="c3", name="get_other", arguments={"q": "a"}),
            ),
            ChatResponse(content="Done.", model="stub"),
        ]
    )
    messages: list[ChatMessage] = [ChatMessage(role="user", content="go")]

    response = await al.run_agent_loop(
        llm=llm,
        messages=messages,
        tools=composite.all_tools,
        executor=composite,
        max_iterations=10,
    )

    assert response.content == "Done."
    # The failing call executed exactly once (mirror did not re-execute).
    assert executed.count("search_lookup") == 1
    # One real failure - counter at 1, below _MAX_TOOL_FAILURES - no nudge.
    assert not _has_repeated_failure_nudge(messages)


# ─────────────────────────────────────────────────────────────────────────
# File-bearing results: cache hits mirror the files, cosmetic argument
# changes hit the cache, and the near-duplicate guard steers without blocking
# (trace 722375f1, 2026-09-24)
# ─────────────────────────────────────────────────────────────────────────


def _download_executor() -> tuple[CompositeToolExecutor, list[dict]]:
    """``download_notion_file`` stand-in: a read tool whose result is an
    attached image plus a ``[[FILE:id]]`` marker (the incident's shape).
    Returns the executor and the list of argument dicts actually executed."""
    executed: list[dict] = []
    callable_exec = CallableToolExecutor()
    definitions = [
        ToolDefinition(
            name="download_notion_file",
            description="download",
            source="custom:test",
            input_schema={
                "type": "object",
                "properties": {
                    "block_id": {"type": "string"},
                    "filename": {"type": "string"},
                },
                "required": ["block_id"],
            },
            cosmetic_arguments=("filename",),
        )
    ]

    async def _download(block_id: str, filename: str = "", **_kw):
        executed.append({"block_id": block_id, "filename": filename})
        file_id = f"file-{block_id}"
        ref = FileRef.from_bytes(
            _TINY_PNG, filename or f"{block_id}.png", "image/png", file_id=file_id
        )
        return ToolResult(
            tool_call_id="",
            content=(
                f"Downloaded '{ref.filename}' (0.1 KB) from Notion as a reusable "
                f"file. [[FILE:{file_id}]]"
            ),
            files=[ref],
        )

    callable_exec.register("download_notion_file", _download)
    composite = CompositeToolExecutor()
    composite.add_tools(definitions, callable_exec)
    return composite, executed


def _download_call(cid: str, block_id: str, filename: str) -> ToolCall:
    return ToolCall(
        id=cid,
        name="download_notion_file",
        arguments={"block_id": block_id, "filename": filename},
    )


@pytest.mark.asyncio
async def test_cache_hit_on_file_bearing_result_mirrors_the_files(_quiet_langfuse):
    """(a) A cache hit on an image download replays the IMAGE, not a sentence
    saying the image is attached: the replayed tool message carries the
    cached files (sequential path)."""
    composite, executed = _download_executor()
    llm = _ScriptedLLM(
        [
            _tool_call_response(_download_call("c1", "blk-1", "diagram.png")),
            _tool_call_response(_download_call("c2", "blk-1", "diagram.png")),
            ChatResponse(content="Done.", model="stub"),
        ]
    )
    messages: list[ChatMessage] = [ChatMessage(role="user", content="go")]

    await al.run_agent_loop(
        llm=llm,
        messages=messages,
        tools=composite.all_tools,
        executor=composite,
        max_iterations=10,
    )

    assert len(executed) == 1
    first = _tool_messages(messages, "c1")[0]
    replay = _tool_messages(messages, "c2")[0]
    assert replay.content.startswith(_DEDUP_CACHE_MARKER)
    assert replay.files and [f.file_id for f in replay.files] == [
        f.file_id for f in first.files
    ]
    assert replay.files[0].is_image


@pytest.mark.asyncio
async def test_incident_replay_cosmetic_rename_hits_cache_and_keeps_all_images(
    _quiet_langfuse,
):
    """Scripted replay of trace 722375f1: three parallel image downloads, then
    the model asks for the first image again under a new filename. The retry
    is served from the cache with the image mirrored, no second download runs,
    all three images stay visible, and the run ends in one more iteration."""
    composite, executed = _download_executor()
    llm = _ScriptedLLM(
        [
            _tool_call_response(
                _download_call("c1", "blk-1", "platform-overview-current.png"),
                _download_call("c2", "blk-2", "platform-overview-target.png"),
                _download_call("c3", "blk-3", "platform-overview-gaps.png"),
            ),
            # The incident retry: same block, cosmetic filename change.
            _tool_call_response(
                _download_call("c4", "blk-1", "platform-overview-v1b.png")
            ),
            ChatResponse(content="Here is the comparison.", model="stub"),
        ]
    )
    messages: list[ChatMessage] = [ChatMessage(role="user", content="compare the diagrams")]

    response = await al.run_agent_loop(
        llm=llm,
        messages=messages,
        tools=composite.all_tools,
        executor=composite,
        max_iterations=10,
    )

    assert response.content == "Here is the comparison."
    assert llm._idx == 3, "one retry iteration, then the final answer"
    assert [e["block_id"] for e in executed] == ["blk-1", "blk-2", "blk-3"], (
        "the renamed retry must not download the same block a second time"
    )
    # Every image of the batch is still attached going into the final call,
    # and the replayed retry carries the first image too.
    batch = [_tool_messages(messages, cid)[0] for cid in ("c1", "c2", "c3")]
    assert all(m.files for m in batch)
    retry = _tool_messages(messages, "c4")[0]
    assert retry.content.startswith(_DEDUP_CACHE_MARKER)
    assert retry.files and retry.files[0].file_id == "file-blk-1"
    # Nothing in the transcript claims an attachment it does not carry.
    for m in messages:
        if m.role == "tool" and m.files is None:
            assert "removed from the working context" in m.content or (
                "[[FILE:" not in m.content
            )


@pytest.mark.asyncio
async def test_intra_batch_mirror_of_file_bearing_result_carries_files(
    _quiet_langfuse,
):
    """Two calls for the same block in ONE batch (different filenames) execute
    once; the mirrored duplicate carries the primary's files."""
    composite, executed = _download_executor()
    llm = _ScriptedLLM(
        [
            _tool_call_response(
                _download_call("c1", "blk-1", "a.png"),
                _download_call("c2", "blk-1", "b.png"),
            ),
            ChatResponse(content="Done.", model="stub"),
        ]
    )
    messages: list[ChatMessage] = [ChatMessage(role="user", content="go")]

    await al.run_agent_loop(
        llm=llm,
        messages=messages,
        tools=composite.all_tools,
        executor=composite,
        max_iterations=10,
    )

    assert len(executed) == 1
    mirror = _tool_messages(messages, "c2")[0]
    assert mirror.content.startswith(_DEDUP_CACHE_MARKER)
    assert mirror.files and mirror.files[0].file_id == "file-blk-1"


@pytest.mark.asyncio
async def test_near_duplicate_guard_fires_once_and_never_blocks(
    _quiet_langfuse, monkeypatch
):
    """(d) The same read with the same effective arguments more than
    ``_MAX_NEAR_DUPLICATE_CALLS`` times injects ONE system nudge (and one
    user-visible thinking_text) and never blocks a call: with the cache
    disabled every call still executes, before and after the nudge.

    Blocking is the LOOP BREAKER's job, which refuses the fifth
    identical call by default; it is switched off here so this test isolates
    the near-duplicate guard's own contract (steering only)."""
    # Disable the result cache so the calls really execute; the guard counts
    # executed and cached calls alike, and this isolates "never blocks".
    monkeypatch.setattr(al, "_DEDUP_CACHE_MAX_ENTRIES", 0)
    _disable_loop_breaker(monkeypatch)
    composite, executed = _executor_with(["search_lookup"])
    total_calls = _MAX_NEAR_DUPLICATE_CALLS + 3
    llm = _ScriptedLLM(
        [
            _tool_call_response(
                ToolCall(id=f"c{i}", name="search_lookup", arguments={"q": "x"})
            )
            for i in range(total_calls)
        ]
        + [ChatResponse(content="Done.", model="stub")]
    )
    messages: list[ChatMessage] = [ChatMessage(role="user", content="go")]
    events: list[dict] = []

    async def on_event(event: dict) -> None:
        events.append(event)

    response = await al.run_agent_loop(
        llm=llm,
        messages=messages,
        tools=composite.all_tools,
        executor=composite,
        max_iterations=20,
        on_event=on_event,
    )

    assert response.content == "Done."
    assert executed == ["search_lookup"] * total_calls, "the guard must never block a call"
    nudges = [
        m
        for m in messages
        if m.role == "system" and m.content.startswith("[SYSTEM — repeated identical read]")
    ]
    assert len(nudges) == 1, "one-shot per tool"
    assert "'search_lookup'" in nudges[0].content
    assert f"{_MAX_NEAR_DUPLICATE_CALLS + 1} times" in nudges[0].content
    assert "read_file_content" in nudges[0].content
    # The nudge lands right after the call that crossed the threshold — the
    # (threshold + 1)-th result — and before the next call's result.
    crossing = _tool_messages(messages, f"c{_MAX_NEAR_DUPLICATE_CALLS}")[0]
    following = _tool_messages(messages, f"c{_MAX_NEAR_DUPLICATE_CALLS + 1}")[0]
    assert messages.index(crossing) < messages.index(nudges[0]) < messages.index(following)
    warnings = [
        e
        for e in events
        if e.get("status") == "thinking_text" and "search_lookup" in e.get("content", "")
    ]
    assert len(warnings) == 1


@pytest.mark.asyncio
async def test_near_duplicate_guard_fires_when_the_crossing_call_is_a_cache_hit(
    _quiet_langfuse, monkeypatch
):
    """The crossing call served from the cache (sequential path) still gets
    the nudge. That path ``continue``s to the next iteration before the
    post-batch nudge block, and the counter marks the tool as nudged the
    moment it fires - so without the cache-hit injection site the one-shot
    was consumed and the model never read it. A cached re-read is exactly the
    pattern the guard exists for. (Breaker off: with it on, the breaker's own
    nudge takes this one's place - `test_the_near_duplicate_nudge_yields_to_the_breaker`.)"""
    _disable_loop_breaker(monkeypatch)
    composite, executed = _executor_with(["search_lookup"])
    total_calls = _MAX_NEAR_DUPLICATE_CALLS + 2
    llm = _ScriptedLLM(
        [
            _tool_call_response(
                ToolCall(id=f"c{i}", name="search_lookup", arguments={"q": "x"})
            )
            for i in range(total_calls)
        ]
        + [ChatResponse(content="Done.", model="stub")]
    )
    messages: list[ChatMessage] = [ChatMessage(role="user", content="go")]
    events: list[dict] = []

    async def on_event(event: dict) -> None:
        events.append(event)

    response = await al.run_agent_loop(
        llm=llm,
        messages=messages,
        tools=composite.all_tools,
        executor=composite,
        max_iterations=20,
        on_event=on_event,
    )

    assert response.content == "Done."
    assert executed == ["search_lookup"], "every repeat is a cache hit"
    nudges = [
        m
        for m in messages
        if m.role == "system" and m.content.startswith("[SYSTEM — repeated identical read]")
    ]
    assert len(nudges) == 1, "one-shot per tool, injected on the cache-hit path"
    assert f"{_MAX_NEAR_DUPLICATE_CALLS + 1} times" in nudges[0].content
    # Right after the cached result that crossed the threshold, before the
    # next call's (cached) result.
    crossing = _tool_messages(messages, f"c{_MAX_NEAR_DUPLICATE_CALLS}")[0]
    following = _tool_messages(messages, f"c{_MAX_NEAR_DUPLICATE_CALLS + 1}")[0]
    assert crossing.content.startswith(_DEDUP_CACHE_MARKER)
    assert messages.index(crossing) < messages.index(nudges[0]) < messages.index(following)
    warnings = [
        e
        for e in events
        if e.get("status") == "thinking_text" and "search_lookup" in e.get("content", "")
    ]
    assert len(warnings) == 1


@pytest.mark.asyncio
async def test_near_duplicate_guard_resets_after_a_write(_quiet_langfuse, monkeypatch):
    """A write in between makes a re-read legitimate: the counts reset, so
    the threshold is not crossed across the mutation."""
    monkeypatch.setattr(al, "_DEDUP_CACHE_MAX_ENTRIES", 0)
    composite, executed = _executor_with(["search_lookup", "github_create_issue"])
    script = []
    for i in range(_MAX_NEAR_DUPLICATE_CALLS):
        script.append(
            _tool_call_response(
                ToolCall(id=f"a{i}", name="search_lookup", arguments={"q": "x"})
            )
        )
    script.append(
        _tool_call_response(
            ToolCall(id="w", name="github_create_issue", arguments={"title": "t"})
        )
    )
    for i in range(_MAX_NEAR_DUPLICATE_CALLS):
        script.append(
            _tool_call_response(
                ToolCall(id=f"b{i}", name="search_lookup", arguments={"q": "x"})
            )
        )
    script.append(ChatResponse(content="Done.", model="stub"))
    llm = _ScriptedLLM(script)
    messages: list[ChatMessage] = [ChatMessage(role="user", content="go")]

    await al.run_agent_loop(
        llm=llm,
        messages=messages,
        tools=composite.all_tools,
        executor=composite,
        max_iterations=20,
    )

    assert executed.count("search_lookup") == 2 * _MAX_NEAR_DUPLICATE_CALLS
    assert not any(
        m.role == "system" and m.content.startswith("[SYSTEM — repeated identical read]")
        for m in messages
    )


# ───────────────────────────────────────────────────────────────────────
# A poll across a wait, and the age of a replay (#8671)
# ───────────────────────────────────────────────────────────────────────


def test_a_replay_names_its_age():
    replay = al._format_cached_result("open, 12 pending", 42.4)
    assert replay.startswith(_DEDUP_CACHE_MARKER + " Read 42 s ago. ")
    assert replay.endswith("\n\nopen, 12 pending")


def test_a_cache_entry_expires_after_its_age_bound():
    entry = al._CachedToolResult("open")
    assert al._cache_entry_fresh(entry, now=entry.stored_at + 1)
    assert not al._cache_entry_fresh(entry, now=entry.stored_at + al._DEDUP_CACHE_TTL_S)


@pytest.mark.asyncio
async def test_a_read_repeated_after_a_wait_executes_again(_quiet_langfuse, monkeypatch):
    """A CI watch: read the PR state, wait, read it again. The second read
    must reach the tool (the PR may have merged meanwhile), never the cache."""
    from app.tools import wait_policy as wp

    monkeypatch.setattr(wp, "MIN_GRANTED_WAIT_SECONDS", 0.05)
    monkeypatch.setattr(al, "MIN_GRANTED_WAIT_SECONDS", 0.05)
    composite, executed = _executor_with(["list_pr_states"])
    llm = _ScriptedLLM(
        [
            _tool_call_response(ToolCall(id="r1", name="list_pr_states", arguments={"n": 1})),
            _tool_call_response(ToolCall(id="w1", name="wait", arguments={"duration": "10m"})),
            _tool_call_response(ToolCall(id="r2", name="list_pr_states", arguments={"n": 1})),
            ChatResponse(content="Done.", model="stub"),
        ]
    )
    messages: list[ChatMessage] = [ChatMessage(role="user", content="watch CI")]

    await al.run_agent_loop(
        llm=llm,
        messages=messages,
        tools=composite.all_tools,
        executor=composite,
        max_iterations=10,
        wait_policy=wp.WaitPolicy(per_call_seconds=1, total_seconds=600),
    )

    assert executed == ["list_pr_states", "list_pr_states"]
    assert _DEDUP_CACHE_MARKER not in _tool_messages(messages, "r2")[0].content


@pytest.mark.asyncio
async def test_a_quick_repeat_is_still_served_with_its_age(_quiet_langfuse):
    composite, executed = _executor_with(["list_pr_states"])
    llm = _ScriptedLLM(
        [
            _tool_call_response(ToolCall(id="r1", name="list_pr_states", arguments={"n": 1})),
            _tool_call_response(ToolCall(id="r2", name="list_pr_states", arguments={"n": 1})),
            ChatResponse(content="Done.", model="stub"),
        ]
    )
    messages: list[ChatMessage] = [ChatMessage(role="user", content="go")]

    await al.run_agent_loop(
        llm=llm, messages=messages, tools=composite.all_tools, executor=composite, max_iterations=10
    )

    assert executed == ["list_pr_states"]
    assert _tool_messages(messages, "r2")[0].content.startswith(_DEDUP_CACHE_MARKER + " Read 0 s ago.")


@pytest.mark.asyncio
async def test_an_expired_read_executes_again(_quiet_langfuse, monkeypatch):
    monkeypatch.setattr(al, "_DEDUP_CACHE_TTL_S", 0.0)
    composite, executed = _executor_with(["list_pr_states"])
    llm = _ScriptedLLM(
        [
            _tool_call_response(ToolCall(id="r1", name="list_pr_states", arguments={"n": 1})),
            _tool_call_response(
                ToolCall(id="r2", name="list_pr_states", arguments={"n": 1}),
                ToolCall(id="r3", name="list_pr_states", arguments={"n": 2}),
            ),
            ChatResponse(content="Done.", model="stub"),
        ]
    )
    messages: list[ChatMessage] = [ChatMessage(role="user", content="go")]

    await al.run_agent_loop(
        llm=llm, messages=messages, tools=composite.all_tools, executor=composite, max_iterations=10
    )

    # Sequential first read, then the parallel batch: the expired entry is not served.
    assert executed == ["list_pr_states"] * 3
    assert _DEDUP_CACHE_MARKER not in _tool_messages(messages, "r2")[0].content


# ───────────────────────────────────────────────────────────────────────
# Live reads (#11488): a PR, its checks, a work item - state other actors move
# ───────────────────────────────────────────────────────────────────────


def _live_executor(answers: list, *, live_read: bool = True) -> tuple[CompositeToolExecutor, list[str]]:
    """``get_pr_state`` answering *answers* in turn (a str or a ToolResult), plus a write."""
    executed: list[str] = []
    queue = iter(answers)

    async def read(**_kwargs):
        executed.append("get_pr_state")
        return next(queue)

    async def write(**_kwargs):
        executed.append("update_pr")
        return "pushed"

    callable_exec = CallableToolExecutor()
    callable_exec.register("get_pr_state", read)
    callable_exec.register("update_pr", write)
    composite = CompositeToolExecutor()
    composite.add_tools(
        [
            ToolDefinition(name="get_pr_state", description="PR state.", live_read=live_read),
            ToolDefinition(name="update_pr", description="Push."),
        ],
        callable_exec,
    )
    return composite, executed


async def _run(composite, calls: list[list[ToolCall]]) -> list[ChatMessage]:
    llm = _ScriptedLLM([*(_tool_call_response(*batch) for batch in calls), ChatResponse(content="Done.", model="stub")])
    messages: list[ChatMessage] = [ChatMessage(role="user", content="go")]
    await al.run_agent_loop(
        llm=llm, messages=messages, tools=composite.all_tools, executor=composite, max_iterations=12
    )
    return messages


@pytest.mark.asyncio
async def test_read_write_read_returns_fresh(_quiet_langfuse):
    composite, executed = _live_executor(["checks pending", "checks green"])
    read = {"pr": 7}
    messages = await _run(
        composite,
        [
            [ToolCall(id="r1", name="get_pr_state", arguments=read)],
            [ToolCall(id="w1", name="update_pr", arguments={"pr": 7})],
            [ToolCall(id="r2", name="get_pr_state", arguments=read)],
        ],
    )
    assert executed == ["get_pr_state", "update_pr", "get_pr_state"]
    assert _tool_messages(messages, "r2")[0].content == "checks green"


@pytest.mark.asyncio
async def test_an_unknown_answer_is_never_cached_and_the_reread_is_fresh(_quiet_langfuse):
    """No write in between, seconds apart: a transient answer (GitHub still
    computing the mergeability) is re-read, a settled one is then served."""
    composite, executed = _live_executor(
        [ToolResult(tool_call_id="", content="merge unknown", transient=True), "merge clean"]
    )
    read = {"pr": 7}
    messages = await _run(
        composite,
        [
            [ToolCall(id="r1", name="get_pr_state", arguments=read)],
            [ToolCall(id="r2", name="get_pr_state", arguments=read)],
            [ToolCall(id="r3", name="get_pr_state", arguments=read)],
        ],
    )
    assert executed == ["get_pr_state", "get_pr_state"], "the unknown answer was re-read, the settled one cached"
    assert _tool_messages(messages, "r2")[0].content == "merge clean"
    assert _tool_messages(messages, "r3")[0].content.startswith(_DEDUP_CACHE_MARKER)


@pytest.mark.asyncio
async def test_a_transient_answer_in_a_parallel_batch_is_not_cached(_quiet_langfuse):
    composite, executed = _live_executor(
        [ToolResult(tool_call_id="", content="merge unknown", transient=True), "merge clean"]
    )
    await _run(
        composite,
        [
            [
                ToolCall(id="r1", name="get_pr_state", arguments={"pr": 7}),
                ToolCall(id="w0", name="update_pr", arguments={"pr": 8}),
            ],
            [ToolCall(id="r2", name="get_pr_state", arguments={"pr": 7})],
        ],
    )
    assert executed.count("get_pr_state") == 2


def test_a_live_read_entry_lives_its_window_not_the_default_bound():
    entry = al._CachedToolResult("open", ttl=al._LIVE_READ_WINDOW_S)
    assert al._cache_entry_fresh(entry, now=entry.stored_at + al._LIVE_READ_WINDOW_S - 1)
    assert not al._cache_entry_fresh(entry, now=entry.stored_at + al._LIVE_READ_WINDOW_S)
    assert al._LIVE_READ_WINDOW_S < al._DEDUP_CACHE_TTL_S
    plain = al._CachedToolResult("open")
    assert al._cache_entry_fresh(plain, now=plain.stored_at + al._LIVE_READ_WINDOW_S + 1)


@pytest.mark.asyncio
async def test_a_live_read_older_than_its_window_executes_again(_quiet_langfuse, monkeypatch):
    monkeypatch.setattr(al, "_LIVE_READ_WINDOW_S", 0.0)
    composite, executed = _live_executor(["checks pending", "checks green"])
    messages = await _run(
        composite,
        [
            [ToolCall(id="r1", name="get_pr_state", arguments={"pr": 7})],
            [ToolCall(id="r2", name="get_pr_state", arguments={"pr": 7})],
        ],
    )
    assert executed == ["get_pr_state"] * 2
    assert _tool_messages(messages, "r2")[0].content == "checks green"


@pytest.mark.asyncio
async def test_a_read_that_does_not_declare_live_read_keeps_the_default_bound(_quiet_langfuse, monkeypatch):
    monkeypatch.setattr(al, "_LIVE_READ_WINDOW_S", 0.0)
    composite, executed = _live_executor(["a", "b"], live_read=False)
    await _run(
        composite,
        [
            [ToolCall(id="r1", name="get_pr_state", arguments={"pr": 7})],
            [ToolCall(id="r2", name="get_pr_state", arguments={"pr": 7})],
        ],
    )
    assert executed == ["get_pr_state"]


def test_the_pr_checks_ci_and_work_item_reads_declare_live_read():
    import inspect

    from app.integrations.providers.github import actions, checks, pr_states, pulls, stale_prs
    from app.services.builtin_tools import work_items

    sources = {
        "get_github_pr": pulls,
        "list_github_pr_states": pr_states,
        "get_github_check_runs": checks,
        "list_github_workflow_runs": actions,
        "get_github_workflow_run": actions,
        "list_github_workflow_jobs": actions,
        "list_github_prs_needing_update": stale_prs,
        "get_work_item": work_items,
        "list_work_items": work_items,
    }
    for name, module in sources.items():
        text = inspect.getsource(module)
        at = text.index(f'name="{name}",')
        end = text.find("ToolDefinition(", at)
        assert "live_read=True" in text[at : end if end != -1 else len(text)], name
