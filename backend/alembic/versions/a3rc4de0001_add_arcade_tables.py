"""add arcade tables (arcade_scores, arcade_profiles) for the waiting arcade

Purely additive: two new tables, no change to any existing column, so old
replicas during a rolling deploy simply ignore tables they do not know about.
Nothing is backfilled - there is no prior state that could be read as a run.

``arcade_scores`` holds one row per finished Space Invaders run the server
accepted; the leaderboards are computed from it. ``arcade_profiles``
holds the per-player callsign, public-board opt-out and unlocked achievements.

Revision ID: a3rc4de0001
Revises: a7f3run0004
Create Date: 2026-09-26 12:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from app.core.migration_ddl import set_lock_timeout

revision: str = "a3rc4de0001"
# NOT a constant: must always be main's CURRENT head, re-pointed whenever main
# gains a migration (backend/AGENTS.md > "Database migrations (safety)", rule 1).
down_revision: str | Sequence[str] | None = "a7f3run0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_SCORES = "arcade_scores"
_PROFILES = "arcade_profiles"

#: The same definitions as ``ArcadeScore.__table_args__`` (the create_all path).
_SCORE_INDEXES: tuple[tuple[str, list], ...] = (
    # The 24-hour and 7-day boards filter on the run date first.
    ("ix_arcade_scores_created_at", ["created_at"]),
    # Best run per player - DISTINCT ON (user_id) ORDER BY score DESC,
    # created_at - read in index order, a player's best and stats, and the
    # lookup behind the users ON DELETE CASCADE.
    ("ix_arcade_scores_user_best", ["user_id", sa.text("score DESC"), "created_at"]),
)


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


def _has_index(conn, name: str) -> bool:
    return bool(
        conn.execute(
            sa.text(
                "SELECT 1 FROM pg_indexes "
                "WHERE schemaname = current_schema() AND indexname = :n"
            ),
            {"n": name},
        ).scalar()
    )


def upgrade() -> None:
    conn = op.get_bind()

    # Idempotency probes: fresh installs run create_all + stamp head (this body
    # never executes), and the migrations CI job re-runs the revision against
    # an already-upgraded database. Both must be no-ops rather than errors,
    # and every table and index is probed on its own, so a partial state
    # converges too.
    #
    # Nothing else knows these tables exist yet, so nothing can hold a lock on
    # them: no `terminate_lock_holders`, and the indexes below are built
    # non-concurrently, in the same transaction as their table. The foreign key
    # to `users` is the exception: CREATE TABLE installs its triggers on
    # `users`, which takes a SHARE ROW EXCLUSIVE lock there (DROP TABLE takes
    # an ACCESS EXCLUSIVE one to remove them). The lock_timeout belt makes a
    # contended attempt fail fast (55P03, retried by the startup runner)
    # instead of queueing every writer to `users` behind it. Terminating the
    # holders of `users` would kill sessions that merely read it, which this
    # lock does not even conflict with.
    if not _has_table(conn, _SCORES):
        set_lock_timeout(conn)
        op.create_table(
            _SCORES,
            sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
            sa.Column(
                "user_id",
                postgresql.UUID(as_uuid=True),
                sa.ForeignKey("users.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("score", sa.Integer(), nullable=False),
            sa.Column("wave", sa.Integer(), nullable=False, server_default=sa.text("1")),
            sa.Column("kills", sa.Integer(), nullable=False, server_default=sa.text("0")),
            sa.Column("shots", sa.Integer(), nullable=False, server_default=sa.text("0")),
            sa.Column("hits", sa.Integer(), nullable=False, server_default=sa.text("0")),
            sa.Column(
                "ui_destroyed", sa.Integer(), nullable=False, server_default=sa.text("0")
            ),
            sa.Column("bosses", sa.Integer(), nullable=False, server_default=sa.text("0")),
            sa.Column(
                "max_combo", sa.Integer(), nullable=False, server_default=sa.text("0")
            ),
            sa.Column(
                "ufo_kills", sa.Integer(), nullable=False, server_default=sa.text("0")
            ),
            sa.Column(
                "duration_ms", sa.Integer(), nullable=False, server_default=sa.text("0")
            ),
            sa.Column(
                "mode", sa.String(16), nullable=False, server_default=sa.text("'wait'")
            ),
            sa.Column(
                "ranked", sa.Boolean(), nullable=False, server_default=sa.text("true")
            ),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.text("now()"),
            ),
            if_not_exists=True,
        )

    if not _has_table(conn, _PROFILES):
        set_lock_timeout(conn)
        op.create_table(
            _PROFILES,
            sa.Column(
                "user_id",
                postgresql.UUID(as_uuid=True),
                sa.ForeignKey("users.id", ondelete="CASCADE"),
                primary_key=True,
            ),
            sa.Column("callsign", sa.String(3), nullable=True),
            sa.Column(
                "hidden", sa.Boolean(), nullable=False, server_default=sa.text("false")
            ),
            sa.Column("achievements", postgresql.JSON(), nullable=True),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.text("now()"),
            ),
            sa.Column(
                "updated_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.text("now()"),
            ),
            if_not_exists=True,
        )

    missing = [(name, columns) for name, columns in _SCORE_INDEXES if not _has_index(conn, name)]
    if missing:
        # Instant on the table created above; the belt matters for a table left
        # without an index, which may already be taking runs.
        set_lock_timeout(conn)
        for name, columns in missing:
            op.create_index(name, _SCORES, columns, if_not_exists=True)


def downgrade() -> None:
    conn = op.get_bind()

    # Both tables reference only `users`, never each other: drop order is free.
    # Each drop takes an ACCESS EXCLUSIVE lock on `users` to remove its foreign
    # key triggers (see upgrade): the belt makes a busy `users` fail the
    # downgrade fast, to be retried when the table is quiet.
    for table in (_PROFILES, _SCORES):
        if _has_table(conn, table):
            set_lock_timeout(conn)
            op.drop_table(table, if_exists=True)
