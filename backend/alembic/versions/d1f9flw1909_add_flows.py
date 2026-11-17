"""add flows + flow_versions for the visual flow builder

Creates ``flows`` — one row per user-composed automation graph, holding the
React Flow canvas document (``{nodes, edges, viewport}``) in a JSON column plus
the standard three-tier visibility columns — and ``flow_versions``, the
immutable per-save snapshot table modelled on ``prompt_versions``.

Lock safety: both tables are BRAND NEW, so nothing can be reading them, but the
``CREATE TABLE`` statements still carry no inline ``REFERENCES`` — an inline FK
would take SHARE ROW EXCLUSIVE on ``users`` (a hot table) from inside
``CREATE TABLE``, where no pre-flight can protect it. Every FK is therefore added
separately, ``NOT VALID`` (no validation scan), under the canonical recipe
(idempotency probe + early return, then ``terminate_lock_holders`` +
``set_lock_timeout``, then guarded DDL). Indexes are built ``CONCURRENTLY``
outside the ``lock_timeout`` belt (a concurrent build legitimately waits on
snapshots).

The ``flow_versions`` UNIQUE and CHECK constraints are inline: they are
table-local on a table this migration just created, so they take no lock any
wider than the ``CREATE TABLE`` itself.

Object names match exactly what ``create_all`` emits on a fresh install (where
this body never runs), so both install paths converge on identical names.

Revision ID: d1f9flw1909
Revises: d4e5ushr1881
Create Date: 2026-08-09 10:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.core.migration_ddl import set_lock_timeout, terminate_lock_holders

# revision identifiers, used by Alembic.
revision: str = "d1f9flw1909"
# Chains behind main's current single head. Re-pointed repeatedly as main
# advanced during review: `c5g9tou20001`, then `d5e8mfa02786` (TOTP MFA), then
# `f2adpst28350` (widen adapter_state), then `a7f3run0003` (runner blocked shell
# commands), `f9u2fav0001` (user favorite agent ids), `g1novasrc00` (Nova rule
# source), now `d4e5ushr1881` (per-user sharing) — each of which forked off the
# previous parent and would otherwise open a second head. The whole flows
# chain (this → e2c4flwrun2 → e5f7flwevt1 → e6g8flwtrc1 → f7h9runflw1) hangs off
# it. Leaving the old parent blocks every deployment — see backend/AGENTS.md >
# "Database migrations (safety)", rule 1. Re-point again on any further rebase.
down_revision: str | None = "d4e5ushr1881"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_FLOWS = "flows"
_FLOW_VERSIONS = "flow_versions"

# (constraint name, table, column, referenced table, literal DDL)
#
# The DDL is spelled out literally per constraint rather than interpolated in a
# loop: the ``DO $$`` block is the idempotent constraint idiom, and building it
# with an f-string would embed identifiers in a ``SELECT`` (flagged as a SQL
# injection vector by ruff's S608, correctly — migration DDL must never be
# assembled from variables).
_FOREIGN_KEYS: tuple[tuple[str, str, str, str, str], ...] = (
    (
        "flows_user_id_fkey",
        _FLOWS,
        "user_id",
        "users",
        """
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_constraint
                WHERE conname = 'flows_user_id_fkey'
            ) THEN
                ALTER TABLE flows
                    ADD CONSTRAINT flows_user_id_fkey
                    FOREIGN KEY (user_id)
                    REFERENCES users(id) ON DELETE CASCADE
                    NOT VALID;
            END IF;
        END $$;
        """,
    ),
    (
        "flow_versions_flow_id_fkey",
        _FLOW_VERSIONS,
        "flow_id",
        _FLOWS,
        """
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_constraint
                WHERE conname = 'flow_versions_flow_id_fkey'
            ) THEN
                ALTER TABLE flow_versions
                    ADD CONSTRAINT flow_versions_flow_id_fkey
                    FOREIGN KEY (flow_id)
                    REFERENCES flows(id) ON DELETE CASCADE
                    NOT VALID;
            END IF;
        END $$;
        """,
    ),
    (
        "flow_versions_author_id_fkey",
        _FLOW_VERSIONS,
        "author_id",
        "users",
        """
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_constraint
                WHERE conname = 'flow_versions_author_id_fkey'
            ) THEN
                ALTER TABLE flow_versions
                    ADD CONSTRAINT flow_versions_author_id_fkey
                    FOREIGN KEY (author_id)
                    REFERENCES users(id) ON DELETE SET NULL
                    NOT VALID;
            END IF;
        END $$;
        """,
    ),
)

# (index name, table, column)
_INDEXES: tuple[tuple[str, str, str], ...] = (
    ("ix_flows_user_id", _FLOWS, "user_id"),
    ("ix_flows_status", _FLOWS, "status"),
    ("ix_flow_versions_flow_id", _FLOW_VERSIONS, "flow_id"),
    ("ix_flow_versions_author_id", _FLOW_VERSIONS, "author_id"),
    ("ix_flow_versions_created_at", _FLOW_VERSIONS, "created_at"),
)


def _table_exists(conn, table: str) -> bool:
    # Scope the probe to the active schema — information_schema spans ALL
    # schemas, so a same-named table elsewhere must not make the probe skip the
    # DDL in the schema being migrated.
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

    # ── 1. flows ─────────────────────────────────────────────────────────────
    op.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {_FLOWS} (
            id UUID NOT NULL,
            user_id UUID,
            name VARCHAR(200) NOT NULL,
            description TEXT NOT NULL DEFAULT '',
            status VARCHAR(20) NOT NULL DEFAULT 'draft',
            graph JSON NOT NULL DEFAULT '{{}}',
            version_number INTEGER NOT NULL DEFAULT 1,
            tags JSON,
            is_company_managed BOOLEAN NOT NULL DEFAULT false,
            shared_group_ids JSON,
            shared_group_permissions JSON,
            created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
            updated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
            CONSTRAINT flows_pkey PRIMARY KEY (id)
        )
        """
    )

    # ── 2. flow_versions ─────────────────────────────────────────────────────
    # UNIQUE + CHECK are inline: table-local on a table created right above, so
    # they widen no lock beyond the CREATE TABLE.
    op.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {_FLOW_VERSIONS} (
            id UUID NOT NULL,
            flow_id UUID NOT NULL,
            version_number INTEGER NOT NULL,
            graph JSON NOT NULL DEFAULT '{{}}',
            author_id UUID,
            commit_message TEXT,
            is_locked BOOLEAN NOT NULL DEFAULT false,
            created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
            CONSTRAINT flow_versions_pkey PRIMARY KEY (id),
            CONSTRAINT uq_flow_versions_flow_ver UNIQUE (flow_id, version_number),
            CONSTRAINT ck_flow_versions_version_positive CHECK (version_number > 0)
        )
        """
    )

    # ── 3. Foreign keys (separate, NOT VALID, one pre-flight each) ───────────
    for constraint, table, column, referenced, ddl in _FOREIGN_KEYS:
        # Skip when the referencing column is absent (never on a real install)
        # or the constraint already exists — steady-state re-runs take NO lock.
        if not _column_exists(conn, table, column):
            continue
        if _constraint_exists(conn, constraint):
            continue
        # A referenced table that does not exist yet would make the ALTER fail;
        # skipping keeps the migration re-runnable rather than wedging startup.
        if not _table_exists(conn, referenced):
            continue
        terminate_lock_holders(conn, table)
        terminate_lock_holders(conn, referenced)
        set_lock_timeout(conn)
        op.execute(ddl)

    # ── 4. Indexes ───────────────────────────────────────────────────────────
    # CONCURRENTLY must run outside a transaction and must NOT be wrapped in a
    # lock_timeout (the build legitimately waits on snapshots).
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

    # Dropping the tables removes their constraints with them; the FKs are only
    # dropped separately when a table survives (it never does here). Children
    # first so the flow_versions -> flows FK never blocks the parent drop.
    for table in (_FLOW_VERSIONS, _FLOWS):
        if _table_exists(conn, table):
            terminate_lock_holders(conn, table)
            set_lock_timeout(conn)
            op.execute(f"DROP TABLE IF EXISTS {table}")
