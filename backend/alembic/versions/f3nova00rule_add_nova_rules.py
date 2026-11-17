"""add nova_rules for active prompt-safety guardrails (Nova framework)

Creates ``nova_rules`` — one row per Nova ``.nov`` detection rule with a
per-rule block/log action and the standard three-tier visibility columns.
Brand-new table: no inline ``REFERENCES`` (the user FK is added separately,
``NOT VALID``, under the canonical recipe); indexes build ``CONCURRENTLY``.

Object names match what ``create_all`` emits on a fresh install.

``f3nova00rule`` was cut from ``a7f3run0003`` while another PR cut
``f9u2fav0001`` from the same parent, and both merged an hour apart on
2026-08-21 — so the history had **two heads** (``f9u2fav0001`` and this
branch's tip ``g1novasrc00``). Alembic refuses ``upgrade head`` with an
ambiguous tip and ``tests/test_migration_integrity.py::test_single_head``
fails for every branch based on that history.

The fork is repaired by chaining rather than by a merge revision: a
two-parent merge keeps the head count at one but leaves the middle of the
chain branched, which kills the Migrations job's step-by-step ``upgrade +1``
walk with "Ambiguous walk" (see ``f7b3mrg0001``, repaired the same way).

The Nova branch is chained *behind* the favorites branch, not the other way
round: ``f9u2fav0001`` merged first, so deployments upgraded in that window
are already stamped at it. Chaining favorites on top would make these two
Nova revisions ancestors of a revision those databases have already applied,
and their tables would never be created.

Revision ID: f3nova00rule
Revises: f9u2fav0001
Create Date: 2026-08-12 12:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.core.migration_ddl import set_lock_timeout, terminate_lock_holders

# revision identifiers, used by Alembic.
revision: str = "f3nova00rule"
down_revision: str | None = "f9u2fav0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_NOVA_RULES = "nova_rules"

_FOREIGN_KEYS: tuple[tuple[str, str, str, str, str], ...] = (
    (
        "nova_rules_user_id_fkey",
        _NOVA_RULES,
        "user_id",
        "users",
        """
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_constraint
                WHERE conname = 'nova_rules_user_id_fkey'
            ) THEN
                ALTER TABLE nova_rules
                    ADD CONSTRAINT nova_rules_user_id_fkey
                    FOREIGN KEY (user_id)
                    REFERENCES users(id) ON DELETE CASCADE
                    NOT VALID;
            END IF;
        END $$;
        """,
    ),
)

_INDEXES: tuple[tuple[str, str, str], ...] = (
    ("ix_nova_rules_user_id", _NOVA_RULES, "user_id"),
    ("ix_nova_rules_is_company_managed", _NOVA_RULES, "is_company_managed"),
    ("ix_nova_rules_enabled", _NOVA_RULES, "enabled"),
)


def _table_exists(conn, table: str) -> bool:
    return bool(
        conn.execute(
            sa.text(
                "SELECT 1 FROM information_schema.tables "
                "WHERE table_schema = current_schema() AND table_name = :table"
            ),
            {"table": table},
        ).scalar()
    )


def _column_exists(conn, table: str, column: str) -> bool:
    return bool(
        conn.execute(
            sa.text(
                "SELECT 1 FROM information_schema.columns "
                "WHERE table_schema = current_schema() "
                "AND table_name = :table AND column_name = :column"
            ),
            {"table": table, "column": column},
        ).scalar()
    )


def _constraint_exists(conn, name: str) -> bool:
    return bool(
        conn.execute(
            sa.text("SELECT 1 FROM pg_constraint WHERE conname = :name"),
            {"name": name},
        ).scalar()
    )


def upgrade() -> None:
    conn = op.get_bind()

    op.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {_NOVA_RULES} (
            id UUID NOT NULL,
            user_id UUID,
            name VARCHAR(200) NOT NULL,
            description TEXT NOT NULL DEFAULT '',
            category VARCHAR(100) NOT NULL DEFAULT '',
            action VARCHAR(20) NOT NULL DEFAULT 'log',
            rule_text TEXT NOT NULL DEFAULT '',
            enabled BOOLEAN NOT NULL DEFAULT true,
            tags JSON,
            is_company_managed BOOLEAN NOT NULL DEFAULT false,
            shared_group_ids JSON,
            shared_group_permissions JSON,
            created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
            updated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
            CONSTRAINT nova_rules_pkey PRIMARY KEY (id),
            CONSTRAINT ck_nova_rules_action CHECK (action IN ('block','log'))
        )
        """
    )

    for constraint, table, column, referenced, ddl in _FOREIGN_KEYS:
        if not _column_exists(conn, table, column):
            continue
        if _constraint_exists(conn, constraint):
            continue
        if not _table_exists(conn, referenced):
            continue
        terminate_lock_holders(conn, table)
        terminate_lock_holders(conn, referenced)
        set_lock_timeout(conn)
        op.execute(ddl)

    with op.get_context().autocommit_block():
        op.execute("SET statement_timeout = 0")
        for index_name, table, column in _INDEXES:
            if not _column_exists(conn, table, column):
                continue
            op.execute(
                f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {index_name} "
                f"ON {table} ({column})"
            )


def downgrade() -> None:
    conn = op.get_bind()

    with op.get_context().autocommit_block():
        for index_name, _table, _column in _INDEXES:
            op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {index_name}")

    if _table_exists(conn, _NOVA_RULES):
        terminate_lock_holders(conn, _NOVA_RULES)
        set_lock_timeout(conn)
        op.execute(f"DROP TABLE IF EXISTS {_NOVA_RULES}")
