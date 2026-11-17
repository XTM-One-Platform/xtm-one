"""``create_index_in_place`` lets an upgrade land on a busy install.

A ``CREATE INDEX CONCURRENTLY`` waits for every older snapshot on the whole
database. During a rolling deploy the previous release always holds one, so
the workspace migrations' builds rode their ceiling on every boot and the
upgrade never landed. These tests pin what the replacement does instead: a
plain build, bounded by ``lock_timeout`` after the terminate pre-flight, that
takes no lock at all when the index is already valid.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from app.core.migration_ddl import DEFAULT_LOCK_TIMEOUT, create_index_in_place

_INDEX = "ix_worksets_workspace"
_TABLE = "worksets"
_DEFINITION = "(workspace_id)"

_VERSIONS = Path(__file__).resolve().parents[1] / "alembic" / "versions"


class _FakeResult:
    def __init__(self, value=None):
        self._value = value

    def scalar(self):
        return self._value

    def fetchall(self):
        return []


class _FakeConn:
    """Records every statement; reports the index state, optionally fails the build."""

    def __init__(self, *, state: bool | None = None, build_error: Exception | None = None):
        self.statements: list[str] = []
        self.params: list[dict | None] = []
        self._state = state
        self._build_error = build_error

    def execute(self, clause, params=None):
        sql = str(clause)
        self.statements.append(sql)
        self.params.append(params)
        if "i.indisvalid" in sql:
            return _FakeResult(self._state)
        if sql.startswith("CREATE") and self._build_error is not None:
            raise self._build_error
        return _FakeResult()

    def index_of(self, needle: str) -> int:
        return next(i for i, s in enumerate(self.statements) if needle in s)

    def settings(self) -> dict[str, str]:
        return {
            ("lock_timeout" if "lock_timeout" in stmt else "statement_timeout"): p["timeout"]
            for stmt, p in zip(self.statements, self.params, strict=True)
            if "set_config" in stmt and p
        }


def test_a_valid_index_takes_no_lock():
    conn = _FakeConn(state=True)

    assert create_index_in_place(conn, _INDEX, _TABLE, _DEFINITION) is True

    assert len(conn.statements) == 1
    assert "pg_stat_activity" not in conn.statements[0]


def test_an_absent_index_is_built_in_place_under_a_lock_timeout():
    conn = _FakeConn(state=None)

    assert create_index_in_place(conn, _INDEX, _TABLE, _DEFINITION) is True

    build = conn.index_of("CREATE INDEX IF NOT EXISTS ix_worksets_workspace ON worksets (workspace_id)")
    assert not any("CONCURRENTLY" in s for s in conn.statements)
    assert conn.index_of("pg_stat_activity") < build
    assert conn.settings()["lock_timeout"] == DEFAULT_LOCK_TIMEOUT
    assert conn.settings()["statement_timeout"] != "0"
    assert conn.index_of("RESET lock_timeout") > build
    assert conn.index_of("RESET statement_timeout") > build


def test_an_invalid_leftover_is_dropped_before_the_rebuild():
    conn = _FakeConn(state=False)

    assert create_index_in_place(conn, _INDEX, _TABLE, _DEFINITION) is True

    assert conn.index_of("DROP INDEX IF EXISTS ix_worksets_workspace") < conn.index_of("CREATE INDEX")


def test_a_unique_index_is_built_unique():
    conn = _FakeConn()

    create_index_in_place(conn, "uq_worksets_workspace_key", _TABLE, "(workspace_id, key)", unique=True)

    assert conn.index_of("CREATE UNIQUE INDEX IF NOT EXISTS uq_worksets_workspace_key") >= 0


def test_a_failed_performance_index_does_not_stop_startup():
    conn = _FakeConn(build_error=RuntimeError("canceling statement due to lock timeout"))

    assert create_index_in_place(conn, _INDEX, _TABLE, _DEFINITION) is False
    assert conn.index_of("RESET lock_timeout") >= 0


def test_a_failed_required_index_is_raised_for_the_runner_to_retry():
    error = RuntimeError("canceling statement due to lock timeout")
    conn = _FakeConn(build_error=error)

    with pytest.raises(RuntimeError) as raised:
        create_index_in_place(conn, _INDEX, _TABLE, _DEFINITION, required=True)

    assert raised.value is error
    assert conn.index_of("RESET statement_timeout") >= 0


@pytest.mark.parametrize(
    ("index", "table"),
    [("ix_ok; DROP TABLE users", _TABLE), (_INDEX, "worksets; DROP TABLE users")],
)
def test_non_identifiers_are_rejected(index, table):
    with pytest.raises(ValueError, match="SQL identifiers"):
        create_index_in_place(_FakeConn(), index, table, _DEFINITION)


@pytest.mark.parametrize(
    "filename",
    [
        "w2wsp3314002_workspace_conversations.py",
        "w3wsp3314003_workspace_tool_scope.py",
        "w4wsp3314004_workspace_shared_identity.py",
        "i7con1939005_investigation_continuation.py",
        "f2runix5258_index_file_attachments_assignment_run_id.py",
    ],
)
def test_the_rolling_deploy_chain_never_builds_concurrently_on_upgrade(filename):
    tree = ast.parse((_VERSIONS / filename).read_text(encoding="utf-8"))
    upgrade = next(
        node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "upgrade"
    )
    source = ast.unparse(upgrade)

    assert "CONCURRENTLY" not in source
    assert "create_index_concurrently" not in source
    assert "create_index_in_place" in source
