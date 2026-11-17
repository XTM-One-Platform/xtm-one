"""Add presentation collaboration: versions, comments, last editor.

Three additions, all additive:

* ``presentation_versions`` — one immutable spec snapshot per saved revision
  (agent turn, manual edit, restore). Numbered by the deck's own ``revision``.
* ``presentation_comments`` — slide-anchored review notes with flat threads
  and a resolved state.
* ``presentations.updated_by_user_id`` — who saved the current revision.

Safety contract (backend/AGENTS.md > "Database migrations (safety)"):

* Idempotent — ``CREATE TABLE IF NOT EXISTS``, ``ADD COLUMN IF NOT EXISTS``,
  guarded FK adds. Fresh installs run ``create_all`` + ``stamp head`` so the
  body never executes there; existing installs run it exactly once, and a
  crashed replica may retry.
* Lock-safe — the ``ADD COLUMN`` on the hot ``presentations`` table is
  nullable with no default (metadata-only, PG 11+) and follows the canonical
  recipe: idempotency probe, ``terminate_lock_holders`` pre-flight, then
  ``set_lock_timeout`` belt. ``CREATE TABLE`` on a name that does not exist
  takes no lock anyone else holds. FK constraints referencing ``presentations``
  / ``users`` are added ``NOT VALID`` (no scan of the referenced table, a
  ShareRowExclusive lock on it that the terminate + timeout guard) and then
  validated — validation only takes a ShareUpdateExclusive lock. Indexes
  are built ``CONCURRENTLY`` inside an autocommit block.

Revision ID: j4p3358collab
Revises: i3p3358adesc
Create Date: 2026-09-08
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.core.migration_ddl import set_lock_timeout, terminate_lock_holders

revision: str = "j4p3358collab"
down_revision: str | None = "i3p3358adesc"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _table_exists(conn, table: str) -> bool:
    return bool(conn.execute(sa.text("SELECT to_regclass(:name) IS NOT NULL"), {"name": table}).scalar())


def _column_exists(conn, table: str, column: str) -> bool:
    return bool(
        conn.execute(
            sa.text("SELECT 1 FROM information_schema.columns WHERE table_name = :t AND column_name = :c"),
            {"t": table, "c": column},
        ).scalar()
    )


def _constraint_exists(conn, name: str) -> bool:
    return bool(conn.execute(sa.text("SELECT 1 FROM pg_constraint WHERE conname = :n"), {"n": name}).scalar())


def _add_fk(
    conn,
    *,
    name: str,
    table: str,
    column: str,
    ref_table: str,
    ref_column: str,
    on_delete: str,
) -> None:
    """``ADD CONSTRAINT ... NOT VALID`` then ``VALIDATE``, both guarded.

    NOT VALID skips the scan of *ref_table* while holding the stronger lock;
    VALIDATE re-checks existing rows under a lock that lets reads and writes
    through. On a table this migration just created there are no rows to
    check, but the shape is the same one every existing migration uses.
    """
    if not _constraint_exists(conn, name):
        terminate_lock_holders(conn, table)
        terminate_lock_holders(conn, ref_table)
        set_lock_timeout(conn)
        conn.execute(
            sa.text(
                f"ALTER TABLE {table} ADD CONSTRAINT {name} "
                f"FOREIGN KEY ({column}) REFERENCES {ref_table} ({ref_column}) "
                f"ON DELETE {on_delete} NOT VALID"
            )
        )
    set_lock_timeout(conn)
    conn.execute(sa.text(f"ALTER TABLE {table} VALIDATE CONSTRAINT {name}"))


def _create_index_concurrently(name: str, table: str, columns: str) -> None:
    with op.get_context().autocommit_block():
        op.execute(sa.text(f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {name} ON {table} ({columns})"))


def upgrade() -> None:
    conn = op.get_bind()

    # ── presentations.updated_by_user_id ─────────────────────────────────
    if not _column_exists(conn, "presentations", "updated_by_user_id"):
        terminate_lock_holders(conn, "presentations")
        set_lock_timeout(conn)
        conn.execute(sa.text("ALTER TABLE presentations ADD COLUMN IF NOT EXISTS updated_by_user_id UUID"))
    _add_fk(
        conn,
        name="fk_presentations_updated_by_user_id_users",
        table="presentations",
        column="updated_by_user_id",
        ref_table="users",
        ref_column="id",
        on_delete="SET NULL",
    )

    # ── presentation_versions ─────────────────────────────────────────────
    if not _table_exists(conn, "presentation_versions"):
        set_lock_timeout(conn)
        conn.execute(
            sa.text(
                """
                CREATE TABLE IF NOT EXISTS presentation_versions (
                    id UUID PRIMARY KEY,
                    presentation_id UUID NOT NULL,
                    revision INTEGER NOT NULL,
                    title VARCHAR(300) NOT NULL,
                    slide_count INTEGER NOT NULL DEFAULT 0,
                    spec JSON NOT NULL,
                    source VARCHAR(20) NOT NULL,
                    summary VARCHAR(500),
                    restored_from_revision INTEGER,
                    author_id UUID,
                    created_at TIMESTAMP WITH TIME ZONE NOT NULL,
                    CONSTRAINT uq_presentation_versions_revision
                        UNIQUE (presentation_id, revision)
                )
                """
            )
        )
    _add_fk(
        conn,
        name="fk_presentation_versions_presentation_id_presentations",
        table="presentation_versions",
        column="presentation_id",
        ref_table="presentations",
        ref_column="id",
        on_delete="CASCADE",
    )
    _add_fk(
        conn,
        name="fk_presentation_versions_author_id_users",
        table="presentation_versions",
        column="author_id",
        ref_table="users",
        ref_column="id",
        on_delete="SET NULL",
    )
    _create_index_concurrently(
        "ix_presentation_versions_presentation_id",
        "presentation_versions",
        "presentation_id",
    )
    _create_index_concurrently(
        "ix_presentation_versions_presentation_created",
        "presentation_versions",
        "presentation_id, created_at",
    )

    # ── presentation_comments ─────────────────────────────────────────────
    if not _table_exists(conn, "presentation_comments"):
        set_lock_timeout(conn)
        conn.execute(
            sa.text(
                """
                CREATE TABLE IF NOT EXISTS presentation_comments (
                    id UUID PRIMARY KEY,
                    presentation_id UUID NOT NULL,
                    slide_id VARCHAR(64),
                    parent_id UUID,
                    author_id UUID,
                    body TEXT NOT NULL,
                    resolved_at TIMESTAMP WITH TIME ZONE,
                    resolved_by_user_id UUID,
                    created_at TIMESTAMP WITH TIME ZONE NOT NULL,
                    updated_at TIMESTAMP WITH TIME ZONE NOT NULL,
                    CONSTRAINT ck_presentation_comments_body_not_empty
                        CHECK (char_length(body) > 0)
                )
                """
            )
        )
    _add_fk(
        conn,
        name="fk_presentation_comments_presentation_id_presentations",
        table="presentation_comments",
        column="presentation_id",
        ref_table="presentations",
        ref_column="id",
        on_delete="CASCADE",
    )
    _add_fk(
        conn,
        name="fk_presentation_comments_parent_id_presentation_comments",
        table="presentation_comments",
        column="parent_id",
        ref_table="presentation_comments",
        ref_column="id",
        on_delete="CASCADE",
    )
    _add_fk(
        conn,
        name="fk_presentation_comments_author_id_users",
        table="presentation_comments",
        column="author_id",
        ref_table="users",
        ref_column="id",
        on_delete="SET NULL",
    )
    _add_fk(
        conn,
        name="fk_presentation_comments_resolved_by_user_id_users",
        table="presentation_comments",
        column="resolved_by_user_id",
        ref_table="users",
        ref_column="id",
        on_delete="SET NULL",
    )
    _create_index_concurrently(
        "ix_presentation_comments_presentation_id",
        "presentation_comments",
        "presentation_id",
    )
    _create_index_concurrently(
        "ix_presentation_comments_presentation_created",
        "presentation_comments",
        "presentation_id, created_at",
    )


def downgrade() -> None:
    conn = op.get_bind()
    set_lock_timeout(conn)
    conn.execute(sa.text("DROP TABLE IF EXISTS presentation_comments"))
    conn.execute(sa.text("DROP TABLE IF EXISTS presentation_versions"))
    if _column_exists(conn, "presentations", "updated_by_user_id"):
        terminate_lock_holders(conn, "presentations")
        set_lock_timeout(conn)
        conn.execute(sa.text("ALTER TABLE presentations DROP COLUMN IF EXISTS updated_by_user_id"))
