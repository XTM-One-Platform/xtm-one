"""Tests for the pgvector ANN (HNSW) indexes on ``embeddings``.

Assessment finding ARCH-2: every cosine-distance search over ``embeddings``
was an exact O(n) sequential scan because no ANN index existed on any pgvector
column.  The fix is a set of partial expression HNSW indexes per supported
embedding dimension, declared on the model (so ``create_all`` builds them on
fresh installs) and built post-startup by the background builder on existing
installs, plus query-side casts so the planner can actually match the
expression indexes.

HNSW builds take minutes on populated tables, so they
must NEVER run inside the blocking startup path (migration body or boot
reconciler) — that starved the Kubernetes liveness probe and crash-looped the
container.  ``app/core/ann_index_builder.py`` owns the builds instead.

These tests pin:
  * the four index declarations on the ``Embedding`` model (names, hnsw access
    method, opclass, partial predicate) and their compiled DDL,
  * that the reconciler matches them by name only (expression + partial
    indexes must never be column-set guarded),
  * that the migration body and the boot reconciler DEFER the builds while the downgrade still drops the indexes,
  * the background builder's safety properties (advisory lock on the build
    connection, statement_timeout handling, IF NOT EXISTS, invalid-leftover
    drop, halfvec guard, duplicate-key tolerance, never crashing the app),
  * the query-side ``ann_cosine_distance`` cast contract.
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import DatabaseError
from sqlalchemy.schema import CreateIndex

from app.core import ann_index_builder, index_maintenance
from app.models.embedding import ALLOWED_EMBEDDING_DIMENSIONS, Embedding
from app.services.vector import (
    ANN_HALFVEC_DIMENSIONS,
    ANN_VECTOR_DIMENSIONS,
    ann_cosine_distance,
)

_EXPECTED_INDEX_NAMES = {
    f"ix_embeddings_embedding_hnsw_{dim}"
    for dim in ALLOWED_EMBEDDING_DIMENSIONS
}

_PG_DIALECT = postgresql.dialect()


def _hnsw_model_indexes() -> dict[str, object]:
    return {
        ix.name: ix
        for ix in Embedding.__table__.indexes
        if ix.name in _EXPECTED_INDEX_NAMES
    }


# ---------------------------------------------------------------------------
# Model declarations — fresh installs get the indexes via create_all and the
# boot reconciler reads them from Base.metadata.
# ---------------------------------------------------------------------------


def test_model_declares_one_hnsw_index_per_supported_dimension():
    """Every dimension in ``ALLOWED_EMBEDDING_DIMENSIONS`` must have a
    model-declared HNSW index — that is the registration the boot-time
    reconciler (``index_maintenance.reconcile_model_indexes``) and fresh
    installs (``create_all``) rely on."""
    declared = _hnsw_model_indexes()
    assert set(declared) == _EXPECTED_INDEX_NAMES, (
        f"expected HNSW indexes {sorted(_EXPECTED_INDEX_NAMES)} declared on "
        f"the Embedding model; got {sorted(declared)}"
    )
    for ix in declared.values():
        assert ix.dialect_options["postgresql"]["using"] == "hnsw"


def test_model_hnsw_ddl_compiles_with_cast_opclass_and_predicate():
    """The compiled DDL (what create_all and the reconciler execute) must use
    the fixed-dimension cast, the cosine opclass, and the partial predicate.
    3072 dims exceed pgvector's 2000-dim ``vector`` opclass limit so that
    index must go through ``halfvec``."""
    declared = _hnsw_model_indexes()
    for dim in ALLOWED_EMBEDDING_DIMENSIONS:
        ddl = str(
            CreateIndex(
                declared[f"ix_embeddings_embedding_hnsw_{dim}"]
            ).compile(dialect=_PG_DIALECT)
        )
        assert "USING hnsw" in ddl, ddl
        assert f"WHERE embedding_dimensions = {dim}" in ddl, ddl
        if dim in ANN_HALFVEC_DIMENSIONS:
            assert f"(embedding::halfvec({dim})) halfvec_cosine_ops" in ddl, ddl
        else:
            assert dim in ANN_VECTOR_DIMENSIONS
            assert f"(embedding::vector({dim})) vector_cosine_ops" in ddl, ddl


def test_reconciler_matches_hnsw_indexes_by_name_only():
    """Expression + partial indexes must yield no plain column signature, so
    the reconciler's column-set coverage guard can never skip creating them
    because some other index happens to touch the same columns."""
    for ix in _hnsw_model_indexes().values():
        assert index_maintenance._plain_column_signature(ix) is None, (
            f"{ix.name} must be matched by name only, not by column set"
        )


def test_reconciler_compiles_hnsw_ddl_to_idempotent_form():
    """The boot reconciler turns the compiled DDL into the idempotent
    ``CREATE INDEX IF NOT EXISTS`` form — pin that the regex rewrite works on
    the HNSW expression indexes too."""
    for ix in _hnsw_model_indexes().values():
        ddl = str(CreateIndex(ix).compile(dialect=_PG_DIALECT)).strip()
        idempotent = index_maintenance.idempotent_if_not_exists(ddl)
        assert idempotent.startswith("CREATE INDEX IF NOT EXISTS "), idempotent
        assert "CONCURRENTLY" not in idempotent


# ---------------------------------------------------------------------------
# Startup path — the boot reconciler must DEFER the HNSW builds:
# an HNSW build on a populated table takes minutes and the startup path runs
# BEFORE uvicorn accepts connections, so a synchronous build starves the
# liveness probe and crash-loops the container.
# ---------------------------------------------------------------------------


def test_hnsw_indexes_are_classified_as_deferred():
    """All four model HNSW indexes must be deferred; ordinary btree / GIN
    indexes must NOT be (they stay on the boot reconciler)."""
    deferred_names = {
        ix.name for ix in index_maintenance.deferred_model_indexes()
    }
    assert deferred_names >= _EXPECTED_INDEX_NAMES

    by_name = {ix.name: ix for ix in Embedding.__table__.indexes}
    assert not index_maintenance.is_deferred_index(
        by_name["ix_embeddings_kb_dim"]
    ), "plain btree indexes must stay on the boot reconciler"
    assert not index_maintenance.is_deferred_index(
        by_name["ix_embeddings_content_fts"]
    ), "GIN indexes must stay on the boot reconciler"


def test_boot_reconciler_never_builds_hnsw_indexes():
    """``reconcile_model_indexes`` runs inside the blocking startup path, so
    it must never emit HNSW DDL even when every index is missing."""
    import app.models  # noqa: F401  (register all models on Base.metadata)

    class _RecordingConn:
        def __init__(self):
            self.ddl: list[str] = []

        def execute(self, stmt, *_a, **_k):
            sql = str(stmt)
            if "array_agg" in sql or "indisvalid" in sql or "relkind" in sql:
                return []  # catalog probes: empty DB, everything missing
            self.ddl.append(sql)
            return []

    conn = _RecordingConn()
    index_maintenance.reconcile_model_indexes(conn)
    assert conn.ddl, "expected the reconciler to create the non-deferred indexes"
    hnsw_ddl = [s for s in conn.ddl if "hnsw" in s.lower()]
    assert not hnsw_ddl, (
        f"boot reconciler must never build HNSW indexes in the startup "
        f"path; got {hnsw_ddl}"
    )


# ---------------------------------------------------------------------------
# Background builder — the post-startup half of the fix.
# ---------------------------------------------------------------------------


class _FakeBuilderConn:
    """Sync-Connection stub for ``_build_ann_indexes_sync``.

    Routes the catalog probes to canned rows and records DDL.  ``fail_with``
    maps an index name to an exception its CREATE statement should raise.
    """

    def __init__(
        self,
        *,
        halfvec_available=True,
        existing_names=frozenset(),
        invalid_names=frozenset(),
        fail_with=None,
    ):
        self._halfvec = halfvec_available
        self._existing = set(existing_names)
        self._invalid = set(invalid_names)
        self._fail_with = fail_with or {}
        self.ddl: list[str] = []

    def execute(self, stmt, *_a, **_k):
        sql = str(stmt)
        if "pg_type" in sql:  # halfvec probe
            probe = MagicMock()
            probe.first.return_value = (1,) if self._halfvec else None
            return probe
        if "NOT i.indisvalid" in sql:
            return [(n,) for n in self._invalid]
        if "relkind = 'i'" in sql:
            return [(n,) for n in self._existing]
        # DDL statement.
        for name, exc in self._fail_with.items():
            if name in sql and sql.startswith("CREATE"):
                raise exc
        self.ddl.append(sql)
        return []


def test_builder_creates_all_missing_indexes_in_place():
    conn = _FakeBuilderConn()
    created = ann_index_builder._build_ann_indexes_sync(conn)
    assert set(created) == _EXPECTED_INDEX_NAMES
    creates = [s for s in conn.ddl if s.startswith("CREATE INDEX IF NOT EXISTS")]
    assert len(creates) == len(_EXPECTED_INDEX_NAMES)
    # Plain in-place builds — never CONCURRENTLY (hangs behind another live
    # pod's idle-in-transaction sessions during a rolling deploy).
    assert not any("CONCURRENTLY" in s for s in conn.ddl)


def test_builder_skips_existing_valid_indexes():
    conn = _FakeBuilderConn(existing_names=_EXPECTED_INDEX_NAMES)
    created = ann_index_builder._build_ann_indexes_sync(conn)
    assert created == []
    assert not conn.ddl


def test_builder_drops_invalid_leftover_before_recreating():
    """CREATE ... IF NOT EXISTS checks existence, not validity — an INVALID
    leftover from an interrupted build must be dropped first."""
    name = "ix_embeddings_embedding_hnsw_1536"
    conn = _FakeBuilderConn(existing_names={name}, invalid_names={name})
    created = ann_index_builder._build_ann_indexes_sync(conn)
    assert name in created
    drops = [s for s in conn.ddl if s.startswith("DROP INDEX IF EXISTS")]
    creates = [s for s in conn.ddl if name in s and s.startswith("CREATE")]
    assert drops and creates
    assert conn.ddl.index(drops[0]) < conn.ddl.index(creates[0])


def test_builder_skips_halfvec_index_when_type_unavailable():
    conn = _FakeBuilderConn(halfvec_available=False)
    created = ann_index_builder._build_ann_indexes_sync(conn)
    assert "ix_embeddings_embedding_hnsw_3072" not in created
    assert set(created) == _EXPECTED_INDEX_NAMES - {
        "ix_embeddings_embedding_hnsw_3072"
    }


def test_builder_tolerates_duplicate_key_from_concurrent_build():
    """The production crash loop ended in ``23505 duplicate key value
    violates unique constraint "pg_class_relname_nsp_index"`` when a
    restarted pod raced an orphaned build from the killed pod.  The builder
    must treat that as 'someone else built it' and continue."""

    class _DupError(Exception):
        pgcode = "23505"

    name = "ix_embeddings_embedding_hnsw_768"
    conn = _FakeBuilderConn(
        fail_with={
            name: _DupError(
                "duplicate key value violates unique constraint "
                '"pg_class_relname_nsp_index"'
            )
        }
    )
    created = ann_index_builder._build_ann_indexes_sync(conn)
    assert name not in created
    assert set(created) == _EXPECTED_INDEX_NAMES - {name}


def test_duplicate_index_error_detection():
    """The shared duplicate detector must walk the exception chain for the
    driver SQLSTATE (asyncpg ``sqlstate`` / DBAPI ``pgcode``) and fall back
    to a string probe — see ``index_maintenance.is_duplicate_index_error``."""

    class _SqlstateError(Exception):
        sqlstate = "42P07"

    class _WrappedError(Exception):
        pass

    wrapped = _WrappedError("boom")
    wrapped.__cause__ = _SqlstateError()
    assert index_maintenance.is_duplicate_index_error(_SqlstateError())
    assert index_maintenance.is_duplicate_index_error(wrapped)
    assert index_maintenance.is_duplicate_index_error(
        Exception(
            'duplicate key value violates unique constraint '
            '"pg_class_relname_nsp_index"'
        )
    )
    assert not index_maintenance.is_duplicate_index_error(
        Exception("connection refused")
    )


def _fake_async_engine(
    *,
    try_lock_results,
    run_sync_result=None,
    run_sync_raises=None,
    cleanup_raises=None,
):
    """Async-engine stub for ``_try_build_once`` / ``build_ann_indexes``.

    The advisory lock, the timeout statements, and ``run_sync`` all happen
    on the SAME connection object — mirroring the real safety contract that
    the lock lives on the backend executing the builds.  ``cleanup_raises``
    makes the post-build ``RESET statement_timeout`` raise, simulating a
    cancellation delivered at the first cleanup await (or a broken
    connection).  The fake connection exposes the
    ``sync_connection.connection.dbapi_connection.driver_connection``
    chain so ``_terminate_build_connection`` can be asserted against.
    """
    lock_results = list(try_lock_results)
    executed: list[str] = []
    run_sync_calls: list[object] = []
    driver = MagicMock()  # exposes .terminate()

    class _Result:
        def __init__(self, value):
            self._value = value

        def scalar(self):
            return self._value

    class _AutocommitConn:
        async def execute(self, stmt, *_a, **_k):
            sql = str(stmt)
            executed.append(sql)
            if "pg_try_advisory_lock" in sql:
                return _Result(lock_results.pop(0))
            if cleanup_raises is not None and "RESET statement_timeout" in sql:
                raise cleanup_raises
            return _Result(None)

        async def run_sync(self, fn, *_a, **_k):
            run_sync_calls.append(fn)
            if run_sync_raises is not None:
                raise run_sync_raises
            return run_sync_result if run_sync_result is not None else []

    class _Inner:
        def __init__(self):
            self._autocommit = _AutocommitConn()
            self.sync_connection = MagicMock()
            self.sync_connection.connection.dbapi_connection.driver_connection = (
                driver
            )

        async def execution_options(self, **_kwargs):
            return self._autocommit

    class _ConnectCM:
        async def __aenter__(self):
            return _Inner()

        async def __aexit__(self, *_a):
            return False

    engine = MagicMock()
    engine.dialect.name = "postgresql"
    engine.connect = lambda: _ConnectCM()
    return engine, executed, run_sync_calls, driver


@pytest.mark.asyncio
async def test_try_build_once_locks_builds_and_releases():
    engine, executed, run_sync_calls, driver = _fake_async_engine(
        try_lock_results=[True], run_sync_result=["ix_x"]
    )
    done = await ann_index_builder._try_build_once(engine)
    assert done is True
    assert run_sync_calls, "the sync builder must run after the lock"
    joined = "\n".join(executed)
    assert "pg_try_advisory_lock" in joined
    assert "SET statement_timeout = 0" in joined
    # The pooled connection must get its 30 s default back, and the lock
    # must be released.
    assert "RESET statement_timeout" in joined
    assert "pg_advisory_unlock" in joined
    # Lock acquired BEFORE the timeout lift and the builds.
    assert executed.index(
        next(s for s in executed if "pg_try_advisory_lock" in s)
    ) < executed.index(next(s for s in executed if "statement_timeout = 0" in s))
    # Graceful cleanup ran — the connection must NOT be terminated.
    driver.terminate.assert_not_called()


@pytest.mark.asyncio
async def test_try_build_once_backs_off_when_lock_held_elsewhere():
    """When another instance (or an orphaned backend from a killed pod)
    holds the build lock, the attempt must return False WITHOUT building —
    this is what prevents the duplicate-build 23505 race."""
    engine, executed, run_sync_calls, _driver = _fake_async_engine(
        try_lock_results=[False]
    )
    done = await ann_index_builder._try_build_once(engine)
    assert done is False
    assert not run_sync_calls
    assert not any("SET statement_timeout = 0" in s for s in executed)


@pytest.mark.asyncio
async def test_try_build_once_releases_lock_on_build_failure():
    engine, executed, _, driver = _fake_async_engine(
        try_lock_results=[True], run_sync_raises=RuntimeError("build blew up")
    )
    with pytest.raises(RuntimeError):
        await ann_index_builder._try_build_once(engine)
    joined = "\n".join(executed)
    assert "pg_advisory_unlock" in joined
    assert "RESET statement_timeout" in joined
    # Cleanup succeeded normally — no need to terminate the connection.
    driver.terminate.assert_not_called()


@pytest.mark.asyncio
async def test_try_build_once_terminates_connection_when_cleanup_cancelled():
    """Cancellation-resilient cleanup: when the build task is cancelled
    during shutdown, the ``RESET statement_timeout`` / ``pg_advisory_unlock``
    cleanup awaits can themselves be interrupted by the pending
    ``CancelledError``.  The connection must then be synchronously
    TERMINATED (so the lifted timeout and the held advisory lock die with
    the session instead of leaking into the pool) and the original
    cancellation must still propagate to the caller."""
    engine, executed, _, driver = _fake_async_engine(
        try_lock_results=[True],
        run_sync_raises=asyncio.CancelledError(),
        cleanup_raises=asyncio.CancelledError(),
    )
    with pytest.raises(asyncio.CancelledError):
        await ann_index_builder._try_build_once(engine)
    driver.terminate.assert_called_once()
    # The unlock never ran (cleanup died at RESET) — termination is what
    # releases the session state instead.
    assert not any("pg_advisory_unlock" in s for s in executed)


@pytest.mark.asyncio
async def test_try_build_once_cancellation_survives_cleanup_failure():
    """When the build is cancelled and the cleanup then fails with an
    ORDINARY exception (broken connection), the caller must still see the
    original ``CancelledError`` — otherwise ``build_ann_indexes`` would
    swallow shutdown as a generic build failure — and the connection must
    be terminated."""
    engine, _, _, driver = _fake_async_engine(
        try_lock_results=[True],
        run_sync_raises=asyncio.CancelledError(),
        cleanup_raises=RuntimeError("connection is closed"),
    )
    with pytest.raises(asyncio.CancelledError):
        await ann_index_builder._try_build_once(engine)
    driver.terminate.assert_called_once()


@pytest.mark.asyncio
async def test_try_build_once_build_error_wins_over_cleanup_failure():
    """When the build fails with an ordinary error AND the cleanup then also
    fails (broken connection), the caller must see the ORIGINAL build error
    — the actionable root cause — not the secondary cleanup failure.  The
    cleanup failure stays chained as ``__cause__`` and the connection must
    be terminated."""
    engine, _, _, driver = _fake_async_engine(
        try_lock_results=[True],
        run_sync_raises=RuntimeError("build blew up"),
        cleanup_raises=RuntimeError("connection is closed"),
    )
    with pytest.raises(RuntimeError, match="build blew up") as excinfo:
        await ann_index_builder._try_build_once(engine)
    assert isinstance(excinfo.value.__cause__, RuntimeError)
    assert "connection is closed" in str(excinfo.value.__cause__)
    driver.terminate.assert_called_once()


@pytest.mark.asyncio
async def test_try_build_once_cancelled_cleanup_wins_over_build_failure():
    """When a plain build failure is followed by a ``CancelledError``
    delivered DURING the cleanup (shutdown raced the failure), cancellation
    must win — propagating the build error instead would let the task
    swallow its own cancellation — and the connection must be terminated."""
    engine, _, _, driver = _fake_async_engine(
        try_lock_results=[True],
        run_sync_raises=RuntimeError("build blew up"),
        cleanup_raises=asyncio.CancelledError(),
    )
    with pytest.raises(asyncio.CancelledError):
        await ann_index_builder._try_build_once(engine)
    driver.terminate.assert_called_once()


@pytest.mark.asyncio
async def test_try_build_once_terminates_connection_when_only_cleanup_fails():
    """A successful build whose cleanup fails must terminate the connection
    (never return a session with statement_timeout=0 / a held lock to the
    pool) and propagate the cleanup error."""
    engine, _, _, driver = _fake_async_engine(
        try_lock_results=[True],
        run_sync_result=[],
        cleanup_raises=RuntimeError("connection is closed"),
    )
    with pytest.raises(RuntimeError):
        await ann_index_builder._try_build_once(engine)
    driver.terminate.assert_called_once()


@pytest.mark.asyncio
async def test_build_ann_indexes_never_raises_on_failure(monkeypatch, caplog):
    """A failed background build must degrade (exact-scan search) — it must
    NEVER propagate and crash the app."""
    engine, _, _, _driver = _fake_async_engine(
        try_lock_results=[True], run_sync_raises=RuntimeError("build blew up")
    )
    import app.core.database as db_module

    monkeypatch.setattr(db_module, "engine", engine)

    with caplog.at_level("ERROR"):
        await ann_index_builder.build_ann_indexes()

    assert "Background ANN index build failed" in caplog.text


@pytest.mark.asyncio
async def test_build_ann_indexes_skips_non_postgres(monkeypatch):
    engine = MagicMock()
    engine.dialect.name = "sqlite"
    connect_calls: list[int] = []
    engine.connect = lambda: connect_calls.append(1)
    import app.core.database as db_module

    monkeypatch.setattr(db_module, "engine", engine)

    await ann_index_builder.build_ann_indexes()
    assert not connect_calls


# ---------------------------------------------------------------------------
# Query-side cast contract — the planner only uses an expression index when
# the ORDER BY expression matches the indexed expression.
# ---------------------------------------------------------------------------


def _compiled(expr) -> str:
    return str(expr.compile(dialect=_PG_DIALECT))


def test_ann_cosine_distance_casts_vector_dimensions():
    for dim in ANN_VECTOR_DIMENSIONS:
        sql = _compiled(ann_cosine_distance([0.1] * dim, dim))
        assert f"CAST(embeddings.embedding AS VECTOR({dim}))" in sql, sql
        assert "<=>" in sql


def test_ann_cosine_distance_uses_halfvec_only_when_available():
    (dim,) = ANN_HALFVEC_DIMENSIONS
    with_halfvec = _compiled(
        ann_cosine_distance([0.1] * dim, dim, halfvec_ok=True)
    )
    assert f"CAST(embeddings.embedding AS HALFVEC({dim}))" in with_halfvec
    # Without halfvec (pgvector < 0.7) the cast would error server-side, so
    # the expression must stay on the plain exact-scan path.
    without_halfvec = _compiled(
        ann_cosine_distance([0.1] * dim, dim, halfvec_ok=False)
    )
    assert "HALFVEC" not in without_halfvec
    assert "CAST" not in without_halfvec


def test_ann_cosine_distance_falls_back_to_plain_distance():
    """Unindexed dimensions (and the no-metadata path) must keep the exact
    pre-index behaviour: a plain uncast ``embedding <=> $1``."""
    for dims in (None, 384, 2000):
        sql = _compiled(ann_cosine_distance([0.1] * 8, dims))
        assert "CAST" not in sql, sql
        assert "<=>" in sql


def test_supported_dimensions_stay_in_sync_with_model():
    """``ALLOWED_EMBEDDING_DIMENSIONS`` (the dimensions the Settings UI
    offers) must exactly equal the ANN-indexed dimensions — adding a new
    supported dimension without its HNSW index would silently reintroduce
    seq-scan searches for that dimension."""
    assert set(ALLOWED_EMBEDDING_DIMENSIONS) == set(ANN_VECTOR_DIMENSIONS) | set(
        ANN_HALFVEC_DIMENSIONS
    )


# ---------------------------------------------------------------------------
# 23505 catalog-duplicate race guard — migration + boot reconciler
#
# When two pods execute ``CREATE INDEX IF NOT EXISTS <name>`` simultaneously
# (both pass the pg_class existence check before either commits), the second
# pod gets ``ERROR 23505`` on ``pg_class_relname_nsp_index``.  Both the
# migration and the boot-time reconciler must catch this and treat it as an
# idempotent success (the index already exists or is being built).
# ---------------------------------------------------------------------------


def _make_23505_exc(include_pg_class: bool = True) -> DatabaseError:
    """Build a fake SQLAlchemy DatabaseError that mimics psycopg2's 23505."""
    orig = MagicMock()
    orig.pgcode = "23505"
    orig.__str__ = lambda self: (
        'duplicate key value violates unique constraint "pg_class_relname_nsp_index"'
        if include_pg_class
        else 'duplicate key value violates unique constraint "some_other_constraint"'
    )
    exc = DatabaseError("statement", {}, orig)
    return exc


def test_is_duplicate_index_error_detects_23505_pg_class():
    """``is_duplicate_index_error`` returns True for a 23505 on pg_class."""
    exc = _make_23505_exc(include_pg_class=True)
    assert index_maintenance.is_duplicate_index_error(exc)


def test_is_duplicate_index_error_false_for_other_23505():
    """A 23505 that does NOT mention pg_class is a genuine data error (e.g.
    ``CREATE UNIQUE INDEX`` over duplicate rows) and must propagate."""
    exc = _make_23505_exc(include_pg_class=False)
    assert not index_maintenance.is_duplicate_index_error(exc)


def test_is_duplicate_index_error_false_for_other_exception():
    """``is_duplicate_index_error`` returns False for a non-23505 exception."""
    assert not index_maintenance.is_duplicate_index_error(ValueError("unrelated"))


def test_reconciler_skips_index_on_23505_catalog_duplicate(caplog):
    """``reconcile_indexes`` must treat a 23505 catalog-duplicate as a no-op.

    If ``conn.execute(CREATE INDEX IF NOT EXISTS …)`` raises the catalog-race
    error, the reconciler must log a warning and continue — it must NOT
    propagate the exception (which would crash startup and create a crash loop).
    """
    import logging

    ix = next(
        ix
        for ix in Embedding.__table__.indexes
        if ix.name == "ix_embeddings_embedding_hnsw_768"
    )

    # Simulate an existing names scan that does NOT include this index so the
    # reconciler tries to create it.
    mock_conn = MagicMock()
    mock_conn.execute.side_effect = [
        # _existing_index_names
        MagicMock(__iter__=lambda s: iter([])),
        # _invalid_index_names
        MagicMock(__iter__=lambda s: iter([])),
        # _existing_column_signatures
        MagicMock(__iter__=lambda s: iter([])),
        # The actual CREATE INDEX — raises the catalog-duplicate 23505
        _make_23505_exc(include_pg_class=True),
    ]

    with caplog.at_level(logging.WARNING, logger="app.core.index_maintenance"):
        created = index_maintenance.reconcile_indexes(mock_conn, [ix])

    # The index was created by the OTHER pod — it must NOT be reported as
    # created by this reconciliation pass.
    assert created == []
    # A warning must be logged so operators can see the concurrent race.
    assert any(
        "concurrent-build duplicate" in r.getMessage() for r in caplog.records
    )


def test_reconciler_propagates_non_23505_error():
    """``reconcile_indexes`` must still propagate non-catalog-race errors."""

    ix = next(
        ix
        for ix in Embedding.__table__.indexes
        if ix.name == "ix_embeddings_embedding_hnsw_768"
    )

    mock_conn = MagicMock()
    generic_exc = DatabaseError("stmt", {}, Exception("disk full"))
    mock_conn.execute.side_effect = [
        MagicMock(__iter__=lambda s: iter([])),
        MagicMock(__iter__=lambda s: iter([])),
        MagicMock(__iter__=lambda s: iter([])),
        generic_exc,
    ]

    import pytest

    with pytest.raises(DatabaseError):
        index_maintenance.reconcile_indexes(mock_conn, [ix])
