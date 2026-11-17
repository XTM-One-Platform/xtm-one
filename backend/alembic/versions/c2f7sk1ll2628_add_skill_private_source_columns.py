"""add private-repository skill source columns

Adds to ``agent_skills``:
  - ``source_integration_id`` (UUID, FK -> integrations.id ON DELETE SET NULL)
    — the GitHub integration whose already-encrypted credential authenticates
    the fetch of a ``SKILL.md`` living in a private repository.  No secret is
    stored on the skill row; only the reference.
  - ``source_ref`` (VARCHAR(255)) — branch / tag / commit to track, overriding
    the ref carried by the URL.  NULL = the repository's default branch HEAD.
  - ``auth_failure_count`` (INTEGER NOT NULL DEFAULT 0) — consecutive
    authentication failures during auto-refresh, kept separate from
    ``refresh_failure_count`` so an expired credential is never quarantined as
    a permanently broken URL.

All three are constant-default / nullable ``ADD COLUMN`` (metadata-only on
PG 11+) and follow the canonical lock-safe recipe: idempotency probe ->
terminate_lock_holders -> set_lock_timeout -> guarded DDL.  The FK constraint
and its supporting index are added under the same guard set, the index built
with ``CREATE INDEX CONCURRENTLY`` outside the lock_timeout belt.

Revision ID: c2f7sk1ll2628
Revises: a1b2l4ng0001
Create Date: 2026-07-28 09:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.core.migration_ddl import set_lock_timeout, terminate_lock_holders

# revision identifiers, used by Alembic.
revision: str = "c2f7sk1ll2628"
down_revision: str | None = "a1b2l4ng0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "agent_skills"
#: Match the names PostgreSQL / SQLAlchemy produce on a fresh install
#: (``create_all`` + ``stamp head``, where this body never runs), so both
#: install paths end up with identically-named objects and a future migration
#: can drop or alter them by one name.
_FK_NAME = "agent_skills_source_integration_id_fkey"
_INDEX_NAME = "ix_agent_skills_source_integration_id"

# (column, DDL type + constraint clause)
_COLUMNS: tuple[tuple[str, str], ...] = (
    ("source_integration_id", "UUID"),
    ("source_ref", "VARCHAR(255)"),
    ("auth_failure_count", "INTEGER NOT NULL DEFAULT 0"),
)


def _column_exists(conn, table: str, column: str) -> bool:
    # Scope the probe to the active schema — information_schema.columns spans
    # ALL schemas, so a same-named table elsewhere must not make the probe
    # skip the ADD COLUMN in the schema being migrated.
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

    for column, ddl_type in _COLUMNS:
        # 1. Idempotency probe — steady-state re-runs take NO lock.
        if _column_exists(conn, _TABLE, column):
            continue
        # 2. Pre-flight: kill same-role lock holders so the ALTER is instant.
        terminate_lock_holders(conn, _TABLE)
        # 3. Safety belt: fail fast (55P03) instead of wedging the lock queue.
        set_lock_timeout(conn)
        # 4. Guarded, constant-default (metadata-only) DDL.
        op.execute(
            f"ALTER TABLE {_TABLE} ADD COLUMN IF NOT EXISTS {column} {ddl_type}"
        )

    # FK: skipped entirely when the referencing column is missing (a fresh
    # install ran create_all, which already produced both column and FK).
    if _column_exists(conn, _TABLE, "source_integration_id") and not _constraint_exists(
        conn, _FK_NAME
    ):
        terminate_lock_holders(conn, _TABLE)
        set_lock_timeout(conn)
        # Literal DDL (no interpolation): the ``DO $$`` block is the idempotent
        # constraint idiom, and ``NOT VALID`` skips the full-table validation
        # scan so the ALTER stays metadata-only.
        op.execute(
            """
            DO $$
            BEGIN
                IF NOT EXISTS (
                    SELECT 1 FROM pg_constraint
                    WHERE conname = 'agent_skills_source_integration_id_fkey'
                ) THEN
                    ALTER TABLE agent_skills
                        ADD CONSTRAINT agent_skills_source_integration_id_fkey
                        FOREIGN KEY (source_integration_id)
                        REFERENCES integrations(id) ON DELETE SET NULL
                        NOT VALID;
                END IF;
            END $$;
            """
        )

    # Index for the ON DELETE SET NULL sweep and for "which skills use this
    # integration" lookups.  CONCURRENTLY must run outside a transaction and
    # must NOT be wrapped in a lock_timeout (the build legitimately waits on
    # snapshots).
    if _column_exists(conn, _TABLE, "source_integration_id"):
        with op.get_context().autocommit_block():
            op.execute("SET statement_timeout = 0")
            op.execute(
                f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {_INDEX_NAME} "
                f"ON {_TABLE} (source_integration_id)"
            )


def downgrade() -> None:
    conn = op.get_bind()

    with op.get_context().autocommit_block():
        op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {_INDEX_NAME}")

    if _constraint_exists(conn, _FK_NAME):
        terminate_lock_holders(conn, _TABLE)
        set_lock_timeout(conn)
        op.execute(f"ALTER TABLE {_TABLE} DROP CONSTRAINT IF EXISTS {_FK_NAME}")

    for column, _ddl_type in _COLUMNS:
        if not _column_exists(conn, _TABLE, column):
            continue
        terminate_lock_holders(conn, _TABLE)
        set_lock_timeout(conn)
        op.execute(f"ALTER TABLE {_TABLE} DROP COLUMN IF EXISTS {column}")
