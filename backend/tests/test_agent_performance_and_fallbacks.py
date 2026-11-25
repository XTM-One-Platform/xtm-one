"""Regression coverage for the agent-performance + silent-fallback batch.

These tests pin down the high-impact fixes that turned previously-silent
failures into ERROR logs and parallelised serial loaders / embedders.  A
regression that re-introduces the silent fallback (or drops the
parallel ``asyncio.gather``) will fail loudly here instead of silently
hiding production problems.

All tests are pure unit tests with mocked LLM / DB dependencies — no
network, no database, no Redis.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# parse_tool_arguments — malformed JSON must log WARNING with tool name
# ---------------------------------------------------------------------------


def test_parse_tool_arguments_logs_warning_on_malformed_json(
    caplog: pytest.LogCaptureFixture,
):
    """The LLM occasionally emits invalid JSON for tool arguments
    (truncation, escape glitches, etc.). The fallback ``{"raw": raw}``
    used to be silent, making "tool received unexpected args" errors
    impossible to diagnose. This test pins the WARNING contract so a
    regression that swallows the log fails loudly.
    """
    from app.llm.base import parse_tool_arguments

    caplog.set_level(logging.WARNING, logger="app.llm.base")
    result = parse_tool_arguments(
        '{"title": "broken', tool_name="github_create_issue"
    )
    assert result == {"raw": '{"title": "broken'}, (
        "Fallback must still return the raw payload under 'raw' so the "
        "tool executor receives *something* and can fail explicitly."
    )
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) >= 1, (
        "Malformed JSON must produce at least one WARNING — a regression "
        "that silently returns {'raw': raw} would fail this assertion."
    )
    msg = " ".join(r.getMessage() for r in warnings)
    assert "github_create_issue" in msg, (
        "WARNING must include the tool name so operators can diagnose "
        "*which* tool the LLM is mis-emitting JSON for."
    )


def test_parse_tool_arguments_empty_returns_empty_dict_without_warning(
    caplog: pytest.LogCaptureFixture,
):
    """Empty/None args (legitimate for parameterless tools) must NOT
    produce a WARNING — pin the no-noise contract so a regression that
    treats empty input as malformed doesn't flood production logs.
    """
    from app.llm.base import parse_tool_arguments

    caplog.set_level(logging.WARNING, logger="app.llm.base")
    assert parse_tool_arguments("", tool_name="ping") == {}
    assert parse_tool_arguments("{}", tool_name="ping") == {}
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert not warnings, (
        f"Empty / valid args must be silent; got warnings: {warnings}"
    )


def test_parse_tool_arguments_warning_does_not_leak_raw_payload(
    caplog: pytest.LogCaptureFixture,
):
    """The WARNING must not log ``raw[:200]``,
    which can leak email bodies / prompt content / credentials that the
    LLM tried to forward to a tool. WARNING-level logs are routinely
    aggregated to external observability services (Datadog, Sentry,
    Splunk), so a 200-char snippet of arbitrary user / tool payload is
    a real data-exfiltration surface, especially when the very payload
    that tripped the JSON parser is most likely the one containing weird
    content.

    Pin the new redacted shape: the WARNING must contain tool name +
    JSON error location + payload length + a 16-char SHA-256 correlation
    hash, but MUST NOT contain the raw payload bytes.
    """
    import re

    from app.llm.base import parse_tool_arguments

    # A payload that visibly contains "secrets" — a regression that
    # restores the raw snippet log would let this leak into the log.
    sensitive_raw = (
        '{"email_body": "Hi John, the password is hunter2 '
        'and the api_key is sk-secret-NEVER-LOG-ME-please ' + "x" * 50 + '"}'
    )
    # Truncate to force a JSON decode failure (closing quote + brace gone).
    sensitive_raw_truncated = sensitive_raw[:120]

    caplog.set_level(logging.WARNING, logger="app.llm.base")
    result = parse_tool_arguments(
        sensitive_raw_truncated, tool_name="send_email"
    )
    # Fallback contract unchanged: the raw payload still reaches the
    # tool executor under the "raw" key. ONLY the logging changed.
    assert result == {"raw": sensitive_raw_truncated}

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert warnings, "Malformed JSON must still produce a WARNING."

    full_log = " ".join(r.getMessage() for r in warnings)

    # The redacted shape must contain the diagnostic signals.
    assert "tool=send_email" in full_log, (
        "WARNING must name the tool that broke."
    )
    assert "raw_len=" in full_log, (
        "WARNING must include the raw payload length so operators can "
        "triage 'tiny invalid payload' vs 'truncated mid-stream'."
    )
    assert re.search(r"raw_sha256=[0-9a-f]{16}\b", full_log), (
        "WARNING must include a 16-char SHA-256 correlation hash so "
        "operators can count duplicates without seeing payload content."
    )
    assert "pos=" in full_log, (
        "WARNING must include the JSON error byte offset for triage."
    )

    # AND it must NOT contain payload bytes. A regression that restores
    # the ``raw[:200]`` snippet log would fail every one of these.
    for sensitive_marker in (
        "hunter2",
        "sk-secret-NEVER-LOG-ME-please",
        "email_body",
        "Hi John",
        "password",
        "api_key",
    ):
        assert sensitive_marker not in full_log, (
            f"WARNING must NOT contain payload bytes ({sensitive_marker!r} "
            f"leaked). A regression to the raw[:200] snippet log would "
            f"trip this assertion. Got log: {full_log!r}"
        )


# ---------------------------------------------------------------------------
# load_mcp_tools — total failure must ERROR, not silently disappear
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_load_mcp_tools_db_failure_logs_error_not_warning(
    caplog: pytest.LogCaptureFixture,
):
    """When the DB lookup for MCP servers blows up, the previous
    behaviour was a quiet WARNING + return None — every MCP tool the
    agent had configured silently disappeared with no operator-visible
    signal. Pin the new ERROR contract.
    """
    from app.services import builtin_tools

    composite = MagicMock()

    class _BrokenDB:
        async def execute(self, *args, **kwargs):
            raise RuntimeError("simulated DB outage")

    caplog.set_level(logging.ERROR, logger="app.services.builtin_tools")
    result = await builtin_tools.load_mcp_tools(
        _BrokenDB(),  # type: ignore[arg-type]
        ["00000000-0000-0000-0000-000000000001"],
        composite,
    )
    assert result is None
    errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert errors, (
        "Total MCP load failure must produce an ERROR log so the "
        "silent 'agent has no tools' failure mode is finally diagnosable."
    )
    assert "MCP tools" in errors[0].getMessage()


@pytest.mark.asyncio
async def test_load_custom_tools_db_failure_logs_error(
    caplog: pytest.LogCaptureFixture,
):
    """Same contract as ``load_mcp_tools``: total failure must ERROR."""
    from app.services import builtin_tools

    composite = MagicMock()

    class _BrokenDB:
        async def execute(self, *args, **kwargs):
            raise RuntimeError("simulated DB outage")

    caplog.set_level(logging.ERROR, logger="app.services.builtin_tools")
    await builtin_tools.load_custom_tools(
        _BrokenDB(),  # type: ignore[arg-type]
        ["00000000-0000-0000-0000-000000000002"],
        composite,
    )
    errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert errors, (
        "Total custom-tool load failure must produce an ERROR log."
    )
    assert "custom tools" in errors[0].getMessage()


# ---------------------------------------------------------------------------
# embed_text — embedding generation failure must ERROR (corrupted memory)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_embed_text_failure_logs_error_for_unsearchable_memory(
    caplog: pytest.LogCaptureFixture,
):
    """A ``None`` embedding causes ``persist_memory`` to store an entry
    *without* an embedding vector — invisible to semantic search forever.
    Over time this silently corrupts the memory index. ERROR-level so
    operators see the failure before it accumulates.
    """
    from app.services import persistent_memory

    class _BrokenProvider:
        async def embed(self, text, model=None):
            raise RuntimeError("embedding provider down")

    caplog.set_level(logging.ERROR, logger="app.services.persistent_memory")
    result = await persistent_memory.embed_text(
        None,  # type: ignore[arg-type]
        "some fact",
        _provider_model=(_BrokenProvider(), "fake-model"),
    )
    assert result is None, "embed_text returns None on failure (contract)"
    errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert errors, (
        "Embedding failure must ERROR — silent fallback to None stores "
        "an unsearchable memory entry and corrupts the memory index."
    )


# ---------------------------------------------------------------------------
# reconcile_and_persist_facts — embeddings must be gathered concurrently
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reconcile_embeds_facts_in_parallel(monkeypatch):
    """The pre-fix loop awaited each ``embed_text`` sequentially, turning
    a 10-fact compaction into O(n) embedding latency. Lock the new
    parallel-gather contract **deterministically** by counting the
    maximum number of concurrently in-flight ``embed_text`` calls — a
    sequential loop sees at most one call active at a time, while
    ``asyncio.gather`` over the fact list sees all facts active
    simultaneously.

    Earlier revisions of this test used a wall-clock ceiling
    (``elapsed < 0.30`` against 5 × ``await asyncio.sleep(0.1)``); that
    flaked on busy CI workers, slow Windows event-loop sleep precision
    (~16 ms), and GC pauses inside the test runner. The
    max-concurrency counter is contract-on-shape rather than
    contract-on-speed, so it can't flake.
    """
    from app.services import persistent_memory

    # Stub _extract_candidate_facts so we control the fact count without
    # invoking an LLM.
    fake_facts = [f"fact #{i}" for i in range(5)]

    async def _fake_extract(*args, **kwargs):
        return fake_facts

    monkeypatch.setattr(
        persistent_memory,
        "_extract_candidate_facts",
        _fake_extract,
    )

    # Stub embed_text to bump an "active call" counter on entry, yield
    # to the event loop (so concurrent gathered tasks have a chance to
    # also enter), then decrement on exit. ``max_concurrent`` records
    # the high-water mark across the whole run.
    call_count = 0
    active = 0
    max_concurrent = 0

    async def _instrumented_embed(*args, **kwargs):
        nonlocal call_count, active, max_concurrent
        call_count += 1
        active += 1
        max_concurrent = max(max_concurrent, active)
        try:
            # A single ``await`` is enough — it relinquishes control to
            # the event loop, which under ``asyncio.gather`` schedules
            # the next gathered coroutine. With 5 facts gathered, all
            # 5 reach ``active += 1`` before any of them resume; under
            # a sequential ``for fact in facts: await embed_text(...)``
            # the next coroutine doesn't even exist until the current
            # one completes, so ``active`` never exceeds 1.
            await asyncio.sleep(0)
            return [0.0] * 4  # dummy embedding vector
        finally:
            active -= 1

    monkeypatch.setattr(persistent_memory, "embed_text", _instrumented_embed)

    # Stub _find_related_facts to return empty — keeps the test free of
    # DB dependencies and short-circuits the curator-LLM branch.
    async def _no_related(*args, **kwargs):
        return []

    monkeypatch.setattr(
        persistent_memory, "_find_related_facts", _no_related
    )

    # Stub persist_memory so we don't touch the DB.
    async def _fake_persist(*args, **kwargs):
        return None

    monkeypatch.setattr(persistent_memory, "persist_memory", _fake_persist)

    # Stub the embedding provider lookup (returns a sentinel tuple).
    fake_provider = MagicMock()

    async def _fake_get_embedding_provider(*args, **kwargs):
        return (fake_provider, "fake-model", "fake-id")

    # ``in_transaction`` backs the commit-release points; returning
    # False keeps the fake session transaction-less (a bare MagicMock would
    # be truthy and trigger an un-awaitable ``commit()``).
    fake_db = MagicMock()
    fake_db.in_transaction = lambda: False

    with patch(
        "app.llm.router.get_embedding_provider",
        _fake_get_embedding_provider,
    ):
        await persistent_memory.reconcile_and_persist_facts(
            db=fake_db,
            llm=AsyncMock(),
            agent_id="00000000-0000-0000-0000-000000000abc",
            messages_text="dummy transcript",
            user_id=None,
            is_company_managed=False,
        )

    assert call_count == len(fake_facts), (
        f"All {len(fake_facts)} facts must be embedded; got {call_count}"
    )
    # The deterministic concurrency assertion: with ``asyncio.gather``,
    # all 5 facts enter ``embed_text`` before any of them finish, so
    # ``max_concurrent`` reaches exactly ``len(fake_facts)``. With a
    # sequential ``for ... await`` loop, only one is ever active.
    assert max_concurrent == len(fake_facts), (
        f"Embeddings must run concurrently — observed max in-flight "
        f"calls = {max_concurrent}, expected {len(fake_facts)}. A "
        f"regression that re-introduces the sequential `for fact in "
        f"facts: emb = await embed_text(...)` would cap "
        f"``max_concurrent`` at 1."
    )


# ---------------------------------------------------------------------------
# _stream_text_chunked — short payloads skip artificial typing delay
# ---------------------------------------------------------------------------
#
# Both tests below mock ``app.tools.agent_loop.asyncio.sleep`` with an
# ``AsyncMock`` and assert against its ``await_count`` instead of using a
# wall-clock ceiling. The previous shape used ``elapsed < 0.005`` /
# ``elapsed < 0.10`` ceilings which can flake on busy CI runners even when
# ``_stream_text_chunked`` is correct (Windows event-loop sleep precision
# is ~16 ms, a GC pause inside the test runner can blow either ceiling).
# ``await_count`` is a direct, deterministic pin on the contract under
# test: short payload (<= ``_SIMULATED_CHUNK_SIZE``) must NOT sleep at
# all; long payload sleeps once between two consecutive chunks, i.e.
# ``ceil(len / chunk_size) - 1`` times — the delay paces the next chunk,
# so nothing waits once the last one is out.


@pytest.mark.asyncio
async def test_stream_text_chunked_empty_payload_emits_nothing(monkeypatch):
    """Empty payload must be a complete no-op — no token event, no sleep.

    The pre-fix ``range(0, len(text), chunk_size)`` loop emitted nothing
    for ``text == ""``; the new short-payload fast path would otherwise
    forward the empty string to ``_stream_token`` and surface a spurious
    empty-token event to the frontend (e.g. when a tool-result follow-up
    legitimately ends up empty). Pin the no-event contract.
    """
    from unittest.mock import AsyncMock

    from app.tools import agent_loop

    sleep_mock = AsyncMock()
    monkeypatch.setattr(agent_loop.asyncio, "sleep", sleep_mock)

    received_chunks: list[str] = []

    async def _capture(chunk: str) -> None:
        received_chunks.append(chunk)

    await agent_loop._stream_text_chunked(_capture, "")

    assert received_chunks == [], (
        f"Empty payload must NOT produce any token event — "
        f"observed chunks: {received_chunks!r}"
    )
    assert sleep_mock.await_count == 0, (
        f"Empty payload must NOT trigger any sleep — "
        f"observed {sleep_mock.await_count} await(s)."
    )


@pytest.mark.asyncio
async def test_stream_text_chunked_short_payload_skips_sleep(monkeypatch):
    """Tool-result follow-ups under the chunk size shouldn't pretend to
    type — the artificial sleep used to add 150–300 ms per tool iteration
    for no user-visible benefit. Pin the no-sleep contract for short
    payloads.
    """
    from unittest.mock import AsyncMock

    from app.tools import agent_loop

    sleep_mock = AsyncMock()
    monkeypatch.setattr(agent_loop.asyncio, "sleep", sleep_mock)

    received_chunks: list[str] = []

    async def _capture(chunk: str) -> None:
        received_chunks.append(chunk)

    short_text = "ok"  # well under _SIMULATED_CHUNK_SIZE
    await agent_loop._stream_text_chunked(_capture, short_text)

    assert received_chunks == [short_text], (
        "Short payload must be delivered as a single chunk."
    )
    assert sleep_mock.await_count == 0, (
        f"Short payload must NOT trigger any ``asyncio.sleep`` call — "
        f"observed {sleep_mock.await_count} await(s). The pre-fix shape "
        f"would sleep once per chunk regardless of payload size."
    )


@pytest.mark.asyncio
async def test_stream_text_chunked_long_payload_chunks_with_minimal_sleep(
    monkeypatch,
):
    """Long payloads still chunk for the typing effect, but with the
    reduced 2 ms-per-chunk delay (was 10 ms — saved 150–300 ms per tool
    iteration on average-sized tool responses). Pin the chunk count AND
    the per-chunk sleep value deterministically — no wall-clock.
    """
    from unittest.mock import AsyncMock

    from app.tools import agent_loop

    sleep_mock = AsyncMock()
    monkeypatch.setattr(agent_loop.asyncio, "sleep", sleep_mock)

    received_chunks: list[str] = []

    async def _capture(chunk: str) -> None:
        received_chunks.append(chunk)

    # 1024 chars / 256 chunk size = 4 chunks, one sleep BETWEEN two of
    # them (3 in all): the delay paces the next chunk, and nothing waits
    # once the last one is out.
    long_text = "x" * 1024
    await agent_loop._stream_text_chunked(_capture, long_text)

    assert len(received_chunks) >= 4, (
        f"Long payload must be split into multiple chunks; got "
        f"{len(received_chunks)}"
    )
    assert "".join(received_chunks) == long_text, (
        "Chunks must reconstruct the original text exactly."
    )
    # One sleep between two chunks. A regression to the pre-fix 64-char
    # chunks would produce 1024/64 = 16 chunks and 15 sleeps, which would
    # trip this assertion deterministically.
    assert sleep_mock.await_count == len(received_chunks) - 1 == 3, (
        f"Long payload must sleep once between two chunks — observed "
        f"{sleep_mock.await_count} sleep(s) across {len(received_chunks)} "
        f"chunks. Pre-fix shape (64-char chunks) would yield 15 sleeps."
    )
    # Each sleep must use the reduced 2 ms delay — pinning the value
    # prevents a regression to the previous 10 ms cadence.
    for call in sleep_mock.await_args_list:
        delay = call.args[0] if call.args else call.kwargs.get("delay")
        assert delay == pytest.approx(0.002, abs=1e-6), (
            f"Per-chunk sleep must be 2 ms — observed {delay!r}. "
            f"Pre-fix shape used 10 ms."
        )


# ---------------------------------------------------------------------------
# Large system prompt observability — assignment path now warns
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_assignment_large_system_prompt_logs_warning(
    caplog: pytest.LogCaptureFixture,
):
    """The chat path already warned on oversized prompts; assignments
    used to be silent. Pin the new WARNING so operators see when an
    assignment is accreting a 25K+ token system prompt that's silently
    degrading instruction-following + slowing the LLM.
    """
    import uuid

    from app.models.agent import Agent
    from app.models.assignment import Assignment
    from app.services.assignment_helpers import _build_system_message

    # Build a persona large enough to exceed the 20K-token threshold
    # (~60K chars at ~3 chars/token).
    huge_persona = "X " * 35_000  # 70K chars → ~23K tokens

    agent = Agent(name="Bloated agent", persona=huge_persona)
    assignment = Assignment(
        agent_id=uuid.uuid4(),
        name="Bloated assignment",
        trigger_type="webhook",
        trigger_config={},
        prompt="Process the payload",
    )

    caplog.set_level(
        logging.WARNING, logger="app.services.assignment_helpers"
    )

    class _FakeScalarResult:
        def scalar_one_or_none(self):
            return None

    class _FakeDB:
        async def execute(self, *args, **kwargs):
            return _FakeScalarResult()

    await _build_system_message(
        db=_FakeDB(),  # type: ignore[arg-type]
        agent=agent,
        assignment=assignment,
        user_id=None,
        user_memory_text="",
        agent_memory_text="",
        element_bindings={},
    )

    warnings = [
        r
        for r in caplog.records
        if r.levelno == logging.WARNING
        and "Large assignment system prompt" in r.getMessage()
    ]
    assert warnings, (
        "Oversized assignment system prompt must produce a WARNING so "
        "operators can diagnose accumulating prompt bloat."
    )
    msg = warnings[0].getMessage()
    assert "Bloated agent" in msg
    assert "Bloated assignment" in msg


# ---------------------------------------------------------------------------
# Memory truncation — oversized Redis medium-term and combined memory
# ---------------------------------------------------------------------------


def test_format_memory_for_prompt_truncates_oversized_medium_term(
    caplog: pytest.LogCaptureFixture,
):
    """format_memory_for_prompt must cap the medium-term summary so a runaway
    consolidation cycle cannot bloat the system prompt.
    """
    from app.services.agent_memory import (
        _MAX_MEDIUM_TERM_PROMPT_CHARS,
        format_memory_for_prompt,
    )

    # Build a medium-term summary larger than the cap.
    oversized_medium = "X " * (_MAX_MEDIUM_TERM_PROMPT_CHARS // 2 + 500)  # > cap
    assert len(oversized_medium) > _MAX_MEDIUM_TERM_PROMPT_CHARS

    caplog.set_level(logging.WARNING, logger="app.services.agent_memory")

    result = format_memory_for_prompt(
        {"medium_term": oversized_medium, "short_term": []},
        agent_label="BloatedAgent",
    )

    # The cap is a STRICT upper bound for the medium-term block (notice
    # included) — only the section header sits outside it.
    header = "── Agent Memory (accumulated knowledge) ──"
    assert len(result) <= _MAX_MEDIUM_TERM_PROMPT_CHARS + len(header) + 1
    # The truncation notice must be present.
    assert "truncated" in result
    # A WARNING must have been logged, attributed to the agent so operators
    # can correlate the transient truncation with the automatic compression
    # that follows (write-time clamp / self-heal pass).
    warnings = [
        r
        for r in caplog.records
        if r.levelno == logging.WARNING and "medium-term memory" in r.getMessage()
    ]
    assert warnings, "Oversized medium-term memory must produce a WARNING"
    assert "BloatedAgent" in warnings[0].getMessage()


def test_format_memory_for_prompt_unchanged_when_within_cap():
    """Memory within the cap must pass through unchanged (no truncation)."""
    from app.services.agent_memory import (
        _MAX_MEDIUM_TERM_PROMPT_CHARS,
        format_memory_for_prompt,
    )

    short_medium = "This is a normal summary."
    assert len(short_medium) < _MAX_MEDIUM_TERM_PROMPT_CHARS

    result = format_memory_for_prompt({"medium_term": short_medium, "short_term": []})

    assert short_medium in result
    assert "truncated" not in result


@pytest.mark.asyncio
async def test_load_memory_context_truncates_combined_oversized_memory(
    caplog: pytest.LogCaptureFixture,
):
    """_load_memory_context must cap the combined agent_memory_text and log a
    WARNING so operators can diagnose runaway memory growth.
    """
    from app.models.agent import Agent
    from app.services.assignment_helpers import (
        _MAX_ASSIGNMENT_MEMORY_CHARS,
        _load_memory_context,
    )

    agent = Agent(
        name="BigMemory",
        user_id=uuid.uuid4(),
        memory_short_term_enabled=True,
        memory_medium_term_enabled=True,
        memory_persistent_enabled=False,
        memory_user_enabled=False,
    )

    # Patch format_memory_for_prompt to return an oversized block,
    # simulating a badly accreted medium-term summary.
    oversized_text = "M " * (_MAX_ASSIGNMENT_MEMORY_CHARS // 2 + 1_000)
    assert len(oversized_text) > _MAX_ASSIGNMENT_MEMORY_CHARS

    caplog.set_level(logging.WARNING, logger="app.services.assignment_helpers")

    class _FakeDB:
        pass

    # Patch load_memory + format_memory_for_prompt so the agent Redis memory
    # path returns our oversized block, bypassing the real Redis.  The cap
    # breach also schedules an automatic self-heal pass — patch it
    # so the test does not reach for a live Redis/DB.
    with (
        patch("app.services.agent_memory.load_memory", new_callable=AsyncMock) as mock_load,
        patch(
            "app.services.agent_memory.format_memory_for_prompt",
            return_value=oversized_text,
        ),
        patch(
            "app.services.persistent_memory.auto_heal_agent_memory",
            new_callable=AsyncMock,
        ) as mock_heal,
    ):
        mock_load.return_value = {"medium_term": oversized_text, "short_term": []}
        agent_mem_text, _ = await _load_memory_context(
            db=_FakeDB(),  # type: ignore[arg-type]
            agent=agent,
            user_id=None,
        )
        # Let the fire-and-forget self-heal task run to completion.
        import asyncio as _aio

        from app.services.assignment_helpers import _self_heal_tasks

        if _self_heal_tasks:
            await _aio.gather(*_self_heal_tasks)

    # The cap breach must schedule the automatic self-heal instead
    # of relying on operators reading worker logs, on the lane the run reads.
    mock_heal.assert_awaited_once_with(str(agent.id), scope_id=str(agent.user_id))

    # Strict upper bound: the truncation notice is carved out of the cap,
    # never appended on top of it.
    assert len(agent_mem_text) <= _MAX_ASSIGNMENT_MEMORY_CHARS
    assert "truncated" in agent_mem_text

    warnings = [
        r
        for r in caplog.records
        if r.levelno == logging.WARNING and "combined memory context" in r.getMessage()
    ]
    assert warnings, (
        "Oversized combined agent memory must produce a WARNING so operators "
        "can diagnose accumulating memory bloat."
    )
    assert "BigMemory" in warnings[0].getMessage()


# ---------------------------------------------------------------------------
# OpenAI list_models fallback now logs WARNING (was silent)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_openai_list_models_fallback_logs_warning(
    caplog: pytest.LogCaptureFixture,
):
    """When the live OpenAI models endpoint is unreachable, the previous
    behaviour returned a hardcoded fallback list with zero logging — the
    admin UI then displayed stale models forever and nobody noticed.
    Pin the new WARNING contract.
    """
    from app.llm.openai_provider import OpenAIProvider

    provider = OpenAIProvider(api_key="sk-test-not-real")

    async def _broken_list():
        raise RuntimeError("simulated upstream outage")

    provider.client.models.list = _broken_list  # type: ignore[assignment]

    caplog.set_level(logging.WARNING, logger="app.llm.openai_provider")
    models = await provider.list_models()

    assert models, "Fallback list must be returned for UI compatibility."
    warnings = [
        r
        for r in caplog.records
        if r.levelno == logging.WARNING
        and "list_models" in r.getMessage()
    ]
    assert warnings, (
        "list_models endpoint failure must produce a WARNING — silent "
        "hardcoded-list fallback hides the upstream outage."
    )


# ---------------------------------------------------------------------------
# Tool argument round-trip through providers preserves tool_name in logs
# ---------------------------------------------------------------------------


def test_openai_tool_args_pass_tool_name_for_diagnostics():
    """When a stream contains malformed tool args, the WARNING must
    name the offending tool. Pin the signature so providers don't
    drop the ``tool_name`` keyword in a future refactor.
    """
    import inspect

    from app.llm.base import parse_tool_arguments

    sig = inspect.signature(parse_tool_arguments)
    assert "tool_name" in sig.parameters, (
        "parse_tool_arguments must accept `tool_name` so provider call "
        "sites can include the offending tool in malformed-JSON logs."
    )

    # All known providers must pass tool_name through. Read the source
    # to ensure none has regressed to the no-kwarg call.
    from pathlib import Path

    backend_root = Path(__file__).parent.parent / "app" / "llm"
    provider_files = [
        backend_root / "openai_provider.py",
        backend_root / "openai_compat.py",
        backend_root / "anthropic_provider.py",
        backend_root / "bedrock_provider.py",
    ]
    for fp in provider_files:
        text = fp.read_text(encoding="utf-8")
        if "parse_tool_arguments" not in text:
            continue
        # Find every call site and verify it includes tool_name=.
        for idx, line in enumerate(text.splitlines(), start=1):
            if "parse_tool_arguments(" in line and not line.lstrip().startswith(
                ("#", "from ", "import ", "def ", "sig.")
            ):
                # Collect the next ~6 lines as the call may span multiple.
                call_block = "\n".join(
                    text.splitlines()[idx - 1 : idx + 6]
                )
                assert "tool_name" in call_block, (
                    f"{fp.name}:{idx} calls parse_tool_arguments without "
                    f"`tool_name=` — malformed-JSON warnings will be "
                    f"un-diagnosable for this provider.\nBlock:\n"
                    f"{call_block}"
                )


# ---------------------------------------------------------------------------
# Background memory write task is strongly referenced
# ---------------------------------------------------------------------------


def test_agent_chat_keeps_strong_refs_to_background_memory_tasks():
    """Module-level set must exist so ``asyncio.create_task`` results
    aren't GC'd mid-flight. Without this, the backgrounded memory
    writes added for latency reduction would silently vanish when
    Python's weak-ref behaviour for ``create_task`` collects them.
    """
    from app.services import agent_chat

    assert hasattr(agent_chat, "_BACKGROUND_MEMORY_TASKS"), (
        "agent_chat must hold a strong ref to background memory tasks."
    )
    assert isinstance(agent_chat._BACKGROUND_MEMORY_TASKS, set), (
        "Strong-ref container should be a set so completed tasks "
        "can be ``discard``ed by the done_callback."
    )


def test_assignment_engine_uses_existing_background_task_set():
    """Re-uses the pre-existing ``_background_tasks`` set so background
    memory-persistence tasks share the same GC-anchor.
    """
    from app.services import assignment_engine

    assert hasattr(assignment_engine, "_background_tasks"), (
        "assignment_engine must have a background-task set for "
        "fire-and-forget memory writes."
    )
    assert isinstance(
        assignment_engine._background_tasks, set
    )


# ---------------------------------------------------------------------------
# Tool loading + DB-query patterns — must NOT share an AsyncSession across
# concurrent ``asyncio.gather`` arms
# ---------------------------------------------------------------------------
#
# An earlier round of this PR introduced ``asyncio.gather`` over
# ``load_mcp_tools`` / ``load_custom_tools`` / ``db.execute(...)`` on the
# shared ``AsyncSession``. SQLAlchemy 2.x explicitly forbids concurrent
# operations on a single async session (see the InvalidRequestError on
# the async-session error reference: "this session is provisioning a new
# connection; concurrent operations are not permitted"), so those
# gathers were a latent crash bug — they could fail under load and leave
# the agent / GA with no tools or a half-loaded config.
#
# These tests pin the SAFE contract: the call sites must NOT re-introduce
# any ``asyncio.gather`` that runs the DB-touching loaders concurrently
# against the chat-handler's session. A regression that puts
# ``load_custom_tools`` + ``load_mcp_tools`` back inside a single gather
# will trip these tests.


def _read(name: str) -> str:
    from pathlib import Path

    return (
        Path(__file__).parent.parent / "app" / "services" / name
    ).read_text(encoding="utf-8")


def test_agent_chat_does_not_gather_load_custom_and_load_mcp_concurrently():
    """``load_custom_tools`` and ``load_mcp_tools`` both call
    ``db.execute(...)`` against the shared ``AsyncSession``. They must
    NOT be wrapped in a single ``asyncio.gather`` / ``_aio.gather`` —
    SQLAlchemy raises ``InvalidRequestError`` on concurrent operations.
    """
    text = _read("agent_chat.py")
    for forbidden in (
        "_aio.gather(_custom_tools_task, _mcp_tools_task)",
        "asyncio.gather(_custom_tools_task, _mcp_tools_task)",
        "asyncio.gather(_mcp_tools_task, _custom_tools_task)",
        "_aio.gather(_mcp_tools_task, _custom_tools_task)",
    ):
        assert forbidden not in text, (
            f"agent_chat must NOT gather custom + MCP tool loading on the "
            f"shared AsyncSession ('{forbidden}' would raise "
            f"InvalidRequestError under load)."
        )


def test_general_chat_does_not_gather_load_custom_and_load_mcp_concurrently():
    """Same contract for the General Assistant path."""
    text = _read("general_chat.py")
    for forbidden in (
        "asyncio.gather(_mcp_load_task, _custom_load_task)",
        "_aio.gather(_mcp_load_task, _custom_load_task)",
        "asyncio.gather(_custom_load_task, _mcp_load_task)",
        "_aio.gather(_custom_load_task, _mcp_load_task)",
    ):
        assert forbidden not in text, (
            f"general_chat must NOT gather MCP + custom tool loading on "
            f"the shared AsyncSession ('{forbidden}' would raise "
            f"InvalidRequestError under load)."
        )


def test_general_chat_does_not_gather_ga_config_and_user_record_selects():
    """``GeneralAssistantConfig`` and ``User`` selects MUST stay
    sequential — SQLAlchemy forbids concurrent ``db.execute`` on the
    same session.
    """
    text = _read("general_chat.py")
    # Find every ``asyncio.gather(`` occurrence and verify none of them
    # straddle the two SELECT statements.
    forbidden_pairs = (
        ("asyncio.gather(", "select(GeneralAssistantConfig)", "select(User)"),
        ("_aio.gather(", "select(GeneralAssistantConfig)", "select(User)"),
    )
    for gather_token, ga_select, user_select in forbidden_pairs:
        idx = 0
        while True:
            pos = text.find(gather_token, idx)
            if pos == -1:
                break
            # Look at the next ~300 chars after the gather token for the
            # two-select pattern. A safe call site won't have both
            # inside that window.
            window = text[pos : pos + 300]
            assert not (ga_select in window and user_select in window), (
                f"general_chat must NOT gather "
                f"{ga_select} and {user_select} on the shared "
                f"AsyncSession (would raise InvalidRequestError)."
            )
            idx = pos + len(gather_token)


def test_assignment_engine_integration_tool_failure_does_not_write_run_error():
    """``AssignmentRun.error`` is the
    failure-state field consumed everywhere else in the engine (lines
    445, 450, 459, 477, 1998) and rendered in red by the UI as "this
    run failed". The previous shape wrote a degraded-but-completed
    message into ``run.error`` while still letting the run reach
    ``status="completed"``, leaving the user with a green completed run
    that's also marked with a red error.

    The ERROR log at the same site already surfaces the failure to
    operators via audit tooling. Pin that ``run.error`` is no longer
    assigned in the integration-tools failure branch, so a regression
    that re-introduces the inconsistency trips here.
    """
    import inspect

    from app.services import assignment_engine

    source = inspect.getsource(assignment_engine.execute_assignment_run)
    # Find the integration-tools exception branch — the ERROR log uses
    # this exact string, so we can locate the block deterministically.
    needle = "Failed to load integration tools for assignment"
    assert needle in source, (
        "Test fixture is stale — the integration-tools ERROR log message "
        "no longer matches. Update the needle or remove the test."
    )
    block_start = source.index(needle)
    # Look at the ~400 chars following the ERROR log site for any
    # ``run.error =`` assignment. A regression that restores the
    # degraded-completed write would trip this.
    block = source[block_start : block_start + 1500]
    assert "run.error =" not in block, (
        "The integration-tools failure branch in execute_assignment_run "
        "must NOT assign to ``run.error``. "
        "``AssignmentRun.error`` is failure-state; the ERROR "
        "log already surfaces the degradation to operators."
    )


def test_persistent_memory_falls_back_to_sequential_when_provider_lookup_fails():
    """If ``get_embedding_provider(db)`` fails up-front (``_pm is
    None``), the per-fact gather can't run safely — each
    ``embed_text`` would re-issue the failing DB lookup concurrently.
    Source-level check: the implementation must guard the gather on
    ``_pm is not None`` and fall back to a per-fact loop otherwise.
    """
    from pathlib import Path

    src = (
        Path(__file__).parent.parent
        / "app"
        / "services"
        / "persistent_memory.py"
    )
    text = src.read_text(encoding="utf-8")
    # The guarded shape must exist:
    assert "if _pm is not None:" in text, (
        "reconcile_and_persist_facts must guard the parallel embed-gather "
        "on a successfully resolved embedding provider — without the "
        "guard, a failed provider lookup falls into N concurrent "
        "get_embedding_provider(db) calls on the shared AsyncSession."
    )
    # And the fallback must be a sequential per-fact loop:
    assert "for fact in facts:" in text, (
        "reconcile_and_persist_facts must fall back to a sequential "
        "per-fact embed loop when the provider lookup failed."
    )


# ---------------------------------------------------------------------------
# Smoke: all modified provider files still expose chat()
# ---------------------------------------------------------------------------


def test_providers_still_expose_chat_after_parse_tool_arguments_refactor():
    """Sanity check: passing ``tool_name=`` through every provider's
    chat-stream code path must not break the provider class itself.
    A typo in the refactor would surface as an import-time AttributeError.
    """
    from app.llm import (
        anthropic_provider,
        bedrock_provider,
        openai_compat,
        openai_provider,
    )

    for mod in (
        openai_provider,
        openai_compat,
        anthropic_provider,
        bedrock_provider,
    ):
        # All providers must define a class with a ``chat`` method.
        provider_classes = [
            v
            for v in vars(mod).values()
            if isinstance(v, type) and hasattr(v, "chat")
        ]
        assert provider_classes, (
            f"{mod.__name__} must still expose a provider class with chat()."
        )


# ---------------------------------------------------------------------------
# Sanity for the JSON-load helper used in the regression suite
# ---------------------------------------------------------------------------


def test_json_helper_available():
    """Sanity: ``json`` is importable. Several assertions above rely on
    it implicitly via dict comparisons.
    """
    assert json.dumps({"ok": True}) == '{"ok": true}'


def _awaited_preload_modules(node) -> list[str] | None:
    """Return the literal module names if ``node`` is an AWAITED call to
    ``ensure_hot_path_imports``, else None.

    Requiring the ``ast.Await`` wrapper means a regression that drops the
    ``await`` (leaving a never-run coroutine) no longer counts as a preload
    and fails the ordering assertions in
    :func:`_assert_inline_imports_preloaded`.
    """
    import ast

    if not isinstance(node, ast.Await) or not isinstance(node.value, ast.Call):
        return None
    func = node.value.func
    name = getattr(func, "id", None) or getattr(func, "attr", None)
    if name != "ensure_hot_path_imports":
        return None
    return [
        arg.value
        for arg in node.value.args
        if isinstance(arg, ast.Constant) and isinstance(arg.value, str)
    ]


def _assert_inline_imports_preloaded(func, label: str, hot_modules: tuple[str, ...]):
    """Assert every inline hot-path import in *func* has an earlier preload.

    Every ``from <hot_module> import name`` inside *func* must be preceded
    (in source order) by an awaited ``ensure_hot_path_imports`` call naming
    the module it resolves from. AST-based so quote style, line wrapping,
    and multi-name imports cannot produce false positives/negatives.
    """
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(func))
    preload_linenos: dict[str, int] = {}
    inline_imports: list[tuple[str, int]] = []
    for node in ast.walk(tree):
        modules = _awaited_preload_modules(node)
        if modules:
            for module in modules:
                # ast.walk order is not source order - keep the earliest.
                prior = preload_linenos.get(module, node.lineno)
                preload_linenos[module] = min(prior, node.lineno)
        if isinstance(node, ast.ImportFrom) and node.module in hot_modules:
            inline_imports.append((node.module, node.lineno))

    assert inline_imports, (
        f"{label}: expected inline hot-path imports to still exist (bind "
        f"names at call time - never cache function objects)"
    )
    for module, import_lineno in inline_imports:
        assert module in preload_linenos, (
            f"{label}: inline import of {module} (line {import_lineno}) has "
            f"no ensure_hot_path_imports preload"
        )
        assert preload_linenos[module] < import_lineno, (
            f"{label}: ensure_hot_path_imports({module!r}) must run before "
            f"the inline import at line {import_lineno}"
        )

    # The hot path must actually await the preload helper.
    assert preload_linenos, f"{label}: no awaited ensure_hot_path_imports call found"


def test_hot_import_helper_offloads_to_thread():
    """The shared offload helper must dispatch imports to a thread.

    ``app.core.hot_imports.ensure_hot_path_imports`` is the single shared
    helper behind the event-loop-stall fixes; it must keep
    dispatching first-load imports via ``asyncio.to_thread`` with a
    warm-cache short-circuit (no threadpool hop once loaded via helper).
    """
    import ast
    import inspect

    from app.core import hot_imports

    ensure_tree = ast.parse(inspect.getsource(hot_imports.ensure_hot_path_imports))
    assert any(
        isinstance(n, ast.Attribute) and n.attr == "to_thread"
        for n in ast.walk(ensure_tree)
    ), "ensure_hot_path_imports must offload via asyncio.to_thread"
    assert any(
        isinstance(n, ast.Name) and n.id == "_hot_path_modules_loaded"
        for n in ast.walk(ensure_tree)
    ), "ensure_hot_path_imports must short-circuit via _hot_path_modules_loaded"
    loader_tree = ast.parse(inspect.getsource(hot_imports._import_modules_sync))
    assert any(
        isinstance(n, ast.Attribute) and n.attr == "import_module"
        for n in ast.walk(loader_tree)
    ), "_import_modules_sync must use importlib.import_module"


def test_assignment_engine_offloads_hot_imports_from_event_loop():
    """Heavy first-use imports in assignment execution must be offloaded.

    Lazy-importing the LLM router / integration tools
    inline in async hot paths blocked the worker event loop for ~2-3s while
    large SDK import chains (e.g. ``google.genai``) loaded on the loop
    thread. The fix preloads those modules via ``asyncio.to_thread`` before
    every inline hot-path import; the inline ``from module import name``
    then resolves through ``sys.modules`` without blocking. Names must
    still be bound at call time (never cached as function objects) so
    monkeypatching e.g. ``app.llm.router.get_llm_for_user`` keeps working.
    """
    from app.services import assignment_engine

    hot_modules = ("app.llm.router", "app.services.integration_tools")
    for func, label in (
        (assignment_engine.execute_assignment_run, "execute_assignment_run"),
        (assignment_engine._run_activation_gate, "_run_activation_gate"),
    ):
        _assert_inline_imports_preloaded(func, label, hot_modules)


def test_document_processor_offloads_hot_imports_from_event_loop():
    """Heavy first-use imports in document processing must be offloaded.

    The ``app.llm.router`` import chain (including
    ``AnthropicProvider`` → ``anthropic`` → ``httpx`` → ``click``) was
    imported inline in ``_process_document_impl``, blocking the worker
    event loop for ~2.6s on first use.  The fix mirrors the pattern in
    ``assignment_engine.py``: offload via
    ``ensure_hot_path_imports`` / ``asyncio.to_thread`` before the inline
    ``from app.llm.router import get_embedding_provider``.
    """
    from app.services import document_processor

    _assert_inline_imports_preloaded(
        document_processor._process_document_impl,
        "_process_document_impl",
        ("app.llm.router",),
    )

