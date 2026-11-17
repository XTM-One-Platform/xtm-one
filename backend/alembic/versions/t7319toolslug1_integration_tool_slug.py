"""stable tool-name suffix per integration instance

Adds ``integrations.tool_slug``: the suffix an instance's tools carry
(``<tool>__<slug>``) when an agent binds several integrations of one provider.
Until now the suffix was recomputed from the display name at every tool build,
so renaming an integration renamed its tools under every persona, skill and
assignment gate citing them. The backfill writes, for every existing row, the
slug the display name yields today (``derive_tool_slug``, the algorithm the
tool build used), so no tool name changes at deploy; from then on a rename
leaves it alone.

**Lock safety.** ``ADD COLUMN`` of a nullable column without default is
metadata-only, but takes a brief ``ACCESS EXCLUSIVE`` lock on ``integrations``,
a hot table: canonical recipe (idempotency probe, ``terminate_lock_holders``,
``set_lock_timeout``, guarded DDL). The backfill updates rows by primary key in
bounded batches; ``integrations`` holds one row per connected account, so it
stays small. Idempotent: only rows whose ``tool_slug`` is NULL are written, so
an interrupted run resumes and a re-run is a no-op. Fresh installs run
``create_all`` + ``stamp head``: the body never executes there.

Revision ID: t7319toolslug1
Revises: w7245filefleet1
Create Date: 2026-11-02 11:30:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.core.integration_tool_slug import derive_tool_slug
from app.core.migration_ddl import set_lock_timeout, terminate_lock_holders

revision: str = "t7319toolslug1"
# NOT a constant: must always be main's CURRENT head, re-pointed whenever main
# gains a migration (backend/AGENTS.md > "Database migrations (safety)", rule 1).
down_revision: str | Sequence[str] | None = "w7245filefleet1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_BATCH_SIZE = 500


def _has_column(conn, table: str, column: str) -> bool:
    return (
        conn.execute(
            sa.text(
                "SELECT 1 FROM information_schema.columns "
                "WHERE table_schema = current_schema() "
                "AND table_name = :table AND column_name = :column"
            ),
            {"table": table, "column": column},
        ).first()
        is not None
    )


def upgrade() -> None:
    conn = op.get_bind()
    if not _has_column(conn, "integrations", "tool_slug"):
        terminate_lock_holders(conn, "integrations")
        set_lock_timeout(conn)
        op.execute("ALTER TABLE integrations ADD COLUMN IF NOT EXISTS tool_slug VARCHAR(32)")
    while True:
        rows = conn.execute(
            sa.text(
                "SELECT id, provider, name FROM integrations "
                "WHERE tool_slug IS NULL ORDER BY id LIMIT :limit"
            ),
            {"limit": _BATCH_SIZE},
        ).fetchall()
        if not rows:
            break
        for row in rows:
            conn.execute(
                sa.text("UPDATE integrations SET tool_slug = :slug WHERE id = :id AND tool_slug IS NULL"),
                {"slug": derive_tool_slug(row.name, row.provider or "", row.id), "id": row.id},
            )


def downgrade() -> None:
    conn = op.get_bind()
    if not _has_column(conn, "integrations", "tool_slug"):
        return
    terminate_lock_holders(conn, "integrations")
    set_lock_timeout(conn)
    op.execute("ALTER TABLE integrations DROP COLUMN IF EXISTS tool_slug")
