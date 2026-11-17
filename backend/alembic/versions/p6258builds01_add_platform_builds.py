"""add platform_builds

The builds the platform has booted with: one row per ``(version, git_sha)``,
written at startup by ``services/platform_builds.record_running_build``. The
``platform_insights`` tools cut counts and costs per deployed build from it.
Nothing is backfilled: the history starts at the first boot that records it.

Lock profile: a new table with no foreign key, so ``CREATE TABLE`` and its
unique constraint take no lock any other session holds. The belt is set anyway.
Dropping it (downgrade) takes ACCESS EXCLUSIVE on the table itself and follows
the canonical recipe.

The column set mirrors ``PlatformBuild`` in ``app/models/platform_build.py``,
the constraint its declaration, so a fresh install's ``create_all`` and this
body agree.

Revision ID: p6258builds01
Revises: r6169wake01
Create Date: 2026-10-21 20:30:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.core.migration_ddl import set_lock_timeout, terminate_lock_holders

revision: str = "p6258builds01"
# NOT a constant: must always be main's CURRENT head, re-pointed whenever main
# gains a migration (backend/AGENTS.md > "Database migrations (safety)", rule 1).
down_revision: str | Sequence[str] | None = "r6169wake01"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "platform_builds"


def _has_table(conn) -> bool:
    return bool(
        conn.execute(
            sa.text(
                "SELECT 1 FROM information_schema.tables "
                "WHERE table_schema = current_schema() AND table_name = :n"
            ),
            {"n": _TABLE},
        ).scalar()
    )


def upgrade() -> None:
    conn = op.get_bind()
    if _has_table(conn):
        return
    set_lock_timeout(conn)
    op.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {_TABLE} (
            id UUID PRIMARY KEY,
            version VARCHAR(100) NOT NULL,
            git_sha VARCHAR(64) NOT NULL,
            first_seen_at TIMESTAMPTZ NOT NULL,
            last_seen_at TIMESTAMPTZ NOT NULL,
            CONSTRAINT uq_platform_builds_version_sha UNIQUE (version, git_sha)
        )
        """
    )


def downgrade() -> None:
    conn = op.get_bind()
    if not _has_table(conn):
        return
    terminate_lock_holders(conn, _TABLE)
    set_lock_timeout(conn)
    op.execute(f"DROP TABLE IF EXISTS {_TABLE}")
