import uuid
from types import SimpleNamespace

import pytest

from app.services import vector as vector_module
from app.services.hybrid_search import hybrid_search_embeddings
from app.services.vector import search_knowledge_bases


class _FakeScalarResult:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return self

    def all(self):
        return self._rows

    def first(self):
        return self._rows[0] if self._rows else None

    def scalar_one_or_none(self):
        # ``_scoped_statement_timeout`` calls ``SHOW statement_timeout``
        # then ``.scalar_one_or_none()`` to capture the prior value.
        # Returning ``None`` (or an empty list-wrapped row) routes the
        # context manager into the "no safe prior captured" branch,
        # which is what we want for the bulk of these tests: they
        # don't care about the restore-mode and only assert on the
        # search-side queries.
        if not self._rows:
            return None
        first = self._rows[0]
        # Surface a SHOW-style scalar (``str``) if the test fixture
        # supplied one; otherwise the row's repr-as-value (rare).
        return first if isinstance(first, str) else None


class _FakeMappingResult:
    def __init__(self, rows):
        self._rows = rows

    def mappings(self):
        return self

    def all(self):
        return self._rows

    def first(self):
        return self._rows[0] if self._rows else None

    def scalar_one_or_none(self):
        if not self._rows:
            return None
        first = self._rows[0]
        return first if isinstance(first, str) else None


def _is_timeout_stmt(query: str) -> bool:
    """Identify any of the search helpers' bookkeeping queries so tests can
    ignore them when counting/asserting on substantive queries:

    * ``_scoped_statement_timeout`` — ``SHOW`` to capture the prior value,
      ``SET LOCAL`` to apply the cap, ``SET LOCAL ... = DEFAULT`` or
      ``SET LOCAL ... = '<prior>'`` to restore on exit,
    * ``get_pgvector_capabilities`` — the cached ``pg_extension`` version
      probe backing the ANN index casts,
    * ``apply_hnsw_iterative_scan`` — the ``SET LOCAL hnsw.iterative_scan``
      recall guard for filtered ANN searches.

    Centralised here so a future change to the helpers (different wording,
    different value) only needs to update one predicate.
    """
    q = query.lower()
    if "set local statement_timeout" in q:
        return True
    if "set local statement_timeout" in q.replace("'", ""):
        return True
    if "from pg_extension" in q:
        return True
    if "hnsw.iterative_scan" in q:
        return True
    return "show statement_timeout" in q


def _substantive_calls(calls: list[str]) -> list[str]:
    return [c for c in calls if not _is_timeout_stmt(c)]


def _find_set_local_apply_call(calls: list[str]) -> str | None:
    """Return the ``SET LOCAL statement_timeout = '<ms>'`` call that
    applies the search-scoped cap (NOT the restore-call that uses
    ``DEFAULT`` or a duration suffix).  Tests that previously asserted
    on ``calls[0]`` should now find this call explicitly because the
    context manager prepends a ``SHOW`` and appends a restore.
    """
    for call in calls:
        lowered = call.lower()
        if "set local statement_timeout" not in lowered:
            continue
        if "default" in lowered:
            continue
        # The apply call has a plain milliseconds integer (no unit
        # suffix); the restore call (when ``prior_value`` was captured)
        # carries a PG duration suffix like ``5s`` or ``100ms``.
        return call
    return None


@pytest.mark.asyncio
async def test_search_knowledge_bases_retries_with_legacy_null_filters_when_strict_empty():
    calls: list[str] = []
    legacy_hit = SimpleNamespace(id=uuid.uuid4())

    class FakeDB:
        def __init__(self):
            self._responses = [[], [object()], [legacy_hit]]

        async def execute(self, query):
            calls.append(str(query))
            if _is_timeout_stmt(str(query)):
                return _FakeScalarResult([])
            return _FakeScalarResult(self._responses.pop(0))

    kb_id = str(uuid.uuid4())
    db = FakeDB()

    results = await search_knowledge_bases(
        db,
        user_id=None,
        knowledge_base_ids=[kb_id],
        query_embedding=[0.1, 0.2, 0.3],
        top_k=5,
        expected_dimensions=3,
        expected_model="text-embedding-3-large",
    )

    substantive = _substantive_calls(calls)
    assert results == [legacy_hit]
    assert len(substantive) == 3
    assert "embedding_dimensions IS NULL" not in substantive[0]
    assert "embedding_model IS NULL" not in substantive[0]
    assert "SELECT embeddings.id" in substantive[1]
    assert "embedding_dimensions IS NULL" in substantive[2]
    assert "embedding_model IS NULL" in substantive[2]


@pytest.mark.asyncio
async def test_hybrid_search_retries_with_legacy_null_filters_when_strict_empty():
    calls: list[str] = []
    kb_id = str(uuid.uuid4())

    class FakeDB:
        def __init__(self):
            self._responses = [
                [],
                [object()],
                [
                    {
                        "id": uuid.uuid4(),
                        "content": "legacy chunk",
                        "vector_rank": 1,
                        "text_rank": None,
                        "vector_similarity": 0.9,
                        "text_relevance": 0,
                        "rrf_score": 0.01,
                        "source_type": "document",
                        "source_id": "doc-1",
                        "knowledge_base_id": uuid.UUID(kb_id),
                    }
                ],
            ]

        async def execute(self, query, params=None):
            if params and "expected_embedding_model" in params:
                assert params["expected_embedding_model"] == "text-embedding-3-large"
            calls.append(str(query))
            if _is_timeout_stmt(str(query)):
                return _FakeMappingResult([])
            return _FakeMappingResult(self._responses.pop(0))

    results = await hybrid_search_embeddings(
        db=FakeDB(),
        query_text="timeout test",
        query_embedding=[0.1, 0.2, 0.3],
        knowledge_base_ids=[kb_id],
        top_k=5,
        expected_embedding_dimensions=3,
        expected_embedding_model="text-embedding-3-large",
    )

    substantive = _substantive_calls(calls)
    assert len(results) == 1
    assert results[0].content == "legacy chunk"
    assert len(substantive) == 3
    assert "embedding_dimensions IS NULL" not in substantive[0]
    assert "embedding_model IS NULL" not in substantive[0]
    assert "SELECT embeddings.id" in substantive[1]
    assert "embedding_dimensions IS NULL" in substantive[2]
    assert "embedding_model IS NULL" in substantive[2]


@pytest.mark.asyncio
async def test_search_knowledge_bases_does_not_fallback_when_strict_returns_rows():
    calls: list[str] = []
    hit = SimpleNamespace(id=uuid.uuid4())

    class FakeDB:
        async def execute(self, query):
            calls.append(str(query))
            return _FakeScalarResult([hit])

    results = await search_knowledge_bases(
        FakeDB(),
        user_id=None,
        knowledge_base_ids=[str(uuid.uuid4())],
        query_embedding=[0.1, 0.2, 0.3],
        top_k=5,
        expected_dimensions=3,
        expected_model="text-embedding-3-large",
    )

    assert results == [hit]
    assert len(_substantive_calls(calls)) == 1


@pytest.mark.asyncio
async def test_hybrid_search_does_not_fallback_when_strict_returns_rows():
    calls: list[str] = []
    kb_id = str(uuid.uuid4())

    class FakeDB:
        async def execute(self, query, _params=None):
            calls.append(str(query))
            return _FakeMappingResult(
                [
                    {
                        "id": uuid.uuid4(),
                        "content": "strict chunk",
                        "vector_rank": 1,
                        "text_rank": 1,
                        "vector_similarity": 0.9,
                        "text_relevance": 0.2,
                        "rrf_score": 0.02,
                        "source_type": "document",
                        "source_id": "doc-1",
                        "knowledge_base_id": uuid.UUID(kb_id),
                    }
                ]
            )

    results = await hybrid_search_embeddings(
        db=FakeDB(),
        query_text="timeout test",
        query_embedding=[0.1, 0.2, 0.3],
        knowledge_base_ids=[kb_id],
        top_k=5,
        expected_embedding_dimensions=3,
        expected_embedding_model="text-embedding-3-large",
    )

    assert len(results) == 1
    assert results[0].content == "strict chunk"
    assert len(_substantive_calls(calls)) == 1


@pytest.mark.asyncio
async def test_search_knowledge_bases_engages_probe_when_only_derived_dim_filter_active():
    """The legacy-NULL probe must engage whenever there is ANY active
    metadata filter — including the dimension filter derived from
    ``len(query_embedding)`` when the caller omits explicit kwargs.

    Pre-review-fix this test asserted the opposite (1 substantive
    call, fallback skipped).  The bot pointed out — correctly —
    that the strict query always filters
    ``embedding_dimensions == target_dimensions`` derived from
    ``len(query_embedding)``, so legacy rows with NULL dimensions are
    silently dropped unless the probe-and-fallback path can rescue
    them.

    Updated contract: with a non-empty ``query_embedding`` and no
    explicit kwargs, the call sequence is strict → probe.  If the
    probe finds no candidates (as in this test where the DB returns
    empty), the fallback retrieval is skipped — so we expect exactly
    2 substantive queries, not 1.
    """
    calls: list[str] = []

    class FakeDB:
        async def execute(self, query):
            calls.append(str(query))
            return _FakeScalarResult([])

    results = await search_knowledge_bases(
        FakeDB(),
        user_id=None,
        knowledge_base_ids=[str(uuid.uuid4())],
        query_embedding=[0.1, 0.2, 0.3],
        top_k=5,
        expected_dimensions=None,
        expected_model=None,
    )

    assert results == []
    substantive = _substantive_calls(calls)
    assert len(substantive) == 2, (
        f"strict + probe expected when target_dimensions > 0; "
        f"observed {substantive!r}"
    )
    # The probe is the lightweight ``SELECT embeddings.id ...`` query.
    assert "SELECT embeddings.id" in substantive[1]


@pytest.mark.asyncio
async def test_hybrid_search_skips_fallback_without_metadata_filters():
    calls: list[str] = []

    class FakeDB:
        async def execute(self, query, _params=None):
            calls.append(str(query))
            return _FakeMappingResult([])

    results = await hybrid_search_embeddings(
        db=FakeDB(),
        query_text="timeout test",
        query_embedding=[0.1, 0.2, 0.3],
        knowledge_base_ids=[str(uuid.uuid4())],
        top_k=5,
        expected_embedding_dimensions=None,
        expected_embedding_model=None,
    )

    assert results == []
    assert len(_substantive_calls(calls)) == 1


@pytest.mark.asyncio
async def test_search_knowledge_bases_skips_heavy_fallback_when_no_legacy_null_candidates():
    calls: list[str] = []

    class FakeDB:
        def __init__(self):
            self._responses = [[], []]

        async def execute(self, query):
            calls.append(str(query))
            if _is_timeout_stmt(str(query)):
                return _FakeScalarResult([])
            return _FakeScalarResult(self._responses.pop(0))

    results = await search_knowledge_bases(
        FakeDB(),
        user_id=None,
        knowledge_base_ids=[str(uuid.uuid4())],
        query_embedding=[0.1, 0.2, 0.3],
        top_k=5,
        expected_dimensions=3,
        expected_model="text-embedding-3-large",
    )

    substantive = _substantive_calls(calls)
    assert results == []
    assert len(substantive) == 2
    assert "SELECT embeddings.id" in substantive[1]


@pytest.mark.asyncio
async def test_hybrid_search_skips_heavy_fallback_when_no_legacy_null_candidates():
    calls: list[str] = []

    class FakeDB:
        def __init__(self):
            self._responses = [[], []]

        async def execute(self, query, _params=None):
            calls.append(str(query))
            if _is_timeout_stmt(str(query)):
                return _FakeMappingResult([])
            return _FakeMappingResult(self._responses.pop(0))

    results = await hybrid_search_embeddings(
        db=FakeDB(),
        query_text="timeout test",
        query_embedding=[0.1, 0.2, 0.3],
        knowledge_base_ids=[str(uuid.uuid4())],
        top_k=5,
        expected_embedding_dimensions=3,
        expected_embedding_model="text-embedding-3-large",
    )

    substantive = _substantive_calls(calls)
    assert results == []
    assert len(substantive) == 2
    assert "SELECT embeddings.id" in substantive[1]


@pytest.mark.asyncio
async def test_search_knowledge_bases_returns_empty_when_fallback_query_errors():
    substantive_calls = 0

    class FakeDB:
        async def execute(self, query):
            nonlocal substantive_calls
            if _is_timeout_stmt(str(query)):
                return _FakeScalarResult([])
            substantive_calls += 1
            if substantive_calls == 1:
                return _FakeScalarResult([])
            if substantive_calls == 2:
                return _FakeScalarResult([object()])
            raise RuntimeError("statement timeout")

    results = await search_knowledge_bases(
        FakeDB(),
        user_id=None,
        knowledge_base_ids=[str(uuid.uuid4())],
        query_embedding=[0.1, 0.2, 0.3],
        top_k=5,
        expected_dimensions=3,
        expected_model="text-embedding-3-large",
    )

    assert results == []
    assert substantive_calls == 3


@pytest.mark.asyncio
async def test_hybrid_search_returns_empty_when_fallback_query_errors():
    substantive_calls = 0

    class FakeDB:
        async def execute(self, query, _params=None):
            nonlocal substantive_calls
            query_sql = str(query)
            if _is_timeout_stmt(query_sql):
                return _FakeMappingResult([])
            substantive_calls += 1
            if substantive_calls == 1:
                assert "WITH vector_results AS" in query_sql
                return _FakeMappingResult([])
            if substantive_calls == 2:
                assert "SELECT embeddings.id" in query_sql
                return _FakeMappingResult([object()])
            raise RuntimeError("statement timeout")

    results = await hybrid_search_embeddings(
        db=FakeDB(),
        query_text="timeout test",
        query_embedding=[0.1, 0.2, 0.3],
        knowledge_base_ids=[str(uuid.uuid4())],
        top_k=5,
        expected_embedding_dimensions=3,
        expected_embedding_model="text-embedding-3-large",
    )

    assert results == []
    assert substantive_calls == 3


# ---------------------------------------------------------------------------
# SET LOCAL statement_timeout guard — addresses the actual root cause of the
# pool-exhaustion by capping individual searches so
# they cannot consume the full 30 s global timeout.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_search_knowledge_bases_applies_statement_timeout(monkeypatch):
    """A ``SET LOCAL statement_timeout`` apply call must be issued by
    the scoped context manager before any expensive vector query runs,
    so the fast-fail guard is in place.

    The context manager actually emits three DB calls in order
    (``SHOW statement_timeout`` to capture the prior value, then
    ``SET LOCAL statement_timeout = '<ms>'`` — a bare millisecond
    integer with no unit suffix, e.g. ``'5000'`` — to apply the cap,
    then a final restore on exit), so the contract is "an apply call
    MUST appear in the call list before the expensive query", not
    "the apply call MUST be the very first call".
    ``_find_set_local_apply_call`` locates the apply call regardless
    of its position so a future refactor that swaps the probe/apply
    order doesn't accidentally fail this test.
    """
    monkeypatch.setattr(
        vector_module.app_settings, "kb_search_statement_timeout_ms", 5000
    )

    calls: list[str] = []

    class FakeDB:
        async def execute(self, query):
            calls.append(str(query))
            return _FakeScalarResult([SimpleNamespace(id=uuid.uuid4())])

    await search_knowledge_bases(
        FakeDB(),
        user_id=None,
        knowledge_base_ids=[str(uuid.uuid4())],
        query_embedding=[0.1, 0.2, 0.3],
        top_k=5,
    )

    assert calls, "expected at least one DB call"
    apply_call = _find_set_local_apply_call(calls)
    assert apply_call is not None, (
        f"expected a SET LOCAL apply call in {calls!r} — the scoped "
        "context manager should issue one to cap the search"
    )
    assert "5000" in apply_call, (
        f"the apply call {apply_call!r} should carry the configured "
        "kb_search_statement_timeout_ms value"
    )


@pytest.mark.asyncio
async def test_search_knowledge_bases_skips_timeout_when_disabled(monkeypatch):
    """Setting ``kb_search_statement_timeout_ms`` to ``0`` must be a
    no-op — no ``SET LOCAL`` is issued and the search inherits the
    global pool timeout.
    """
    monkeypatch.setattr(
        vector_module.app_settings, "kb_search_statement_timeout_ms", 0
    )

    calls: list[str] = []

    class FakeDB:
        async def execute(self, query):
            calls.append(str(query))
            return _FakeScalarResult([SimpleNamespace(id=uuid.uuid4())])

    await search_knowledge_bases(
        FakeDB(),
        user_id=None,
        knowledge_base_ids=[str(uuid.uuid4())],
        query_embedding=[0.1, 0.2, 0.3],
        top_k=5,
    )

    # Assert specifically on statement_timeout bookkeeping — other
    # bookkeeping (the pgvector capability probe) may still run.
    assert not any("statement_timeout" in c.lower() for c in calls)


@pytest.mark.asyncio
async def test_hybrid_search_applies_statement_timeout(monkeypatch):
    monkeypatch.setattr(
        vector_module.app_settings, "kb_search_statement_timeout_ms", 5000
    )

    calls: list[str] = []

    class FakeDB:
        async def execute(self, query, _params=None):
            calls.append(str(query))
            return _FakeMappingResult([])

    await hybrid_search_embeddings(
        db=FakeDB(),
        query_text="ping",
        query_embedding=[0.1, 0.2, 0.3],
        knowledge_base_ids=[str(uuid.uuid4())],
        top_k=5,
    )

    assert calls, "expected at least one DB call"
    apply_call = _find_set_local_apply_call(calls)
    assert apply_call is not None, (
        f"expected a SET LOCAL apply call in {calls!r}"
    )
    assert "5000" in apply_call


@pytest.mark.asyncio
async def test_scoped_statement_timeout_restores_prior_value(monkeypatch):
    """Regression: the scoped context manager must restore the
    transaction's prior ``statement_timeout`` when it exits so that
    downstream queries in the same FastAPI request session (a single
    DB transaction by default) inherit their pool-default timeout
    instead of our search-specific cap.

    Without this contract, a KB search that fires near the end of a
    request would silently shorten the timeout window for an audit-log
    insert, a metric update, etc. that runs after the search returns.
    """
    monkeypatch.setattr(
        vector_module.app_settings, "kb_search_statement_timeout_ms", 5000
    )

    calls: list[str] = []

    class FakeDB:
        async def execute(self, query):
            sql = str(query)
            calls.append(sql)
            # SHOW returns the prior value as a scalar string per
            # PostgreSQL semantics.
            if "show statement_timeout" in sql.lower():
                return _FakeScalarResult(["30s"])
            return _FakeScalarResult([])

    db = FakeDB()
    async with vector_module._scoped_statement_timeout(db):
        pass

    # Expected call sequence:
    #   1. SHOW statement_timeout (capture prior)
    #   2. SET LOCAL statement_timeout = '5000' (apply cap)
    #   3. SET LOCAL statement_timeout = '30s' (restore on exit)
    assert any("show statement_timeout" in c.lower() for c in calls), (
        f"missing SHOW statement_timeout capture in {calls!r}"
    )
    apply_call = _find_set_local_apply_call(calls)
    assert apply_call is not None and "5000" in apply_call, (
        f"missing SET LOCAL ... = '5000' apply call in {calls!r}"
    )
    restore_calls = [
        c
        for c in calls
        if "set local statement_timeout" in c.lower() and "30s" in c.lower()
    ]
    assert restore_calls, (
        f"the prior value '30s' must be restored on context exit; "
        f"observed {calls!r}"
    )


@pytest.mark.asyncio
async def test_scoped_statement_timeout_falls_back_to_default_when_prior_value_unsafe(
    monkeypatch,
):
    """Regression: if ``SHOW statement_timeout`` returns a value
    outside the safe PG-duration whitelist (defense-in-depth against
    a future GUC change introducing exotic unit suffixes), the
    context manager must restore via ``DEFAULT`` rather than inlining
    the unsafe string back into a ``SET LOCAL`` (which would be a
    potential SQL-injection vector).
    """
    monkeypatch.setattr(
        vector_module.app_settings, "kb_search_statement_timeout_ms", 5000
    )

    calls: list[str] = []

    class FakeDB:
        async def execute(self, query):
            sql = str(query)
            calls.append(sql)
            if "show statement_timeout" in sql.lower():
                # Simulated unsafe / unexpected PG response — must
                # NOT be inlined back into SET LOCAL.
                return _FakeScalarResult(["'; DROP TABLE embeddings; --"])
            return _FakeScalarResult([])

    db = FakeDB()
    async with vector_module._scoped_statement_timeout(db):
        pass

    # No call must inline the malicious string back into the session.
    assert not any("drop table" in c.lower() for c in calls), (
        "the context manager must reject prior values outside the safe "
        "PG-duration whitelist instead of inlining them back into "
        "SET LOCAL"
    )
    # The restore must instead fall back to ``= DEFAULT``.
    assert any(
        "set local statement_timeout" in c.lower() and "default" in c.lower()
        for c in calls
    ), (
        f"unsafe prior value must trigger a DEFAULT restore; "
        f"observed {calls!r}"
    )


@pytest.mark.asyncio
async def test_search_knowledge_bases_legacy_null_fallback_engages_without_explicit_kwargs(
    monkeypatch,
):
    """Regression: a caller that omits ``expected_dimensions`` and
    ``expected_model`` must still get the legacy-NULL fallback path
    when ``target_dimensions = len(query_embedding) > 0``.

    The strict query always filters ``embedding_dimensions ==
    target_dimensions`` (derived from the query embedding length),
    which silently excludes legacy rows where
    ``embedding_dimensions IS NULL``.  Pre-fix, the fallback gate
    required at least one explicit metadata kwarg to be set; a
    caller passing only ``kb_ids + query_embedding`` would never
    recover legacy rows.  The fix broadens the gate to engage
    whenever ANY metadata filter (explicit OR derived) is active.
    """
    monkeypatch.setattr(
        vector_module.app_settings, "kb_search_statement_timeout_ms", 0
    )

    legacy_hit = SimpleNamespace(id=uuid.uuid4())
    calls: list[str] = []

    class FakeDB:
        def __init__(self):
            self._scalar_responses = [
                [],  # strict embedding search returns nothing
                [object()],  # legacy probe returns at least one row
                [legacy_hit],  # legacy fallback search returns the hit
            ]

        async def execute(self, query):
            sql = str(query)
            calls.append(sql)
            if _is_timeout_stmt(sql):
                return _FakeScalarResult([])
            return _FakeScalarResult(self._scalar_responses.pop(0))

    results = await search_knowledge_bases(
        FakeDB(),
        user_id=None,
        knowledge_base_ids=[str(uuid.uuid4())],
        query_embedding=[0.1, 0.2, 0.3],
        top_k=5,
        # NO expected_dimensions / expected_model — pre-fix this
        # would short-circuit and miss legacy NULL rows.
    )

    substantive = _substantive_calls(calls)
    assert results == [legacy_hit], (
        "Legacy NULL-dimension rows must be recoverable via the "
        "fallback path even when the caller omits expected_dimensions "
        "/ expected_model kwargs (target_dimensions is derived from "
        "len(query_embedding))."
    )
    # 3 substantive calls = strict + probe + fallback retrieval.
    assert len(substantive) == 3, (
        f"expected strict + probe + fallback DB roundtrip, got "
        f"{len(substantive)} substantive queries: {substantive!r}"
    )
    assert "embedding_dimensions IS NULL" in substantive[2], (
        "the fallback retrieval query must include the legacy NULL "
        "branch on embedding_dimensions to actually fetch legacy rows."
    )


@pytest.mark.asyncio
async def test_search_knowledge_bases_skips_legacy_fallback_when_target_dimensions_zero(
    monkeypatch,
):
    """The legacy-NULL fallback gate must NOT engage when there is no
    derived metadata filter to compensate for.

    When ``query_embedding`` is empty and the caller passes no
    ``expected_*`` kwargs, ``target_dimensions == 0`` so the strict
    query has NO dimension filter at all — meaning legacy NULL rows
    already match the strict query.  Engaging the fallback in this
    case would only add a wasted probe round-trip.
    """
    monkeypatch.setattr(
        vector_module.app_settings, "kb_search_statement_timeout_ms", 0
    )

    calls: list[str] = []

    class FakeDB:
        async def execute(self, query):
            sql = str(query)
            calls.append(sql)
            if _is_timeout_stmt(sql):
                return _FakeScalarResult([])
            return _FakeScalarResult([])  # strict empty, then nothing else

    await search_knowledge_bases(
        FakeDB(),
        user_id=None,
        knowledge_base_ids=[str(uuid.uuid4())],
        query_embedding=[],  # empty → target_dimensions == 0
        top_k=5,
    )

    substantive = _substantive_calls(calls)
    assert len(substantive) == 1, (
        f"with no metadata filter active (target_dimensions=0 and no "
        f"expected_model), only the strict query should run; got "
        f"{substantive!r}"
    )
