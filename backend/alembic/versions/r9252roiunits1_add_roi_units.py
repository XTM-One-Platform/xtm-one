"""add roi_units and roi_unit_versions (#9252)

Open ROI unit catalog, slice O1 of ``dev-docs/roi-outcomes.md`` (section
3.1): the organization's custom units of work, next to the built-in catalog
of ``services/roi.py``, and every version of each.

New tables, created only when missing (idempotent); no index beyond the
primary keys and the unique constraint, built in place on new empty tables,
so no CONCURRENTLY is needed. The FK-bearing CREATE TABLE and the downgrade
follow the lock-safe recipe (``terminate_lock_holders`` + ``set_lock_timeout``).

Revision ID: r9252roiunits1
Revises: r9162mbrdflt1
Create Date: 2026-11-06 14:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from app.core.migration_ddl import set_lock_timeout, terminate_lock_holders

revision: str = "r9252roiunits1"
# NOT a constant: must always be main's CURRENT head, re-pointed on every rebase
# (backend/AGENTS.md > "Database migrations (safety)", rule 1).
down_revision: str | Sequence[str] | None = "r9162mbrdflt1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_UNITS = "roi_units"
_VERSIONS = "roi_unit_versions"


def _has_table(conn, name: str) -> bool:
    return bool(
        conn.execute(
            sa.text(
                "SELECT 1 FROM information_schema.tables WHERE table_schema = current_schema() AND table_name = :n"
            ),
            {"n": name},
        ).scalar()
    )


def _user_fk(name: str) -> sa.Column:
    return sa.Column(
        name,
        postgresql.UUID(as_uuid=True),
        sa.ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )


def upgrade() -> None:
    conn = op.get_bind()
    if not _has_table(conn, _UNITS):
        terminate_lock_holders(conn, "users")
        set_lock_timeout(conn)
        op.create_table(
            _UNITS,
            sa.Column("key", sa.String(64), nullable=False),
            sa.Column("name", sa.String(200), nullable=False),
            sa.Column("role", sa.String(100), nullable=False, server_default=sa.text("''")),
            sa.Column("manual_min_low", sa.Float(), nullable=False),
            sa.Column("manual_min_likely", sa.Float(), nullable=False),
            sa.Column("manual_min_high", sa.Float(), nullable=False),
            sa.Column("review_min", sa.Float(), nullable=False),
            sa.Column("rate_role", sa.String(64), nullable=False, server_default=sa.text("'analyst'")),
            sa.Column("source", sa.String(300), nullable=False, server_default=sa.text("''")),
            sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("version", sa.Integer(), nullable=False, server_default=sa.text("1")),
            _user_fk("created_by_user_id"),
            _user_fk("updated_by_user_id"),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
            sa.PrimaryKeyConstraint("key"),
            sa.CheckConstraint(
                "manual_min_low <= manual_min_likely AND manual_min_likely <= manual_min_high",
                name="ck_roi_units_ordered",
            ),
            sa.CheckConstraint("manual_min_low >= 0 AND review_min >= 0", name="ck_roi_units_positive"),
            if_not_exists=True,
        )
    if not _has_table(conn, _VERSIONS):
        terminate_lock_holders(conn, "users")
        set_lock_timeout(conn)
        op.create_table(
            _VERSIONS,
            sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column("unit_key", sa.String(64), nullable=False),
            sa.Column("version", sa.Integer(), nullable=False),
            sa.Column("snapshot", postgresql.JSONB(), nullable=False),
            _user_fk("user_id"),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("unit_key", "version", name="uq_roi_unit_versions_unit_version"),
            if_not_exists=True,
        )


def downgrade() -> None:
    conn = op.get_bind()
    for table in (_VERSIONS, _UNITS):
        if not _has_table(conn, table):
            continue
        terminate_lock_holders(conn, table)
        set_lock_timeout(conn)
        op.drop_table(table, if_exists=True)
