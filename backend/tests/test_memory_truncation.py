"""Memory consolidation truncation safety.

A consolidation generation cut by ``max_tokens`` ends mid-word, and that
clipped text used to be stored to Redis, persisted durably to PostgreSQL,
and injected into every later system prompt as-is (observed in production:
a consolidated entry ending literally with "Fully eradicated and res").

These tests pin the fixes:

* ``consolidate_memory`` checks the generation's ``stop_reason``: a
  ``max_tokens`` cut is retried once with a doubled budget, and a result
  that is still cut is trimmed to the last complete statement and marked
  with an explicit truncation notice before storage.
* ``_compress_summary`` applies the same stop-reason handling to its own
  generation, and its hard-truncate fallback cuts at a clean boundary with
  a notice instead of slicing mid-word.
* ``format_memory_for_prompt`` truncates boundary-aware, so the injected
  block never ends mid-word right before the notice.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.services import agent_memory as agent_memory_mod
from app.services.agent_memory import (
    _CLIPPED_GENERATION_NOTICE,
    _MAX_MEDIUM_TERM_PROMPT_CHARS,
    _SUMMARY_MAX_TOKENS,
    _compress_summary,
    _drop_clipped_tail,
    _strip_stale_notices,
    _truncate_at_boundary,
    format_memory_for_prompt,
)

AGENT_ID = "00000000-0000-0000-0000-000000000001"

COMPLETE = "- fact one is stable.\n- fact two is stable."
CLIPPED = "## Incident ITS-212 (closed)\n- Fully eradicated and res"


def _fake_llm(*responses):
    """LLM stub whose chat() returns ChatResponse-like objects in order.

    Each item is either a string (natural completion) or a
    ``(content, stop_reason)`` tuple.
    """
    queue = [
        r if isinstance(r, tuple) else (r, "end_turn")
        for r in responses
    ]

    async def _chat(**_kwargs):
        content, stop_reason = queue.pop(0)
        return SimpleNamespace(content=content, stop_reason=stop_reason)

    return SimpleNamespace(chat=AsyncMock(side_effect=_chat))


class _FakeRedis:
    def __init__(self, store: dict | None = None) -> None:
        self.store: dict = store or {}

    async def get(self, key):
        return self.store.get(key)

    async def set(self, *args, **kwargs):
        self.store[args[0]] = args[1]
        return True

    async def exists(self, *_keys):
        return 0


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def test_drop_clipped_tail_cuts_at_line_boundary():
    assert _drop_clipped_tail(CLIPPED) == "## Incident ITS-212 (closed)"


def test_drop_clipped_tail_keeps_clean_ending():
    assert _drop_clipped_tail(COMPLETE) == COMPLETE


def test_drop_clipped_tail_single_line_sentence_terminator():
    """A single-line result with no newline or ". " must still cut at other
    sentence terminators instead of keeping the mid-word tail."""
    assert (
        _drop_clipped_tail("A complete sentence! trailing fragm")
        == "A complete sentence!"
    )


def test_drop_clipped_tail_drops_trailing_fragment_without_boundary():
    """No statement boundary in the second half: keep the dangling clause
    but discard the final whitespace-delimited fragment, which is the
    potentially mid-word artifact of the max_tokens cut."""
    text = "one single very long statement without any boundary marke"
    assert (
        _drop_clipped_tail(text) == "one single very long statement without any boundary"
    )


def test_drop_clipped_tail_keeps_final_word_when_cut_after_whitespace():
    """A raw generation ending in whitespace completed its final word before
    the cut - the fragment-drop fallback must not discard it."""
    assert (
        _drop_clipped_tail("one single complete word ")
        == "one single complete word"
    )


def test_drop_clipped_tail_keeps_single_unbroken_token():
    """A single token has nothing safe to cut back to."""
    assert _drop_clipped_tail("supercalifragilisticexpialidoci") == (
        "supercalifragilisticexpialidoci"
    )


def test_truncate_at_boundary_respects_limit_and_word_boundary():
    text = "word " * 3_000  # 15_000 chars
    notice = "\n[notice]"
    result = _truncate_at_boundary(text, 6_000, notice)
    assert len(result) <= 6_000
    assert result.endswith(notice)
    body = result[: -len(notice)]
    # The cut must land on a word boundary, never inside "word".
    assert body.endswith("word")


def test_truncate_at_boundary_noop_within_limit():
    assert _truncate_at_boundary(COMPLETE, 6_000, "\n[notice]") == COMPLETE


def test_truncate_at_boundary_notice_wider_than_limit():
    """A notice that cannot fit within the limit is dropped: the size
    contract (result always fits *limit*) must hold for any input, not
    just the module constants."""
    text = "word " * 100
    result = _truncate_at_boundary(text, 10, "\n[a notice longer than the limit]")
    assert len(result) <= 10
    assert result == text[:10]


def test_strip_stale_notices_removes_both_notice_forms():
    """Stored notices - with or without their leading newline - are meta
    signals about a past generation and must not reach the next LLM call."""
    text = (
        "- fact one."
        + _CLIPPED_GENERATION_NOTICE
        + "\n- fact two."
        + "\n"
        + agent_memory_mod._BUDGET_TRUNCATION_NOTICE.lstrip("\n")
    )
    cleaned = _strip_stale_notices(text)
    assert _CLIPPED_GENERATION_NOTICE.lstrip("\n") not in cleaned
    assert agent_memory_mod._BUDGET_TRUNCATION_NOTICE.lstrip("\n") not in cleaned
    assert "- fact one." in cleaned
    assert "- fact two." in cleaned


# ---------------------------------------------------------------------------
# consolidate_memory - stop-reason handling
# ---------------------------------------------------------------------------


async def _run_consolidation(llm, *, medium_term: str = "", short_summary: str = "Processed the inbox."):
    fake_redis = _FakeRedis()
    persist_mock = AsyncMock(return_value=None)

    async def fake_load_memory(_agent_id: str, *, scope_id: str | None = None):
        return {
            "short_term": [
                {
                    "source": "Triage",
                    "source_type": "assignment",
                    "summary": short_summary,
                    "timestamp": "2026-09-01T12:00:00",
                }
            ],
            "medium_term": medium_term,
        }

    async def fake_redis_client():
        return fake_redis

    class _FakeSession:
        def __call__(self):
            return self

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return False

        async def commit(self):
            return None

    with (
        patch.object(agent_memory_mod, "load_memory", side_effect=fake_load_memory),
        patch.object(agent_memory_mod, "redis_client", side_effect=fake_redis_client),
        patch("app.services.persistent_memory.persist_memory", new=persist_mock),
        patch("app.core.database.async_session", new=_FakeSession()),
    ):
        await agent_memory_mod.consolidate_memory(
            AGENT_ID, llm, force=True, scope_id=None, scope_is_user_id=True
        )
    return fake_redis, persist_mock


@pytest.mark.asyncio
async def test_consolidation_retries_once_on_max_tokens_cut():
    """A generation cut by max_tokens is retried once with a doubled budget;
    the complete retry result is stored without any truncation notice."""
    llm = _fake_llm((CLIPPED, "max_tokens"), COMPLETE)

    fake_redis, persist_mock = await _run_consolidation(llm)

    stored = fake_redis.store[agent_memory_mod._medium_term_key(AGENT_ID)]
    assert stored == COMPLETE
    assert _CLIPPED_GENERATION_NOTICE not in stored
    assert llm.chat.await_count == 2
    retry_kwargs = llm.chat.await_args_list[1].kwargs
    assert retry_kwargs["max_tokens"] == _SUMMARY_MAX_TOKENS * 2
    persist_mock.assert_awaited_once()
    assert persist_mock.await_args.kwargs["content"] == COMPLETE


@pytest.mark.asyncio
async def test_consolidation_trims_and_marks_when_retry_still_cut():
    """When even the retry hits max_tokens, the stored summary must end at a
    complete statement with an explicit truncation notice - never mid-word.
    The durable PostgreSQL copy must match the Redis copy."""
    llm = _fake_llm((CLIPPED, "max_tokens"), (CLIPPED, "max_tokens"))

    fake_redis, persist_mock = await _run_consolidation(llm)

    stored = fake_redis.store[agent_memory_mod._medium_term_key(AGENT_ID)]
    assert stored == "## Incident ITS-212 (closed)" + _CLIPPED_GENERATION_NOTICE
    assert "and res" not in stored
    assert persist_mock.await_args.kwargs["content"] == stored


@pytest.mark.asyncio
@pytest.mark.parametrize("retry_content", ["", "   \n"])
async def test_consolidation_keeps_clipped_first_response_when_retry_empty(
    retry_content,
):
    """An empty (or whitespace-only) retry must not clobber the usable
    clipped first response: the preserved first response still carries its
    ``max_tokens`` stop reason, so the trim-and-notice fallback stores its
    complete statements instead of silently falling back to the OLD
    medium-term summary and dropping the newly consolidated activity."""
    llm = _fake_llm((CLIPPED, "max_tokens"), (retry_content, "end_turn"))
    old_medium = "- old fact."

    fake_redis, persist_mock = await _run_consolidation(llm, medium_term=old_medium)

    stored = fake_redis.store[agent_memory_mod._medium_term_key(AGENT_ID)]
    assert stored == "## Incident ITS-212 (closed)" + _CLIPPED_GENERATION_NOTICE
    assert stored != old_medium
    assert "and res" not in stored
    assert llm.chat.await_count == 2
    persist_mock.assert_awaited_once()
    assert persist_mock.await_args.kwargs["content"] == stored
    # The counter reset is now backed by a stored consolidation, not a
    # silent fallback to the old summary.
    assert fake_redis.store[agent_memory_mod._counter_key(AGENT_ID)] == 0


@pytest.mark.asyncio
async def test_consolidation_keeps_clipped_first_response_when_retry_raises():
    """A retry that raises (transient provider error, model rejecting the
    doubled budget) is the same data-loss path as an empty retry: the
    exception must be contained so the usable clipped first response still
    flows through the trim-and-notice fallback instead of escaping to the
    outer handler and discarding it entirely."""
    calls = {"n": 0}

    async def _chat(**_kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            return SimpleNamespace(content=CLIPPED, stop_reason="max_tokens")
        raise RuntimeError("provider down")

    llm = SimpleNamespace(chat=AsyncMock(side_effect=_chat))
    old_medium = "- old fact."

    fake_redis, persist_mock = await _run_consolidation(llm, medium_term=old_medium)

    stored = fake_redis.store[agent_memory_mod._medium_term_key(AGENT_ID)]
    assert stored == "## Incident ITS-212 (closed)" + _CLIPPED_GENERATION_NOTICE
    assert stored != old_medium
    assert llm.chat.await_count == 2
    persist_mock.assert_awaited_once()
    assert persist_mock.await_args.kwargs["content"] == stored


@pytest.mark.asyncio
async def test_consolidation_whitespace_first_response_keeps_old_summary():
    """A whitespace-only first response is truthy but carries nothing
    storable: when its retry is also blank, the old summary must be kept -
    trimming the preserved whitespace would otherwise reduce it to "" and
    store a bare truncation notice as the whole memory."""
    llm = _fake_llm(("   \n", "max_tokens"), ("", "end_turn"))
    old_medium = "- old fact."

    fake_redis, _persist_mock = await _run_consolidation(llm, medium_term=old_medium)

    stored = fake_redis.store[agent_memory_mod._medium_term_key(AGENT_ID)]
    assert stored == old_medium
    assert _CLIPPED_GENERATION_NOTICE not in stored
    assert llm.chat.await_count == 2


@pytest.mark.asyncio
async def test_consolidation_strips_stale_notice_from_llm_input():
    """A notice stored with a previous cycle's summary must not be fed back
    to the consolidation LLM as if it were memory content - the model would
    copy it into the fresh output, and a second cut would stack a new
    notice on top of the copied one."""
    llm = _fake_llm(COMPLETE)
    stale_medium = "- old fact." + _CLIPPED_GENERATION_NOTICE

    await _run_consolidation(llm, medium_term=stale_medium)

    sent_messages = llm.chat.await_args_list[0].kwargs["messages"]
    user_content = sent_messages[1].content
    assert _CLIPPED_GENERATION_NOTICE.lstrip("\n") not in user_content
    assert "- old fact." in user_content


@pytest.mark.asyncio
async def test_consolidation_and_compression_turn_thinking_off():
    """On a model that thinks by default, thinking shares the summary budget
    and cut every consolidation and its retry: all three calls ask for
    no thinking, like every other memory utility call."""
    llm = _fake_llm((CLIPPED, "max_tokens"), COMPLETE)
    await _run_consolidation(llm)
    compress_llm = _fake_llm(COMPLETE)
    await _compress_summary("x" * 10_000, compress_llm, agent_id=AGENT_ID)

    calls = [*llm.chat.await_args_list, *compress_llm.chat.await_args_list]
    assert len(calls) == 3
    assert all(call.kwargs.get("disable_thinking") is True for call in calls)


@pytest.mark.asyncio
async def test_consolidation_stores_complete_generation_unchanged():
    llm = _fake_llm(COMPLETE)

    fake_redis, _persist_mock = await _run_consolidation(llm)

    stored = fake_redis.store[agent_memory_mod._medium_term_key(AGENT_ID)]
    assert stored == COMPLETE
    assert llm.chat.await_count == 1


# ---------------------------------------------------------------------------
# _compress_summary - stop-reason handling + boundary-aware fallback
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_compress_summary_marks_max_tokens_cut():
    llm = _fake_llm((CLIPPED, "max_tokens"))
    result = await _compress_summary("x" * 10_000, llm, agent_id=AGENT_ID)
    assert result == "## Incident ITS-212 (closed)" + _CLIPPED_GENERATION_NOTICE


@pytest.mark.asyncio
async def test_compress_summary_fallback_truncates_at_boundary():
    """LLM failure degrades to a hard truncation that must stay within the
    cap, cut at a word boundary, and carry an explicit notice."""
    oversized = "word " * (_MAX_MEDIUM_TERM_PROMPT_CHARS // 2)
    llm = SimpleNamespace(chat=AsyncMock(side_effect=RuntimeError("provider down")))
    result = await _compress_summary(oversized, llm, agent_id=AGENT_ID)
    assert len(result) <= _MAX_MEDIUM_TERM_PROMPT_CHARS
    assert result.endswith(agent_memory_mod._BUDGET_TRUNCATION_NOTICE)
    body = result[: -len(agent_memory_mod._BUDGET_TRUNCATION_NOTICE)]
    assert body.endswith("word")


# ---------------------------------------------------------------------------
# format_memory_for_prompt - boundary-aware prompt clip
# ---------------------------------------------------------------------------


def test_format_memory_for_prompt_clips_at_word_boundary():
    oversized = "word " * (_MAX_MEDIUM_TERM_PROMPT_CHARS // 2)
    block = format_memory_for_prompt(
        {"medium_term": oversized, "short_term": []}, agent_label="test-agent"
    )
    assert agent_memory_mod._BUDGET_TRUNCATION_NOTICE in block
    body = block.split(agent_memory_mod._BUDGET_TRUNCATION_NOTICE)[0]
    assert body.endswith("word")


def test_format_memory_for_prompt_leaves_within_cap_summary_alone():
    block = format_memory_for_prompt(
        {"medium_term": COMPLETE, "short_term": []}, agent_label="test-agent"
    )
    assert COMPLETE in block
    assert agent_memory_mod._BUDGET_TRUNCATION_NOTICE not in block
