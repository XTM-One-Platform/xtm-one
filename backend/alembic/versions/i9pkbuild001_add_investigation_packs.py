"""add investigation_packs, investigation_pack_versions and two run columns

The Deep Investigation pack builder: packs users build, with the
three-tier visibility columns, and one immutable row per saved definition.
A run records the version of each built pack it used
(``investigations.source_pack_versions``) and the plan it followed
(``investigations.declared_plan``). Purely additive; old replicas ignore the
tables and the nullable columns.

Lock profile: the two tables are new, but their foreign keys to ``users``
(and to ``investigation_packs``) take a SHARE ROW EXCLUSIVE lock on the
referenced table, so they are created under the lock_timeout belt. The two
nullable columns without a default are metadata-only on ``investigations``,
which every live block polls: canonical pre-flight (probe ->
terminate_lock_holders -> set_lock_timeout -> guarded DDL).

Revision ID: i9pkbuild001
Revises: q5257shrgnt1
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from app.core.migration_ddl import set_lock_timeout, terminate_lock_holders

revision: str = "i9pkbuild001"
# NOT a constant: must always be main's CURRENT head, re-pointed whenever main
# gains a migration (backend/AGENTS.md > "Database migrations (safety)", rule 1).
down_revision: str | Sequence[str] | None = "q5257shrgnt1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_PACKS = "investigation_packs"
_VERSIONS = "investigation_pack_versions"
_RUNS = "investigations"
_RUN_COLUMNS = ("source_pack_versions", "declared_plan")


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


def _has_index(conn, name: str) -> bool:
    return bool(
        conn.execute(
            sa.text("SELECT 1 FROM pg_indexes WHERE schemaname = current_schema() AND indexname = :n"),
            {"n": name},
        ).scalar()
    )


def upgrade() -> None:
    conn = op.get_bind()

    if not _has_table(conn, _PACKS):
        set_lock_timeout(conn)
        op.create_table(
            _PACKS,
            sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
            sa.Column(
                "user_id",
                postgresql.UUID(as_uuid=True),
                sa.ForeignKey("users.id", ondelete="CASCADE"),
                nullable=True,
            ),
            sa.Column("name", sa.String(200), nullable=False),
            sa.Column("description", sa.Text(), nullable=False, server_default=sa.text("''")),
            sa.Column("subject_types", postgresql.JSON(), nullable=False),
            sa.Column("recommended", sa.Boolean(), nullable=False, server_default=sa.text("false")),
            sa.Column("max_iterations", sa.Integer(), nullable=False, server_default=sa.text("10")),
            sa.Column("definition", postgresql.JSON(), nullable=False),
            sa.Column("version_number", sa.Integer(), nullable=False, server_default=sa.text("1")),
            sa.Column("is_company_managed", sa.Boolean(), nullable=True, server_default=sa.text("false")),
            sa.Column("shared_group_ids", postgresql.JSON(), nullable=True),
            sa.Column("shared_group_permissions", postgresql.JSON(), nullable=True),
            sa.Column("shared_user_ids", postgresql.JSON(), nullable=True),
            sa.Column("shared_user_permissions", postgresql.JSON(), nullable=True),
            sa.Column("access_restricted", sa.Boolean(), nullable=False, server_default=sa.text("false")),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
            if_not_exists=True,
        )
    if not _has_index(conn, "ix_investigation_packs_user_id"):
        op.create_index("ix_investigation_packs_user_id", _PACKS, ["user_id"], if_not_exists=True)

    if not _has_table(conn, _VERSIONS):
        set_lock_timeout(conn)
        op.create_table(
            _VERSIONS,
            sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
            sa.Column(
                "pack_id",
                postgresql.UUID(as_uuid=True),
                sa.ForeignKey("investigation_packs.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("version_number", sa.Integer(), nullable=False),
            sa.Column("definition", postgresql.JSON(), nullable=False),
            sa.Column("subject_types", postgresql.JSON(), nullable=False),
            sa.Column(
                "author_id",
                postgresql.UUID(as_uuid=True),
                sa.ForeignKey("users.id", ondelete="SET NULL"),
                nullable=True,
            ),
            sa.Column("commit_message", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
            sa.UniqueConstraint("pack_id", "version_number", name="uq_investigation_pack_versions_pack_ver"),
            sa.CheckConstraint("version_number > 0", name="ck_investigation_pack_versions_positive"),
            if_not_exists=True,
        )
    for name, column in (
        ("ix_investigation_pack_versions_pack_id", "pack_id"),
        ("ix_investigation_pack_versions_author_id", "author_id"),
        ("ix_investigation_pack_versions_created_at", "created_at"),
    ):
        if not _has_index(conn, name):
            op.create_index(name, _VERSIONS, [column], if_not_exists=True)

    missing = [c for c in _RUN_COLUMNS if not _column_exists(conn, _RUNS, c)]
    if missing:
        terminate_lock_holders(conn, _RUNS)
        set_lock_timeout(conn)
        for column in missing:
            op.execute(f"ALTER TABLE {_RUNS} ADD COLUMN IF NOT EXISTS {column} JSON")


def downgrade() -> None:
    conn = op.get_bind()
    present = [c for c in _RUN_COLUMNS if _column_exists(conn, _RUNS, c)]
    if present:
        terminate_lock_holders(conn, _RUNS)
        set_lock_timeout(conn)
        for column in present:
            op.execute(f"ALTER TABLE {_RUNS} DROP COLUMN IF EXISTS {column}")
    # DROP TABLE removes the foreign-key triggers on the referenced tables,
    # an ACCESS EXCLUSIVE lock there: under the belt too.
    if _has_table(conn, _VERSIONS):
        set_lock_timeout(conn)
        op.drop_table(_VERSIONS, if_exists=True)
    if _has_table(conn, _PACKS):
        set_lock_timeout(conn)
        op.drop_table(_PACKS, if_exists=True)
