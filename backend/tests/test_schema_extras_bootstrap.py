"""Tests for ``core.migrations._ensure_schema_extras``.

Pins the bootstrap path for the ``knowledge_documents.updated_at``
trigger and the ``(status, updated_at)`` index.  Fresh installs run
``Base.metadata.create_all`` + ``alembic stamp head`` and therefore
NEVER execute the migration body — without an idempotent bootstrap
pass that runs every boot, fresh databases would silently regress on
the stuck-document sweeper:

* the ``BEFORE UPDATE`` trigger would never fire for ORM-bypass
  updates (raw ``UPDATE knowledge_documents SET status=…`` paths), so
  ``updated_at`` would stay stale and the sweeper would treat freshly-
  re-enqueued docs as stuck on the next tick, OR
* the sweeper's ``WHERE status IN (…) AND updated_at < cutoff`` would
  seq-scan a populated table instead of using the index.

These tests guard both: (1) the bootstrap is wired into the migration
entry-point, (2) the SQL is idempotent and uses ``clock_timestamp()``
(not ``now()``), (3) the index bootstrap uses plain DDL on an
AUTOCOMMIT connection — NOT ``CONCURRENTLY``, which deadlocks against
the sibling workers blocked on ``pg_advisory_lock()``.
"""

from __future__ import annotations

import inspect
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.core import migrations

# ---------------------------------------------------------------------------
# SQL contract — static guards on the constants the helper executes.
# ---------------------------------------------------------------------------


def test_trigger_function_sql_is_idempotent():
    """The function bootstrap MUST use ``CREATE OR REPLACE FUNCTION`` so
    repeated calls on existing installs are no-ops (vs. plain
    ``CREATE FUNCTION`` which would raise on the second boot)."""
    assert "CREATE OR REPLACE FUNCTION" in migrations._KB_DOCUMENT_TRIGGER_FUNCTION_SQL
    assert "knowledge_documents_set_updated_at" in (
        migrations._KB_DOCUMENT_TRIGGER_FUNCTION_SQL
    )


def test_trigger_function_uses_clock_timestamp_not_now():
    """The trigger MUST use ``clock_timestamp()`` so each row gets the
    actual wall-clock time the trigger fired, not the surrounding
    transaction's start time.  Round-3 bug fix — a long-running KB sync
    that holds one transaction open across many page upserts would
    otherwise stamp every row with the same old timestamp and the
    sweeper would immediately re-enqueue the freshly-synced docs on
    the next tick.  A regression that swaps in ``now()`` /
    ``current_timestamp`` / ``transaction_timestamp()`` here trips this
    test."""
    sql = migrations._KB_DOCUMENT_TRIGGER_FUNCTION_SQL.lower()
    assert "clock_timestamp()" in sql, (
        f"trigger must use clock_timestamp(), got SQL:\n"
        f"{migrations._KB_DOCUMENT_TRIGGER_FUNCTION_SQL}"
    )
    # Negative assertion: the failed alternatives must NOT appear.
    # Allow substrings (e.g. inside identifiers) but not as bare calls.
    for forbidden in ("now()", "current_timestamp", "transaction_timestamp()"):
        assert forbidden not in sql, (
            f"trigger MUST NOT use {forbidden} (returns transaction-start "
            f"time, not wall-clock); rendered SQL was:\n"
            f"{migrations._KB_DOCUMENT_TRIGGER_FUNCTION_SQL}"
        )


def test_trigger_create_pattern_is_idempotent():
    """The CREATE TRIGGER bootstrap MUST be preceded by a
    ``DROP TRIGGER IF EXISTS`` so the function-refresh path (where the
    function body changed) cleanly rebinds the trigger to the replaced
    function.  Without the DROP, the second boot would raise
    ``trigger already exists``."""
    assert "DROP TRIGGER IF EXISTS" in migrations._KB_DOCUMENT_TRIGGER_DROP_SQL
    assert "trg_knowledge_documents_set_updated_at" in (
        migrations._KB_DOCUMENT_TRIGGER_DROP_SQL
    )
    assert "CREATE TRIGGER" in migrations._KB_DOCUMENT_TRIGGER_CREATE_SQL
    assert "BEFORE UPDATE" in migrations._KB_DOCUMENT_TRIGGER_CREATE_SQL
    assert "FOR EACH ROW" in migrations._KB_DOCUMENT_TRIGGER_CREATE_SQL


def test_updated_at_backfill_uses_coalesce_and_clock_timestamp():
    """The backfill MUST set ``updated_at = COALESCE(created_at,
    clock_timestamp()) WHERE updated_at IS NULL``.  This mirrors the
    migration body so a stamped-fresh install (where
    ``_ensure_model_columns`` added the column as bare nullable) gets
    existing rows backfilled — without this the sweeper's predicate
    silently never matches the pre-existing rows.

    Pin the exact shape so a regression that drops the COALESCE (and
    uses ``clock_timestamp()`` unconditionally — losing the
    document's actual created-at age) or that touches non-NULL rows
    (rewriting freshly-updated rows back to ``created_at``, which
    would re-trigger the sweeper) trips here.  ``clock_timestamp()``
    not ``now()`` for the same reason as the trigger: a slow
    bootstrap inside a long transaction would otherwise tag every
    backfilled row with the transaction's *start* time.
    """
    sql = migrations._KB_DOCUMENT_BACKFILL_UPDATED_AT_SQL
    assert "UPDATE knowledge_documents" in sql
    assert "COALESCE(created_at, clock_timestamp())" in sql, (
        f"backfill must reuse created_at when available so the "
        f"sweeper sees the document's true age, falling back to "
        f"clock_timestamp() (NOT now()) for NULL-created_at rows; "
        f"got SQL: {sql}"
    )
    assert "WHERE updated_at IS NULL" in sql, (
        f"backfill MUST scope to rows that need it — touching non-"
        f"NULL rows would rewrite freshly-updated rows back to "
        f"created_at and re-trigger the sweeper; got SQL: {sql}"
    )
    # Negative assertions: the failed alternatives must NOT appear.
    sql_lower = sql.lower()
    for forbidden in ("now()", "current_timestamp", "transaction_timestamp()"):
        assert forbidden not in sql_lower, (
            f"backfill MUST NOT use {forbidden} (returns transaction-"
            f"start time, which on a long bootstrap would be older "
            f"than the sweeper's pending cutoff); got SQL: {sql}"
        )


def test_updated_at_set_default_uses_clock_timestamp():
    """The ``SET DEFAULT`` mirrors the trigger and the migration's
    column server_default: ``clock_timestamp()``, not ``now()``.  A
    raw SQL ``INSERT`` that omits ``updated_at`` inside a long-running
    transaction would otherwise get the transaction's *start* time as
    the default, immediately tagging the just-inserted row as "stuck"
    on the next sweeper tick.
    """
    sql = migrations._KB_DOCUMENT_SET_DEFAULT_UPDATED_AT_SQL
    assert "ALTER TABLE knowledge_documents" in sql
    assert "ALTER COLUMN updated_at SET DEFAULT clock_timestamp()" in sql
    sql_lower = sql.lower()
    for forbidden in ("now()", "current_timestamp", "transaction_timestamp()"):
        assert forbidden not in sql_lower, (
            f"SET DEFAULT MUST NOT use {forbidden}; got SQL: {sql}"
        )


def test_updated_at_set_not_null_targets_updated_at_only():
    """``SET NOT NULL`` enforces the constraint after the backfill.
    Pin the column name so a regression that targets a different
    column (e.g. ``created_at``) — which would crash on legitimately
    NULL rows the migration never touched — trips here.
    """
    sql = migrations._KB_DOCUMENT_SET_NOT_NULL_UPDATED_AT_SQL
    assert "ALTER TABLE knowledge_documents" in sql
    assert "ALTER COLUMN updated_at SET NOT NULL" in sql


def test_index_bootstrap_is_plain_and_idempotent():
    """The (status, updated_at) index MUST use plain ``CREATE INDEX
    IF NOT EXISTS`` — explicitly NOT ``CONCURRENTLY``:

    * NOT ``CONCURRENTLY``: the bootstrap runs while holding the
      migration advisory lock.  Concurrent DDL waits for every other
      open transaction — including the sibling uvicorn workers/pods
      blocked inside ``SELECT pg_advisory_lock(...)``, which cannot
      finish until the lock holder releases.  That mutual deadlock
      hung every multi-worker deploy (liveness SIGTERM → INVALID
      index → CrashLoopBackOff).
    * ``IF NOT EXISTS``: idempotent across reboots and across the
      Alembic-first install path (where the migration already created
      the index, and this bootstrap is a metadata-only check).

    A regression that reintroduces ``CONCURRENTLY`` or drops
    ``IF NOT EXISTS`` trips here."""
    sql = migrations._KB_DOCUMENT_STATUS_UPDATED_AT_INDEX_SQL.upper()
    assert "CONCURRENTLY" not in sql, (
        "bootstrap index MUST NOT use CONCURRENTLY — it deadlocks "
        "against the advisory-lock waiters during multi-worker boots; "
        f"got SQL: {sql}"
    )
    assert "CREATE INDEX IF NOT EXISTS" in sql
    assert "IX_KNOWLEDGE_DOCUMENTS_STATUS_UPDATED_AT" in sql
    assert "KNOWLEDGE_DOCUMENTS" in sql
    assert "(STATUS, UPDATED_AT)" in sql

    drop_sql = migrations._KB_DOCUMENT_STATUS_UPDATED_AT_INDEX_DROP_SQL.upper()
    assert "CONCURRENTLY" not in drop_sql, (
        "the invalid-index recovery DROP MUST NOT use CONCURRENTLY "
        "for the same advisory-lock deadlock reason; got: "
        f"{drop_sql}"
    )
    assert "DROP INDEX IF EXISTS" in drop_sql


def test_kb_status_index_bootstrap_is_plain_and_idempotent():
    """The ``(knowledge_base_id, status)`` count index MUST use
    the same plain ``CREATE INDEX IF NOT EXISTS`` shape as the sweeper
    index (NOT ``CONCURRENTLY`` — advisory-lock deadlock) and be
    registered in the shared bootstrap list so the bootstrap actually
    creates it."""
    sql = migrations._KB_DOCUMENT_KB_STATUS_INDEX_SQL.upper()
    assert "CONCURRENTLY" not in sql, (
        "bootstrap index MUST NOT use CONCURRENTLY; got: " + sql
    )
    assert "CREATE INDEX IF NOT EXISTS" in sql
    assert "IX_KNOWLEDGE_DOCUMENTS_KB_STATUS" in sql
    assert "KNOWLEDGE_DOCUMENTS" in sql
    assert "(KNOWLEDGE_BASE_ID, STATUS)" in sql

    drop_sql = migrations._KB_DOCUMENT_KB_STATUS_INDEX_DROP_SQL.upper()
    assert "CONCURRENTLY" not in drop_sql, (
        "recovery DROP MUST NOT use CONCURRENTLY; got: " + drop_sql
    )
    assert "DROP INDEX IF EXISTS" in drop_sql
    assert "IX_KNOWLEDGE_DOCUMENTS_KB_STATUS" in drop_sql

    registered = {name for name, _c, _d in migrations._KB_DOCUMENT_CONCURRENT_INDEXES}
    assert migrations._KB_DOCUMENT_KB_STATUS_INDEX_NAME in registered
    assert migrations._KB_DOCUMENT_STATUS_UPDATED_AT_INDEX_NAME in registered


# ---------------------------------------------------------------------------
# Idempotency probes — the structural fix for the SQLSTATE 57014 crash-loop.
# These reads decide whether the lock-taking Phase A DDL
# runs at all; their SQL shape must stay a pure catalog read.
# ---------------------------------------------------------------------------


def test_default_check_sql_reads_pg_attrdef_for_updated_at():
    """The column-default probe MUST read the stored default expression
    from ``pg_attrdef`` for ``knowledge_documents.updated_at`` so the
    ``ALTER … SET DEFAULT`` (which takes an ACCESS EXCLUSIVE lock) is
    skipped once applied — the primary guard against the every-boot
    crash-loop.  A regression that drops the probe (forcing the DDL to
    run unconditionally again) or targets the wrong column trips here."""
    sql = migrations._KB_DOCUMENT_UPDATED_AT_DEFAULT_CHECK_SQL
    assert "pg_attrdef" in sql
    assert "pg_get_expr" in sql, (
        "default probe MUST render the stored node tree via pg_get_expr "
        "so the comparison sees ``clock_timestamp()`` as text; got: " + sql
    )
    assert "knowledge_documents" in sql
    assert "updated_at" in sql
    # Must be a read — never accidentally a write/DDL.
    upper = sql.upper().strip()
    assert upper.startswith("SELECT"), (
        "default probe MUST be a SELECT (pure catalog read, no table "
        "lock); got: " + sql
    )


def test_trigger_present_and_body_check_sql_are_catalog_reads():
    """The trigger-presence probe MUST read ``pg_trigger`` for our
    trigger name (filtering internal constraint triggers) and the
    function-body probe MUST read ``pg_proc.prosrc`` — both pure catalog
    reads so the steady-state boot can skip the DROP/CREATE TRIGGER DDL
    (the second half of the crash-loop's lock-taking statements)."""
    present = migrations._KB_DOCUMENT_TRIGGER_PRESENT_CHECK_SQL
    assert "pg_trigger" in present
    assert "trg_knowledge_documents_set_updated_at" in present
    assert "tgisinternal" in present, (
        "presence probe MUST exclude internal constraint triggers via "
        "NOT tgisinternal; got: " + present
    )
    assert present.upper().strip().startswith("SELECT")

    body = migrations._KB_DOCUMENT_TRIGGER_FUNCTION_BODY_CHECK_SQL
    assert "prosrc" in body
    # Assert on the catalog table the probe actually reads (``pg_proc``)
    # rather than the function *name*: since the rewrite the probe joins
    # ``pg_proc`` via ``pg_trigger.tgfoid`` and no longer references
    # ``proname``, so the function name only appears incidentally as a
    # substring of the trigger name — a misleading thing to assert on.
    assert "pg_proc" in body
    assert body.upper().strip().startswith("SELECT")
    # The body probe MUST join through the trigger (pg_trigger.tgfoid →
    # pg_proc.oid) rather than matching pg_proc.proname by name +
    # search_path.  A name-only match is ambiguous: a same-named function
    # in another schema on the search_path makes scalar_one_or_none()
    # raise MultipleResultsFound, which _is_updated_at_trigger_current
    # swallows as "not current" — forcing DROP/CREATE TRIGGER DDL on every
    # boot and reintroducing the lock-taking crash-loop.
    assert "pg_trigger" in body and "tgfoid" in body, (
        "function-body probe MUST be tied to the actual trigger via "
        "pg_trigger.tgfoid so it is unambiguous (no MultipleResultsFound "
        "on a same-named function in another schema); got: " + body
    )
    assert "trg_knowledge_documents_set_updated_at" in body, (
        "function-body probe MUST scope to our trigger name; got: " + body
    )


# ---------------------------------------------------------------------------
# Wiring — make sure ``run_migrations`` actually calls the bootstrap.
# ---------------------------------------------------------------------------


def test_run_migrations_invokes_ensure_schema_extras():
    """Static guard: ``run_migrations`` MUST call
    ``_ensure_schema_extras()`` inside the migration-lock block.  A
    refactor that accidentally drops the call would let fresh installs
    boot without the trigger / index — the sweeper would silently
    regress and the bug would only surface in production after the
    pending threshold expires."""
    source = inspect.getsource(migrations.run_migrations)
    assert "_ensure_schema_extras()" in source, (
        "run_migrations must invoke _ensure_schema_extras() so fresh "
        "installs (which skip the migration body via create_all + "
        "stamp head) still get the knowledge_documents trigger and "
        "the (status, updated_at) index"
    )


# ---------------------------------------------------------------------------
# Runtime — the helper drives the right DDL through the right connection
# isolation modes.
# ---------------------------------------------------------------------------


class _StatefulCheckTracker:
    """Tracks the transient CHECK constraint's state across SQL calls
    in the fake engine so the live-safe NOT NULL sequence can run
    end-to-end against deterministic probe answers:

      * Initial state mirrors a stamped-fresh install where the column
        was just added as bare nullable: no CHECK yet (``state=None``).
      * After ``ADD CONSTRAINT … CHECK … NOT VALID`` the state flips to
        ``False`` (constraint exists but ``convalidated=false``).
      * After ``VALIDATE CONSTRAINT`` the state flips to ``True``.
      * After ``DROP CONSTRAINT`` the state flips back to ``None``.

    This lets the fake's CHECK-state probe return whatever the
    production code would actually see at each call site, so a single
    bootstrap run drives ADD → VALIDATE → SET NOT NULL → DROP exactly
    as it would in PostgreSQL.
    """

    def __init__(self, *, initial: bool | None = None):
        self.state: bool | None = initial

    def observe(self, sql: str) -> None:
        if "ADD CONSTRAINT" in sql and "NOT VALID" in sql:
            self.state = False
        elif "VALIDATE CONSTRAINT" in sql:
            self.state = True
        elif "DROP CONSTRAINT" in sql:
            self.state = None


def _build_fake_engine(
    *,
    invalid_index_present: bool = False,
    column_already_not_null: bool = True,
    default_already_set: bool = True,
    trigger_already_current: bool = True,
    check_tracker: _StatefulCheckTracker | None = None,
    begin_executed: list[str] | None = None,
    connect_executed: list[tuple[str, dict | None]] | None = None,
    isolation_calls: list[str] | None = None,
    index_create_raises: BaseException | None = None,
    begin_outcomes: list[BaseException | None] | None = None,
):
    """Build a stub engine for ``_ensure_schema_extras`` that captures
    each SQL string + bind params on the AUTOCOMMIT path and feeds the
    invalid-index pre-check a controllable answer.

    ``column_already_not_null`` controls the answer the ``attnotnull``
    probe (``_is_updated_at_already_not_null``) gets back.  Defaults to
    ``True`` because that's the common case on every boot after the
    first (``create_all`` on fresh installs, migration body on existing
    installs) — only the narrow stamped-fresh-with-pre-existing-column
    path actually drives the live-safe NOT NULL sequence.

    ``default_already_set`` controls the answer the column-default probe
    (``_is_updated_at_default_set``, reads ``pg_attrdef``) gets back, and
    ``trigger_already_current`` controls the trigger-presence +
    function-body probes (``_is_updated_at_trigger_current``).  Both
    default to ``True`` — the steady-state boot where Phase A is a no-op
    (the guard against the SQLSTATE 57014 crash-loop).  Set them ``False`` to exercise the
    genuine first-deploy DDL path.

    ``check_tracker`` is a stateful tracker for the transient NOT NULL
    CHECK constraint.  Pass an explicit tracker (e.g.
    ``_StatefulCheckTracker(initial=None)``) to drive the live-safe
    sequence end-to-end with realistic state transitions on each SQL
    call.  When omitted, the probe always returns ``None`` (the column
    is already NOT NULL path doesn't go near it).

    Returns the engine and the three capture lists so individual tests
    can pass narrow ones in instead of re-declaring all three.
    """
    begin_executed = begin_executed if begin_executed is not None else []
    connect_executed = connect_executed if connect_executed is not None else []
    isolation_calls = isolation_calls if isolation_calls is not None else []

    class _Result:
        def __init__(self, value):
            self._value = value

        def scalar(self):
            return self._value

        def scalar_one_or_none(self):
            return self._value

    class _BeginCM:
        def __init__(self):
            self.execute = AsyncMock(side_effect=self._execute)

        async def _execute(self, stmt, params=None):
            sql = str(stmt)
            begin_executed.append(sql)
            # The transactional block runs the CHECK-state probe just
            # before deciding whether to ADD CONSTRAINT NOT VALID.
            if "convalidated" in sql:
                return _Result(
                    check_tracker.state if check_tracker is not None else None
                )
            # Update the stateful tracker as ADD / DROP run inside the
            # transactional blocks (begin block runs ADD; finalize
            # block runs DROP).
            if check_tracker is not None:
                check_tracker.observe(sql)
            return _Result(None)

        async def __aenter__(self):
            # Drive the Phase A retry path: each entered ``begin()``
            # context pops the next outcome — an exception simulates a
            # lock/timeout failure on that attempt, ``None`` (or an
            # exhausted list) succeeds.
            if begin_outcomes:
                outcome = begin_outcomes.pop(0)
                if isinstance(outcome, BaseException):
                    raise outcome
            return self

        async def __aexit__(self, *_args):
            return False

    class _AutocommitConn:
        def __init__(self):
            self.execute = AsyncMock(side_effect=self._execute)

        async def _execute(self, stmt, params=None):
            sql = str(stmt)
            connect_executed.append((sql, params))
            # CHECK-state probe inside the AUTOCOMMIT validate path —
            # reads the stateful tracker so the response reflects
            # whatever the transactional ADD CONSTRAINT did upstream.
            if "convalidated" in sql:
                return _Result(
                    check_tracker.state if check_tracker is not None else None
                )
            # Update the stateful tracker as VALIDATE runs on the
            # AUTOCOMMIT connection.
            if check_tracker is not None:
                check_tracker.observe(sql)
            # Feed the invalid-index pre-check a deterministic answer
            # so the helper takes / does not take the DROP branch as
            # the test scenario requires.
            if "indisvalid" in sql:
                return _Result(1 if invalid_index_present else None)
            # Simulate a failure on the CREATE INDEX only if the test
            # asks for one — used by the timeout / leftover
            # invalid-index regression cases below.
            if (
                index_create_raises is not None
                and "CREATE INDEX" in sql
            ):
                raise index_create_raises
            return _Result(None)

    class _ConnectCM:
        async def __aenter__(self):
            self._inner = _Inner()
            return self._inner

        async def __aexit__(self, *_args):
            return False

    class _Inner:
        def __init__(self):
            self._autocommit = _AutocommitConn()

        async def execution_options(self, **kwargs):
            if "isolation_level" in kwargs:
                isolation_calls.append(kwargs["isolation_level"])
            return self._autocommit

        async def execute(self, stmt, params=None):
            # Direct calls without ``execution_options`` are used by the
            # idempotency probes that read system catalogs outside any
            # AUTOCOMMIT setup.  Pin each answer here so tests can drive
            # the skip / first-deploy branches deterministically.
            sql = str(stmt)
            if "attnotnull" in sql:
                # _is_updated_at_already_not_null (pg_attribute).
                return _Result(column_already_not_null)
            if "adbin" in sql:
                # _is_updated_at_default_set (pg_attrdef): return the
                # rendered default expression or NULL.
                return _Result(
                    "clock_timestamp()" if default_already_set else None
                )
            if "prosrc" in sql:
                # _is_updated_at_trigger_current function-body probe.
                # Checked BEFORE ``pg_trigger`` because the body probe
                # joins through ``pg_trigger`` (tgfoid → pg_proc) to stay
                # unambiguous, so its SQL contains *both* substrings —
                # routing on ``pg_trigger`` first would misclassify it as
                # the presence probe and return a bool instead of prosrc.
                return _Result(
                    "BEGIN NEW.updated_at = clock_timestamp(); END"
                    if trigger_already_current
                    else None
                )
            if "pg_trigger" in sql:
                # _is_updated_at_trigger_current presence probe.
                return _Result(trigger_already_current)
            return _Result(None)

    fake_engine = MagicMock()
    fake_engine.begin = lambda: _BeginCM()
    fake_engine.connect = lambda: _ConnectCM()
    return fake_engine, begin_executed, connect_executed, isolation_calls


@pytest.mark.asyncio
async def test_ensure_schema_extras_runs_trigger_in_transaction_index_in_autocommit(
    monkeypatch,
):
    """End-to-end shape: the helper must (1) execute the function +
    trigger drop + trigger create inside a transactional ``begin()``
    block, (2) execute the index builds on an AUTOCOMMIT-isolation
    connection so each statement commits (and releases its lock)
    immediately instead of holding it for one big transaction.

    Drives a stub engine that captures both the SQL strings and the
    isolation mode of each connection so the assertion is precise.
    """
    # First-deploy scenario: the column was just built NOT NULL by
    # ``create_all`` (so the live-safe NOT NULL sequence is short-
    # circuited) but the DEFAULT and trigger have NOT been applied yet
    # (default_already_set / trigger_already_current = False) — this is
    # the genuine path where Phase A DDL must actually run.
    fake_engine, begin_executed, connect_executed, isolation_calls = (
        _build_fake_engine(
            column_already_not_null=True,
            default_already_set=False,
            trigger_already_current=False,
        )
    )
    monkeypatch.setattr(migrations, "engine", fake_engine)

    await migrations._ensure_schema_extras()

    # Phase A first sets a short ``lock_timeout`` (so a blocked DDL
    # fails fast with 55P03 instead of stalling traffic for 30 s and
    # crash-looping the deploy) and lifts the statement
    # timeout (so a genuine backfill can complete), THEN runs the
    # column + trigger DDL.  Order is pinned: ``SET DEFAULT`` first so
    # any concurrent INSERT racing the (skipped here) backfill picks up
    # the default, then function + drop trigger + create trigger.
    assert len(begin_executed) == 6, (
        f"first-deploy transactional block should run 6 statements "
        f"(lock_timeout + statement_timeout guards + SET DEFAULT + "
        f"function + drop trigger + create trigger; backfill skipped "
        f"because the column is already NOT NULL); got "
        f"{len(begin_executed)}: {begin_executed}"
    )
    assert "lock_timeout" in begin_executed[0], (
        f"Phase A MUST set lock_timeout FIRST so a blocked DDL fails "
        f"fast instead of hogging the lock queue; got {begin_executed[0]}"
    )
    assert "SET LOCAL statement_timeout = 0" in begin_executed[1], (
        f"Phase A MUST lift statement_timeout so an honest backfill can "
        f"complete (lock contention is bounded by lock_timeout); got "
        f"{begin_executed[1]}"
    )
    assert "ALTER COLUMN updated_at SET DEFAULT clock_timestamp()" in (
        begin_executed[2]
    )
    assert "CREATE OR REPLACE FUNCTION" in begin_executed[3]
    assert "DROP TRIGGER IF EXISTS" in begin_executed[4]
    # No SET NOT NULL, no ADD/VALIDATE/DROP CHECK CONSTRAINT — the
    # short-circuit branch is fast and skips all of them.
    for sql in begin_executed:
        assert "SET NOT NULL" not in sql, (
            f"short-circuit branch MUST skip SET NOT NULL when the "
            f"column is already NOT NULL; got {sql}"
        )
        assert "CHECK (updated_at IS NOT NULL)" not in sql, (
            f"short-circuit branch MUST skip ADD CONSTRAINT CHECK "
            f"when the column is already NOT NULL; got {sql}"
        )
    # The CREATE TRIGGER is the last statement in the block.
    assert "CREATE TRIGGER" in begin_executed[-1]

    # AUTOCOMMIT was set BEFORE any statement on the index connection —
    # each plain CREATE INDEX must commit (and release its SHARE lock)
    # immediately instead of accumulating locks in one transaction.
    assert isolation_calls == ["AUTOCOMMIT"], (
        f"index bootstrap MUST flip the connection to AUTOCOMMIT before "
        f"running the index builds; got isolation calls: {isolation_calls}"
    )

    # On a clean run (no invalid index) the AUTOCOMMIT path runs exactly
    # five statements in this order — one SET, then a (check, CREATE) pair
    # for each of the two bootstrap-managed indexes:
    #   1. SET statement_timeout = 0  (so the builds can outlast 30 s)
    #   2. invalid-index pre-check for (status, updated_at)   → None → no DROP
    #   3. CREATE INDEX IF NOT EXISTS (status, updated_at)
    #   4. invalid-index pre-check for (knowledge_base_id, status)
    #   5. CREATE INDEX IF NOT EXISTS (knowledge_base_id, status)
    sql_only = [sql for sql, _params in connect_executed]
    assert len(sql_only) == 5, (
        f"clean-run AUTOCOMMIT path should issue exactly 5 statements "
        f"(SET timeout, then check+CREATE for each of the two indexes); got "
        f"{len(sql_only)}: {sql_only}"
    )
    assert "SET statement_timeout = 0" in sql_only[0]
    assert "indisvalid" in sql_only[1]
    assert "CREATE INDEX IF NOT EXISTS" in sql_only[2]
    assert "(status, updated_at)" in sql_only[2]
    assert "indisvalid" in sql_only[3]
    assert "CREATE INDEX IF NOT EXISTS" in sql_only[4]
    assert "(knowledge_base_id, status)" in sql_only[4]
    # The deadlock regression guard: NO statement on the boot path may
    # use CONCURRENTLY.
    assert not any("CONCURRENTLY" in s for s in sql_only), (
        f"bootstrap MUST NOT issue CONCURRENTLY DDL — it deadlocks "
        f"against advisory-lock waiters during multi-worker boots; "
        f"got: {sql_only}"
    )


@pytest.mark.asyncio
async def test_ensure_schema_extras_disables_statement_timeout_before_create(
    monkeypatch,
):
    """``SET statement_timeout = 0`` MUST run BEFORE the CREATE INDEX
    so the build can outlast the 30 s default the application engine
    sets on every pooled connection in ``app/core/database.py``.
    Without this reset, an index build on a populated production table
    would time out and fail the boot.

    Pins both presence AND ordering: the SET must come first on the
    AUTOCOMMIT connection, before the invalid-check and before the
    CREATE INDEX itself.
    """
    fake_engine, _, connect_executed, _ = _build_fake_engine()
    monkeypatch.setattr(migrations, "engine", fake_engine)

    await migrations._ensure_schema_extras()

    sql_only = [sql for sql, _params in connect_executed]
    assert sql_only, "AUTOCOMMIT connection must have executed at least one statement"
    # Presence: the SET must appear somewhere on the AUTOCOMMIT path.
    assert any("SET statement_timeout = 0" in s for s in sql_only), (
        f"AUTOCOMMIT connection MUST issue ``SET statement_timeout = 0`` so "
        f"the index builds can outlast the 30 s pool default; "
        f"got: {sql_only}"
    )
    # Ordering: the SET must come BEFORE the CREATE INDEX.  An ordering
    # regression (SET after CREATE) would still pass the presence check
    # but would be useless in production — the timeout would already
    # have fired during the build.
    set_index = next(
        i for i, s in enumerate(sql_only) if "SET statement_timeout = 0" in s
    )
    create_index = next(
        i for i, s in enumerate(sql_only) if "CREATE INDEX" in s
    )
    assert set_index < create_index, (
        f"SET statement_timeout MUST run BEFORE CREATE INDEX "
        f"(otherwise the 30 s default fires during the build); got order: "
        f"{sql_only}"
    )


@pytest.mark.asyncio
async def test_ensure_schema_extras_drops_invalid_index_before_recreate(monkeypatch):
    """``CREATE INDEX IF NOT EXISTS`` only checks existence — it does
    NOT check ``pg_index.indisvalid``.  So a leftover invalid index
    from a prior failed bootstrap (e.g. an old CONCURRENTLY build
    killed by the liveness probe) blocks future bootstrap attempts
    forever: the create skips, the sweeper seq-scans, and there's no
    error in the logs.

    Pin the recovery path: when the pre-check returns a row, the
    helper MUST issue a plain ``DROP INDEX IF EXISTS`` (NOT
    ``CONCURRENTLY`` — advisory-lock deadlock) for the expected
    index name BEFORE issuing the CREATE.
    """
    fake_engine, _, connect_executed, _ = _build_fake_engine(
        invalid_index_present=True
    )
    monkeypatch.setattr(migrations, "engine", fake_engine)

    await migrations._ensure_schema_extras()

    sql_only = [sql for sql, _params in connect_executed]
    # Both bootstrap-managed indexes report invalid here, so the path runs
    # SET + (check, DROP, CREATE) × 2 = 7 statements.
    assert len(sql_only) == 7, (
        f"invalid-index recovery should issue 7 statements (SET, then "
        f"check+DROP+CREATE for each of the two indexes); got "
        f"{len(sql_only)}: {sql_only}"
    )
    assert "SET statement_timeout = 0" in sql_only[0]
    assert "indisvalid" in sql_only[1]
    assert "DROP INDEX IF EXISTS" in sql_only[2]
    assert migrations._KB_DOCUMENT_STATUS_UPDATED_AT_INDEX_NAME in sql_only[2]
    assert "CREATE INDEX IF NOT EXISTS" in sql_only[3]
    # The recovery path must not deadlock either.
    assert not any("CONCURRENTLY" in s for s in sql_only), (
        f"invalid-index recovery MUST NOT issue CONCURRENTLY DDL; "
        f"got: {sql_only}"
    )
    # The second index is dropped + recreated too.
    dropped = [s for s in sql_only if "DROP INDEX IF EXISTS" in s]
    assert any(
        migrations._KB_DOCUMENT_KB_STATUS_INDEX_NAME in s for s in dropped
    ), f"kb_status index must also be dropped when invalid; got {sql_only}"
    assert any(
        "(knowledge_base_id, status)" in s for s in sql_only
    ), f"kb_status index must be recreated; got {sql_only}"


@pytest.mark.asyncio
async def test_ensure_schema_extras_skips_drop_when_index_is_valid(monkeypatch):
    """Symmetry guard for the recovery path: when the pre-check returns
    no row (no invalid leftover), the helper MUST NOT issue a DROP.
    A regression that DROPs unconditionally would cause a valid index
    to be rebuilt on every boot — expensive on populated tables and
    fragile if the build itself races.
    """
    fake_engine, _, connect_executed, _ = _build_fake_engine(
        invalid_index_present=False
    )
    monkeypatch.setattr(migrations, "engine", fake_engine)

    await migrations._ensure_schema_extras()

    sql_only = [sql for sql, _params in connect_executed]
    assert not any("DROP INDEX" in s for s in sql_only), (
        f"DROP INDEX MUST NOT run when the pre-check sees no invalid "
        f"leftover (would rebuild valid indexes on every boot); got: "
        f"{sql_only}"
    )


@pytest.mark.asyncio
async def test_ensure_schema_extras_passes_index_name_param_to_check(monkeypatch):
    """The invalid-index pre-check uses a bind parameter for the index
    name rather than string-interpolating it — pin the contract so a
    future regression that switches to f-string interpolation (and
    risks SQL injection / quoting issues if the name ever becomes
    dynamic) trips here.  Also pins the canonical name string against
    a regression that renames the index on one side of the helper.
    """
    fake_engine, _, connect_executed, _ = _build_fake_engine(
        invalid_index_present=False
    )
    monkeypatch.setattr(migrations, "engine", fake_engine)

    await migrations._ensure_schema_extras()

    check_calls = [
        (sql, params) for sql, params in connect_executed if "indisvalid" in sql
    ]
    assert check_calls, "invalid-index pre-check must run"
    sql, params = check_calls[0]
    assert ":index_name" in sql, (
        f"invalid-index check MUST bind the index name via parameter, "
        f"not string interpolation; got SQL: {sql}"
    )
    assert params == {
        "index_name": migrations._KB_DOCUMENT_STATUS_UPDATED_AT_INDEX_NAME
    }, (
        f"invalid-index check MUST receive the canonical index name as "
        f"bind value; got params: {params}"
    )


@pytest.mark.asyncio
async def test_ensure_schema_extras_raises_on_trigger_failure(monkeypatch, caplog):
    """A trigger-bootstrap failure MUST halt startup, not be swallowed.

    The sweeper depends on the trigger for ORM-bypass UPDATE paths to
    refresh ``updated_at`` — without it, freshly re-enqueued docs would
    keep matching the cutoff filter and get re-enqueued every tick.
    Better to crash startup loudly than to boot a deployment where the
    sweeper silently misbehaves."""
    class _BoomCM:
        async def __aenter__(self):
            raise RuntimeError("DDL session blew up")

        async def __aexit__(self, *_args):
            return False

    # The ``_is_updated_at_already_not_null`` probe runs before the
    # transactional block.  Give it a properly-shaped connection that
    # reports the column as already NOT NULL (so the live-safe NOT
    # NULL phases are skipped — the focus of this test is the trigger
    # phase failure).  Without this stub the probe would interact with
    # a bare MagicMock and emit an unwaited-coroutine RuntimeWarning.
    class _ProbeConn:
        class _R:
            def scalar_one_or_none(self):
                return True

        async def execute(self, _stmt, _params=None):
            return self._R()

    class _ProbeConnectCM:
        async def __aenter__(self):
            return _ProbeConn()

        async def __aexit__(self, *_args):
            return False

    fake_engine = MagicMock()
    fake_engine.begin = lambda: _BoomCM()
    fake_engine.connect = lambda: _ProbeConnectCM()
    monkeypatch.setattr(migrations, "engine", fake_engine)

    with caplog.at_level("ERROR"), pytest.raises(RuntimeError) as exc:
        await migrations._ensure_schema_extras()

    assert "Schema-extras bootstrap failed" in str(exc.value)
    assert "BEFORE UPDATE trigger" in str(exc.value)
    assert (
        "Failed to ensure knowledge_documents trigger / column constraints"
        in caplog.text
    )


@pytest.mark.asyncio
async def test_ensure_schema_extras_raises_on_index_failure(monkeypatch, caplog):
    """An index-bootstrap failure MUST also halt startup for the same
    reason — without the (status, updated_at) index, the sweeper would
    seq-scan ``knowledge_documents`` on every autonomous tick, which
    on a populated table multiplies into a multi-second query that
    starves the rest of the tick.  Better to halt startup loudly.

    Drives the steady-state branch (everything already current →
    Phase A is a no-op) so the index phase is the only place that can
    fail.  The fake raises on the ``CREATE INDEX``.
    """
    fake_engine, _begin, _connect, _iso = _build_fake_engine(
        index_create_raises=RuntimeError("Index build blew up"),
    )
    monkeypatch.setattr(migrations, "engine", fake_engine)

    with caplog.at_level("ERROR"), pytest.raises(RuntimeError) as exc:
        await migrations._ensure_schema_extras()

    assert "Schema-extras bootstrap failed" in str(exc.value)
    assert "(status, updated_at) index" in str(exc.value)
    assert (
        "Failed to ensure knowledge_documents concurrent indexes" in caplog.text
    )


# ---------------------------------------------------------------------------
# Live-safe NOT NULL — the actual fix for the Copilot-flagged
# ``ACCESS EXCLUSIVE`` lock under full-table-scan validation.
# ---------------------------------------------------------------------------


def test_live_safe_not_null_constants_use_check_pattern():
    """SQL-shape guard for the four constants that make up the live-safe
    NOT NULL sequence (CHECK NOT VALID → VALIDATE → SET NOT NULL → DROP
    CHECK).  A regression that swaps any one of these back to a bare
    ``ALTER COLUMN … SET NOT NULL`` reintroduces the ``ACCESS
    EXCLUSIVE`` full-table-scan stall on populated production tables
    — pin all four pieces here so the safety property cannot regress
    silently."""
    add_sql = migrations._KB_DOCUMENT_ADD_CHECK_NOT_VALID_UPDATED_AT_SQL
    assert "ADD CONSTRAINT" in add_sql
    assert "CHECK (updated_at IS NOT NULL)" in add_sql
    assert "NOT VALID" in add_sql, (
        "ADD CONSTRAINT MUST use NOT VALID — without it Postgres "
        "runs a full-table scan under ACCESS EXCLUSIVE, defeating "
        "the live-safe pattern; got: " + add_sql
    )
    assert (
        migrations._KB_DOCUMENT_UPDATED_AT_CHECK_CONSTRAINT_NAME in add_sql
    )

    validate_sql = migrations._KB_DOCUMENT_VALIDATE_CHECK_UPDATED_AT_SQL
    assert "VALIDATE CONSTRAINT" in validate_sql
    assert (
        migrations._KB_DOCUMENT_UPDATED_AT_CHECK_CONSTRAINT_NAME
        in validate_sql
    )

    drop_sql = migrations._KB_DOCUMENT_DROP_CHECK_UPDATED_AT_SQL
    assert "DROP CONSTRAINT" in drop_sql
    assert "IF EXISTS" in drop_sql, (
        "DROP CONSTRAINT MUST use IF EXISTS so a partial-failure "
        "recovery (CHECK still present from a prior crashed boot) "
        "doesn't raise on the cleanup; got: " + drop_sql
    )

    # The transient CHECK constraint must use the SAME name in both
    # the migration and the bootstrap so a partial migration that left
    # the CHECK around doesn't conflict on the next bootstrap run.
    assert (
        migrations._KB_DOCUMENT_UPDATED_AT_CHECK_CONSTRAINT_NAME
        == "ck_knowledge_documents_updated_at_not_null"
    )


@pytest.mark.asyncio
async def test_ensure_schema_extras_runs_live_safe_not_null_when_column_nullable(
    monkeypatch,
):
    """When the ``attnotnull`` probe reports the column is still
    nullable, the bootstrap MUST run the full live-safe NOT NULL
    sequence:

      1. Inside the transactional block (after DEFAULT + UPDATE
         backfill + trigger DDL): ADD CONSTRAINT CHECK NOT VALID.
      2. On a fresh AUTOCOMMIT connection: VALIDATE CONSTRAINT — the
         SHARE UPDATE EXCLUSIVE lock acquired during the validation
         scan is the whole point of the live-safe pattern.
      3. In a separate transactional block: SET NOT NULL (fast
         metadata-only because PG ≥ 12 uses the validated CHECK to
         skip the full-table scan), then DROP CONSTRAINT.

    A regression that drops the live-safe pattern and reverts to a
    bare ``ALTER COLUMN … SET NOT NULL`` reintroduces the
    ``ACCESS EXCLUSIVE`` full-table-scan stall — pin the four-step
    shape here so it can't regress silently."""
    # Drive the stamped-fresh install scenario: column is nullable on
    # entry, no CHECK constraint exists yet, the bootstrap must add +
    # validate + set NOT NULL + drop CHECK across the three connection
    # phases.  The stateful tracker flips the probe's response on
    # each ADD / VALIDATE / DROP so each subsequent probe sees the
    # exact catalog state production would.
    tracker = _StatefulCheckTracker(initial=None)
    fake_engine, begin_executed, connect_executed, isolation_calls = (
        _build_fake_engine(
            column_already_not_null=False,
            default_already_set=False,
            trigger_already_current=False,
            check_tracker=tracker,
        )
    )
    monkeypatch.setattr(migrations, "engine", fake_engine)

    await migrations._ensure_schema_extras()

    # Transactional phase: lock_timeout + statement_timeout guards,
    # then DEFAULT, UPDATE backfill, FUNCTION, DROP TRIGGER, CREATE
    # TRIGGER, lookup CHECK state, ADD CONSTRAINT NOT VALID — plus the
    # second transactional block which runs SET NOT NULL + DROP
    # CONSTRAINT.
    assert "ADD CONSTRAINT" in " ".join(begin_executed), (
        f"live-safe path MUST issue ADD CONSTRAINT CHECK NOT VALID "
        f"in the transactional block before VALIDATE; got: "
        f"{begin_executed}"
    )
    assert "NOT VALID" in " ".join(begin_executed)
    assert "CHECK (updated_at IS NOT NULL)" in " ".join(begin_executed)
    # The final txn block writes SET NOT NULL + DROP CONSTRAINT.
    assert any("SET NOT NULL" in s for s in begin_executed), (
        f"live-safe path MUST run SET NOT NULL after VALIDATE; got: "
        f"{begin_executed}"
    )
    assert any(
        "DROP CONSTRAINT IF EXISTS" in s for s in begin_executed
    ), (
        f"live-safe path MUST clean up the transient CHECK after "
        f"SET NOT NULL succeeds; got: {begin_executed}"
    )

    # AUTOCOMMIT path: VALIDATE CONSTRAINT, SET timeout, invalid-check,
    # CREATE INDEX.  The VALIDATE must come BEFORE the index path
    # (different connections in the production code, but both go
    # through ``engine.connect()`` + autocommit so the test sees them
    # in order).
    sql_only = [sql for sql, _params in connect_executed]
    validate_index = next(
        (i for i, s in enumerate(sql_only) if "VALIDATE CONSTRAINT" in s),
        None,
    )
    create_index = next(
        (i for i, s in enumerate(sql_only) if "CREATE INDEX" in s),
        None,
    )
    assert validate_index is not None, (
        f"live-safe path MUST run VALIDATE CONSTRAINT on an AUTOCOMMIT "
        f"connection (so the SHARE UPDATE EXCLUSIVE lock allows "
        f"concurrent reads + writes); got: {sql_only}"
    )
    assert create_index is not None
    assert validate_index < create_index, (
        f"VALIDATE CONSTRAINT MUST run BEFORE the index build "
        f"(otherwise the index phase isn't reached and the bootstrap "
        f"is in an inconsistent state); got: {sql_only}"
    )
    # Two AUTOCOMMIT isolation flips: one for VALIDATE, one for the
    # index.  A regression that runs VALIDATE inside the transactional
    # block would only show 1 AUTOCOMMIT flip (the index) and the SUEx
    # lock would escalate.
    assert isolation_calls == ["AUTOCOMMIT", "AUTOCOMMIT"], (
        f"live-safe path MUST flip TWO connections to AUTOCOMMIT — "
        f"one for VALIDATE CONSTRAINT, one for the index builds; "
        f"got isolation_calls: {isolation_calls}"
    )


@pytest.mark.asyncio
async def test_ensure_schema_extras_skips_live_safe_when_column_already_not_null(
    monkeypatch,
):
    """Symmetry guard for the live-safe path: when the column is
    already NOT NULL (fresh installs after ``create_all``, existing
    installs after the Alembic migration), the bootstrap MUST NOT
    run the live-safe sequence — every boot would otherwise pay the
    cost of the VALIDATE scan even when there's nothing to validate.
    """
    fake_engine, begin_executed, connect_executed, isolation_calls = (
        _build_fake_engine(column_already_not_null=True)
    )
    monkeypatch.setattr(migrations, "engine", fake_engine)

    await migrations._ensure_schema_extras()

    # No ADD CONSTRAINT / VALIDATE / SET NOT NULL / DROP CONSTRAINT
    # anywhere on the short-circuit path.
    all_sql = " ".join(begin_executed) + " ".join(
        sql for sql, _params in connect_executed
    )
    forbidden = (
        "ADD CONSTRAINT",
        "VALIDATE CONSTRAINT",
        "SET NOT NULL",
        "DROP CONSTRAINT",
    )
    for needle in forbidden:
        assert needle not in all_sql, (
            f"short-circuit path MUST NOT run live-safe NOT NULL "
            f"statements when the column is already NOT NULL; saw "
            f"{needle!r} in executed SQL"
        )
    # Exactly one AUTOCOMMIT flip — the index build only.
    assert isolation_calls == ["AUTOCOMMIT"], (
        f"short-circuit path MUST flip exactly ONE connection to "
        f"AUTOCOMMIT (the index build); got: {isolation_calls}"
    )


# ---------------------------------------------------------------------------
# Crash-loop hardening: idempotency skip + lock_timeout +
# retry/backoff so Phase A can't crash-loop the deploy on lock contention.
# ---------------------------------------------------------------------------


def test_phase_a_lock_timeout_is_short_and_local():
    """Phase A MUST guard the DDL with a SHORT, transaction-local
    ``lock_timeout`` so a blocked statement fails fast (SQLSTATE 55P03)
    instead of sitting at the head of the lock queue for the full 30 s
    ``statement_timeout`` — the exact behaviour that turned a transient
    lock conflict into the SQLSTATE 57014 crash-loop.  Statement timeout
    is lifted (``SET LOCAL statement_timeout = 0``) so a genuine backfill
    can still complete; contention is bounded by ``lock_timeout``."""
    lock_sql = migrations._PHASE_A_SET_LOCK_TIMEOUT_SQL
    assert "SET LOCAL lock_timeout" in lock_sql, (
        "lock_timeout MUST be SET LOCAL (transaction-scoped) so it never "
        "leaks to other pooled users of the connection; got: " + lock_sql
    )
    assert migrations._PHASE_A_LOCK_TIMEOUT_MS <= 10_000, (
        "Phase A lock_timeout MUST be short (a few seconds, well under "
        "the 30 s statement_timeout) so a blocked DDL fails fast instead "
        f"of stalling traffic; got {migrations._PHASE_A_LOCK_TIMEOUT_MS}ms"
    )
    assert (
        "SET LOCAL statement_timeout = 0"
        in migrations._PHASE_A_DISABLE_STATEMENT_TIMEOUT_SQL
    )


@pytest.mark.asyncio
async def test_phase_a_is_noop_when_everything_already_current(monkeypatch):
    """The structural crash-loop fix: when the column DEFAULT is already
    ``clock_timestamp()``, the trigger is present + current, and the
    column is already NOT NULL (the universal steady state after the
    first successful deploy), Phase A MUST NOT open a transaction at all
    — so it takes NO lock on ``knowledge_documents`` and the SQLSTATE
    57014 crash-loop is structurally impossible."""
    fake_engine, begin_executed, _connect, isolation_calls = (
        _build_fake_engine(
            column_already_not_null=True,
            default_already_set=True,
            trigger_already_current=True,
        )
    )
    monkeypatch.setattr(migrations, "engine", fake_engine)

    await migrations._ensure_schema_extras()

    assert begin_executed == [], (
        f"steady-state boot MUST NOT open a Phase A transaction (no lock "
        f"on knowledge_documents); got: {begin_executed}"
    )
    # Only the concurrent-index AUTOCOMMIT path runs.
    assert isolation_calls == ["AUTOCOMMIT"], (
        f"steady-state boot should only flip the index connection to "
        f"AUTOCOMMIT; got: {isolation_calls}"
    )


@pytest.mark.asyncio
async def test_phase_a_skips_set_default_when_already_applied(monkeypatch):
    """Symmetry guard: when ONLY the DEFAULT is already applied (trigger
    still missing), Phase A MUST skip the ``SET DEFAULT`` (the ACCESS
    EXCLUSIVE statement) but still (re)create the trigger.  A regression
    that runs ``SET DEFAULT`` unconditionally reintroduces the lock the
    crash-loop hinged on."""
    fake_engine, begin_executed, _connect, _iso = _build_fake_engine(
        column_already_not_null=True,
        default_already_set=True,
        trigger_already_current=False,
    )
    monkeypatch.setattr(migrations, "engine", fake_engine)

    await migrations._ensure_schema_extras()

    joined = " ".join(begin_executed)
    assert "SET DEFAULT" not in joined, (
        f"Phase A MUST skip SET DEFAULT when the default is already "
        f"applied (no ACCESS EXCLUSIVE lock); got: {begin_executed}"
    )
    assert "CREATE TRIGGER" in joined, (
        f"Phase A MUST still (re)create the trigger when it is missing; "
        f"got: {begin_executed}"
    )


@pytest.mark.parametrize("code", ["55P03", "57014"])
def test_is_retryable_ddl_lock_error_matches_sqlstate(code):
    """``55P03`` (lock_timeout) and ``57014`` (statement_timeout) are the
    two transient contention SQLSTATEs the Phase A retry loop must treat
    as retryable."""

    class _CodedError(Exception):
        sqlstate = code

    assert migrations._is_retryable_ddl_lock_error(_CodedError("x")) is True


def test_is_retryable_ddl_lock_error_matches_pgcode_attr():
    """Detection MUST also read ``pgcode`` (some driver/wrapper layers
    expose the SQLSTATE under that name)."""

    class _CodedError(Exception):
        pgcode = "55P03"

    assert migrations._is_retryable_ddl_lock_error(_CodedError("x")) is True


def test_is_retryable_ddl_lock_error_follows_exception_chain():
    """SQLAlchemy wraps the driver error, so detection MUST walk the
    ``__cause__`` / ``__context__`` chain to find the SQLSTATE."""

    class _InnerError(Exception):
        sqlstate = "57014"

    try:
        try:
            raise _InnerError("canceled")
        except _InnerError as inner:
            raise RuntimeError("wrapped by SQLAlchemy") from inner
    except RuntimeError as outer:
        assert migrations._is_retryable_ddl_lock_error(outer) is True


def test_is_retryable_ddl_lock_error_matches_message_substring():
    """When no structured SQLSTATE is available, fall back to a message
    probe so detection stays robust."""
    exc = RuntimeError("ERROR: canceling statement due to statement timeout")
    assert migrations._is_retryable_ddl_lock_error(exc) is True


def test_is_retryable_ddl_lock_error_rejects_generic_error():
    """A genuine, non-lock error MUST NOT be retried — the caller hard-
    fails immediately rather than burning attempts on a hopeless DDL."""
    assert migrations._is_retryable_ddl_lock_error(RuntimeError("boom")) is False


@pytest.mark.asyncio
async def test_phase_a_retries_on_lock_timeout_then_succeeds(monkeypatch, caplog):
    """The genuine first-deploy path MUST ride out transient lock
    contention: two ``lock_timeout`` failures, then a clean success on
    the third attempt, with exponential backoff between attempts — not a
    crash-loop."""
    sleeps: list[float] = []

    async def _fake_sleep(delay):
        sleeps.append(delay)

    monkeypatch.setattr(migrations.asyncio, "sleep", _fake_sleep)

    class _LockTimeoutError(Exception):
        sqlstate = "55P03"

    fake_engine, begin_executed, _connect, _iso = _build_fake_engine(
        column_already_not_null=True,
        default_already_set=False,
        trigger_already_current=False,
        begin_outcomes=[
            _LockTimeoutError("canceling statement due to lock timeout"),
            _LockTimeoutError("canceling statement due to lock timeout"),
            None,
        ],
    )
    monkeypatch.setattr(migrations, "engine", fake_engine)

    with caplog.at_level("WARNING"):
        await migrations._ensure_schema_extras()

    # Two backoff sleeps (after attempts 1 and 2), exponential.
    assert sleeps == [
        migrations._PHASE_A_RETRY_BASE_DELAY,
        migrations._PHASE_A_RETRY_BASE_DELAY * 2,
    ], f"expected exponential backoff sleeps; got {sleeps}"
    # The third (successful) attempt actually ran the DDL.
    assert any("SET DEFAULT clock_timestamp()" in s for s in begin_executed), (
        f"the successful retry MUST have run the Phase A DDL; got: "
        f"{begin_executed}"
    )
    assert any("retrying in" in r.message for r in caplog.records), (
        "a retry MUST log a warning so the contention is observable"
    )


@pytest.mark.asyncio
async def test_phase_a_raises_after_max_attempts_under_sustained_lock(
    monkeypatch, caplog
):
    """Sustained lock contention MUST eventually hard-fail (after the
    bounded attempts) rather than retry forever — preserving the
    "refuse to start with a misbehaving sweeper" invariant, but only
    after genuinely trying to ride out the contention."""

    async def _fake_sleep(_delay):
        return None

    monkeypatch.setattr(migrations.asyncio, "sleep", _fake_sleep)

    class _LockTimeoutError(Exception):
        sqlstate = "55P03"

    outcomes: list[BaseException | None] = [
        _LockTimeoutError("lock timeout")
        for _ in range(migrations._PHASE_A_MAX_ATTEMPTS)
    ]
    fake_engine, *_rest = _build_fake_engine(
        column_already_not_null=True,
        default_already_set=False,
        trigger_already_current=False,
        begin_outcomes=outcomes,
    )
    monkeypatch.setattr(migrations, "engine", fake_engine)

    with caplog.at_level("ERROR"), pytest.raises(RuntimeError) as exc:
        await migrations._ensure_schema_extras()

    assert "Schema-extras bootstrap failed" in str(exc.value)
    assert f"{migrations._PHASE_A_MAX_ATTEMPTS} attempts" in str(exc.value), (
        f"the exhausted-retries error MUST mention the attempt count; "
        f"got: {exc.value}"
    )


@pytest.mark.asyncio
async def test_phase_a_raises_immediately_on_non_retryable_error(monkeypatch):
    """A non-lock error (a genuine schema bug) MUST fail immediately with
    the trigger/column message and NO retry sleeps — retrying a hopeless
    DDL would only delay the loud crash."""
    sleeps: list[float] = []

    async def _fake_sleep(delay):
        sleeps.append(delay)

    monkeypatch.setattr(migrations.asyncio, "sleep", _fake_sleep)

    fake_engine, *_rest = _build_fake_engine(
        column_already_not_null=True,
        default_already_set=False,
        trigger_already_current=False,
        begin_outcomes=[RuntimeError("relation does not exist")],
    )
    monkeypatch.setattr(migrations, "engine", fake_engine)

    with pytest.raises(RuntimeError) as exc:
        await migrations._ensure_schema_extras()

    assert "BEFORE UPDATE trigger" in str(exc.value)
    assert sleeps == [], (
        f"non-retryable errors MUST fail fast with no backoff sleeps; "
        f"got: {sleeps}"
    )
