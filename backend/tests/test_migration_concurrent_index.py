"""``create_index_concurrently`` must never wedge startup (outage 2026-08-28).

``CREATE INDEX CONCURRENTLY`` waits for every open snapshot on the table,
without limit.  On a pre-existing hot table that wait is unbounded, and
migrations run *before* the pod is marked ready — so an unbounded build means
the platform serves a blanket 503 for as long as it waits.  That is what
``ix_assignment_runs_failure_cause`` did to prod and staging.

These tests pin the three properties that make the helper safe:

* the build is BOUNDED (a real ``statement_timeout``, never ``0``);
* a failed build is SWALLOWED, not raised — startup continues without the
  index rather than keeping the platform dark for a query plan;
* the INVALID index a killed build leaves behind is dropped, both before a
  rebuild and after a failure, because ``IF NOT EXISTS`` would otherwise match
  that corpse forever and no later deploy would ever rebuild it.
"""

from __future__ import annotations

import pytest

from app.core.migration_ddl import (
    DEFAULT_INDEX_BUILD_TIMEOUT,
    INDEX_CLEANUP_TIMEOUT,
    create_index_concurrently,
)

_INDEX = "ix_assignment_runs_failure_cause"
_TABLE = "assignment_runs"
_DEFINITION = "(failure_cause) WHERE failure_cause IS NOT NULL"


class _FakeResult:
    def __init__(self, value):
        self._value = value

    def scalar(self):
        return self._value


class _FakeConn:
    """Records every statement; optionally reports an INVALID index / fails the build."""

    def __init__(self, *, index_is_invalid: bool = False, build_error: Exception | None = None):
        self.statements: list[str] = []
        self.params: list[dict | None] = []
        self._index_is_invalid = index_is_invalid
        self._build_error = build_error

    def execute(self, clause, params=None):
        sql = str(clause)
        self.statements.append(sql)
        self.params.append(params)

        if "NOT i.indisvalid" in sql:
            result = _FakeResult(1 if self._index_is_invalid else None)
            # A drop only happens once; the probe is truthful afterwards.
            return result
        if sql.startswith("DROP INDEX CONCURRENTLY"):
            self._index_is_invalid = False
            return _FakeResult(None)
        if "CREATE INDEX CONCURRENTLY" in sql and self._build_error is not None:
            # PostgreSQL leaves an INVALID index behind when a concurrent
            # build fails or is killed — simulate that, it is the whole
            # reason the helper cleans up on the failure path.
            self._index_is_invalid = True
            raise self._build_error
        return _FakeResult(None)

    def sql_matching(self, needle: str) -> list[str]:
        return [s for s in self.statements if needle in s]


def _timeout_params(conn: _FakeConn) -> list[str]:
    return [
        p["timeout"]
        for stmt, p in zip(conn.statements, conn.params, strict=True)
        if "set_config" in stmt and "statement_timeout" in stmt and p
    ]


def test_build_is_bounded_and_timeout_is_reset():
    conn = _FakeConn()

    assert create_index_concurrently(conn, _INDEX, _TABLE, _DEFINITION) is True

    # Bounded: a real interval, never the unbounded 0 the outage ran with.
    assert _timeout_params(conn) == [DEFAULT_INDEX_BUILD_TIMEOUT]
    assert DEFAULT_INDEX_BUILD_TIMEOUT != "0"
    assert "SET statement_timeout = 0" not in " ".join(conn.statements)

    assert conn.sql_matching("CREATE INDEX CONCURRENTLY IF NOT EXISTS")
    # The session must not carry the ceiling into whatever runs next.
    assert conn.sql_matching("RESET statement_timeout")


def test_failed_build_does_not_raise_and_clears_the_invalid_residue():
    conn = _FakeConn(build_error=RuntimeError("canceling statement due to statement timeout"))

    # Swallowed: startup continues without the index.
    assert create_index_concurrently(conn, _INDEX, _TABLE, _DEFINITION) is False

    # The residue of the failed build is dropped so a later deploy (or the
    # operator running the logged SQL) starts from a clean slate.
    assert conn.sql_matching("DROP INDEX CONCURRENTLY IF EXISTS")
    assert conn.sql_matching("RESET statement_timeout")


def test_invalid_leftover_is_dropped_before_the_rebuild():
    conn = _FakeConn(index_is_invalid=True)

    assert create_index_concurrently(conn, _INDEX, _TABLE, _DEFINITION) is True

    drop_at = next(
        i for i, s in enumerate(conn.statements) if s.startswith("DROP INDEX CONCURRENTLY")
    )
    create_at = next(
        i for i, s in enumerate(conn.statements) if "CREATE INDEX CONCURRENTLY" in s
    )
    # Order matters: IF NOT EXISTS would match the corpse and skip the rebuild.
    assert drop_at < create_at


def test_valid_index_is_not_dropped():
    conn = _FakeConn(index_is_invalid=False)

    create_index_concurrently(conn, _INDEX, _TABLE, _DEFINITION)

    assert not conn.sql_matching("DROP INDEX CONCURRENTLY")


@pytest.mark.parametrize(
    ("index", "table"),
    [
        ("ix_ok; DROP TABLE users", _TABLE),
        (_INDEX, "assignment_runs; DROP TABLE users"),
    ],
)
def test_non_identifiers_are_rejected(index, table):
    with pytest.raises(ValueError, match="SQL identifiers"):
        create_index_concurrently(_FakeConn(), index, table, _DEFINITION)


def test_post_failure_cleanup_rebounds_instead_of_riding_the_build_ceiling():
    """The cleanup must not inherit the build ceiling.

    Verified against PostgreSQL: ``DROP INDEX CONCURRENTLY`` waits on the same
    snapshots as the build, so with a blocker present it is cancelled by
    ``statement_timeout`` exactly like the build is.  Inheriting the 5min build
    ceiling would make a single index cost startup twice the budget — the
    opposite of what this helper exists to do.
    """
    conn = _FakeConn(build_error=RuntimeError("canceling statement due to statement timeout"))

    assert create_index_concurrently(conn, _INDEX, _TABLE, _DEFINITION) is False

    timeouts = _timeout_params(conn)
    # No invalid leftover on entry, so the only ceilings are: the build, then
    # the post-failure cleanup re-bounding itself short.
    assert timeouts == [DEFAULT_INDEX_BUILD_TIMEOUT, INDEX_CLEANUP_TIMEOUT]


def test_pre_build_cleanup_is_bounded_before_the_build_ceiling_is_set():
    """A leftover corpse must not be dropped unbounded either.

    The pre-build drop runs before any ceiling is in place, so without its own
    bound it would inherit whatever the session carries — in the worst case no
    limit at all, hanging startup before the build even starts.
    """
    conn = _FakeConn(index_is_invalid=True)

    assert create_index_concurrently(conn, _INDEX, _TABLE, _DEFINITION) is True

    timeouts = _timeout_params(conn)
    assert timeouts == [INDEX_CLEANUP_TIMEOUT, DEFAULT_INDEX_BUILD_TIMEOUT]
