"""add non-vectorized template containers + template asset columns

Creates ``template_containers`` — reusable template metadata (kind, tags,
theme, aspect ratio, layouts) with **no** embedding/chunking columns and no
vector column, deliberately disjoint from the ``knowledge_bases`` pipeline.

Extends ``file_attachments`` with:
  - ``template_container_id`` (UUID, FK -> template_containers.id ON DELETE
    CASCADE) — the container an asset (font, image, logo, …) belongs to. A
    template asset has no meaning outside its container, hence CASCADE rather
    than the SET NULL used by this table's other context FKs.
  - ``asset_role`` (VARCHAR(50)) — structured role of the asset so a consumer
    feature can pick "the heading font" without sniffing content types.

Lock safety: the two ``ADD COLUMN`` statements are nullable / constant-default
(metadata-only on PG 11+) and both FKs are added ``NOT VALID`` so no
full-table validation scan runs. Every lock-taking statement follows the
canonical recipe — idempotency probe + early return, then
``terminate_lock_holders`` + ``set_lock_timeout``, then the guarded DDL. The
supporting indexes are built with ``CREATE INDEX CONCURRENTLY`` outside the
``lock_timeout`` belt (a concurrent build legitimately waits on snapshots).

Object names match exactly what ``create_all`` produces on a fresh install
(where this body never runs), so both install paths converge on identically
named constraints and indexes.

Revision ID: e3t9tpl2636
Revises: f6a2oash0001
Create Date: 2026-07-29 10:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.core.migration_ddl import set_lock_timeout, terminate_lock_holders

# revision identifiers, used by Alembic.
revision: str = "e3t9tpl2636"
# NOT a constant: this must always be main's CURRENT head, so it is re-pointed on
# every rebase (so far: c2f7sk1ll2628 -> b7f1e6a93c02 -> b2c3agex0001 ->
# f6a2oash0001). Leaving it on a stale parent forks the history into two heads,
# which fails `test_single_head` and — because `alembic stamp head` then has no
# single answer — also takes the fresh-install and E2E jobs down. Chaining behind
# main's head is the fix, not a merge migration (backend/AGENTS.md > "Database
# migrations (safety)", rule 1).
down_revision: str | None = "f6a2oash0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_CONTAINERS = "template_containers"
_ATTACHMENTS = "file_attachments"

_CONTAINERS_USER_FK = "template_containers_user_id_fkey"
_ATTACHMENTS_CONTAINER_FK = "file_attachments_template_container_id_fkey"

# (index name, table, column expression)
_INDEXES: tuple[tuple[str, str, str], ...] = (
    ("ix_template_containers_user_id", _CONTAINERS, "user_id"),
    (
        "ix_template_containers_is_company_managed",
        _CONTAINERS,
        "is_company_managed",
    ),
    ("ix_template_containers_kind", _CONTAINERS, "kind"),
    (
        "ix_file_attachments_template_container_id",
        _ATTACHMENTS,
        "template_container_id",
    ),
)

# (column, DDL type + constraint clause) added to ``file_attachments``
_ATTACHMENT_COLUMNS: tuple[tuple[str, str], ...] = (
    ("template_container_id", "UUID"),
    ("asset_role", "VARCHAR(50)"),
)


def _table_exists(conn, table: str) -> bool:
    # Scope the probe to the active schema — information_schema spans ALL
    # schemas, so a same-named table elsewhere must not make the probe skip
    # the DDL in the schema being migrated.
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

    # ── 1. template_containers ───────────────────────────────────────────────
    # CREATE TABLE takes no lock on any existing table as long as no inline FK
    # references one; the FK to ``users`` is added separately below under the
    # canonical recipe (an inline REFERENCES would take SHARE ROW EXCLUSIVE on
    # ``users`` — the hottest table in the schema — inside CREATE TABLE, where
    # no probe/pre-flight can protect it).
    op.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {_CONTAINERS} (
            id UUID NOT NULL,
            user_id UUID,
            is_company_managed BOOLEAN NOT NULL DEFAULT FALSE,
            shared_group_ids JSON,
            shared_group_permissions JSON,
            name VARCHAR(200) NOT NULL,
            description TEXT,
            kind VARCHAR(50) NOT NULL DEFAULT 'presentation',
            tags JSON,
            theme VARCHAR(100),
            theme_config JSON,
            aspect_ratio VARCHAR(30),
            layouts JSON,
            extra_metadata JSON,
            lookup_count INTEGER NOT NULL DEFAULT 0,
            created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
            updated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
            CONSTRAINT template_containers_pkey PRIMARY KEY (id)
        )
        """
    )

    if not _constraint_exists(conn, _CONTAINERS_USER_FK):
        terminate_lock_holders(conn, _CONTAINERS)
        terminate_lock_holders(conn, "users")
        set_lock_timeout(conn)
        # Literal DDL (no interpolation): the ``DO $$`` block is the idempotent
        # constraint idiom, and ``NOT VALID`` skips the validation scan so the
        # statement stays metadata-only.
        op.execute(
            """
            DO $$
            BEGIN
                IF NOT EXISTS (
                    SELECT 1 FROM pg_constraint
                    WHERE conname = 'template_containers_user_id_fkey'
                ) THEN
                    ALTER TABLE template_containers
                        ADD CONSTRAINT template_containers_user_id_fkey
                        FOREIGN KEY (user_id)
                        REFERENCES users(id) ON DELETE CASCADE
                        NOT VALID;
                END IF;
            END $$;
            """
        )

    # ── 2. file_attachments columns ──────────────────────────────────────────
    for column, ddl_type in _ATTACHMENT_COLUMNS:
        # Idempotency probe — steady-state re-runs take NO lock.
        if _column_exists(conn, _ATTACHMENTS, column):
            continue
        terminate_lock_holders(conn, _ATTACHMENTS)
        set_lock_timeout(conn)
        op.execute(
            f"ALTER TABLE {_ATTACHMENTS} ADD COLUMN IF NOT EXISTS {column} {ddl_type}"
        )

    # FK skipped entirely when the referencing column is missing (a fresh
    # install ran create_all, which already produced both column and FK).
    if _column_exists(
        conn, _ATTACHMENTS, "template_container_id"
    ) and not _constraint_exists(conn, _ATTACHMENTS_CONTAINER_FK):
        terminate_lock_holders(conn, _ATTACHMENTS)
        terminate_lock_holders(conn, _CONTAINERS)
        set_lock_timeout(conn)
        op.execute(
            """
            DO $$
            BEGIN
                IF NOT EXISTS (
                    SELECT 1 FROM pg_constraint
                    WHERE conname = 'file_attachments_template_container_id_fkey'
                ) THEN
                    ALTER TABLE file_attachments
                        ADD CONSTRAINT file_attachments_template_container_id_fkey
                        FOREIGN KEY (template_container_id)
                        REFERENCES template_containers(id) ON DELETE CASCADE
                        NOT VALID;
                END IF;
            END $$;
            """
        )

    # ── 3. Indexes ───────────────────────────────────────────────────────────
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

    if _constraint_exists(conn, _ATTACHMENTS_CONTAINER_FK):
        terminate_lock_holders(conn, _ATTACHMENTS)
        set_lock_timeout(conn)
        op.execute(
            f"ALTER TABLE {_ATTACHMENTS} "
            f"DROP CONSTRAINT IF EXISTS {_ATTACHMENTS_CONTAINER_FK}"
        )

    for column, _ddl_type in _ATTACHMENT_COLUMNS:
        if not _column_exists(conn, _ATTACHMENTS, column):
            continue
        terminate_lock_holders(conn, _ATTACHMENTS)
        set_lock_timeout(conn)
        op.execute(f"ALTER TABLE {_ATTACHMENTS} DROP COLUMN IF EXISTS {column}")

    if _table_exists(conn, _CONTAINERS):
        terminate_lock_holders(conn, _CONTAINERS)
        set_lock_timeout(conn)
        op.execute(f"DROP TABLE IF EXISTS {_CONTAINERS}")
