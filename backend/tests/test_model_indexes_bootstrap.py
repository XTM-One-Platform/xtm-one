"""Tests for the model-index reconciler + its boot wiring.

Fresh installs run ``Base.metadata.create_all`` + ``alembic stamp head`` and
NEVER execute migration bodies, so any index that lived only in a migration body
was silently absent on fresh / stamped-fresh installs. ``_ensure_model_indexes``
(backed by ``index_maintenance.reconcile_indexes``) is the boot-time safety net
that reconciles the model's index set against the live DB on every boot.

These tests pin:
  * the column-set coverage guard only counts VALID indexes (an INVALID leftover
    must NOT block creating the canonical model index),
  * ``reconcile_indexes`` create / skip / invalid-recreate behaviour,
  * the boot wiring (``run_migrations`` calls it) and the AUTOCOMMIT +
    ``statement_timeout = 0`` connection contract, including that a failure
    halts startup loudly.
"""

from __future__ import annotations

import inspect
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import Column, Integer, MetaData, String, Table
from sqlalchemy.schema import Index

from app.core import index_maintenance, migrations

# ---------------------------------------------------------------------------
# SQL contract — the coverage query must exclude INVALID indexes.
# ---------------------------------------------------------------------------


def test_column_signature_query_filters_invalid_indexes():
    """``_existing_column_signatures`` MUST filter on ``ix.indisvalid``.

    A failed ``CREATE INDEX CONCURRENTLY`` leaves an INVALID index in the
    catalog. If the coverage query counted it, the reconciler would treat the
    column set as already covered and skip creating the canonical (valid) model
    index — leaving the table with NO usable index while reporting success. Pin
    the filter in the boot reconciler so the guard cannot silently regress."""
    src = inspect.getsource(index_maintenance._existing_column_signatures)
    assert "ix.indisvalid" in src, (
        "reconciler coverage query must exclude INVALID indexes via "
        "ix.indisvalid; got:\n" + src
    )


# ---------------------------------------------------------------------------
# reconcile_indexes — create / skip / invalid-recreate behaviour.
# ---------------------------------------------------------------------------


def _index(name: str, table: str, *cols: str) -> Index:
    md = MetaData()
    columns = [Column(c, String if c != "id" else Integer) for c in cols]
    tbl = Table(table, md, *columns)
    return Index(name, *[tbl.c[c] for c in cols])


class _FakeReconcileConn:
    """Sync-Connection stub for ``reconcile_indexes``.

    Routes the three catalog SELECTs to canned rows by SQL fingerprint and
    records every DDL statement executed so tests can assert create / drop.
    """

    def __init__(self, *, existing_names, invalid_names, signatures):
        self._existing = existing_names
        self._invalid = invalid_names
        self._signatures = signatures
        self.ddl: list[str] = []

    def execute(self, stmt, *_a, **_k):
        sql = str(stmt)
        if "array_agg" in sql:  # _existing_column_signatures
            # (table, index_name, is_unique, cols) — legacy coverage indexes in
            # these tests are non-unique, matching the non-unique model indexes.
            return [(t, "_n", False, list(cols)) for (t, cols) in self._signatures]
        if "NOT i.indisvalid" in sql:  # _invalid_index_names
            return [(n,) for n in self._invalid]
        if "relkind = 'i'" in sql:  # _existing_index_names
            return [(n,) for n in self._existing]
        # A DDL statement (CREATE / DROP INDEX ...).
        self.ddl.append(sql)
        return []


def test_reconcile_creates_missing_index_with_no_coverage():
    conn = _FakeReconcileConn(existing_names=set(), invalid_names=set(), signatures=set())
    idx = _index("ix_assignment_runs_status", "assignment_runs", "status")
    created = index_maintenance.reconcile_indexes(conn, [idx])
    assert created == ["assignment_runs.ix_assignment_runs_status"]
    assert any("CREATE INDEX IF NOT EXISTS" in s for s in conn.ddl)
    assert not any("CONCURRENTLY" in s for s in conn.ddl)


def test_reconcile_skips_when_covered_by_valid_legacy_index():
    # A VALID legacy-named index already covers (assignment_runs, status).
    conn = _FakeReconcileConn(
        existing_names={"ix_workflow_runs_status"},
        invalid_names=set(),
        signatures={("assignment_runs", ("status",))},
    )
    idx = _index("ix_assignment_runs_status", "assignment_runs", "status")
    created = index_maintenance.reconcile_indexes(conn, [idx])
    assert created == []  # no duplicate
    assert not conn.ddl


def test_reconcile_creates_when_legacy_coverage_is_invalid():
    # The legacy index exists but is INVALID, so (post-fix) it contributes NO
    # signature. The canonical model index must therefore be created — not
    # skipped — so the table ends up with a usable index.
    conn = _FakeReconcileConn(
        existing_names={"ix_workflow_runs_status"},
        invalid_names={"ix_workflow_runs_status"},
        signatures=set(),  # invalid index filtered out of coverage
    )
    idx = _index("ix_assignment_runs_status", "assignment_runs", "status")
    created = index_maintenance.reconcile_indexes(conn, [idx])
    assert created == ["assignment_runs.ix_assignment_runs_status"]
    assert any("CREATE INDEX IF NOT EXISTS" in s for s in conn.ddl)
    assert not any("CONCURRENTLY" in s for s in conn.ddl)


def test_reconcile_drops_then_recreates_invalid_canonical_index():
    # The canonical-named index itself is INVALID → must be dropped in place
    # before recreating (CREATE ... IF NOT EXISTS only checks existence).
    conn = _FakeReconcileConn(
        existing_names={"ix_assignment_runs_status"},
        invalid_names={"ix_assignment_runs_status"},
        signatures=set(),
    )
    idx = _index("ix_assignment_runs_status", "assignment_runs", "status")
    created = index_maintenance.reconcile_indexes(conn, [idx])
    assert created == ["assignment_runs.ix_assignment_runs_status"]
    drops = [s for s in conn.ddl if "DROP INDEX IF EXISTS" in s]
    creates = [s for s in conn.ddl if "CREATE INDEX IF NOT EXISTS" in s]
    assert drops and creates, f"expected a DROP then CREATE; got {conn.ddl}"
    assert conn.ddl.index(drops[0]) < conn.ddl.index(creates[0])
    # Never the concurrent form — it hangs behind another live pod on deploy.
    assert not any("CONCURRENTLY" in s for s in conn.ddl)


def test_reconcile_drops_invalid_canonical_but_skips_create_when_valid_legacy_covers():
    # An interrupted build left the canonical-named index INVALID, AND a VALID
    # legacy-named index already covers the same (table, columns). The reconciler
    # must DROP the unusable INVALID canonical index but NOT recreate it — the
    # valid legacy index already covers the column set, so recreating would leave
    # a duplicate next to it. The coverage guard must therefore apply even when
    # the canonical name was INVALID (it is dropped first; ``signatures`` already
    # excludes invalid indexes).
    conn = _FakeReconcileConn(
        existing_names={"ix_assignment_runs_status", "ix_workflow_runs_status"},
        invalid_names={"ix_assignment_runs_status"},
        signatures={("assignment_runs", ("status",))},  # the VALID legacy index
    )
    idx = _index("ix_assignment_runs_status", "assignment_runs", "status")
    created = index_maintenance.reconcile_indexes(conn, [idx])
    assert created == []  # no duplicate created
    drops = [s for s in conn.ddl if "DROP INDEX IF EXISTS" in s]
    creates = [s for s in conn.ddl if "CREATE INDEX IF NOT EXISTS" in s]
    assert drops, f"expected the INVALID canonical index to be dropped; got {conn.ddl}"
    assert not creates, (
        f"must NOT recreate when a valid legacy index covers the column set; "
        f"got {conn.ddl}"
    )
    assert not any("CONCURRENTLY" in s for s in conn.ddl)


# ---------------------------------------------------------------------------
# Wiring — run_migrations must invoke the reconciler on boot.
# ---------------------------------------------------------------------------


def test_run_migrations_invokes_ensure_model_indexes():
    source = inspect.getsource(migrations.run_migrations)
    assert "_ensure_model_indexes()" in source, (
        "run_migrations must invoke _ensure_model_indexes() so fresh / "
        "stamped-fresh installs (which skip migration bodies) still get every "
        "model-declared index"
    )


# ---------------------------------------------------------------------------
# Runtime — AUTOCOMMIT + statement_timeout = 0; failures halt startup.
# ---------------------------------------------------------------------------


def _build_fake_engine(*, run_sync_result=None, run_sync_raises=None):
    executed: list[str] = []
    isolation_calls: list[str] = []

    class _AutocommitConn:
        def __init__(self):
            self.execute = AsyncMock(side_effect=self._execute)

        async def _execute(self, stmt, *_a, **_k):
            executed.append(str(stmt))

        async def run_sync(self, fn, *_a, **_k):
            if run_sync_raises is not None:
                raise run_sync_raises
            return run_sync_result if run_sync_result is not None else []

    class _Inner:
        def __init__(self):
            self._autocommit = _AutocommitConn()

        async def execution_options(self, **kwargs):
            if "isolation_level" in kwargs:
                isolation_calls.append(kwargs["isolation_level"])
            return self._autocommit

    class _ConnectCM:
        async def __aenter__(self):
            return _Inner()

        async def __aexit__(self, *_a):
            return False

    fake_engine = MagicMock()
    fake_engine.connect = lambda: _ConnectCM()
    return fake_engine, executed, isolation_calls


@pytest.mark.asyncio
async def test_ensure_model_indexes_uses_autocommit_and_disables_timeout(monkeypatch):
    """The reconciler MUST run on an AUTOCOMMIT connection (so each plain
    ``CREATE INDEX IF NOT EXISTS`` commits and releases its SHARE lock
    immediately instead of holding every lock until one big transaction commits)
    and disable the 30 s statement timeout first (an in-place build on a
    populated table — notably the GIN full-text indexes — can take minutes)."""
    fake_engine, executed, isolation_calls = _build_fake_engine(run_sync_result=[])
    monkeypatch.setattr(migrations, "engine", fake_engine)

    await migrations._ensure_model_indexes()

    assert isolation_calls == ["AUTOCOMMIT"], (
        f"reconciler MUST flip the connection to AUTOCOMMIT; got {isolation_calls}"
    )
    assert any("SET statement_timeout = 0" in s for s in executed), (
        f"reconciler MUST disable statement_timeout before building; got {executed}"
    )


@pytest.mark.asyncio
async def test_ensure_model_indexes_raises_on_failure(monkeypatch, caplog):
    """A reconciliation failure MUST halt startup loudly rather than boot a
    deployment that silently seq-scans under load."""
    fake_engine, _, _ = _build_fake_engine(
        run_sync_raises=RuntimeError("index build blew up")
    )
    monkeypatch.setattr(migrations, "engine", fake_engine)

    with caplog.at_level("ERROR"), pytest.raises(RuntimeError) as exc:
        await migrations._ensure_model_indexes()

    assert "Index reconciliation failed" in str(exc.value)
    assert "Model index reconciliation failed" in caplog.text
