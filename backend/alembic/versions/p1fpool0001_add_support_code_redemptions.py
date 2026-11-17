"""add support_code_redemptions — the burn ledger for Filigran support codes

Purely additive: one new table, no change to any existing column, so old
replicas during a rolling deploy simply ignore a table they do not know about.
Nothing is backfilled — no prior state can be read as a redeemed code.

The table exists for one reason: a support code is verified **offline**,
against the license trust anchor already embedded in the image, so its
signature alone is replayable forever. Storing the code's own id here under a
UNIQUE constraint is what makes redemption single-use, and makes two concurrent
redemptions of the same code a database refusal rather than a race between two
application-level checks.

Revision ID: p1fpool0001
Revises: a2a2intmodel1
Create Date: 2026-08-25 12:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from app.core.migration_ddl import set_lock_timeout

revision: str = "p1fpool0001"
down_revision: str | Sequence[str] | None = "a2a2intmodel1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "support_code_redemptions"


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


def upgrade() -> None:
    conn = op.get_bind()

    # Idempotency probe: fresh installs run create_all + stamp head (this body
    # never executes), and the migrations CI job re-runs the revision against
    # an already-upgraded database. Both must be no-ops rather than errors.
    if _has_table(conn, _TABLE):
        return

    # Nothing else knows this table exists yet: no `terminate_lock_holders`,
    # and the indexes are built non-concurrently. The foreign key to `users`
    # takes a SHARE ROW EXCLUSIVE lock there; the lock_timeout belt makes a
    # contended attempt fail fast (55P03, retried by the startup runner)
    # instead of queueing every writer to `users` behind it.
    set_lock_timeout(conn)
    op.create_table(
        _TABLE,
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        # The code's own id claim. UNIQUE is the single-use guarantee.
        sa.Column("code_id", sa.String(64), nullable=False),
        sa.Column("kind", sa.String(64), nullable=False),
        sa.Column("platform_scope", sa.String(64), nullable=True),
        sa.Column("issued_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "redeemed_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        # SET NULL, not CASCADE: deleting the admin who redeemed a code must
        # not delete the proof the code was burned.
        sa.Column(
            "redeemed_by_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("note", sa.Text(), nullable=True),
        sa.UniqueConstraint("code_id", name="uq_support_code_redemptions_code_id"),
    )
    op.create_index(
        "ix_support_code_redemptions_code_id", _TABLE, ["code_id"]
    )
    op.create_index(
        "ix_support_code_redemptions_redeemed_by_user_id",
        _TABLE,
        ["redeemed_by_user_id"],
    )


def downgrade() -> None:
    conn = op.get_bind()

    if _has_table(conn, _TABLE):
        # Dropping the table removes its foreign key triggers from `users`.
        set_lock_timeout(conn)
        op.drop_table(_TABLE)
