"""video_project_revisions: the spec of a video at each revision (#10798)

One row per save that makes a revision (``projects.save_spec``): the authored
spec stored, the author, the change lines and the field paths changed per
scene, so ``get_video(revision=N)`` reads an earlier spec and ``edit_video``'s
``restore_scene`` copies a scene back. A new table, nothing rewritten; the
lock-safe recipe (``terminate_lock_holders`` + ``set_lock_timeout``) covers the
``video_projects`` FK. Fresh installs run ``create_all`` + ``stamp head``: the
body never executes there.

Revision ID: v10798vidrev1
Revises: u10741tmplro1
Create Date: 2026-11-10 12:30:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from app.core.migration_ddl import set_lock_timeout, terminate_lock_holders

revision: str = "v10798vidrev1"
# NOT a constant: must always be main's CURRENT head, re-pointed on every rebase
# (backend/AGENTS.md > "Database migrations (safety)", rule 1).
down_revision: str | Sequence[str] | None = "u10741tmplro1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "video_project_revisions"


def _has_table(conn, name: str) -> bool:
    return bool(
        conn.execute(
            sa.text(
                "SELECT 1 FROM information_schema.tables WHERE table_schema = current_schema() AND table_name = :n"
            ),
            {"n": name},
        ).scalar()
    )


def upgrade() -> None:
    conn = op.get_bind()
    if _has_table(conn, _TABLE):
        return
    terminate_lock_holders(conn, "video_projects")
    set_lock_timeout(conn)
    op.create_table(
        _TABLE,
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "project_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("video_projects.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("spec", sa.JSON(), nullable=False),
        sa.Column("summary", sa.JSON(), nullable=False),
        sa.Column("changed", sa.JSON(), nullable=False),
        sa.Column("author_label", sa.String(320), nullable=True),
        sa.Column("via", sa.String(16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.UniqueConstraint("project_id", "revision", name="uq_video_project_revisions_revision"),
        if_not_exists=True,
    )


def downgrade() -> None:
    conn = op.get_bind()
    if _has_table(conn, _TABLE):
        terminate_lock_holders(conn, _TABLE)
        set_lock_timeout(conn)
        op.drop_table(_TABLE, if_exists=True)
