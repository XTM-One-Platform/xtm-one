"""Tests for agent memory embedding provenance and re-embedding (MEM-3).

``AgentMemoryEntry`` rows now record which embedding model produced their
vector (``embedding_model``) and the vector size (``embedding_dimensions``),
mirroring the KB ``embeddings`` table.  This enables:

1. Stamping provenance on every memory embedding write.
2. Dimension-filtered cosine searches — pgvector's ``<=>`` errors out (or
   returns garbage) when query and stored vectors have different sizes,
   which happens after an admin changes the platform embedding model.
3. A re-embed maintenance task (``reembed_stale_memories``) that finds
   NULL-stamped or model/dimension-mismatched entries and re-embeds them
   with the current model.
4. Stale-embedding counts in ``get_memory_health``.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.models.agent_memory_entry import AgentMemoryEntry
from app.services import persistent_memory

AGENT_ID = "00000000-0000-0000-0000-000000000001"


@pytest.fixture(autouse=True)
def _nobody_excluded_from_embeddings():
    """The fake sessions cannot answer who the embedding provider excludes:
    nobody is, unless a test says otherwise."""
    with patch.object(
        persistent_memory,
        "_embedding_excluded_users",
        new=AsyncMock(return_value=frozenset()),
    ):
        yield


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _Result:
    """Canned result for a single ``db.execute`` call."""

    def __init__(
        self,
        *,
        all_rows=None,
        scalar=None,
        scalar_one_or_none=None,
        scalars_all=None,
    ):
        self._all_rows = all_rows if all_rows is not None else []
        self._scalar = scalar
        self._sone = scalar_one_or_none
        self._scalars_all = scalars_all if scalars_all is not None else []

    def all(self):
        return self._all_rows

    def fetchall(self):
        return self._all_rows

    def scalar(self):
        return self._scalar

    def scalar_one_or_none(self):
        return self._sone

    def scalars(self):
        return SimpleNamespace(all=lambda: self._scalars_all)


class _FakeDB:
    """Minimal AsyncSession stand-in returning canned results in order."""

    def __init__(self, results: list[_Result]):
        self._results = list(results)
        self.queries: list[object] = []
        self.added: list[object] = []

    async def execute(self, query, *args, **kwargs):
        self.queries.append(query)
        return self._results.pop(0)

    def add(self, obj):
        self.added.append(obj)

    async def flush(self):
        return None

    def in_transaction(self):
        # The commit-release points only commit when a transaction
        # is open; the fake session never opens one.
        return False

    async def commit(self):
        return None


def _compiled(query) -> str:
    """Best-effort SQL text of a SQLAlchemy statement for assertions."""
    return str(query)


# ---------------------------------------------------------------------------
# 1. Writes stamp embedding_model / embedding_dimensions
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_persist_memory_stamps_model_and_dimensions():
    """A new fact entry must carry the resolved model id and vector size."""
    db = _FakeDB([_Result(scalar_one_or_none=None)])  # dedup lookup → no dup

    with patch.object(
        persistent_memory,
        "embed_text_with_model",
        new=AsyncMock(return_value=([0.1, 0.2, 0.3, 0.4], "test-embed-model")),
    ):
        entry = await persistent_memory.persist_memory(
            db,  # type: ignore[arg-type]
            agent_id=AGENT_ID,
            content="The currency prefix is EUR.",
            memory_type="fact",
        )

    assert entry is not None
    assert entry.embedding_model == "test-embed-model"
    assert entry.embedding_dimensions == 4
    assert db.added == [entry]


@pytest.mark.asyncio
async def test_persist_memory_consolidated_upsert_restamps_provenance():
    """Updating the consolidated entry must refresh the provenance stamp,
    otherwise a model change would leave the row marked with the old model."""
    existing = AgentMemoryEntry(
        agent_id=uuid.UUID(AGENT_ID),
        content="old summary",
        memory_type="consolidated",
        source="chat",
        embedding=[0.5] * 8,
        embedding_model="old-model",
        embedding_dimensions=8,
    )
    db = _FakeDB([_Result(scalar_one_or_none=existing)])

    with patch.object(
        persistent_memory,
        "embed_text_with_model",
        new=AsyncMock(return_value=([0.1, 0.2], "new-model")),
    ):
        entry = await persistent_memory.persist_memory(
            db,  # type: ignore[arg-type]
            agent_id=AGENT_ID,
            content="new summary",
            memory_type="consolidated",
        )

    assert entry is existing
    assert entry.embedding_model == "new-model"
    assert entry.embedding_dimensions == 2


@pytest.mark.asyncio
async def test_persist_memory_without_embedding_leaves_stamp_null():
    """Embedding failure must store NULL provenance (= stale/unknown), not
    a bogus stamp."""
    db = _FakeDB([])

    with patch.object(
        persistent_memory,
        "embed_text_with_model",
        new=AsyncMock(return_value=(None, None)),
    ):
        entry = await persistent_memory.persist_memory(
            db,  # type: ignore[arg-type]
            agent_id=AGENT_ID,
            content="Unembeddable fact for some reason.",
            memory_type="fact",
        )

    assert entry is not None
    assert entry.embedding is None
    assert entry.embedding_model is None
    assert entry.embedding_dimensions is None


# ---------------------------------------------------------------------------
# 2. Cosine searches filter on matching dimensions
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_search_agent_memories_filters_on_query_dimensions():
    """The semantic search query must restrict matches to rows whose
    ``embedding_dimensions`` equals the query embedding's size — a
    dimension mismatch makes pgvector error or return garbage."""
    db = _FakeDB([_Result(all_rows=[])])

    with patch.object(
        persistent_memory,
        "embed_text",
        new=AsyncMock(return_value=[0.1, 0.2, 0.3]),
    ):
        results = await persistent_memory.search_agent_memories(
            db,  # type: ignore[arg-type]
            agent_id=AGENT_ID,
            query="what currency?",
        )

    assert results == []
    assert len(db.queries) == 1
    sql = _compiled(db.queries[0])
    assert "embedding_dimensions" in sql, (
        "search_agent_memories must filter on embedding_dimensions to avoid "
        "cosine_distance over mismatched vector sizes"
    )


def test_matching_dimensions_filter_excludes_null_stamps():
    """NULL-stamped rows must NOT match (SQL ``NULL = n`` is not true);
    the re-embed maintenance task is responsible for fixing them."""
    clause = persistent_memory._matching_dimensions_filter([0.0] * 5)
    sql = _compiled(clause)
    assert "embedding_dimensions" in sql
    # An equality comparison treats NULL as non-matching in SQL — make sure
    # the clause is a plain equality and not an IS NULL / coalesce variant.
    assert "IS NULL" not in sql.upper()


@pytest.mark.asyncio
async def test_duplicate_fact_check_filters_on_dimensions():
    """The dedup cosine query must apply the same dimension guard."""
    db = _FakeDB([_Result(scalar_one_or_none=None)])

    is_dup = await persistent_memory._is_duplicate_fact(
        db,  # type: ignore[arg-type]
        agent_id=AGENT_ID,
        user_id=None,
        embedding=[0.1] * 6,
    )

    assert is_dup is False
    sql = _compiled(db.queries[0])
    assert "embedding_dimensions" in sql


@pytest.mark.asyncio
async def test_find_related_facts_filters_on_dimensions():
    """The reconciliation contradiction-zone query must apply the guard."""
    db = _FakeDB([_Result(all_rows=[])])

    related = await persistent_memory._find_related_facts(
        db,  # type: ignore[arg-type]
        agent_id=AGENT_ID,
        user_id=None,
        embedding=[0.1] * 6,
    )

    assert related == []
    sql = _compiled(db.queries[0])
    assert "embedding_dimensions" in sql


# ---------------------------------------------------------------------------
# 3. reembed_stale_memories
# ---------------------------------------------------------------------------


def _stale_entries() -> list[AgentMemoryEntry]:
    null_stamped = AgentMemoryEntry(
        id=uuid.uuid4(),
        agent_id=uuid.UUID(AGENT_ID),
        content="legacy entry without provenance",
        memory_type="fact",
        source="chat",
        embedding=[0.9] * 4,
        embedding_model=None,
        embedding_dimensions=None,
    )
    mismatched = AgentMemoryEntry(
        id=uuid.uuid4(),
        agent_id=uuid.UUID(AGENT_ID),
        content="entry embedded with the previous model",
        memory_type="fact",
        source="compaction",
        embedding=[0.8] * 4,
        embedding_model="old-model",
        embedding_dimensions=4,
    )
    no_vector = AgentMemoryEntry(
        id=uuid.uuid4(),
        agent_id=uuid.UUID(AGENT_ID),
        content="entry that never got an embedding",
        memory_type="fact",
        source="chat",
        embedding=None,
        embedding_model=None,
        embedding_dimensions=None,
    )
    return [null_stamped, mismatched, no_vector]


@pytest.mark.asyncio
async def test_reembed_stale_memories_reembeds_and_stamps():
    """NULL-embedding, NULL-stamped, and model-mismatched rows must all be
    re-embedded with the current model and stamped."""
    entries = _stale_entries()
    # Single short page (3 entries < batch size) — the keyset loop stops
    # after one page query.
    db = _FakeDB([_Result(scalars_all=entries)])

    provider = SimpleNamespace(embed=AsyncMock(return_value=[0.1] * 8))
    with patch.object(
        persistent_memory,
        "_resolve_embedding_target",
        new=AsyncMock(return_value=(provider, "new-model", 8)),
    ):
        count = await persistent_memory.reembed_stale_memories(
            db,  # type: ignore[arg-type]
            agent_id=AGENT_ID,
        )

    assert count == 3
    for entry in entries:
        assert entry.embedding == [0.1] * 8
        assert entry.embedding_model == "new-model"
        assert entry.embedding_dimensions == 8

    # The stale-selection query must consider both provenance columns.
    sql = _compiled(db.queries[0])
    assert "embedding_model" in sql
    assert "embedding_dimensions" in sql


@pytest.mark.asyncio
async def test_reembed_stale_memories_counts_failures_separately():
    """A provider failure on one entry must not abort the batch — the
    failing entry keeps its old state and is excluded from the count."""
    entries = _stale_entries()[:2]

    calls = {"n": 0}

    async def _flaky_embed(text, model=None):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("embedding provider hiccup")
        return [0.2] * 8

    db = _FakeDB([_Result(scalars_all=entries)])
    provider = SimpleNamespace(embed=_flaky_embed)
    with patch.object(
        persistent_memory,
        "_resolve_embedding_target",
        new=AsyncMock(return_value=(provider, "new-model", 8)),
    ):
        count = await persistent_memory.reembed_stale_memories(
            db,  # type: ignore[arg-type]
            agent_id=AGENT_ID,
        )

    assert count == 1
    assert entries[0].embedding_model is None  # failed → untouched
    assert entries[1].embedding_model == "new-model"


@pytest.mark.asyncio
async def test_reembed_stale_memories_noop_without_provider():
    """No configured embedding provider → no-op, never an exception."""
    db = _FakeDB([])

    with patch.object(
        persistent_memory,
        "_resolve_embedding_target",
        new=AsyncMock(return_value=(None, None, None)),
    ):
        count = await persistent_memory.reembed_stale_memories(
            db,  # type: ignore[arg-type]
        )

    assert count == 0
    assert db.queries == []


@pytest.mark.asyncio
async def test_reembed_stale_memories_noop_when_nothing_stale():
    db = _FakeDB([_Result(all_rows=[])])

    provider = SimpleNamespace(embed=AsyncMock())
    with patch.object(
        persistent_memory,
        "_resolve_embedding_target",
        new=AsyncMock(return_value=(provider, "new-model", None)),
    ):
        count = await persistent_memory.reembed_stale_memories(
            db,  # type: ignore[arg-type]
        )

    assert count == 0
    provider.embed.assert_not_called()


@pytest.mark.asyncio
async def test_reembed_paginates_with_keyset_cursor():
    """The stale set must be iterated in DB-sized pages with a
    (created_at, id) keyset cursor — not materialized as one full id list
    (a platform-wide model change marks EVERY entry stale at once)."""
    entries = _stale_entries()[:2]
    for e in entries:
        e.created_at = datetime.now(UTC)
    db = _FakeDB(
        [
            _Result(scalars_all=[entries[0]]),  # page 1 (full)
            _Result(scalars_all=[entries[1]]),  # page 2 (full)
            _Result(scalars_all=[]),  # page 3 — empty, loop ends
        ]
    )

    provider = SimpleNamespace(embed=AsyncMock(return_value=[0.4] * 8))
    with patch.object(
        persistent_memory,
        "_resolve_embedding_target",
        new=AsyncMock(return_value=(provider, "new-model", 8)),
    ):
        count = await persistent_memory.reembed_stale_memories(
            db,  # type: ignore[arg-type]
            agent_id=AGENT_ID,
            batch_size=1,
        )

    assert count == 2
    assert len(db.queries) == 3
    # Pages after the first must advance the keyset cursor instead of
    # re-reading from the start (or relying on a precomputed id snapshot).
    first_where = str(db.queries[0].whereclause)
    second_where = str(db.queries[1].whereclause)
    assert "created_at" not in first_where
    assert "created_at" in second_where


@pytest.mark.asyncio
async def test_reembed_cursor_moves_past_persistently_failing_entries():
    """Entries that keep failing to embed REMAIN stale — the keyset cursor
    must move past them so the loop terminates instead of re-selecting the
    same failing rows forever."""
    entry = _stale_entries()[0]
    entry.created_at = datetime.now(UTC)
    db = _FakeDB(
        [
            _Result(scalars_all=[entry]),  # page 1 — full page, all fail
            _Result(scalars_all=[]),  # page 2 — cursor moved past, empty
        ]
    )

    provider = SimpleNamespace(embed=AsyncMock(return_value=[]))  # always fails
    with patch.object(
        persistent_memory,
        "_resolve_embedding_target",
        new=AsyncMock(return_value=(provider, "new-model", 8)),
    ):
        count = await persistent_memory.reembed_stale_memories(
            db,  # type: ignore[arg-type]
            agent_id=AGENT_ID,
            batch_size=1,
        )

    assert count == 0
    assert len(db.queries) == 2  # terminated — no infinite re-select
    assert entry.embedding_model is None  # failed entry left stale


# ---------------------------------------------------------------------------
# 4. get_memory_health includes stale-embedding counts
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_memory_health_reports_stale_embedding_count():
    now = datetime.now(UTC)
    db = _FakeDB(
        [
            _Result(all_rows=[("fact", 3)]),  # counts by type
            _Result(scalar_one_or_none=now),  # oldest fact
            _Result(scalar_one_or_none=now),  # newest fact
            _Result(scalar=1),  # no_embedding_count
            _Result(scalar=2),  # stale_embedding_count
            _Result(scalar=0),  # retention stale_count
            _Result(all_rows=[]),  # per-scope over-cap counts
        ]
    )

    provider = SimpleNamespace(embed=AsyncMock())
    with patch.object(
        persistent_memory,
        "_resolve_embedding_target",
        new=AsyncMock(return_value=(provider, "current-model", None)),
    ):
        health = await persistent_memory.get_memory_health(
            db,  # type: ignore[arg-type]
            agent_id=AGENT_ID,
        )

    assert health["no_embedding_count"] == 1
    assert health["stale_embedding_count"] == 2
    assert health["embedding_model"] == "current-model"


@pytest.mark.asyncio
async def test_memory_health_degrades_without_provider():
    """When no embedding provider is configured the stale count is 0 and
    no extra count query is issued."""
    now = datetime.now(UTC)
    db = _FakeDB(
        [
            _Result(all_rows=[("fact", 1)]),
            _Result(scalar_one_or_none=now),
            _Result(scalar_one_or_none=now),
            _Result(scalar=0),  # no_embedding_count
            _Result(scalar=0),  # retention stale_count
            _Result(all_rows=[]),  # per-scope over-cap counts
        ]
    )

    with patch.object(
        persistent_memory,
        "_resolve_embedding_target",
        new=AsyncMock(return_value=(None, None, None)),
    ):
        health = await persistent_memory.get_memory_health(
            db,  # type: ignore[arg-type]
            agent_id=AGENT_ID,
        )

    assert health["stale_embedding_count"] == 0
    assert health["embedding_model"] is None


# ---------------------------------------------------------------------------
# 5. Stale clause shape
# ---------------------------------------------------------------------------


def test_stale_embedding_clause_includes_dimension_check_when_configured():
    sql = _compiled(persistent_memory._stale_embedding_clause("m", 1536))
    assert sql.count("embedding_dimensions") >= 2  # IS NULL + != check


def test_stale_embedding_clause_skips_dimension_mismatch_when_unset():
    """``expected_dims`` is None when the admin left the dimension unset
    (model-native size) — entries with the right model must not be
    flagged stale just because their dimension differs from a default."""
    with_dims = _compiled(persistent_memory._stale_embedding_clause("m", 1536))
    without_dims = _compiled(persistent_memory._stale_embedding_clause("m", None))
    assert with_dims.count("embedding_dimensions") > without_dims.count(
        "embedding_dimensions"
    )


# ---------------------------------------------------------------------------
# 6. _resolve_embedding_target probes the model-native dimension
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clear_native_dims_cache():
    persistent_memory._NATIVE_DIMS_CACHE.clear()
    yield
    persistent_memory._NATIVE_DIMS_CACHE.clear()


def _target_patches(provider, configured_dims):
    """Patch the embedding-provider and platform-settings lookups used by
    ``_resolve_embedding_target`` (both are imported inside the function)."""
    ps = SimpleNamespace(embedding_dimensions=configured_dims)
    return (
        patch(
            "app.llm.router.get_embedding_provider",
            new=AsyncMock(return_value=(provider, "probe-model", "pid")),
        ),
        patch(
            "app.api.v1.platform_settings_helpers.get_platform_settings",
            new=AsyncMock(return_value=ps),
        ),
    )


@pytest.mark.asyncio
async def test_resolve_target_probes_native_dims_when_unset():
    """With PlatformSettings dimensions unset, the provider must be probed
    so rows stamped with a previously-configured shortened dimension are
    still flagged stale (otherwise they stay invisible forever)."""
    provider = SimpleNamespace(embed=AsyncMock(return_value=[0.1] * 1536))
    p1, p2 = _target_patches(provider, None)
    with p1, p2:
        _prov, model, dims = await persistent_memory._resolve_embedding_target(
            _FakeDB([])  # type: ignore[arg-type]
        )

    assert model == "probe-model"
    assert dims == 1536
    provider.embed.assert_awaited_once()


@pytest.mark.asyncio
async def test_resolve_target_caches_native_dims_probe():
    """The native-size probe must run once per model, not per call."""
    provider = SimpleNamespace(embed=AsyncMock(return_value=[0.1] * 768))
    p1, p2 = _target_patches(provider, None)
    with p1, p2:
        await persistent_memory._resolve_embedding_target(_FakeDB([]))  # type: ignore[arg-type]
        _prov, _model, dims = await persistent_memory._resolve_embedding_target(
            _FakeDB([])  # type: ignore[arg-type]
        )

    assert dims == 768
    provider.embed.assert_awaited_once()


@pytest.mark.asyncio
async def test_resolve_target_skips_probe_when_dims_configured():
    """An admin-configured dimension must be used as-is — no probe call."""
    provider = SimpleNamespace(embed=AsyncMock())
    p1, p2 = _target_patches(provider, 1024)
    with p1, p2:
        _prov, _model, dims = await persistent_memory._resolve_embedding_target(
            _FakeDB([])  # type: ignore[arg-type]
        )

    assert dims == 1024
    provider.embed.assert_not_called()


@pytest.mark.asyncio
async def test_resolve_target_probe_failure_degrades_to_none_dims():
    """A failing probe must not raise — dims stay None (model-only staleness)."""
    provider = SimpleNamespace(embed=AsyncMock(side_effect=RuntimeError("down")))
    p1, p2 = _target_patches(provider, None)
    with p1, p2:
        _prov, model, dims = await persistent_memory._resolve_embedding_target(
            _FakeDB([])  # type: ignore[arg-type]
        )

    assert model == "probe-model"
    assert dims is None


# ---------------------------------------------------------------------------
# 7. update_memory_entry nulls provenance when the re-embed fails
# ---------------------------------------------------------------------------


def _update_entry_fixtures():
    from app.api.v1.agent_memory import UpdateMemoryRequest

    agent_uuid = uuid.UUID(AGENT_ID)
    user = SimpleNamespace(id=uuid.uuid4(), role="user", is_admin=False)
    # The user owns the agent (``user_id == user.id``) so the per-user
    # memory scope filter (MEM-1, merged from main) grants full visibility.
    agent = SimpleNamespace(
        id=agent_uuid, user_id=user.id, is_company_managed=False, name="A"
    )
    entry = AgentMemoryEntry(
        id=uuid.uuid4(),
        agent_id=agent_uuid,
        content="old content",
        memory_type="fact",
        source="chat",
        embedding=[0.5] * 4,
        embedding_model="old-model",
        embedding_dimensions=4,
    )
    entry.created_at = datetime.now(UTC)
    entry.updated_at = datetime.now(UTC)
    body = UpdateMemoryRequest(content="new content")
    return agent, user, entry, body


@pytest.mark.asyncio
async def test_update_memory_entry_stamps_provenance_on_success():
    from app.api.v1 import agent_memory as api

    agent, user, entry, body = _update_entry_fixtures()
    db = _FakeDB([_Result(scalar_one_or_none=entry)])

    with (
        patch.object(api, "_managed_agent", new=AsyncMock(return_value=agent)),
        patch.object(api, "log_audit", new=AsyncMock()),
        patch(
            "app.services.persistent_memory.embed_text_with_model",
            new=AsyncMock(return_value=([0.2] * 8, "new-model")),
        ),
    ):
        await api.update_memory_entry(
            AGENT_ID, str(entry.id), body, None, user=user, db=db  # type: ignore[arg-type]
        )

    assert entry.content == "new content"
    assert entry.embedding == [0.2] * 8
    assert entry.embedding_model == "new-model"
    assert entry.embedding_dimensions == 8


@pytest.mark.asyncio
async def test_update_memory_entry_nulls_provenance_on_embed_failure():
    """When the re-embed fails after a content change, the OLD vector and
    provenance must be cleared — otherwise semantic search scores the entry
    against stale text and the still-current stamp hides it from the
    re-embed maintenance task."""
    from app.api.v1 import agent_memory as api

    agent, user, entry, body = _update_entry_fixtures()
    db = _FakeDB([_Result(scalar_one_or_none=entry)])

    with (
        patch.object(api, "_managed_agent", new=AsyncMock(return_value=agent)),
        patch.object(api, "log_audit", new=AsyncMock()),
        patch(
            "app.services.persistent_memory.embed_text_with_model",
            new=AsyncMock(return_value=(None, None)),
        ),
    ):
        await api.update_memory_entry(
            AGENT_ID, str(entry.id), body, None, user=user, db=db  # type: ignore[arg-type]
        )

    assert entry.content == "new content"
    assert entry.embedding is None
    assert entry.embedding_model is None
    assert entry.embedding_dimensions is None


@pytest.mark.asyncio
async def test_update_memory_entry_strips_null_bytes():
    """User-supplied content must be sanitized against \\x00 before the flush
    (same asyncpg CharacterNotInRepertoireError class) and the
    re-embed must receive the sanitized text."""
    from app.api.v1 import agent_memory as api
    from app.api.v1.agent_memory import UpdateMemoryRequest

    agent, user, entry, _body = _update_entry_fixtures()
    body = UpdateMemoryRequest(content="new\x00 content")
    db = _FakeDB([_Result(scalar_one_or_none=entry)])

    embed_mock = AsyncMock(return_value=([0.2] * 8, "new-model"))
    with (
        patch.object(api, "_managed_agent", new=AsyncMock(return_value=agent)),
        patch.object(api, "log_audit", new=AsyncMock()),
        patch(
            "app.services.persistent_memory.embed_text_with_model",
            new=embed_mock,
        ),
    ):
        await api.update_memory_entry(
            AGENT_ID, str(entry.id), body, None, user=user, db=db  # type: ignore[arg-type]
        )

    assert entry.content == "new content"
    assert embed_mock.await_args.args[1] == "new content"


# ---------------------------------------------------------------------------
# 8. Empty embedding vectors are treated as failures
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_embed_text_with_model_treats_empty_vector_as_failure():
    """Some providers return ``[]`` instead of raising on failure — stamping
    ``embedding_dimensions=0`` would make the row unsearchable and possibly
    invisible to the stale clause.  The helper must return ``(None, None)``."""
    provider = SimpleNamespace(embed=AsyncMock(return_value=[]))

    embedding, model = await persistent_memory.embed_text_with_model(
        _FakeDB([]),  # type: ignore[arg-type]
        "some text",
        _provider_model=(provider, "empty-model"),
    )

    assert embedding is None
    assert model is None


# ---------------------------------------------------------------------------
# 9. Model-optional providers (empty model string) keep working
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reembed_runs_with_empty_string_model():
    """Model-optional providers (e.g. TEI) resolve to ``model == ""`` while
    embedding calls still work — re-embedding must not be disabled."""
    entries = _stale_entries()[:1]
    db = _FakeDB([_Result(scalars_all=entries)])

    provider = SimpleNamespace(embed=AsyncMock(return_value=[0.3] * 8))
    with patch.object(
        persistent_memory,
        "_resolve_embedding_target",
        new=AsyncMock(return_value=(provider, "", 8)),
    ):
        count = await persistent_memory.reembed_stale_memories(
            db,  # type: ignore[arg-type]
            agent_id=AGENT_ID,
        )

    assert count == 1
    assert entries[0].embedding_dimensions == 8


@pytest.mark.asyncio
async def test_memory_health_counts_stale_with_empty_string_model():
    """An empty model string is a valid embedding target — the stale count
    must still be computed (gate is ``is not None``, not truthiness)."""
    now = datetime.now(UTC)
    db = _FakeDB(
        [
            _Result(all_rows=[("fact", 1)]),  # counts by type
            _Result(scalar_one_or_none=now),  # oldest fact
            _Result(scalar_one_or_none=now),  # newest fact
            _Result(scalar=0),  # no_embedding_count
            _Result(scalar=3),  # stale_embedding_count
            _Result(scalar=0),  # retention stale_count
            _Result(all_rows=[]),  # per-scope over-cap counts
        ]
    )

    provider = SimpleNamespace(embed=AsyncMock())
    with patch.object(
        persistent_memory,
        "_resolve_embedding_target",
        new=AsyncMock(return_value=(provider, "", None)),
    ):
        health = await persistent_memory.get_memory_health(
            db,  # type: ignore[arg-type]
            agent_id=AGENT_ID,
        )

    assert health["stale_embedding_count"] == 3
    assert health["embedding_model"] == ""


# ---------------------------------------------------------------------------
# 10. load_persistent_context relevance query applies the dimension guard
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_load_persistent_context_relevance_filters_on_dimensions():
    """The relevance-ranked injection query (runs on every chat turn) must
    apply the same ``embedding_dimensions`` guard as the other cosine
    searches — otherwise the first chat turn after an embedding model
    change errors out on a pgvector dimension mismatch."""
    db = _FakeDB(
        [
            # Probe: more entries than the limit, so the relevance query runs.
            _Result(scalars_all=[uuid.uuid4() for _ in range(11)]),
            _Result(scalars_all=[]),  # relevance query
            _Result(scalars_all=[]),  # recency top-up
        ]
    )

    with patch.object(
        persistent_memory,
        "embed_text",
        new=AsyncMock(return_value=[0.1] * 4),
    ):
        text = await persistent_memory.load_persistent_context(
            db,  # type: ignore[arg-type]
            agent_id=AGENT_ID,
            query="what currency?",
        )

    assert text == ""
    # Assert on the WHERE clause (the SELECT column list of the full-entity
    # queries always mentions the column, so the full SQL is not selective).
    relevance_where = str(db.queries[1].whereclause)
    assert "embedding_dimensions" in relevance_where, (
        "load_persistent_context's relevance query must filter on "
        "embedding_dimensions to avoid cosine_distance over mismatched sizes"
    )
    # The recency fallback must NOT filter on dimensions — entries without
    # embeddings are still injectable by recency.
    assert "embedding_dimensions" not in str(db.queries[2].whereclause)


# ---------------------------------------------------------------------------
# Smoke import
# ---------------------------------------------------------------------------


def test_model_has_provenance_columns():
    cols = {c.name for c in AgentMemoryEntry.__table__.columns}
    assert "embedding_model" in cols
    assert "embedding_dimensions" in cols
