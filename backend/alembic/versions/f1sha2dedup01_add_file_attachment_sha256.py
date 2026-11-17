"""add file_attachments.sha256 for conversation-scoped tool-file dedup

A tool that persists a file it downloaded or generated (``download_notion_file``,
``generate_file``, the code interpreter, ...) goes through
``builtin_tools._shared._persist_generated_file``. Until now every call wrote a
new ``file_attachments`` row and a new S3 object, even for bytes already stored
in the same conversation: trace 722375f1 (2026-09-24) re-downloaded three Notion
PNGs 28 times under cosmetic filename changes and persisted 84 byte-identical
files from one turn.

The persistence path now records the SHA-256 of the bytes and, before writing,
looks for a tool-produced row with the same digest in the SAME conversation -
reusing it instead of creating another. User uploads never set the column and
pre-existing rows carry NULL, so neither takes part in the match.

Lock safety: canonical recipe - idempotency probe + early return, then
``terminate_lock_holders`` + ``set_lock_timeout``, then a metadata-only
``ADD COLUMN IF NOT EXISTS`` (nullable, no default -> no rewrite). The partial
composite index is built ``CONCURRENTLY`` outside the lock_timeout belt through
``create_index_concurrently``: ``file_attachments`` is a PRE-EXISTING, written-to
table, so the build waits on every open snapshot and only a ``statement_timeout``
bounds that wait (the 2026-08-28 ``assignment_runs`` outage, see the helper's
module note). The helper bounds the build, drops the INVALID residue of an
interrupted one (which ``IF NOT EXISTS`` would otherwise match forever) and lets
startup continue without the index; the boot-time model-index reconciler
(``app.core.index_maintenance``) then builds the model-declared index in place.

Revision ID: f1sha2dedup01
Revises: o2osd3696001
Create Date: 2026-09-24 15:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.core.migration_ddl import (
    create_index_concurrently,
    set_lock_timeout,
    terminate_lock_holders,
)

# revision identifiers, used by Alembic.
revision: str = "f1sha2dedup01"
# NOT a constant: must always be main's CURRENT head, re-pointed on every rebase
# (backend/AGENTS.md > "Database migrations (safety)", rule 1).
down_revision: str | None = "o2osd3696001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "file_attachments"
_COLUMN = "sha256"
_INDEX = "ix_file_attachments_conversation_sha256"


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
    # Scoped to the active schema: information_schema.columns spans ALL
    # schemas, so a same-named table elsewhere must not make the probe skip
    # the ADD COLUMN in the schema being migrated.
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


def upgrade() -> None:
    conn = op.get_bind()
    if not _table_exists(conn, _TABLE):
        # Fresh install: create_all builds the full table (column and index
        # included) and stamps head - this body never needs to run.
        return

    if not _column_exists(conn, _TABLE, _COLUMN):
        terminate_lock_holders(conn, _TABLE)
        set_lock_timeout(conn)
        op.execute(
            f"ALTER TABLE {_TABLE} ADD COLUMN IF NOT EXISTS {_COLUMN} VARCHAR(64)"
        )

    # Index guarded independently so a crashed retry between the column and
    # the index still converges. Partial: only rows the dedup can ever match.
    # CONCURRENTLY must run outside a transaction and must not inherit the
    # lock_timeout belt above; the helper bounds the build with its own
    # statement_timeout and clears an INVALID leftover first (module docstring).
    with op.get_context().autocommit_block():
        create_index_concurrently(
            conn,
            _INDEX,
            _TABLE,
            f"(conversation_id, {_COLUMN}) WHERE {_COLUMN} IS NOT NULL",
        )


def downgrade() -> None:
    conn = op.get_bind()
    if not _table_exists(conn, _TABLE):
        return
    with op.get_context().autocommit_block():
        op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {_INDEX}")
    if _column_exists(conn, _TABLE, _COLUMN):
        terminate_lock_holders(conn, _TABLE)
        set_lock_timeout(conn)
        op.execute(f"ALTER TABLE {_TABLE} DROP COLUMN IF EXISTS {_COLUMN}")
