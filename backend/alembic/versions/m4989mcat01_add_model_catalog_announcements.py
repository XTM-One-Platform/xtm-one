"""add model_catalog_announcements - the outbox of model catalog change emails

Purely additive: two new tables, so old replicas during a rolling deploy ignore
tables they do not know. The only foreign key joins the two new tables to each
other (``model_catalog_announcement_recipients`` -> ``model_catalog_announcements``);
none points at an existing table, so no existing table is locked. Nothing is
backfilled: the first boot that runs the new code records the catalog it finds
as the baseline.

Both tables stay small by construction - one announcement per change to the
env-managed model catalog, one recipient row per user emailed about it - so the
announcements table carries no secondary index (the scheduler's claim reads it
whole) and the recipients table is served by its primary key.

Revision ID: m4989mcat01
Revises: w4646upload01
Create Date: 2026-09-29 23:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from app.core.migration_ddl import set_lock_timeout

revision: str = "m4989mcat01"
down_revision: str | Sequence[str] | None = "w4646upload01"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "model_catalog_announcements"
_RECIPIENTS = "model_catalog_announcement_recipients"


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

    # Idempotency probe per table: fresh installs run create_all + stamp head
    # (this body never executes), and a crashed replica may run it again.
    has_announcements = _has_table(conn, _TABLE)
    has_recipients = _has_table(conn, _RECIPIENTS)
    if has_announcements and has_recipients:
        return

    set_lock_timeout(conn)
    if not has_announcements:
        op.create_table(
            _TABLE,
            sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.text("now()"),
            ),
            sa.Column("previous_models", sa.JSON(), nullable=True),
            sa.Column("previous_default_model", sa.String(255), nullable=True),
            sa.Column("models", sa.JSON(), nullable=False),
            sa.Column("default_model", sa.String(255), nullable=True),
            sa.Column("default_provider", sa.String(64), nullable=True),
            sa.Column("status", sa.String(20), nullable=False),
            sa.Column("skip_reason", sa.String(64), nullable=True),
            sa.Column("failed_attempts", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("claim_token", postgresql.UUID(as_uuid=True), nullable=True),
            sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("last_error", sa.Text(), nullable=True),
        )
    if not has_recipients:
        op.create_table(
            _RECIPIENTS,
            sa.Column(
                "announcement_id",
                postgresql.UUID(as_uuid=True),
                sa.ForeignKey(f"{_TABLE}.id", ondelete="CASCADE"),
                primary_key=True,
            ),
            sa.Column("user_id", postgresql.UUID(as_uuid=True), primary_key=True),
            sa.Column("status", sa.String(20), nullable=False),
            sa.Column(
                "updated_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.text("now()"),
            ),
        )


def downgrade() -> None:
    conn = op.get_bind()

    has_announcements = _has_table(conn, _TABLE)
    has_recipients = _has_table(conn, _RECIPIENTS)
    if not has_announcements and not has_recipients:
        return

    set_lock_timeout(conn)
    if has_recipients:
        op.drop_table(_RECIPIENTS)
    if has_announcements:
        op.drop_table(_TABLE)
