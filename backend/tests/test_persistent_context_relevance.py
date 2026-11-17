"""Tests for relevance-ranked persistent context injection (MEM-8).

``load_persistent_context`` used to inject the last N persistent facts
into every prompt ordered purely by recency — regardless of relevance to
the user's message.  The fix adds an optional ``query`` parameter: when
provided and an embedding can be computed, facts are selected by semantic
relevance (pgvector cosine distance, same scoping as
``search_agent_memories``), falling back to recency when the query is
absent, the embedding fails, or fewer than ``limit`` relevant hits exist.

These tests exercise the selection logic with a fake DB session so no
PostgreSQL/pgvector instance is required:

* query provided  -> relevant facts selected (relevance query issued)
* no query        -> recency behaviour unchanged (no embedding call)
* embed failure   -> graceful fallback to recency
* scoping         -> other users' facts are never injected
* zero entries    -> short-circuit before the embedding call
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

from app.models.agent_memory_entry import AgentMemoryEntry
from app.services.persistent_memory import load_persistent_context

_AGENT_ID = "00000000-0000-0000-0000-000000000001"
_USER_ID = "00000000-0000-0000-0000-000000000099"

# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


def _entry(content: str, *, age_minutes: int = 0, memory_type: str = "fact") -> AgentMemoryEntry:
    """Build an in-memory AgentMemoryEntry (never touches the DB)."""
    return AgentMemoryEntry(
        id=uuid.uuid4(),
        agent_id=uuid.UUID(_AGENT_ID),
        user_id=None,
        content=content,
        memory_type=memory_type,
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


def _classify(stmt) -> str:
    """Classify a captured SELECT as probe / relevance / recency.

    Uses only public SQLAlchemy APIs: ``column_descriptions`` for the
    probe's single-column shape and the rendered SQL string for the
    ORDER BY clause (recency orders by ``created_at DESC``; relevance
    orders by the pgvector cosine-distance expression).
    """
    descs = stmt.column_descriptions
    if len(descs) == 1 and descs[0]["name"] == "id":
        return "probe"
    if "ORDER BY agent_memory_entries.created_at DESC" in str(stmt):
        return "recency"
    return "relevance"


class _FakeDB:
    """Fake AsyncSession that dispatches on the *shape* of each SELECT.

    The probe reports ``probe_count`` ids (more than any test's limit by
    default when ``probe_id`` is set, so the semantic path runs).
    """

    def __init__(self, *, probe_id=None, relevant=None, recent=None, probe_count=None):
        self.statements: list = []
        self._probe_id = probe_id
        self._probe_count = probe_count if probe_count is not None else (1_000 if probe_id else 0)
        self._relevant = relevant or []
        self._recent = recent or []

    async def execute(self, stmt):
        self.statements.append(stmt)
        kind = _classify(stmt)
        if kind == "probe":
            return _FakeResult(scalar=self._probe_id, entries=[uuid.uuid4() for _ in range(self._probe_count)])
        if kind == "relevance":
            return _FakeResult(entries=self._relevant)
        return _FakeResult(entries=self._recent)

    def kinds(self) -> list[str]:
        return [_classify(s) for s in self.statements]


_EMBED_PATH = "app.services.persistent_memory.embed_text"
_FAKE_VECTOR = [0.1, 0.2, 0.3]


# ---------------------------------------------------------------------------
# Relevance selection
# ---------------------------------------------------------------------------


async def test_query_selects_relevant_facts() -> None:
    """With a query and a working embedding, facts come from the relevance
    query (ordered by cosine distance, floored by the relevance threshold)."""
    relevant = [
        _entry("Use subregion filter, not country", age_minutes=600),
        _entry("Currency prefix is EUR", age_minutes=10),
    ]
    db = _FakeDB(probe_id=uuid.uuid4(), relevant=relevant)

    with patch(_EMBED_PATH, new=AsyncMock(return_value=_FAKE_VECTOR)) as embed_mock:
        text = await load_persistent_context(
            db, agent_id=_AGENT_ID, user_id=_USER_ID, limit=2, query="how do I filter ADS metrics?"
        )

    assert embed_mock.await_count == 1
    # probe -> relevance; the limit is satisfied so no recency top-up runs.
    assert db.kinds() == ["probe", "relevance"]
    assert "Use subregion filter, not country" in text
    assert "Currency prefix is EUR" in text
    # Output stays in the historical chronological format (oldest first).
    assert text.index("Use subregion filter") < text.index("Currency prefix")
    assert text.startswith("── Persistent Knowledge")


async def test_query_top_up_fills_remainder_by_recency_dedup_by_id() -> None:
    """Fewer relevant hits than limit -> remainder filled by recency,
    deduplicated by id."""
    shared = _entry("Relevant and recent", age_minutes=5)
    other_recent = _entry("Recent but unrelated", age_minutes=1)
    db = _FakeDB(
        probe_id=uuid.uuid4(),
        relevant=[shared],
        recent=[other_recent, shared],
    )

    with patch(_EMBED_PATH, new=AsyncMock(return_value=_FAKE_VECTOR)):
        text = await load_persistent_context(
            db, agent_id=_AGENT_ID, user_id=_USER_ID, limit=3, query="anything"
        )

    assert db.kinds() == ["probe", "relevance", "recency"]
    assert text.count("Relevant and recent") == 1
    assert "Recent but unrelated" in text


async def test_top_up_reaches_limit_when_relevant_hits_fill_recency_window() -> None:
    """Worst-case overlap: every relevant hit also sits inside the
    most-recent ``limit`` rows.  Each overlapping row is skipped by the id
    dedup but already occupies one slot in the selection, so the union of
    both result sets still reaches ``limit`` without any over-fetch."""
    relevant = [
        _entry("Relevant A", age_minutes=1),
        _entry("Relevant B", age_minutes=2),
    ]
    extra = [
        _entry("Recent C", age_minutes=3),
        _entry("Recent D", age_minutes=4),
    ]
    # The recency window (limit=4, newest first) contains both relevant
    # entries plus exactly two fresh ones.
    db = _FakeDB(
        probe_id=uuid.uuid4(),
        relevant=relevant,
        recent=[*relevant, *extra],
    )

    with patch(_EMBED_PATH, new=AsyncMock(return_value=_FAKE_VECTOR)):
        text = await load_persistent_context(
            db, agent_id=_AGENT_ID, user_id=_USER_ID, limit=4, query="q"
        )

    assert db.kinds() == ["probe", "relevance", "recency"]
    # All four unique entries are injected — the limit is fully reached.
    for content in ("Relevant A", "Relevant B", "Recent C", "Recent D"):
        assert content in text
    assert text.count("Relevant A") == 1
    assert text.count("Relevant B") == 1


# ---------------------------------------------------------------------------
# Recency fallbacks
# ---------------------------------------------------------------------------


async def test_no_query_keeps_recency_behavior_and_skips_embedding() -> None:
    recent = [
        _entry("Newest fact", age_minutes=1),
        _entry("Older fact", age_minutes=120),
    ]
    db = _FakeDB(recent=recent)

    with patch(_EMBED_PATH, new=AsyncMock()) as embed_mock:
        text = await load_persistent_context(db, agent_id=_AGENT_ID, user_id=_USER_ID)

    embed_mock.assert_not_awaited()
    # Single recency SELECT — no probe, no relevance query.
    assert db.kinds() == ["recency"]
    # Chronological rendering (oldest first), exactly as before.
    assert text.index("Older fact") < text.index("Newest fact")
    assert "• [fact] Older fact" in text


async def test_embed_failure_falls_back_to_recency() -> None:
    """``embed_text`` returning None (provider down) degrades to recency."""
    recent = [_entry("Fallback fact")]
    db = _FakeDB(probe_id=uuid.uuid4(), recent=recent)

    with patch(_EMBED_PATH, new=AsyncMock(return_value=None)):
        text = await load_persistent_context(
            db, agent_id=_AGENT_ID, user_id=_USER_ID, query="some question"
        )

    assert db.kinds() == ["probe", "recency"]
    assert "Fallback fact" in text


async def test_empty_query_is_treated_as_no_query() -> None:
    db = _FakeDB(recent=[_entry("A fact")])

    with patch(_EMBED_PATH, new=AsyncMock()) as embed_mock:
        text = await load_persistent_context(
            db, agent_id=_AGENT_ID, user_id=_USER_ID, query="   "
        )

    embed_mock.assert_not_awaited()
    assert db.kinds() == ["recency"]
    assert "A fact" in text


# ---------------------------------------------------------------------------
# Zero-entries short-circuit
# ---------------------------------------------------------------------------


async def test_zero_entries_short_circuits_before_embedding() -> None:
    """Agents with no persistent entries must not pay the embedding cost."""
    db = _FakeDB(probe_id=None)

    with patch(_EMBED_PATH, new=AsyncMock()) as embed_mock:
        text = await load_persistent_context(
            db, agent_id=_AGENT_ID, user_id=_USER_ID, query="hello"
        )

    assert text == ""
    embed_mock.assert_not_awaited()
    assert db.kinds() == ["probe"]


async def test_a_scope_within_the_limit_skips_the_embedding() -> None:
    """At most *limit* entries in scope: the recency load returns every one of
    them whatever the query, so the embedding round-trip - on every chat turn,
    before the first token - could not change the answer."""
    recent = [_entry("Newest fact", age_minutes=1), _entry("Older fact", age_minutes=120)]
    db = _FakeDB(probe_id=uuid.uuid4(), probe_count=2, recent=recent)

    with patch(_EMBED_PATH, new=AsyncMock(return_value=_FAKE_VECTOR)) as embed_mock:
        text = await load_persistent_context(
            db, agent_id=_AGENT_ID, user_id=_USER_ID, limit=10, query="how do I filter?"
        )

    embed_mock.assert_not_awaited()
    assert db.kinds() == ["probe", "recency"]
    assert text.index("Older fact") < text.index("Newest fact")


async def test_a_scope_past_the_limit_still_searches_semantically() -> None:
    db = _FakeDB(probe_id=uuid.uuid4(), probe_count=11, relevant=[_entry("Relevant")])

    with patch(_EMBED_PATH, new=AsyncMock(return_value=_FAKE_VECTOR)) as embed_mock:
        await load_persistent_context(db, agent_id=_AGENT_ID, user_id=_USER_ID, limit=10, query="q")

    assert embed_mock.await_count == 1
    assert db.kinds()[:2] == ["probe", "relevance"]


# ---------------------------------------------------------------------------
# Scoping — other users' facts are never injected
# ---------------------------------------------------------------------------


def _where_sql(stmt) -> str:
    return str(stmt.whereclause)


async def test_scoping_with_user_id_matches_search_agent_memories() -> None:
    """With a user_id: agent-level (user_id IS NULL) + that user's entries —
    identical scoping to ``search_agent_memories``."""
    db = _FakeDB(probe_id=uuid.uuid4(), relevant=[_entry("scoped fact")])

    with patch(_EMBED_PATH, new=AsyncMock(return_value=_FAKE_VECTOR)):
        await load_persistent_context(
            db, agent_id=_AGENT_ID, user_id=_USER_ID, limit=1, query="q"
        )

    for stmt in db.statements:
        where = _where_sql(stmt)
        assert "agent_memory_entries.agent_id =" in where
        # Both halves of the OR scope: the given user OR agent-level.
        assert "agent_memory_entries.user_id =" in where
        assert "agent_memory_entries.user_id IS NULL" in where
        # The bound user_id is the caller's, never another user's.
        params = stmt.compile().params
        assert uuid.UUID(_USER_ID) in params.values()


async def test_scoping_without_user_id_restricts_to_agent_level() -> None:
    """Without a user_id (assignments, anonymous): only agent-level entries
    are eligible — per-user facts can never leak."""
    db = _FakeDB(probe_id=uuid.uuid4(), relevant=[_entry("agent-level fact")])

    with patch(_EMBED_PATH, new=AsyncMock(return_value=_FAKE_VECTOR)):
        await load_persistent_context(db, agent_id=_AGENT_ID, user_id=None, limit=1, query="q")

    for stmt in db.statements:
        where = _where_sql(stmt)
        assert "agent_memory_entries.user_id IS NULL" in where
        assert "agent_memory_entries.user_id =" not in where


async def test_consolidated_entries_excluded_in_both_paths() -> None:
    """The relevance path must keep excluding consolidated entries (they are
    injected via the Redis medium-term slot already)."""
    db = _FakeDB(probe_id=uuid.uuid4(), relevant=[_entry("f")], recent=[_entry("g")])

    with patch(_EMBED_PATH, new=AsyncMock(return_value=_FAKE_VECTOR)):
        await load_persistent_context(db, agent_id=_AGENT_ID, user_id=_USER_ID, query="q")

    for stmt in db.statements:
        assert "memory_type !=" in _where_sql(stmt)
