"""organization_context_versions: source ai_draft and the sources column (#9484, S-a)

A version saved from the accepted proposals of a "Draft with AI"
(``dev-docs/ctem-fleets.md`` section 6.1) carries the source ``ai_draft``
and, in the new nullable ``sources`` column (JSONB, NULL on every existing
row), the accepted proposals' ``{field: {source, basis}}`` shown in the
version history. Every existing row keeps its source and reads as before.

Lock safety: idempotency probe + early return, then
``terminate_lock_holders`` + ``set_lock_timeout``; the column is a
metadata-only ``ADD COLUMN IF NOT EXISTS`` of a nullable column with no
default; the check constraint is dropped and re-added ``NOT VALID`` then
validated (the existing rows already satisfy the wider list).
Reversible: ``downgrade`` drops the column and restores the three sources
(``ai_draft`` rows become ``api``, the closest existing source).

Revision ID: o9484aidraft1
Revises: a9437wiask01
Create Date: 2026-11-07 06:45:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.core.migration_ddl import set_lock_timeout, terminate_lock_holders

revision: str = "o9484aidraft1"
# NOT a constant: must always be main's CURRENT head, re-pointed on every rebase
# (backend/AGENTS.md > "Database migrations (safety)", rule 1).
down_revision: str | None = "a9437wiask01"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "organization_context_versions"
_COLUMN = "sources"
_CHECK = "ck_organization_context_versions_source_valid"


def _table_exists(conn) -> bool:
    return bool(
        conn.execute(
            sa.text(
                "SELECT 1 FROM information_schema.tables WHERE table_schema = current_schema() AND table_name = :table"
            ),
            {"table": _TABLE},
        ).scalar()
    )


def _column_exists(conn) -> bool:
    return bool(
        conn.execute(
            sa.text(
                "SELECT 1 FROM information_schema.columns "
                "WHERE table_schema = current_schema() "
                "AND table_name = :table AND column_name = :column"
            ),
            {"table": _TABLE, "column": _COLUMN},
        ).scalar()
    )


def _check_allows_ai_draft(conn) -> bool:
    definition = conn.execute(
        sa.text(
            "SELECT pg_get_constraintdef(c.oid) FROM pg_constraint c "
            "JOIN pg_class t ON t.oid = c.conrelid "
            "WHERE t.relname = :table AND c.conname = :name"
        ),
        {"table": _TABLE, "name": _CHECK},
    ).scalar()
    return bool(definition) and "ai_draft" in str(definition)


def _set_check(conn, sources: tuple[str, ...]) -> None:
    listed = ", ".join(f"'{s}'" for s in sources)
    conn.execute(sa.text(f"ALTER TABLE {_TABLE} DROP CONSTRAINT IF EXISTS {_CHECK}"))
    conn.execute(sa.text(f"ALTER TABLE {_TABLE} ADD CONSTRAINT {_CHECK} CHECK (source IN ({listed})) NOT VALID"))
    conn.execute(sa.text(f"ALTER TABLE {_TABLE} VALIDATE CONSTRAINT {_CHECK}"))


def upgrade() -> None:
    conn = op.get_bind()
    # Fresh install: create_all builds the table from the model and stamps head.
    if not _table_exists(conn):
        return
    if _column_exists(conn) and _check_allows_ai_draft(conn):
        return
    terminate_lock_holders(conn, _TABLE)
    set_lock_timeout(conn)
    conn.execute(sa.text(f"ALTER TABLE {_TABLE} ADD COLUMN IF NOT EXISTS {_COLUMN} JSONB"))
    if not _check_allows_ai_draft(conn):
        _set_check(conn, ("form", "draft", "api", "ai_draft"))


def downgrade() -> None:
    conn = op.get_bind()
    if not _table_exists(conn):
        return
    terminate_lock_holders(conn, _TABLE)
    set_lock_timeout(conn)
    conn.execute(sa.text("UPDATE organization_context_versions SET source = 'api' WHERE source = 'ai_draft'"))
    _set_check(conn, ("form", "draft", "api"))
    conn.execute(sa.text(f"ALTER TABLE {_TABLE} DROP COLUMN IF EXISTS {_COLUMN}"))
