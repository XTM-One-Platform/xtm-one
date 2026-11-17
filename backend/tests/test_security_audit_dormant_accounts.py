"""The dormant-account check reads its sign-ins through their partial index.

``audit_logs`` is never pruned: the last sign-in reads stay cheap only while
``security_audit._completed_sign_in()`` is exactly the predicate of
``ix_audit_logs_sign_ins``, written into the SQL (a plan cannot use a partial
index whose predicate is a bound parameter). The behaviour itself is read on
PostgreSQL in ``test_security_audit_dormant_accounts_postgres.py``.
"""

from __future__ import annotations

import pathlib

from sqlalchemy import select
from sqlalchemy.dialects import postgresql

from app.api.v1 import security_audit
from app.models.audit_log import AuditLog

PREDICATE = "action = 'login' AND entity_type = 'user'"
MIGRATION = pathlib.Path(__file__).parents[1] / "alembic" / "versions" / "a4732signin1_add_audit_log_sign_in_index.py"


def test_the_sign_in_read_is_the_partial_index_predicate_written_into_the_sql():
    compiled = select(AuditLog.id).where(security_audit._completed_sign_in()).compile(
        dialect=postgresql.dialect(), compile_kwargs={"render_postcompile": True}
    )
    assert str(compiled).split("WHERE", 1)[1].strip().replace("audit_logs.", "") == PREDICATE
    assert compiled.params == {}


def test_the_model_and_the_migration_declare_that_predicate():
    (index,) = [index for index in AuditLog.__table__.indexes if index.name == "ix_audit_logs_sign_ins"]
    assert [column.name for column in index.columns] == ["user_id", "created_at"]
    assert str(index.dialect_options["postgresql"]["where"]) == PREDICATE
    source = MIGRATION.read_text(encoding="utf-8")
    assert '_INDEX = "ix_audit_logs_sign_ins"' in source
    assert f'_DEFINITION = "(user_id, created_at) WHERE {PREDICATE}"' in source


def test_the_migration_bounds_the_build_on_the_hot_audit_table():
    """Every audited action writes ``audit_logs``: the build goes through the bounded helper,
    never an unbounded wait on old snapshots in the startup path."""
    source = MIGRATION.read_text(encoding="utf-8")
    assert "create_index_concurrently(conn, _INDEX, _TABLE, _DEFINITION)" in source
    assert "statement_timeout" not in source
