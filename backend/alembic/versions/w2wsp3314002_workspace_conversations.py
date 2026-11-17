"""bind conversations and knowledge bases to workspaces

Additive, and nothing is backfilled. Every conversation that predates
workspaces stays ``workspace_id IS NULL``. "Unfiled" is a real state rather
than a gap to repair: filing a user's entire history into a container they
never chose would be a worse default than leaving it alone, and the backfill
itself would be an unbounded in-transaction UPDATE on the platform's hottest
table — banned by the lock-light rules.

This migration ALTERs ``conversations``, which the model's own comments call
"the hot conversations table" — external-channel routing looks a row up on
every inbound message, and the agentic loop holds sessions open for minutes.
So every step follows ``backend/AGENTS.md`` > "Database migrations (safety)":

* the column is added bare (nullable, no default: metadata-only on PG 11+)
  under the canonical recipe — idempotency probe, ``terminate_lock_holders``,
  ``set_lock_timeout``, guarded DDL;
* its foreign key is added in two steps, like ``a1paus3198``: ``NOT VALID``
  (no scan), then ``VALIDATE CONSTRAINT`` under SHARE UPDATE EXCLUSIVE, which
  blocks neither reads nor writes. An inline ``REFERENCES`` on the
  ``ADD COLUMN`` would validate by scanning the whole table while holding the
  ACCESS EXCLUSIVE lock. It is named as ``create_all`` names it, so fresh and
  migrated installs carry the same constraint;
* the index is built in place through ``create_index_in_place``, not
  ``CONCURRENTLY``: a concurrent build waits for every older snapshot on the
  whole database, which a rolling deploy on a busy install never lets it
  outlast. The plain build holds a SHARE lock on ``conversations`` (writes
  wait, reads do not) for the seconds it runs; best-effort, the model index
  reconciliation builds it after the upgrade if it still fails.

``workspace_knowledge_bases`` is a new table, but its foreign keys lock
``workspaces`` and ``knowledge_bases``, so its ``CREATE TABLE`` runs under
``set_lock_timeout`` too (``a3rc4de0001``).

Revision ID: w2wsp3314002
Revises: w1wsp3314001
Create Date: 2026-09-04 12:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from app.core.migration_ddl import (
    create_index_in_place,
    set_lock_timeout,
    terminate_lock_holders,
)

revision: str = "w2wsp3314002"
down_revision: str | Sequence[str] | None = "w1wsp3314001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "conversations"
#: The name `create_all` gives the model's unnamed ForeignKey.
_FK_NAME = "conversations_workspace_id_fkey"
_INDEX = "ix_conversations_workspace_id"


def _has_table(conn, name: str) -> bool:
    return bool(
        conn.execute(
            sa.text(
                "SELECT 1 FROM information_schema.tables "
                "WHERE table_schema = current_schema() AND table_name = :n"
            ),
            {"n": name},
        ).scalar()
    )


def _has_column(conn, table: str, column: str) -> bool:
    return bool(
        conn.execute(
            sa.text(
                "SELECT 1 FROM information_schema.columns "
                "WHERE table_schema = current_schema() "
                "AND table_name = :t AND column_name = :c"
            ),
            {"t": table, "c": column},
        ).scalar()
    )


def _constraint_state(conn, name: str) -> str | None:
    """``"valid"`` / ``"not_valid"`` for *name*, or ``None`` if absent.

    A replica that died between ``ADD ... NOT VALID`` and ``VALIDATE`` resumes
    at the validation instead of adding the constraint a second time.
    """
    row = conn.execute(
        sa.text(
            "SELECT convalidated FROM pg_constraint "
            "WHERE conname = :n AND connamespace = current_schema()::regnamespace"
        ),
        {"n": name},
    ).scalar()
    if row is None:
        return None
    return "valid" if row else "not_valid"


def upgrade() -> None:
    conn = op.get_bind()

    # ── conversations.workspace_id (EXISTING, HOT table) ─────────────────
    if not _has_column(conn, _TABLE, "workspace_id"):
        terminate_lock_holders(conn, _TABLE)
        set_lock_timeout(conn)
        op.execute(
            sa.text(f"ALTER TABLE {_TABLE} ADD COLUMN IF NOT EXISTS workspace_id UUID")
        )

    state = _constraint_state(conn, _FK_NAME)
    if state is None:
        # SHARE ROW EXCLUSIVE on both tables, held to commit. `workspaces` is
        # only referenced: that lock does not conflict with its readers, so
        # its holders are not terminated (backend/AGENTS.md).
        terminate_lock_holders(conn, _TABLE)
        set_lock_timeout(conn)
        op.execute(
            sa.text(
                f"ALTER TABLE {_TABLE} ADD CONSTRAINT {_FK_NAME} "
                "FOREIGN KEY (workspace_id) REFERENCES workspaces(id) "
                "ON DELETE SET NULL NOT VALID"
            )
        )
        state = "not_valid"
    if state == "not_valid":
        # SHARE UPDATE EXCLUSIVE: blocks neither reads nor writes. The column
        # was created NULL, so nothing can fail the check.
        op.execute(sa.text(f"ALTER TABLE {_TABLE} VALIDATE CONSTRAINT {_FK_NAME}"))

    # ── workspace_knowledge_bases (NEW table) ────────────────────────────
    if not _has_table(conn, "workspace_knowledge_bases"):
        # Its foreign keys lock `workspaces` and `knowledge_bases`.
        set_lock_timeout(conn)
        op.create_table(
            "workspace_knowledge_bases",
            sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
            sa.Column(
                "workspace_id",
                postgresql.UUID(as_uuid=True),
                sa.ForeignKey("workspaces.id", ondelete="CASCADE"),
                nullable=False,
            ),
            # CASCADE from the knowledge base too: the binding is not
            # ownership, so losing either end must remove the row and nothing
            # more. A workspace must never take somebody's corpus with it.
            sa.Column(
                "knowledge_base_id",
                postgresql.UUID(as_uuid=True),
                sa.ForeignKey("knowledge_bases.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.func.now(),
            ),
        )
        op.create_index(
            "uq_workspace_knowledge_bases_pair",
            "workspace_knowledge_bases",
            ["workspace_id", "knowledge_base_id"],
            unique=True,
        )
        op.create_index(
            "ix_workspace_knowledge_bases_kb",
            "workspace_knowledge_bases",
            ["knowledge_base_id"],
        )

    # Last, because the autocommit block commits everything above.
    with op.get_context().autocommit_block():
        create_index_in_place(conn, _INDEX, _TABLE, "(workspace_id)")


def downgrade() -> None:
    conn = op.get_bind()

    if _has_table(conn, "workspace_knowledge_bases"):
        # ACCESS EXCLUSIVE on `workspaces` and `knowledge_bases` to drop the
        # FK triggers.
        set_lock_timeout(conn)
        op.drop_table("workspace_knowledge_bases")

    with op.get_context().autocommit_block():
        op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {_INDEX}")

    if _constraint_state(conn, _FK_NAME) is not None:
        terminate_lock_holders(conn, _TABLE)
        set_lock_timeout(conn)
        op.execute(sa.text(f"ALTER TABLE {_TABLE} DROP CONSTRAINT IF EXISTS {_FK_NAME}"))

    if _has_column(conn, _TABLE, "workspace_id"):
        terminate_lock_holders(conn, _TABLE)
        set_lock_timeout(conn)
        op.execute(sa.text(f"ALTER TABLE {_TABLE} DROP COLUMN IF EXISTS workspace_id"))
