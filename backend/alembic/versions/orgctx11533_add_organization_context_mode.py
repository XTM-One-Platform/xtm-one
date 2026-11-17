"""add the organization context cascade (platform > agent > assignment)

``platform_settings.organization_context_enabled`` (default true),
``agents.organization_context_mode`` and ``assignments.organization_context_mode``
(default ``inherit``) decide whether a turn's system prompt starts with the
organization summary (#11533; ``services/organization_context_injection.py``).
``organization_context_categories`` (platform: a JSON list, default ``[]``;
agent and assignment: NULL = inherit) names the categories preloaded after
it (``services/organization_context_categories.py``).

Each column is a constant-default ``ADD COLUMN`` (metadata-only on PG 11+, no
rewrite; a nullable column has no default at all; dev-docs/reference/migrations-safety.md),
following the canonical lock-safe recipe: idempotency probe ->
terminate_lock_holders -> set_lock_timeout -> guarded DDL. The defaults keep
every existing agent and assignment on today's behaviour for fleet runs, and
every existing agent, seeded or custom, ON (inherit of a platform default
that is on): the upgrade turns nothing off.

Revision ID: orgctx11533
Revises: svcacct0001
Create Date: 2026-11-13 06:30:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.core.migration_ddl import set_lock_timeout, terminate_lock_holders

revision: str = "orgctx11533"
# NOT a constant: must always be main's CURRENT head, re-pointed on every rebase
# (backend/AGENTS.md > "Database migrations (safety)", rule 1).
down_revision: str | Sequence[str] | None = "svcacct0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_COLUMNS: tuple[tuple[str, str, str], ...] = (
    ("platform_settings", "organization_context_enabled", "BOOLEAN NOT NULL DEFAULT true"),
    ("agents", "organization_context_mode", "VARCHAR(10) NOT NULL DEFAULT 'inherit'"),
    ("assignments", "organization_context_mode", "VARCHAR(10) NOT NULL DEFAULT 'inherit'"),
    ("platform_settings", "organization_context_categories", "JSON NOT NULL DEFAULT '[]'::json"),
    ("agents", "organization_context_categories", "JSON"),
    ("assignments", "organization_context_categories", "JSON"),
)


def _has_column(conn, table: str, column: str) -> bool:
    return bool(
        conn.execute(
            sa.text(
                "SELECT 1 FROM information_schema.columns "
                "WHERE table_schema = current_schema() "
                "AND table_name = :table AND column_name = :column"
            ),
            {"table": table, "column": column},
        ).first()
    )


def upgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    for table, column, ddl in _COLUMNS:
        # Fresh installs run create_all + stamp head; this body never executes.
        if not inspector.has_table(table) or _has_column(conn, table, column):
            continue
        terminate_lock_holders(conn, table)
        set_lock_timeout(conn)
        op.execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {column} {ddl}")


def downgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    for table, column, _ddl in reversed(_COLUMNS):
        if not inspector.has_table(table) or not _has_column(conn, table, column):
            continue
        terminate_lock_holders(conn, table)
        set_lock_timeout(conn)
        op.execute(f"ALTER TABLE {table} DROP COLUMN IF EXISTS {column}")
