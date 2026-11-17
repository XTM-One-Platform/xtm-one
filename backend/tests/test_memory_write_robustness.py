"""Tests for memory write robustness (assessment findings MEM-4..MEM-7).

MEM-4: ``store_knowledge`` must not mark a document "ready" when zero
chunks embedded, and must surface partial-indexing info to the agent.

MEM-5: the reconciliation curator LLM call is retried once with a terser
JSON-only reprompt; if it still fails, the ADD fallback is taken and the
returned summary carries ``curator_degraded=True``.

MEM-6: a SUPERSEDE/MERGE decision whose superseded ids ALL fail
validation is explicitly downgraded to ADD (old facts retained, logged)
instead of silently inserting the new fact next to its contradictions.

MEM-7: ``persist_memory`` retries the embedding once when it fails, then
persists the row anyway (durable beats lost) so ``get_memory_health()``
can pick it up via ``no_embedding_count``. The retry goes through
``embed_text_with_model`` so a successful retry keeps the
``embedding_model`` / ``embedding_dimensions`` provenance required by the
dimension-filtered searches. ``embed_text_with_model`` treats empty and
zero-norm provider vectors as failures.
"""

from __future__ import annotations

import json
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import httpx
import pytest

from app.services import persistent_memory as pm
from app.services.builtin_tools import build_kb_store_tool
from app.services.kb_document_errors import TIMEOUT_MESSAGE
from app.tools.base import ToolCall

AGENT_ID = "00000000-0000-0000-0000-000000000001"


# ---------------------------------------------------------------------------
# MEM-4: store_knowledge chunk-failure handling
# ---------------------------------------------------------------------------


class _FakeKBSession:
    """Stand-in for ``async_session()`` — also acts as its own context manager."""

    def __init__(self, kb) -> None:
        self.kb = kb
        self.added: list = []
        self.committed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False

    async def execute(self, _stmt):
        # Only the KnowledgeBase select runs in these tests (replace=False).
        return SimpleNamespace(scalar_one_or_none=lambda: self.kb)

    def add(self, obj) -> None:
        self.added.append(obj)

    async def flush(self) -> None:
        pass

    async def commit(self) -> None:
        self.committed = True


def _make_kb():
    return SimpleNamespace(
        id=uuid.uuid4(),
        name="Docs",
        # Writable: ``store_knowledge`` refuses a read-only knowledge base
        # outright, which would short-circuit these chunk-failure paths.
        is_read_only=False,
        is_company_managed=False,
        user_id=uuid.uuid4(),
        chunk_size=400,
        chunk_overlap=40,
        document_count=0,
        chunk_count=0,
        embedding_model=None,
    )


async def _run_store_knowledge(embed_side_effect, chunks, *, package_origin=False):
    """Run the ``store_knowledge`` tool with a stubbed session/provider.

    Returns ``(tool_output_content, stored_document, fake_session)``.
    """
    kb = _make_kb()
    session = _FakeKBSession(kb)
    provider = SimpleNamespace(
        provider_name="test",
        embed=AsyncMock(side_effect=embed_side_effect),
    )

    result = build_kb_store_tool(None, "user-1", [(str(kb.id), kb.name)])
    assert result is not None
    _, executor = result

    with (
        patch("app.core.database.async_session", new=lambda: session),
        patch(
            "app.api.v1.helpers.is_package_origin_knowledge_base",
            new=AsyncMock(return_value=package_origin),
        ),
        patch(
            "app.llm.router.get_embedding_provider",
            new=AsyncMock(return_value=(provider, "test-model", "pid-1")),
        ),
        patch("app.services.chunking.chunk_text", new=lambda *a, **k: chunks),
    ):
        out = await executor.execute_tool(
            ToolCall(
                id="x",
                name="store_knowledge",
                arguments={
                    "title": "analysis",
                    "content": "long enough body content",
                    "replace": False,
                },
            )
        )

    doc = session.added[0] if session.added else None
    return out.content, doc, session


@pytest.mark.asyncio
async def test_store_knowledge_refuses_a_package_installed_knowledge_base():
    """A skill package re-creates its KB, so nothing may write to it:
    refused before any embedding call, nothing stored."""
    embed = RuntimeError("must not be called")
    content, doc, session = await _run_store_knowledge(
        embed_side_effect=embed, chunks=["a chunk long enough"], package_origin=True
    )

    assert content.startswith("Error:")
    assert "skill package" in content
    assert doc is None
    assert session.committed is False


@pytest.mark.asyncio
async def test_store_knowledge_zero_chunks_embedded_marks_error():
    """All chunks fail to embed -> doc.status='error' with a reason, and the
    tool tells the agent storage FAILED instead of claiming success."""
    chunks = ["first chunk long enough", "second chunk long enough"]
    content, doc, session = await _run_store_knowledge(
        embed_side_effect=RuntimeError("provider down"), chunks=chunks
    )

    assert doc.status == "error"
    assert "no vectors" in (doc.error_message or "")
    assert doc.chunk_count == 0
    assert session.committed is True
    assert content.startswith("Error:")
    assert "NOT searchable" in content
    assert "embedding provider" in content
    # Provider failure is transient: the auto-retry sweeper must be able to
    # pick the doc up, so a retry must be scheduled (retryable=True).
    assert doc.embedding_next_retry_at is not None
    # KB chunk_count must not be bumped when nothing embedded.
    assert session.kb.chunk_count == 0


@pytest.mark.asyncio
async def test_store_knowledge_zero_chunks_names_a_timeout_when_every_call_timed_out():
    """Every embedding call timed out -> the document records the timeout,
    as the document processor does, and never the exception text."""
    request = httpx.Request("POST", "https://embeddings.example/v1")
    content, doc, _session = await _run_store_knowledge(
        embed_side_effect=httpx.ReadTimeout("internal-detail-marker", request=request),
        chunks=["first chunk long enough", "second chunk long enough"],
    )

    assert doc.status == "error"
    assert doc.error_message == TIMEOUT_MESSAGE
    assert content.startswith("Error:")
    assert "internal-detail-marker" not in content


@pytest.mark.asyncio
async def test_store_knowledge_zero_chunks_no_auto_retry_when_disabled():
    """With EMBEDDING_AUTO_RETRY_ENABLED=false the provider-failure message
    must not promise an automatic retry (none is scheduled)."""
    chunks = ["first chunk long enough", "second chunk long enough"]
    with patch(
        "app.services.kb_helpers.app_settings.embedding_auto_retry_enabled",
        new=False,
    ):
        content, doc, _session = await _run_store_knowledge(
            embed_side_effect=RuntimeError("provider down"), chunks=chunks
        )

    assert doc.status == "error"
    assert doc.embedding_next_retry_at is None
    assert content.startswith("Error:")
    assert "retried automatically" not in content
    assert "administrator" in content


@pytest.mark.asyncio
async def test_store_knowledge_no_extractable_text_marks_error_without_blaming_provider():
    """All chunks filtered as too short -> doc.status='error' with a
    no-extractable-text reason; the agent-facing message must NOT blame the
    embedding provider and no auto-retry is scheduled (structural failure)."""
    content, doc, session = await _run_store_knowledge(
        embed_side_effect=AssertionError("embed must not be called"),
        chunks=["short", "tiny"],
    )

    assert doc.status == "error"
    assert "no extractable text" in (doc.error_message or "")
    assert doc.chunk_count == 0
    assert session.committed is True
    assert content.startswith("Error:")
    assert "no extractable text" in content
    assert "provider" not in content.lower()
    # Structural failure: never auto-retried (mirrors document_processor).
    assert doc.embedding_next_retry_at is None
    assert session.kb.chunk_count == 0


@pytest.mark.asyncio
async def test_store_knowledge_unusable_vectors_count_as_failed_chunks():
    """A provider that returns None/empty or zero-norm vectors without
    raising must NOT produce 'ready' docs with unusable embeddings - the
    chunks are counted as failed (here: all fail -> error + provider blame)."""
    chunks = ["first chunk long enough", "second chunk long enough"]
    content, doc, session = await _run_store_knowledge(
        embed_side_effect=[None, [0.0] * 8],  # empty, then zero-norm
        chunks=chunks,
    )

    assert doc.status == "error"
    assert "no vectors" in (doc.error_message or "")
    assert doc.chunk_count == 0
    assert content.startswith("Error:")
    assert "embedding provider" in content
    # No Embedding rows were persisted for the unusable vectors.
    from app.models.embedding import Embedding

    assert not [a for a in session.added if isinstance(a, Embedding)]
    assert session.kb.chunk_count == 0


@pytest.mark.asyncio
async def test_store_knowledge_partial_failure_stays_ready_with_warning_info():
    """One of two chunks fails -> doc stays 'ready' but the tool output
    surfaces the partial-failure counts so the agent knows."""
    chunks = ["first chunk long enough", "second chunk long enough"]
    content, doc, session = await _run_store_knowledge(
        embed_side_effect=[[0.1, 0.2, 0.3], RuntimeError("rate limited")],
        chunks=chunks,
    )

    assert doc.status == "ready"
    assert doc.chunk_count == 1
    assert session.kb.chunk_count == 1
    assert "PARTIAL" in content
    assert "1 of 2" in content
    assert "failed to embed" in content


@pytest.mark.asyncio
async def test_store_knowledge_full_success_unchanged():
    """All chunks embed -> the original success message (no partial info)."""
    chunks = ["first chunk long enough", "second chunk long enough"]
    content, doc, _session = await _run_store_knowledge(
        embed_side_effect=[[0.1, 0.2], [0.3, 0.4]], chunks=chunks
    )

    assert doc.status == "ready"
    assert doc.chunk_count == 2
    assert "2 chunks embedded" in content
    assert "PARTIAL" not in content


# ---------------------------------------------------------------------------
# MEM-5 / MEM-6: reconcile_and_persist_facts curator robustness
# ---------------------------------------------------------------------------


def _fake_db():
    # ``in_transaction`` / ``commit`` back the commit-release points in
    # ``reconcile_and_persist_facts`` (connection handed back around LLM /
    # embedding calls); returning False keeps the fake transaction-less.
    return SimpleNamespace(
        execute=AsyncMock(),
        flush=AsyncMock(),
        in_transaction=lambda: False,
        commit=AsyncMock(),
    )


def _reconcile_patches(*, facts, related, persist_entry=SimpleNamespace()):
    """Common patches for ``reconcile_and_persist_facts`` tests."""
    return (
        patch.object(
            pm, "_extract_candidate_facts", new=AsyncMock(return_value=facts)
        ),
        patch.object(
            pm, "embed_text", new=AsyncMock(return_value=[0.1] * 8)
        ),
        patch.object(
            pm, "_find_related_facts", new=AsyncMock(return_value=related)
        ),
        patch.object(
            pm, "persist_memory", new=AsyncMock(return_value=persist_entry)
        ),
        patch(
            "app.llm.router.get_embedding_provider",
            new=AsyncMock(side_effect=RuntimeError("no provider")),
        ),
    )


def _decision_json(decisions: list[dict]) -> str:
    return json.dumps({"decisions": decisions})


@pytest.mark.asyncio
async def test_curator_failure_retries_once_then_flags_degraded_add_fallback():
    """Both curator attempts fail -> retried exactly once, every candidate
    falls back to plain ADD and the summary carries curator_degraded=True."""
    facts = ["Use subregion filter, not country"]
    related = [
        {"id": str(uuid.uuid4()), "content": "Use country filter", "score": 0.8}
    ]
    db = _fake_db()
    llm = SimpleNamespace(chat=AsyncMock(side_effect=RuntimeError("LLM down")))
    p1, p2, p3, p4, p5 = _reconcile_patches(facts=facts, related=related)
    with p1, p2, p3, p4 as persist_mock, p5:
        summary = await pm.reconcile_and_persist_facts(
            db, llm, agent_id=AGENT_ID, messages_text="transcript"
        )

    assert llm.chat.call_count == 2  # initial attempt + one retry
    assert summary["curator_degraded"] is True
    assert summary["added"] == 1
    assert summary["superseded"] == 0
    assert summary["deleted"] == 0
    persist_mock.assert_awaited_once()
    db.execute.assert_not_awaited()  # no delete issued


@pytest.mark.asyncio
async def test_curator_retry_uses_stricter_prompt_and_recovers():
    """First attempt returns garbage, the terser JSON-only reprompt succeeds
    -> decisions applied normally, no degradation flag."""
    related_id = str(uuid.uuid4())
    facts = ["Use subregion filter, not country"]
    related = [{"id": related_id, "content": "Use country filter", "score": 0.8}]

    responses = [
        SimpleNamespace(content="sure! here is my analysis in prose..."),
        SimpleNamespace(
            content=_decision_json(
                [
                    {
                        "new_fact": facts[0],
                        "action": "SUPERSEDE",
                        "superseded_ids": [related_id],
                    }
                ]
            )
        ),
    ]
    captured_system_prompts: list[str] = []

    async def fake_chat(*, messages, **_kwargs):
        captured_system_prompts.append(
            next(m.content for m in messages if m.role == "system")
        )
        return responses.pop(0)

    db = _fake_db()
    llm = SimpleNamespace(chat=AsyncMock(side_effect=fake_chat))
    p1, p2, p3, p4, p5 = _reconcile_patches(facts=facts, related=related)
    with p1, p2, p3, p4, p5:
        summary = await pm.reconcile_and_persist_facts(
            db, llm, agent_id=AGENT_ID, messages_text="transcript"
        )

    assert llm.chat.call_count == 2
    # The retry must use the terser, stricter JSON-only reprompt.
    assert captured_system_prompts[0] != captured_system_prompts[1]
    assert "Return ONLY the JSON object" in captured_system_prompts[1]
    assert "no code fences" in captured_system_prompts[1]
    assert summary["curator_degraded"] is False
    assert summary["superseded"] == 1
    assert summary["deleted"] == 1
    db.execute.assert_awaited_once()  # the supersede delete


@pytest.mark.asyncio
async def test_curator_degenerate_decisions_trigger_retry_and_recover():
    """A parseable-but-unusable first response (decisions that reference no
    real candidate) must NOT stop the retry: the second attempt's valid
    decisions are applied and no degradation is flagged."""
    related_id = str(uuid.uuid4())
    facts = ["Use subregion filter, not country"]
    related = [{"id": related_id, "content": "Use country filter", "score": 0.8}]

    responses = [
        # Valid JSON shape, but no decision references a candidate fact.
        SimpleNamespace(
            content=_decision_json([{}, {"new_fact": "hallucinated fact"}])
        ),
        SimpleNamespace(
            content=_decision_json(
                [
                    {
                        "new_fact": facts[0],
                        "action": "SUPERSEDE",
                        "superseded_ids": [related_id],
                    }
                ]
            )
        ),
    ]
    db = _fake_db()
    llm = SimpleNamespace(chat=AsyncMock(side_effect=responses))
    p1, p2, p3, p4, p5 = _reconcile_patches(facts=facts, related=related)
    with p1, p2, p3, p4, p5:
        summary = await pm.reconcile_and_persist_facts(
            db, llm, agent_id=AGENT_ID, messages_text="transcript"
        )

    assert llm.chat.call_count == 2
    assert summary["curator_degraded"] is False
    assert summary["superseded"] == 1
    assert summary["deleted"] == 1


@pytest.mark.asyncio
async def test_curator_degenerate_decisions_on_both_attempts_flag_degraded():
    """Degenerate-but-parseable output on BOTH attempts is a degraded
    curator: every candidate falls back to plain ADD and the summary says so
    (previously this skipped the retry and reported curator_degraded=False)."""
    facts = ["Use subregion filter, not country"]
    related = [
        {"id": str(uuid.uuid4()), "content": "Use country filter", "score": 0.8}
    ]
    llm = SimpleNamespace(
        chat=AsyncMock(
            return_value=SimpleNamespace(content=_decision_json([{}]))
        )
    )
    db = _fake_db()
    p1, p2, p3, p4, p5 = _reconcile_patches(facts=facts, related=related)
    with p1, p2, p3, p4 as persist_mock, p5:
        summary = await pm.reconcile_and_persist_facts(
            db, llm, agent_id=AGENT_ID, messages_text="transcript"
        )

    assert llm.chat.call_count == 2
    assert summary["curator_degraded"] is True
    assert summary["added"] == 1
    assert summary["superseded"] == 0
    persist_mock.assert_awaited_once()
    db.execute.assert_not_awaited()


def _merge_cut_at_max_tokens(fact: str, related_id: str) -> SimpleNamespace:
    """A MERGE answer stopped at ``max_tokens`` inside ``merged_content``.

    json-repair turns it into a MERGE that keeps its ``superseded_ids`` and
    loses ``merged_content``, which would delete the superseded fact for the
    bare candidate.
    """
    merge = _decision_json(
        [
            {
                "new_fact": fact,
                "action": "MERGE",
                "superseded_ids": [related_id],
                "merged_content": "Filter by subregion (not country) on every KPI",
            }
        ]
    )
    return SimpleNamespace(
        content=merge[: merge.index("(not country)")], stop_reason="max_tokens"
    )


@pytest.mark.asyncio
async def test_curator_answer_cut_at_max_tokens_is_retried_with_a_doubled_budget():
    """A cut curator answer is never applied: the retry runs with twice the
    budget and its complete answer is what the apply phase uses."""
    related_id = str(uuid.uuid4())
    facts = ["Use subregion filter, not country"]
    related = [{"id": related_id, "content": "Use country filter", "score": 0.8}]
    supersede = SimpleNamespace(
        content=_decision_json(
            [
                {
                    "new_fact": facts[0],
                    "action": "SUPERSEDE",
                    "superseded_ids": [related_id],
                }
            ]
        ),
        stop_reason="end_turn",
    )
    llm = SimpleNamespace(
        chat=AsyncMock(
            side_effect=[_merge_cut_at_max_tokens(facts[0], related_id), supersede]
        )
    )
    db = _fake_db()
    p1, p2, p3, p4, p5 = _reconcile_patches(facts=facts, related=related)
    with p1, p2, p3, p4 as persist_mock, p5:
        summary = await pm.reconcile_and_persist_facts(
            db, llm, agent_id=AGENT_ID, messages_text="transcript"
        )

    assert llm.chat.call_count == 2
    budgets = [call.kwargs["max_tokens"] for call in llm.chat.call_args_list]
    assert budgets == [pm._RECONCILE_MAX_TOKENS, 2 * pm._RECONCILE_MAX_TOKENS]
    assert summary["curator_degraded"] is False
    assert (summary["merged"], summary["superseded"], summary["deleted"]) == (0, 1, 1)
    assert persist_mock.await_args.kwargs["content"] == facts[0]


_FULL_FACTS = (
    "- Use the subregion filter, not country\n"
    "- Currency prefix is EUR on every KPI tile"
)
# Cut inside the last fact: "Currency prefix is" reads like a fact of its own.
_CUT_FACTS = _FULL_FACTS[: _FULL_FACTS.index(" EUR")]


def _answers(*answers: tuple[str, str] | Exception) -> SimpleNamespace:
    """An LLM returning scripted ``(content, stop_reason)`` answers in order."""
    side_effect = [
        answer
        if isinstance(answer, Exception)
        else SimpleNamespace(content=answer[0], stop_reason=answer[1])
        for answer in answers
    ]
    return SimpleNamespace(chat=AsyncMock(side_effect=side_effect))


async def _extract(llm: SimpleNamespace) -> list[str]:
    return await pm._extract_candidate_facts(
        llm, messages_text="transcript", is_company_managed=False, has_user_scope=True
    )


@pytest.mark.asyncio
async def test_a_fact_list_cut_at_max_tokens_is_retried_with_a_doubled_budget():
    llm = _answers((_CUT_FACTS, "max_tokens"), (_FULL_FACTS, "end_turn"))

    facts = await _extract(llm)

    assert facts == [
        "Use the subregion filter, not country",
        "Currency prefix is EUR on every KPI tile",
    ]
    calls = [call.kwargs for call in llm.chat.call_args_list]
    assert [call["max_tokens"] for call in calls] == [
        pm._EXTRACT_MAX_TOKENS,
        2 * pm._EXTRACT_MAX_TOKENS,
    ]
    # A mechanical listing: no thinking to share the budget with.
    assert all(call["disable_thinking"] is True for call in calls)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "retry", [(_CUT_FACTS, "max_tokens"), RuntimeError("provider down")]
)
async def test_a_fact_list_still_cut_loses_only_its_cut_last_line(retry):
    """The cut line never becomes a candidate - reconciliation could let it
    supersede a complete fact - while the complete lines before it are kept,
    whether the retry was cut too or failed."""
    llm = _answers((_CUT_FACTS, "max_tokens"), retry)

    facts = await _extract(llm)

    assert facts == ["Use the subregion filter, not country"]


_EXISTING_PROFILE = (
    "- The user prefers replies in French\n- The user owns the EMEA revenue dashboard"
)
_FULL_PROFILE = _EXISTING_PROFILE + "\n- The user asked to always filter by subregion"
_CUT_PROFILE = _FULL_PROFILE[: _FULL_PROFILE.index("EMEA")]


class _ProfileRedis:
    """Records every ``set``; the consolidation counter is never read (force)."""

    def __init__(self) -> None:
        self.sets: dict[str, object] = {}

    async def get(self, key):
        return self.sets.get(key)

    async def set(self, key, value, ex=None, nx=False):
        if nx and key in self.sets:
            return None
        self.sets[key] = value
        return True

    async def delete(self, *keys):
        for key in keys:
            self.sets.pop(key, None)

    async def eval(self, script, _numkeys, *keys_and_args):
        from tests.memory_redis_double import run_user_memory_script

        return await run_user_memory_script(self, script, keys_and_args)

    async def exists(self, *_keys):
        return 0


def _profile_entries(profile: str) -> list[dict]:
    """*profile* (bullet lines) as stored profile entries."""
    from datetime import UTC, datetime

    from app.services.user_memory_entries import bullet_lines, new_entry

    now = datetime.now(UTC)
    return [new_entry(line, now=now).to_dict() for line in bullet_lines(profile)]


def _rewrite(profile: str) -> str:
    """A consolidation answer rewriting the profile to *profile*, with no notes."""
    from app.services.user_memory_entries import bullet_lines

    return json.dumps({"profile": bullet_lines(profile), "notes": []})


async def _consolidate_profile(
    llm: SimpleNamespace, existing: str = _EXISTING_PROFILE
) -> _ProfileRedis:
    from app.services import user_memory

    redis = _ProfileRedis()
    entries = _profile_entries(existing)
    redis.sets[user_memory._entries_key("user-1")] = json.dumps(entries)
    memory = {
        "short_term": [
            {
                "agent": "Revenue Desk",
                "summary": "The user asked to always filter by subregion.",
                "timestamp": "2026-09-26T12:00:00",
            }
        ],
        "entries": entries,
        "legacy_profile": "",
    }
    with (
        patch.object(user_memory, "load_user_memory", AsyncMock(return_value=memory)),
        patch.object(user_memory, "redis_client", AsyncMock(return_value=redis)),
    ):
        await user_memory.consolidate_user_memory("user-1", llm, force=True)
    return redis


@pytest.mark.asyncio
async def test_a_user_profile_cut_at_max_tokens_is_retried_with_a_doubled_budget():
    from app.services import user_memory

    llm = _answers((_CUT_PROFILE, "max_tokens"), (_rewrite(_FULL_PROFILE), "end_turn"))

    redis = await _consolidate_profile(llm)

    assert redis.sets["user_mem:medium:user-1"] == _FULL_PROFILE
    calls = [call.kwargs for call in llm.chat.call_args_list]
    assert [call["max_tokens"] for call in calls] == [
        user_memory._CONSOLIDATION_MAX_TOKENS,
        2 * user_memory._CONSOLIDATION_MAX_TOKENS,
    ]
    assert all(call["disable_thinking"] is True for call in calls)


@pytest.mark.asyncio
async def test_only_the_second_user_profile_call_is_traced_as_a_retry():
    from contextlib import asynccontextmanager

    from app.services import user_memory

    names: list[str] = []

    @asynccontextmanager
    async def _recording(*, name, messages):
        names.append(name)
        yield SimpleNamespace(response=None)

    llm = _answers((_CUT_PROFILE, "max_tokens"), (_rewrite(_FULL_PROFILE), "end_turn"))
    with patch.object(user_memory, "traced_generation", _recording):
        await _consolidate_profile(llm)

    first, retry = names
    assert retry == f"{first}_retry"
    assert not first.endswith("_retry")


@pytest.mark.asyncio
async def test_a_user_profile_cut_twice_keeps_the_existing_one():
    """The rewrite replaces the only copy of the profile every agent reads: a
    cut one would drop its tail for good. The counter is still reset, so an
    always-cut rewrite costs one attempt per consolidation window, not two
    calls on every turn."""
    from app.services import user_memory

    llm = _answers((_CUT_PROFILE, "max_tokens"), (_CUT_PROFILE, "max_tokens"))

    redis = await _consolidate_profile(llm)

    stored = json.loads(redis.sets[user_memory._entries_key("user-1")])
    assert [entry["text"] for entry in stored] == [
        "The user prefers replies in French",
        "The user owns the EMEA revenue dashboard",
    ]
    assert "user_mem:medium:user-1" not in redis.sets
    assert redis.sets["user_mem:counter:user-1"] == 0


def _long_profile() -> str:
    from app.services import user_memory

    profile = "\n".join(
        f"- The user reviews the QA report of run {i} every morning"
        for i in range(200)
    )
    assert len(profile) > user_memory._MAX_PROFILE_CHARS
    return profile


def _system_prompts(llm: SimpleNamespace) -> list[str]:
    return [
        next(m.content for m in call.kwargs["messages"] if m.role == "system")
        for call in llm.chat.call_args_list
    ]


@pytest.mark.asyncio
async def test_a_user_profile_over_the_cap_is_condensed_before_it_is_stored():
    """A profile that keeps growing ends up longer than one rewrite can
    reproduce, and from then on every rewrite is cut: a rewrite over the cap
    is condensed before it is stored."""
    llm = _answers((_rewrite(_long_profile()), "end_turn"), (_FULL_PROFILE, "end_turn"))

    redis = await _consolidate_profile(llm)

    assert redis.sets["user_mem:medium:user-1"] == _FULL_PROFILE
    assert "grown too large" in _system_prompts(llm)[1]
    calls = llm.chat.call_args_list
    assert all(call.kwargs["disable_thinking"] is True for call in calls)


@pytest.mark.asyncio
async def test_a_profile_no_rewrite_can_reproduce_is_condensed_instead_of_frozen():
    """The profile a rewrite cut twice was asked to carry is over the cap:
    kept as it is, every later rewrite would be cut too, so it is condensed on
    its own and the next window integrates the new activity into that."""
    llm = _answers(
        (_CUT_PROFILE, "max_tokens"),
        (_CUT_PROFILE, "max_tokens"),
        (_EXISTING_PROFILE, "end_turn"),
    )

    redis = await _consolidate_profile(llm, existing=_long_profile())

    assert redis.sets["user_mem:medium:user-1"] == _EXISTING_PROFILE
    assert redis.sets["user_mem:counter:user-1"] == 0
    assert "grown too large" in _system_prompts(llm)[2]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "condensation",
    [
        RuntimeError("provider down"),
        ("- The user prefers replies in", "max_tokens"),
        ("- The user prefers replies in French\n" * 200, "end_turn"),
    ],
)
async def test_a_condensation_that_does_not_fit_stores_whole_lines_within_the_cap(
    condensation,
):
    from app.services import user_memory

    llm = _answers((_rewrite(_long_profile()), "end_turn"), condensation, condensation)

    redis = await _consolidate_profile(llm)

    stored = redis.sets["user_mem:medium:user-1"]
    assert 0 < len(stored) <= user_memory._MAX_PROFILE_CHARS
    assert all(line.startswith("- The user ") for line in stored.splitlines())
    assert stored.splitlines()[-1].endswith(("morning", "French"))


def test_the_injected_user_profile_never_exceeds_the_cap():
    from app.services import user_memory

    block = user_memory.format_user_memory_for_prompt(
        {"short_term": [], "entries": _profile_entries(_long_profile())}
    )

    profile = block.split("\n", 1)[1]
    assert len(profile) <= user_memory._MAX_PROFILE_CHARS
    assert profile.splitlines()[-1].endswith("morning")


def test_the_injection_cut_of_a_profile_stored_over_the_cap_is_logged(caplog):
    from app.services import user_memory

    stored = _long_profile()
    with caplog.at_level("DEBUG", logger="app.services.user_memory"):
        user_memory.format_user_memory_for_prompt({"short_term": [], "entries": _profile_entries(stored)})
        user_memory.format_user_memory_for_prompt(
            {"short_term": [], "entries": _profile_entries(_EXISTING_PROFILE)}
        )

    (record,) = [r for r in caplog.records if "cut to" in r.getMessage()]
    assert str(len(stored)) in record.getMessage()


@pytest.mark.asyncio
async def test_curator_retry_budget_stops_at_the_output_ceiling():
    """Twice the budget is capped at the model's output ceiling: above it the
    retry itself would be an HTTP 400."""
    related_id = str(uuid.uuid4())
    facts = ["Use subregion filter, not country"]
    related = [{"id": related_id, "content": "Use country filter", "score": 0.8}]
    llm = SimpleNamespace(
        chat=AsyncMock(return_value=_merge_cut_at_max_tokens(facts[0], related_id)),
        max_output_tokens_overrides={"small-model": 2000},
    )
    db = _fake_db()
    p1, p2, p3, p4, p5 = _reconcile_patches(facts=facts, related=related)
    with p1, p2, p3, p4, p5:
        await pm.reconcile_and_persist_facts(
            db, llm, agent_id=AGENT_ID, messages_text="transcript", model="small-model"
        )

    budgets = [call.kwargs["max_tokens"] for call in llm.chat.call_args_list]
    assert budgets == [pm._RECONCILE_MAX_TOKENS, 2000]


@pytest.mark.asyncio
async def test_curator_answers_cut_twice_fall_back_to_plain_add():
    """Both answers cut: nothing either says is applied, every candidate takes
    the degraded plain-ADD path and no fact is deleted."""
    related_id = str(uuid.uuid4())
    facts = ["Use subregion filter, not country"]
    related = [{"id": related_id, "content": "Use country filter", "score": 0.8}]
    llm = SimpleNamespace(
        chat=AsyncMock(return_value=_merge_cut_at_max_tokens(facts[0], related_id))
    )
    db = _fake_db()
    p1, p2, p3, p4, p5 = _reconcile_patches(facts=facts, related=related)
    with p1, p2, p3, p4 as persist_mock, p5:
        summary = await pm.reconcile_and_persist_facts(
            db, llm, agent_id=AGENT_ID, messages_text="transcript"
        )

    assert llm.chat.call_count == 2
    assert summary["curator_degraded"] is True
    assert (summary["added"], summary["merged"], summary["deleted"]) == (1, 0, 0)
    assert persist_mock.await_args.kwargs["content"] == facts[0]
    db.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_supersede_with_no_valid_ids_downgrades_to_add():
    """A SUPERSEDE decision whose ids are all hallucinated must not delete
    anything, must persist the fact as a plain ADD, and must not count as
    superseded."""
    facts = ["Use subregion filter, not country"]
    related = [
        {"id": str(uuid.uuid4()), "content": "Use country filter", "score": 0.8}
    ]
    llm = SimpleNamespace(
        chat=AsyncMock(
            return_value=SimpleNamespace(
                content=_decision_json(
                    [
                        {
                            "new_fact": facts[0],
                            "action": "SUPERSEDE",
                            "superseded_ids": [str(uuid.uuid4())],  # hallucinated
                        }
                    ]
                )
            )
        )
    )
    db = _fake_db()
    p1, p2, p3, p4, p5 = _reconcile_patches(facts=facts, related=related)
    with p1, p2, p3, p4 as persist_mock, p5:
        summary = await pm.reconcile_and_persist_facts(
            db, llm, agent_id=AGENT_ID, messages_text="transcript"
        )

    db.execute.assert_not_awaited()  # old facts retained — no delete
    assert summary["added"] == 1
    assert summary["superseded"] == 0
    assert summary["deleted"] == 0
    assert summary["curator_degraded"] is False
    persist_mock.assert_awaited_once()
    # Downgraded ADD persists the original fact via the compaction source.
    kwargs = persist_mock.await_args.kwargs
    assert kwargs["content"] == facts[0]
    assert kwargs["source"] == "compaction"


@pytest.mark.asyncio
async def test_merge_with_no_valid_ids_persists_original_fact_not_merged():
    """MERGE downgraded to ADD must persist the candidate text, not the
    merged_content that references facts which were never deleted."""
    facts = ["Currency prefix is EUR"]
    related = [
        {"id": str(uuid.uuid4()), "content": "Currency prefix is USD", "score": 0.7}
    ]
    llm = SimpleNamespace(
        chat=AsyncMock(
            return_value=SimpleNamespace(
                content=_decision_json(
                    [
                        {
                            "new_fact": facts[0],
                            "action": "MERGE",
                            "superseded_ids": ["not-a-related-id"],
                            "merged_content": "Currency prefix is EUR (was USD)",
                        }
                    ]
                )
            )
        )
    )
    db = _fake_db()
    p1, p2, p3, p4, p5 = _reconcile_patches(facts=facts, related=related)
    with p1, p2, p3, p4 as persist_mock, p5:
        summary = await pm.reconcile_and_persist_facts(
            db, llm, agent_id=AGENT_ID, messages_text="transcript"
        )

    db.execute.assert_not_awaited()
    assert summary["added"] == 1
    assert summary["merged"] == 0
    kwargs = persist_mock.await_args.kwargs
    assert kwargs["content"] == facts[0]


@pytest.mark.asyncio
async def test_supersede_with_some_valid_ids_proceeds_with_valid_subset():
    """When SOME superseded ids are valid, the valid subset is deleted and
    the decision still counts as SUPERSEDE (existing behaviour, now explicit)."""
    valid_id = str(uuid.uuid4())
    facts = ["Use subregion filter, not country"]
    related = [{"id": valid_id, "content": "Use country filter", "score": 0.8}]
    llm = SimpleNamespace(
        chat=AsyncMock(
            return_value=SimpleNamespace(
                content=_decision_json(
                    [
                        {
                            "new_fact": facts[0],
                            "action": "SUPERSEDE",
                            "superseded_ids": [valid_id, str(uuid.uuid4())],
                        }
                    ]
                )
            )
        )
    )
    db = _fake_db()
    p1, p2, p3, p4, p5 = _reconcile_patches(facts=facts, related=related)
    with p1, p2, p3, p4, p5:
        summary = await pm.reconcile_and_persist_facts(
            db, llm, agent_id=AGENT_ID, messages_text="transcript"
        )

    db.execute.assert_awaited_once()
    assert summary["superseded"] == 1
    assert summary["deleted"] == 1
    assert summary["added"] == 0


@pytest.mark.asyncio
async def test_supersede_persists_replacement_with_skip_dedup():
    """A SUPERSEDE deletes its predecessors, so the curated replacement MUST
    be persisted with ``skip_dedup=True`` — otherwise a chance resemblance to
    a THIRD (non-superseded) fact would drop the correction *after* the facts
    it was meant to replace are already gone (silent data loss)."""
    valid_id = str(uuid.uuid4())
    facts = ["Use subregion filter, not country"]
    related = [{"id": valid_id, "content": "Use country filter", "score": 0.8}]
    llm = SimpleNamespace(
        chat=AsyncMock(
            return_value=SimpleNamespace(
                content=_decision_json(
                    [
                        {
                            "new_fact": facts[0],
                            "action": "SUPERSEDE",
                            "superseded_ids": [valid_id],
                        }
                    ]
                )
            )
        )
    )
    db = _fake_db()
    p1, p2, p3, p4, p5 = _reconcile_patches(facts=facts, related=related)
    with p1, p2, p3, p4 as persist_mock, p5:
        summary = await pm.reconcile_and_persist_facts(
            db, llm, agent_id=AGENT_ID, messages_text="transcript"
        )

    assert summary["superseded"] == 1
    assert summary["deleted"] == 1
    persist_mock.assert_awaited_once()
    assert persist_mock.await_args.kwargs["skip_dedup"] is True


@pytest.mark.asyncio
async def test_supersede_zero_rowcount_does_not_bypass_dedup():
    """``skip_dedup`` must track the DELETE's ACTUAL affected-row count, not
    the number of validated superseded ids.  If a concurrent prune / cap
    eviction already removed the rows, the DELETE affects 0 rows: the dedup
    bypass must stay OFF (the predecessors are already gone, so the curated
    fact has nothing to guarantee-replace) and ``summary["deleted"]`` must
    not be over-reported.  Were ``deleted_now`` still ``len(superseded_uuids)``
    this would wrongly set ``skip_dedup=True`` and report ``deleted == 1``."""
    valid_id = str(uuid.uuid4())
    facts = ["Use subregion filter, not country"]
    related = [{"id": valid_id, "content": "Use country filter", "score": 0.8}]
    llm = SimpleNamespace(
        chat=AsyncMock(
            return_value=SimpleNamespace(
                content=_decision_json(
                    [
                        {
                            "new_fact": facts[0],
                            "action": "SUPERSEDE",
                            "superseded_ids": [valid_id],
                        }
                    ]
                )
            )
        )
    )
    # The DELETE matches 0 rows (rows already removed by a concurrent task).
    db = SimpleNamespace(
        execute=AsyncMock(return_value=SimpleNamespace(rowcount=0)),
        flush=AsyncMock(),
        in_transaction=lambda: False,
        commit=AsyncMock(),
    )
    p1, p2, p3, p4, p5 = _reconcile_patches(facts=facts, related=related)
    with p1, p2, p3, p4 as persist_mock, p5:
        summary = await pm.reconcile_and_persist_facts(
            db, llm, agent_id=AGENT_ID, messages_text="transcript"
        )

    db.execute.assert_awaited_once()  # the supersede delete still ran
    assert summary["deleted"] == 0  # nothing actually deleted -> no over-report
    persist_mock.assert_awaited_once()
    assert persist_mock.await_args.kwargs["skip_dedup"] is False


@pytest.mark.asyncio
async def test_plain_add_keeps_dedup_enabled():
    """A plain ADD deletes nothing, so it must keep dedup ACTIVE
    (``skip_dedup=False``) — redundant near-duplicates should still be
    skipped.  A candidate with no related facts skips the curator entirely
    and goes straight to the plain-ADD path."""
    facts = ["A brand new standalone durable fact"]
    db = _fake_db()
    llm = SimpleNamespace(chat=AsyncMock())
    p1, p2, p3, p4, p5 = _reconcile_patches(facts=facts, related=[])
    with p1, p2, p3, p4 as persist_mock, p5:
        summary = await pm.reconcile_and_persist_facts(
            db, llm, agent_id=AGENT_ID, messages_text="transcript"
        )

    assert summary["added"] == 1
    llm.chat.assert_not_awaited()  # no related facts -> no curator call
    persist_mock.assert_awaited_once()
    assert persist_mock.await_args.kwargs["skip_dedup"] is False


# ---------------------------------------------------------------------------
# MEM-7: persist_memory embed retry + durable persistence
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_persist_memory_retries_embedding_then_persists_without_it():
    """Embedding failing twice -> the entry is still persisted (durable
    beats lost) with NULL embedding and NULL provenance."""
    db = SimpleNamespace(add=Mock(), flush=AsyncMock(), execute=AsyncMock())
    embed_mock = AsyncMock(return_value=(None, None))
    with patch.object(pm, "embed_text_with_model", new=embed_mock):
        entry = await pm.persist_memory(
            db, agent_id=AGENT_ID, content="durable fact", memory_type="fact"
        )

    assert embed_mock.await_count == 2  # initial attempt + one retry
    assert entry is not None
    assert entry.embedding is None
    assert entry.embedding_model is None
    assert entry.embedding_dimensions is None
    db.add.assert_called_once_with(entry)


@pytest.mark.asyncio
async def test_persist_memory_embed_retry_success_stores_embedding():
    """Transient failure on the first embed call self-heals on retry.

    The retried embedding must carry full ``embedding_model`` /
    ``embedding_dimensions`` provenance: a row whose dimensions are NULL is
    excluded by ``_matching_dimensions_filter`` and would stay invisible to
    semantic search and dedup until the next re-embed pass.
    """
    db = SimpleNamespace(add=Mock(), flush=AsyncMock(), execute=AsyncMock())
    embed_mock = AsyncMock(side_effect=[(None, None), ([0.1] * 8, "model-x")])
    with (
        patch.object(pm, "embed_text_with_model", new=embed_mock),
        patch.object(pm, "_is_duplicate_fact", new=AsyncMock(return_value=False)),
    ):
        entry = await pm.persist_memory(
            db, agent_id=AGENT_ID, content="durable fact", memory_type="fact"
        )

    assert embed_mock.await_count == 2
    assert entry is not None
    assert entry.embedding == [0.1] * 8
    assert entry.embedding_model == "model-x"
    assert entry.embedding_dimensions == 8


@pytest.mark.asyncio
async def test_persist_memory_strips_null_bytes_before_embedding_and_insert():
    """Null bytes in memory text must be removed before DB persistence."""
    db = SimpleNamespace(add=Mock(), flush=AsyncMock(), execute=AsyncMock())
    embed_mock = AsyncMock(return_value=([0.1] * 3, "model-x"))
    with (
        patch.object(pm, "embed_text_with_model", new=embed_mock),
        patch.object(pm, "_is_duplicate_fact", new=AsyncMock(return_value=False)),
    ):
        entry = await pm.persist_memory(
            db,
            agent_id=AGENT_ID,
            content="alpha\x00beta",
            memory_type="fa\x00ct",
            source="to\x00ol",
        )

    assert entry is not None
    assert entry.content == "alphabeta"
    assert entry.source == "tool"
    assert entry.memory_type == "fact"
    assert embed_mock.await_args.args[1] == "alphabeta"


def test_is_zero_norm_vector_contract():
    """Lock the shared helper's contract: zero norm includes the degenerate
    empty vector (a caller relying on this check alone never persists an
    unusable vector); call sites wanting a distinct empty-vector log check
    emptiness first."""
    from app.core.utils import is_zero_norm_vector

    assert is_zero_norm_vector([0.0, 0.0, 0.0]) is True
    assert is_zero_norm_vector([]) is True
    assert is_zero_norm_vector([0.1, 0.0]) is False


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_vector", [[], [0.0] * 8])
async def test_embed_text_with_model_rejects_empty_and_zero_norm_vectors(
    bad_vector,
):
    """A provider returning an empty or zero-norm vector without raising must
    be treated as a failed embedding (``(None, None)``) so callers store NULL
    instead of an unusable 'searchable' vector and ``no_embedding_count``
    stays meaningful."""
    provider = SimpleNamespace(embed=AsyncMock(return_value=bad_vector))
    embedding, model = await pm.embed_text_with_model(
        SimpleNamespace(), "some text", _provider_model=(provider, "test-model")
    )

    assert embedding is None
    assert model is None


@pytest.mark.asyncio
async def test_reembed_stale_memories_leaves_entry_stale_on_zero_norm_vector():
    """Re-embedding must not stamp a zero-norm vector with the current
    model/dims provenance — that would permanently remove the entry from the
    stale set while poisoning cosine searches. The entry stays stale for the
    next maintenance pass."""
    entry = SimpleNamespace(
        id=uuid.uuid4(),
        user_id=None,
        content="stale fact",
        embedding=None,
        embedding_model=None,
        embedding_dimensions=None,
        created_at=None,
    )

    class _PageDB:
        def __init__(self):
            self.flush = AsyncMock()
            self.commit = AsyncMock()

        def in_transaction(self):
            # The commit-release point only commits when a
            # transaction is open; the fake session never opens one.
            return False

        async def execute(self, _stmt):
            return SimpleNamespace(
                scalars=lambda: SimpleNamespace(all=lambda: [entry])
            )

    provider = SimpleNamespace(embed=AsyncMock(return_value=[0.0] * 8))
    with patch.object(
        pm,
        "_resolve_embedding_target",
        new=AsyncMock(return_value=(provider, "test-model", 8)),
    ):
        reembedded = await pm.reembed_stale_memories(
            _PageDB(), agent_id=AGENT_ID
        )

    assert reembedded == 0
    assert entry.embedding is None
    assert entry.embedding_model is None
    assert entry.embedding_dimensions is None


@pytest.mark.asyncio
async def test_memory_health_still_reports_no_embedding_count():
    """``get_memory_health()`` must keep surfacing ``no_embedding_count`` so
    maintenance can find rows persisted without an embedding (MEM-7).

    The stub answers by inspecting the rendered SQL instead of relying on a
    fixed query sequence, so adding/reordering health queries (e.g. the
    per-scope cap count) does not break this test.
    """

    class _HealthDB:
        async def execute(self, stmt):
            if "embedding IS NULL" in str(stmt):
                return SimpleNamespace(scalar=lambda: 2)
            return SimpleNamespace(
                all=lambda: [],
                scalar_one_or_none=lambda: None,
                scalar=lambda: 0,
            )

    health = await pm.get_memory_health(_HealthDB(), agent_id=AGENT_ID)
    assert health["no_embedding_count"] == 2
