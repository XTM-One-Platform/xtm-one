"""scope the coordination tools to a workspace

Adds `workspace_id` to the three coordination primitives, widens
`worksets.scope`, and constrains the workset column to agree with its scope.
Additive, no backfill: every existing row keeps `workspace_id IS NULL`, which
is what `run` / `flow` / `global` rows must have.

**All three tables are EXISTING and HOT**, unlike `workspaces` in
`w1wsp3314001` — so the canonical lock-safe recipe applies to every statement
here (`backend/AGENTS.md` > "Database migrations (safety)"): idempotency probe
and early return, `terminate_lock_holders`, `set_lock_timeout`, then guarded
DDL. Adding a nullable column with no default is metadata-only on PG 11+, but
it still takes a brief ACCESS EXCLUSIVE lock, and that is precisely the shape
that took production down on 2026-07-05.

Three details that are not obvious and are deliberate:

1. **The foreign keys are added in two steps**, like ``a1paus3198``: the
   column bare (metadata-only), then ``ADD CONSTRAINT ... NOT VALID`` and
   ``VALIDATE CONSTRAINT``. An inline ``REFERENCES`` on the ``ADD COLUMN``
   validates the key by scanning the whole table while it holds the ACCESS
   EXCLUSIVE lock; ``VALIDATE`` scans under SHARE UPDATE EXCLUSIVE, which
   blocks neither reads nor writes. Each key carries the name ``create_all``
   gives it, so fresh and migrated installs agree. ``workspaces`` is only
   referenced: its SHARE ROW EXCLUSIVE lock does not conflict with readers,
   so its holders are not terminated.

2. **The indexes are built IN PLACE, not CONCURRENTLY**, through
   ``create_index_in_place`` in an `autocommit_block`. A concurrent build
   waits for every snapshot older than itself on the whole database, and
   during a rolling deploy on a busy install the previous release always
   holds one: the build rode its ceiling on every boot and the upgrade never
   landed. A plain build only needs a SHARE lock on its own table (writes wait
   for the seconds it takes, reads do not), granted at once after the
   terminate pre-flight and bounded by `lock_timeout`. Best-effort: an index
   that still fails is built by the model index reconciliation after the
   upgrade.

3. **The CHECK is added NOT VALID, then validated separately.** Adding it
   validated scans the whole table under a blocking lock; `VALIDATE
   CONSTRAINT` takes only SHARE UPDATE EXCLUSIVE. It is on `worksets` alone
   because it is the only one of the three with a `scope` column —
   `shared_states` and `work_items` carry no scope at any layer, so for them
   the invariant lives in the write path rather than in DDL.

Widening `worksets.scope` from VARCHAR(10) to VARCHAR(30) does NOT rewrite the
table (increasing a varchar's length is metadata-only since PG 9.2), but it
does take ACCESS EXCLUSIVE, so it goes through the recipe too. "workspace" is
nine characters and fit the old ceiling by one — widening now is what stops
the NEXT scope name from being the one that discovers it.

The per-table `ondelete` differs, and each difference is load-bearing:

* `shared_states` — CASCADE. Plain rows owning no heavy resource, already
  retention-swept; nothing durable is lost with the workspace.
* `worksets` — RESTRICT. A CASCADE would delete the row without calling
  `purge_workset`, orphaning its KnowledgeBase, documents and embeddings.
* `work_items` — RESTRICT. Deleting a workspace must never delete tracked work.

Revision ID: w3wsp3314003
Revises: w2wsp3314002
Create Date: 2026-09-04 15:00:00.000000
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

revision: str = "w3wsp3314003"
down_revision: str | Sequence[str] | None = "w2wsp3314002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: table -> (ondelete, index name)
_TABLES: dict[str, tuple[str, str]] = {
    "shared_states": ("CASCADE", "ix_shared_states_workspace"),
    "worksets": ("RESTRICT", "ix_worksets_workspace"),
    "work_items": ("RESTRICT", "ix_work_items_workspace"),
}


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


def _has_constraint(conn, name: str) -> bool:
    return bool(
        conn.execute(
            sa.text("SELECT 1 FROM pg_constraint WHERE conname = :n"),
            {"n": name},
        ).scalar()
    )


def _constraint_validated(conn, name: str) -> bool:
    return bool(
        conn.execute(
            sa.text("SELECT convalidated FROM pg_constraint WHERE conname = :n"),
            {"n": name},
        ).scalar()
    )


def _scope_length(conn) -> int | None:
    return conn.execute(
        sa.text(
            "SELECT character_maximum_length FROM information_schema.columns "
            "WHERE table_schema = current_schema() "
            "AND table_name = 'worksets' AND column_name = 'scope'"
        )
    ).scalar()


def _fk_name(table: str) -> str:
    """The name `create_all` gives the model's unnamed ForeignKey."""
    return f"{table}_workspace_id_fkey"


def upgrade() -> None:
    conn = op.get_bind()

    for table, (ondelete, _index) in _TABLES.items():
        if not _has_column(conn, table, "workspace_id"):
            terminate_lock_holders(conn, table)
            set_lock_timeout(conn)
            op.add_column(
                table,
                sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=True),
            )
        fk = _fk_name(table)
        if not _has_constraint(conn, fk):
            terminate_lock_holders(conn, table)
            set_lock_timeout(conn)
            op.execute(
                f"ALTER TABLE {table} ADD CONSTRAINT {fk} FOREIGN KEY "
                f"(workspace_id) REFERENCES workspaces(id) ON DELETE {ondelete} "
                "NOT VALID"
            )
        if not _constraint_validated(conn, fk):
            # SHARE UPDATE EXCLUSIVE: blocks neither reads nor writes, and the
            # column was created NULL, so nothing can fail the check.
            op.execute(f"ALTER TABLE {table} VALIDATE CONSTRAINT {fk}")

    # Widen `worksets.scope`. Metadata-only, but still an ALTER on a hot table.
    if (_scope_length(conn) or 0) < 30:
        terminate_lock_holders(conn, "worksets")
        set_lock_timeout(conn)
        op.alter_column(
            "worksets",
            "scope",
            existing_type=sa.String(10),
            type_=sa.String(30),
            existing_nullable=False,
        )

    # NOT VALID first, validated second — see the module docstring.
    if not _has_constraint(conn, "ck_worksets_workspace_scope"):
        terminate_lock_holders(conn, "worksets")
        set_lock_timeout(conn)
        op.execute(
            "ALTER TABLE worksets ADD CONSTRAINT ck_worksets_workspace_scope "
            "CHECK ((scope = 'workspace') = (workspace_id IS NOT NULL)) NOT VALID"
        )
    if not _constraint_validated(conn, "ck_worksets_workspace_scope"):
        # SHARE UPDATE EXCLUSIVE only: readers and writers keep working, so no
        # lock_timeout belt is needed and none is set.
        op.execute(
            "ALTER TABLE worksets VALIDATE CONSTRAINT ck_worksets_workspace_scope"
        )

    # Autocommit: each build releases its SHARE lock the moment it commits.
    with op.get_context().autocommit_block():
        for table, (_ondelete, index) in _TABLES.items():
            create_index_in_place(conn, index, table, "(workspace_id)")


def downgrade() -> None:
    conn = op.get_bind()

    with op.get_context().autocommit_block():
        for _table, (_ondelete, index) in _TABLES.items():
            op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {index}")

    if _has_constraint(conn, "ck_worksets_workspace_scope"):
        terminate_lock_holders(conn, "worksets")
        set_lock_timeout(conn)
        op.execute(
            "ALTER TABLE worksets DROP CONSTRAINT ck_worksets_workspace_scope"
        )

    # Narrow `scope` back. Safe only because nothing longer than the old
    # ceiling can be stored: 'workspace' is 9 characters, and the rows that
    # used it are about to lose their workspace anyway.
    if (_scope_length(conn) or 0) > 10:
        terminate_lock_holders(conn, "worksets")
        set_lock_timeout(conn)
        op.alter_column(
            "worksets",
            "scope",
            existing_type=sa.String(30),
            type_=sa.String(10),
            existing_nullable=False,
        )

    for table in _TABLES:
        if _has_constraint(conn, _fk_name(table)):
            terminate_lock_holders(conn, table)
            set_lock_timeout(conn)
            op.execute(
                f"ALTER TABLE {table} DROP CONSTRAINT IF EXISTS {_fk_name(table)}"
            )
        if not _has_column(conn, table, "workspace_id"):
            continue
        terminate_lock_holders(conn, table)
        set_lock_timeout(conn)
        op.drop_column(table, "workspace_id")
