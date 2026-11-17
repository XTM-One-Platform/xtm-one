"""Smoke tests for the agent chat orchestration surface (``agent_chat``).

``run_agent_chat`` is a ~3000-line orchestrator whose full LLM path requires
a live database, Redis, and a configured LLM provider — a complete mocked
end-to-end invocation through ``run_agent_loop`` is not robustly testable
without an integration environment (dozens of DB round-trips with distinct
result shapes plus MCP/integration/custom-tool loading).  These tests cover
the highest-value behavior that IS reachable deterministically:

* The **agentic quota gate**: a ``QuotaExceededError`` returns the friendly
  refusal message *before* any LLM resolution; anonymous users are metered
  against the platform-wide tiers alone.
* The **skip_llm fast path**: a genuine end-to-end ``run_agent_chat`` call
  (mocked Redis) that returns a response and streams it via ``on_token``
  without ever touching the LLM router.
* ``strip_file_content_from_history`` — pure transformation of the history
  the LLM receives.
* ``load_conversation_files`` — FileContext assembly for internal callers.
* Source-level pins for the **memory scope selection** (anonymous users are
  scoped to the conversation; identified users to their user id) so the
  documented behavior cannot regress silently.

Assessment finding: OBS-8 (critical untested modules).
"""

from __future__ import annotations

import asyncio
import inspect
import json
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import app.services.agent_chat as agent_chat_mod
from app.llm.base import ChatMessage
from app.services.quota import QuotaExceededError
from app.tools.base import ToolResult

USER_ID = "00000000-0000-0000-0000-00000000cccc"


def _agent(**overrides) -> SimpleNamespace:
    """Minimal agent stub for the pre-LLM sections of ``run_agent_chat``."""
    defaults = dict(
        id=uuid.UUID("00000000-0000-0000-0000-00000000dddd"),
        name="Smoke Agent",
        skip_llm=False,
        prepare_tool_ref=None,
        finalize_tool_ref=None,
        # The memory-scope derivation reads these for the two-factor agent-level
        # gate (is_tradecraft_writer_agent(agent.slug, is_builtin=...)); the real
        # Agent model always has them.
        slug="smoke-agent",
        is_builtin=False,
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


class _FakeRedis:
    """Redis stand-in recording incr/decr/expire for activity tracking."""

    def __init__(self) -> None:
        self.calls: list[tuple] = []

    async def incr(self, key):
        self.calls.append(("incr", key))
        return 1

    async def decr(self, key):
        self.calls.append(("decr", key))
        return 0

    async def expire(self, key, ttl):
        self.calls.append(("expire", key, ttl))
        return True

    async def delete(self, key):
        # The activity counter is deleted once it drops to zero on exit.
        self.calls.append(("delete", key))
        return 1


# ---------------------------------------------------------------------------
# Agentic quota gate
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_quota_exceeded_returns_refusal_before_llm_resolution():
    """A quota-exceeded user gets the friendly refusal and the LLM router
    is never consulted (the gate sits before all heavy setup)."""
    check = AsyncMock(
        side_effect=QuotaExceededError("agentic", 100, 100, "monthly")
    )
    increment = AsyncMock()
    get_llm = AsyncMock()

    with (
        patch("app.services.quota.check_agentic_quota", new=check),
        patch("app.services.quota.increment_agentic_usage", new=increment),
        patch("app.llm.router.get_llm_for_user", new=get_llm),
    ):
        text, new_agent_id = await agent_chat_mod.run_agent_chat(
            db=object(),  # type: ignore[arg-type]
            user_id=USER_ID,
            agent=_agent(),  # type: ignore[arg-type]
            history=[],
            user_message="hello",
        )

    assert "quota" in text.lower()
    assert new_agent_id is None
    check.assert_awaited_once()
    # Quota refusal must short-circuit: no usage increment, no LLM lookup.
    increment.assert_not_awaited()
    get_llm.assert_not_awaited()


@pytest.mark.asyncio
async def test_anonymous_user_is_metered_against_the_platform_wide_tiers():
    """Anonymous channel users have no account, so the quota service is asked
    about - and charges - the platform-wide tiers alone (``None``); the
    fast path then exits via skip_llm before the LLM."""
    check = AsyncMock()
    increment = AsyncMock()

    with (
        patch("app.services.quota.check_agentic_quota", new=check),
        patch("app.services.quota.increment_agentic_usage", new=increment),
        patch(
            "app.core.redis.redis_client",
            new=AsyncMock(return_value=_FakeRedis()),
        ),
    ):
        text, new_agent_id = await agent_chat_mod.run_agent_chat(
            db=object(),  # type: ignore[arg-type]
            user_id="anonymous",
            agent=_agent(skip_llm=True),  # type: ignore[arg-type]
            history=[],
            user_message="ping",
        )

    assert text == "ping"
    assert new_agent_id is None
    check.assert_awaited_once()
    assert check.await_args.args[1] is None
    increment.assert_awaited_once()
    assert increment.await_args.args[0] is None


# ---------------------------------------------------------------------------
# skip_llm fast path — mocked end-to-end happy path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_skip_llm_fast_path_streams_and_returns_message():
    """With ``skip_llm`` and no pipeline tools, ``run_agent_chat`` completes
    end to end: quota accounted, response streamed via ``on_token``,
    activity counter incremented and decremented — and the LLM router is
    never imported into the flow."""
    fake_redis = _FakeRedis()
    tokens: list[str] = []

    async def on_token(chunk: str) -> None:
        tokens.append(chunk)

    check = AsyncMock()
    increment = AsyncMock()
    get_llm = AsyncMock()

    with (
        patch("app.services.quota.check_agentic_quota", new=check),
        patch("app.services.quota.increment_agentic_usage", new=increment),
        patch(
            "app.core.redis.redis_client",
            new=AsyncMock(return_value=fake_redis),
        ),
        patch("app.llm.router.get_llm_for_user", new=get_llm),
    ):
        text, new_agent_id = await agent_chat_mod.run_agent_chat(
            db=object(),  # type: ignore[arg-type]
            user_id=USER_ID,
            agent=_agent(skip_llm=True),  # type: ignore[arg-type]
            history=[],
            user_message="echo this back",
            on_token=on_token,
        )

    assert text == "echo this back"
    assert new_agent_id is None
    assert tokens == ["echo this back"]
    # Quota was checked and usage incremented for the identified user.
    check.assert_awaited_once()
    increment.assert_awaited_once()
    assert increment.await_args.args[0] == uuid.UUID(USER_ID)
    # Activity tracking: one incr + expire on entry, one decr on exit.
    ops = [c[0] for c in fake_redis.calls]
    assert ops.count("incr") == 1
    assert ops.count("decr") == 1
    assert "expire" in ops
    # The LLM path was fully bypassed.
    get_llm.assert_not_awaited()


# ---------------------------------------------------------------------------
# Failure envelope + quota refund on the skip_llm pipeline path
# ---------------------------------------------------------------------------


def _pipeline_exec(result: ToolResult) -> SimpleNamespace:
    """A pipeline tool executor stub returning a fixed ToolResult."""
    exec_stub = SimpleNamespace(file_context=None)

    async def _execute_tool(_call):
        return result

    exec_stub.execute_tool = _execute_tool
    return exec_stub


@pytest.mark.asyncio
async def test_quota_exceeded_populates_error_envelope():
    """A quota refusal on the REST path fills the machine-readable envelope
    (not just the assistant text) so import-doc sees the real cause instead of
    a silent 200 — the same failure class the pipeline envelope fixes."""
    check = AsyncMock(
        side_effect=QuotaExceededError("agentic", 100, 100, "monthly")
    )
    error_out: dict = {}

    with (
        patch("app.services.quota.check_agentic_quota", new=check),
        patch("app.services.quota.increment_agentic_usage", new=AsyncMock()),
    ):
        text, _ = await agent_chat_mod.run_agent_chat(
            db=object(),  # type: ignore[arg-type]
            user_id=USER_ID,
            agent=_agent(),  # type: ignore[arg-type]
            history=[],
            user_message="extract",
            error_out=error_out,
        )

    assert "quota" in text.lower()
    assert error_out["error"] == text
    assert error_out["error_code"] == "quota_exceeded"


@pytest.mark.asyncio
async def test_skip_llm_prepare_refusal_refunds_and_skips_finalize():
    """A PREPARE refusal (is_error) surfaces the real cause in the envelope,
    refunds the metered quota (no billable work done), and SHORT-CIRCUITS the
    FINALIZE stage so its output cannot mask the refusal."""
    fake_redis = _FakeRedis()
    refund = AsyncMock()
    err_json = json.dumps({"error": "No enterprise licence configured."})

    load_mock = AsyncMock(
        return_value=(
            [],
            _pipeline_exec(ToolResult("", err_json, is_error=True)),
        )
    )
    error_out: dict = {}

    with (
        patch("app.services.quota.check_agentic_quota", new=AsyncMock()),
        patch("app.services.quota.increment_agentic_usage", new=AsyncMock()),
        patch("app.services.quota.refund_agentic_usage", new=refund),
        patch(
            "app.core.redis.redis_client",
            new=AsyncMock(return_value=fake_redis),
        ),
        patch.object(
            agent_chat_mod, "_load_pipeline_tool_standalone", new=load_mock
        ),
    ):
        text, _ = await agent_chat_mod.run_agent_chat(
            db=object(),  # type: ignore[arg-type]
            user_id=USER_ID,
            agent=_agent(  # type: ignore[arg-type]
                skip_llm=True,
                prepare_tool_ref={"tool_name": "extract_stix"},
                finalize_tool_ref={"tool_name": "finalize"},
            ),
            history=[],
            user_message="extract",
            error_out=error_out,
        )

    assert text == err_json
    refund.assert_awaited_once()
    # FINALIZE must be skipped after a PREPARE error → loader called once only.
    assert load_mock.await_count == 1
    assert error_out["error"] == err_json
    assert error_out["error_code"] == "extraction_failed"


@pytest.mark.asyncio
async def test_skip_llm_prepare_no_files_refunds_via_real_filigran_tool():
    """End-to-end integration: the REAL Filigran ``extract_stix`` prepare tool
    with NO attached files returns the "No files attached" refusal
    (``is_error``), and the skip_llm pipeline must treat that as refund-safe —
    reversing the metered agentic quota. This ties ``_call_api``'s refuse-
    before-work path to ``run_agent_chat``'s refund so a no-input import-doc
    call is never charged (the two are otherwise only covered by separate
    half-tests)."""
    import app.integrations.providers.filigran_ai as filigran_ai
    from app.tools.base import CallableToolExecutor

    fake_redis = _FakeRedis()
    refund = AsyncMock()

    executor = CallableToolExecutor()
    defs = filigran_ai.build_tools(
        token="https://stix.example.com||https://ttp.example.com",
        executor=executor,
    )
    # The pipeline assigns the run's (empty) file_context to this executor, so
    # the real extract_stix sees no files and refuses before any HTTP/PEM work.
    load_mock = AsyncMock(return_value=(defs, executor))
    error_out: dict = {}

    with (
        patch("app.services.quota.check_agentic_quota", new=AsyncMock()),
        patch("app.services.quota.increment_agentic_usage", new=AsyncMock()),
        patch("app.services.quota.refund_agentic_usage", new=refund),
        patch(
            "app.core.redis.redis_client",
            new=AsyncMock(return_value=fake_redis),
        ),
        patch.object(
            agent_chat_mod, "_load_pipeline_tool_standalone", new=load_mock
        ),
    ):
        text, _ = await agent_chat_mod.run_agent_chat(
            db=object(),  # type: ignore[arg-type]
            user_id=USER_ID,
            agent=_agent(  # type: ignore[arg-type]
                skip_llm=True,
                prepare_tool_ref={"tool_name": "extract_stix"},
            ),
            history=[],
            user_message="extract",
            error_out=error_out,
        )

    assert "No files attached" in text
    refund.assert_awaited_once()
    assert error_out["error_code"] == "extraction_failed"


@pytest.mark.asyncio
async def test_skip_llm_finalize_failure_does_not_refund():
    """A FINALIZE failure surfaces the error but must NOT refund: PREPARE
    already ran (and may have billed real extraction work)."""
    fake_redis = _FakeRedis()
    refund = AsyncMock()
    prep_ok = json.dumps({"bundle": "ok"})
    fin_err = json.dumps({"error": "save failed"})

    load_mock = AsyncMock(
        side_effect=[
            ([], _pipeline_exec(ToolResult("", prep_ok, is_error=False))),
            ([], _pipeline_exec(ToolResult("", fin_err, is_error=True))),
        ]
    )
    error_out: dict = {}

    with (
        patch("app.services.quota.check_agentic_quota", new=AsyncMock()),
        patch("app.services.quota.increment_agentic_usage", new=AsyncMock()),
        patch("app.services.quota.refund_agentic_usage", new=refund),
        patch(
            "app.core.redis.redis_client",
            new=AsyncMock(return_value=fake_redis),
        ),
        patch.object(
            agent_chat_mod, "_load_pipeline_tool_standalone", new=load_mock
        ),
    ):
        text, _ = await agent_chat_mod.run_agent_chat(
            db=object(),  # type: ignore[arg-type]
            user_id=USER_ID,
            agent=_agent(  # type: ignore[arg-type]
                skip_llm=True,
                prepare_tool_ref={"tool_name": "extract_stix"},
                finalize_tool_ref={"tool_name": "finalize"},
            ),
            history=[],
            user_message="extract",
            error_out=error_out,
        )

    assert text == fin_err
    # Work was done in PREPARE — the metered quota must stay charged.
    refund.assert_not_awaited()
    assert error_out["error_code"] == "finalize_failed"


# ---------------------------------------------------------------------------
# strip_file_content_from_history — pure history transformation
# ---------------------------------------------------------------------------


class TestStripFileContentFromHistory:
    def test_plain_messages_pass_through_unchanged(self):
        history = [
            ChatMessage(role="user", content="hello"),
            ChatMessage(role="assistant", content="hi there"),
        ]
        cleaned = agent_chat_mod.strip_file_content_from_history(history)
        # Untouched messages are reused (same objects), not copied.
        assert cleaned[0] is history[0]
        assert cleaned[1] is history[1]

    def test_legacy_inline_file_block_is_stripped(self):
        msg = ChatMessage(
            role="user",
            content="Summarize this\n\n---\n\n[File: report.csv]\n\ncol1,col2\n1,2",
        )
        cleaned = agent_chat_mod.strip_file_content_from_history([msg])
        assert "[File:" not in cleaned[0].content
        assert "col1,col2" not in cleaned[0].content
        assert cleaned[0].content == "Summarize this"

    def test_message_that_is_only_a_file_block_gets_marker(self):
        msg = ChatMessage(
            role="user", content="[File: notes.txt]\n\nsome file body"
        )
        cleaned = agent_chat_mod.strip_file_content_from_history([msg])
        assert cleaned[0].content == "(file content removed)"

    def test_structured_files_are_preserved_and_no_marker_added(self):
        # Structured attachments are re-inlined fresh each turn — stripping
        # must not clear them, and an empty body needs no marker.
        fake_ref = SimpleNamespace(file_id="f1")
        msg = ChatMessage(
            role="user",
            content="[File: data.csv]\n\na,b\n1,2",
            files=[fake_ref],
        )
        cleaned = agent_chat_mod.strip_file_content_from_history([msg])
        assert cleaned[0].files == [fake_ref]
        assert cleaned[0].content == ""

    def test_role_and_metadata_preserved_on_rebuilt_message(self):
        msg = ChatMessage(
            role="assistant",
            content="answer\n\n---\n\n[File: x.txt]\n\nbody",
            tool_call_id="tc-1",
            name="tool",
        )
        cleaned = agent_chat_mod.strip_file_content_from_history([msg])
        assert cleaned[0].role == "assistant"
        assert cleaned[0].tool_call_id == "tc-1"
        assert cleaned[0].name == "tool"
        assert cleaned[0].content == "answer"


# ---------------------------------------------------------------------------
# load_conversation_files — FileContext assembly without a conversation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_load_conversation_files_inline_refs_without_conversation():
    """Internal callers without a conversation_id must still get their
    current-turn files into the FileContext — and the DB is never touched."""
    ref_a = SimpleNamespace(file_id="file-a")
    ref_b = SimpleNamespace(file_id="file-b")

    ctx = await agent_chat_mod.load_conversation_files(None, None, [ref_a, ref_b])  # type: ignore[arg-type]

    assert ctx.get("file-a") is ref_a
    assert ctx.get("file-b") is ref_b
    assert len(ctx.list_files()) == 2


@pytest.mark.asyncio
async def test_load_conversation_files_empty_inputs_yield_empty_context():
    ctx = await agent_chat_mod.load_conversation_files(None, None, None)  # type: ignore[arg-type]
    assert ctx.is_empty


# ---------------------------------------------------------------------------
# Memory scope selection — source-level pins (lines ~762-827)
# ---------------------------------------------------------------------------
# The scope selection is buried mid-function behind a live DB + LLM router;
# pin the documented invariants at source level (the established pattern in
# this suite for orchestrator internals, see test_agent_generated_files.py).


class TestMemoryScopeSelectionSource:
    SOURCE = inspect.getsource(agent_chat_mod.run_agent_chat)

    def test_anonymous_scopes_to_conversation_identified_to_user(self):
        # Anonymous users must be scoped to the conversation id so context
        # never leaks across conversations; identified users get a strict
        # per-user scope. The decision now lives in the pure, unit-testable
        # _derive_memory_scope_id helper (WS5); run_agent_chat delegates to it,
        # so assert both the delegation and the helper's anonymous/identified
        # contract here.
        assert "memory_scope_id = _derive_memory_scope_id(" in self.SOURCE
        assert (
            agent_chat_mod._derive_memory_scope_id(
                is_anonymous_user=True,
                is_agent_level_writer=False,
                is_trusted_run=False,
                conversation_id="conv-1",
                user_id="",
            )
            == "conv-1"
        )
        assert (
            agent_chat_mod._derive_memory_scope_id(
                is_anonymous_user=False,
                is_agent_level_writer=False,
                is_trusted_run=False,
                conversation_id="conv-1",
                user_id="user-1",
            )
            == "user-1"
        )

    def test_persistent_memory_drops_user_scope_for_anonymous(self):
        # The durable tier must not attribute anonymous turns to a user.
        assert "user_id=None if is_anonymous_user else user_id" in self.SOURCE

    def test_anonymous_detection_is_uuid_validity(self):
        # "anonymous" (and any non-UUID id) routes to the anonymous branch.
        assert "is_anonymous_user = not is_valid_uuid(user_id)" in self.SOURCE


# TODO: these two pins assert that a log message exists in the source,
# so they survive someone keeping the log line while deleting the refund or the
# decrement, and break on innocent rewording. Replace them with real unit tests
# by extracting the refund + activity-counter release from the ``get_llm_for_user``
# except block into a module-level ``_abort_before_llm(db, user_id, agent_id,
# quota_metered, activity_key, activity_tracked)`` coroutine that can be driven
# with mocks (no live DB) — then delete both pins.


def test_llm_resolution_failure_refunds_metered_quota_source_pin():
    """An LLM-path failure before any billable work (e.g. no provider
    configured) must refund the up-front metered agentic quota — otherwise a
    request that never ran the model is charged. Pinned at source level because
    reaching the LLM-resolution line requires a live DB (the memory/KB setup in
    between)."""
    src = inspect.getsource(agent_chat_mod.run_agent_chat)
    assert (
        "Failed to refund agentic quota after LLM-resolution" in src
    ), "the get_llm_for_user failure path must refund the metered agentic quota"


def test_llm_resolution_failure_releases_activity_counter_source_pin():
    """The same LLM-resolution failure re-raises BEFORE the outer try/finally
    that clears the chat activity counter is entered, so that path must release
    the counter itself — otherwise the agent shows as permanently "active" until
    the key's TTL expires. Pinned at source level for the same live-DB reason as
    the refund pin above; the decrement must live in the same except block."""
    src = inspect.getsource(agent_chat_mod.run_agent_chat)
    # Match a single source line (the message is split across two literals).
    release_marker = "Failed to decrement chat activity counter after "
    assert (
        release_marker in src
    ), "the get_llm_for_user failure path must release the chat activity counter"
    # The release must be inside the LLM-resolution except block, i.e. after the
    # refund and before the trailing bare ``raise`` that exits the branch.
    refund_at = src.index("Failed to refund agentic quota after LLM-resolution")
    release_at = src.index(release_marker)
    assert refund_at < release_at, "release must follow the refund in the same block"


def test_module_constants_smoke():
    """Import-level smoke check for the orchestrator module."""
    assert agent_chat_mod._CHAT_ACTIVITY_TTL >= 1800
    assert callable(agent_chat_mod.run_agent_chat)


# ---------------------------------------------------------------------------
# Root-trace guard: setup failures still close the turn's trace
# ---------------------------------------------------------------------------


def _traced_agent(**overrides) -> SimpleNamespace:
    """Agent stub reaching past the trace open, into the memory-load window.

    ``skip_llm`` returns long before the trace exists, so the guard has to be
    exercised on the full path — which reads more of the agent than the
    pre-LLM stubs above.
    """
    defaults = dict(
        slug="traced-agent",
        template_slug=None,
        is_company_managed=False,
        knowledge_base_ids=[],
        mcp_server_ids=[],
        llm_model=None,
        llm_provider=None,
        llm_provider_id=None,
        memory_short_term_enabled=True,
        memory_medium_term_enabled=False,
        memory_persistent_enabled=False,
        memory_user_enabled=False,
    )
    defaults.update(overrides)
    return _agent(**defaults)


@pytest.mark.asyncio
async def test_setup_failure_after_trace_open_still_ends_the_trace():
    """The root trace is opened before memory loading so the MEMORY span can
    nest under it — which puts the whole setup phase (memory, variables,
    integrations/KB gather, MCP connects, custom-tool loading) between the
    trace open and the agent loop.

    Everything in that window is inside the turn's ``try``, so a raise there
    still runs ``end_trace`` (the only path that writes the durable
    ``llm_traces`` row and flushes the local spans) and still pops the
    contextvar instead of leaking a stale parent into the next task.
    """
    from app.services import langfuse_tracing as lft

    trace_ctx = {"trace": None, "local_spans": [], "sentinel": "root"}
    end_trace = MagicMock()
    fake_redis = _FakeRedis()

    with (
        patch("app.services.quota.check_agentic_quota", new=AsyncMock()),
        patch("app.services.quota.increment_agentic_usage", new=AsyncMock()),
        patch(
            "app.core.redis.redis_client",
            new=AsyncMock(return_value=fake_redis),
        ),
        patch(
            "app.services.langfuse_tracing.trace_llm_call",
            new=AsyncMock(return_value=trace_ctx),
        ),
        # Stands in for any of the ~24 unguarded awaits in the setup window.
        patch(
            "app.services.langfuse_tracing.start_memory_span",
            new=MagicMock(side_effect=RuntimeError("setup blew up")),
        ),
        patch("app.services.langfuse_tracing.end_trace", new=end_trace),
        pytest.raises(RuntimeError, match="setup blew up"),
    ):
        await agent_chat_mod.run_agent_chat(
            db=object(),  # type: ignore[arg-type]
            # Anonymous participant: no user row to fetch, so the setup
            # reaches the trace open without a live database.
            user_id="anon-participant",
            agent=_traced_agent(),  # type: ignore[arg-type]
            history=[],
            user_message="hello",
            conversation_id="conv-1",
            prebuilt_llm=(SimpleNamespace(provider_name="fake"), "fake-model"),
        )

    end_trace.assert_called_once()
    assert end_trace.call_args.args[0] is trace_ctx
    assert "setup blew up" in end_trace.call_args.kwargs["error"]
    # Contextvar popped: the next turn on this task must not inherit it.
    assert lft.get_current_trace_ctx() is None
    # The activity counter is decremented on the way out too.
    assert [c[0] for c in fake_redis.calls].count("decr") == 1


@pytest.mark.asyncio
async def test_cancelled_setup_writes_an_error_row_not_a_successful_one():
    """A cancelled turn must not land in ``llm_traces`` as a success.

    ``end_trace`` branches on the truthiness of ``error`` (``"error" if error
    else "ok"``), and ``str(asyncio.CancelledError())`` is ``""``.  Passing the
    stringified exception straight through would therefore write a killed turn
    as ``status='ok'`` / ``level='DEFAULT'`` with no ``error_message`` and no
    output — a false success, which is worse than the missing row this
    replaced (before the trace was opened this early, ``CancelledError`` simply
    bypassed the ``except Exception`` handlers and nothing was written).

    Cancellation out of ``run_agent_chat`` is a real path, not a hypothetical:
    ``api/v1/chat.py`` handles it explicitly for server shutdown and connection
    teardown.  ``_finish_trace`` substitutes a non-empty label, so this
    drives the *real* ``end_trace`` down to the durable payload and asserts on
    the row that would be persisted.  The sibling test above uses a
    ``RuntimeError`` with a message, so it is exactly the case that misses.
    """
    from app.services import langfuse_tracing as lft

    # Local-only root ctx — the shape ``trace_llm_call`` returns with Langfuse
    # disabled, which is the configuration that relies on the durable row.
    trace_ctx = {
        "trace": None,
        "local_spans": [],
        "local_trace_id": uuid.uuid4(),
        "local_name": "agent-chat:traced-agent",
        "local_source": "traced-agent",
    }
    submitted: list = []
    fake_redis = _FakeRedis()

    with (
        patch("app.services.quota.check_agentic_quota", new=AsyncMock()),
        patch("app.services.quota.increment_agentic_usage", new=AsyncMock()),
        patch(
            "app.core.redis.redis_client",
            new=AsyncMock(return_value=fake_redis),
        ),
        patch(
            "app.services.langfuse_tracing.trace_llm_call",
            new=AsyncMock(return_value=trace_ctx),
        ),
        # Stands in for a cancellation landing anywhere in the setup window.
        patch(
            "app.services.langfuse_tracing.start_memory_span",
            new=MagicMock(side_effect=asyncio.CancelledError()),
        ),
        patch(
            "app.services.tracing.dispatcher.submit_trace_for_flush",
            new=MagicMock(side_effect=lambda *a, **k: submitted.append((a, k))),
        ),
        pytest.raises(asyncio.CancelledError),
    ):
        await agent_chat_mod.run_agent_chat(
            db=object(),  # type: ignore[arg-type]
            user_id="anon-participant",
            agent=_traced_agent(),  # type: ignore[arg-type]
            history=[],
            user_message="hello",
            conversation_id="conv-1",
            prebuilt_llm=(SimpleNamespace(provider_name="fake"), "fake-model"),
        )

    # The durable row: an error, not a silent success.
    assert trace_ctx["local_status"] == "error"
    assert trace_ctx["local_error_message"]
    assert submitted, "the cancelled turn was never submitted for flush"
    trace_data = submitted[0][0][0]
    assert trace_data.status == "error"
    assert trace_data.level == "ERROR"
    assert trace_data.error_message
    # Contextvar popped even on cancellation.
    assert lft.get_current_trace_ctx() is None
