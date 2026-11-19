"""Platform Health engine: health_check_findings, health_score_days, fleets.health_enrolled

Slice S2 of issue #101 (design ``dev-docs/platform-health.md`` sections 1.2
and 3): the deterministic engine's finding store (one row per check and
subject, stable id), its daily score history per module (90 days) and the
fleet enrolment switch (default off: nothing is enrolled on upgrade).

Two new tables with no foreign key: their CREATE takes no lock another
session holds, their indexes are built in place on new, empty tables. The
fleet column is a nullable-free boolean with a server default, metadata-only
on PostgreSQL 11+, behind the lock-safe recipe. No backfill. Fresh installs run
``create_all`` + ``stamp head``: the body never executes there.

Revision ID: h122healthengine
Revises: h121healthpet
Create Date: 2026-11-19 10:45:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from app.core.migration_ddl import set_lock_timeout, terminate_lock_holders

revision: str = "h122healthengine"
# NOT a constant: must always be main's CURRENT head, re-pointed on every rebase
# (backend/AGENTS.md > "Database migrations (safety)", rule 1).
down_revision: str | Sequence[str] | None = "h121healthpet"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_FINDINGS = "health_check_findings"
_DAYS = "health_score_days"
_FLEETS = "fleets"
_COLUMN = "health_enrolled"

_MODULES = "('agents', 'fleets', 'xtm_one', 'opencti', 'openaev', 'opencrq')"


def _has_column(conn) -> bool:
    return bool(
        conn.execute(
            sa.text(
                "SELECT 1 FROM information_schema.columns "
                "WHERE table_schema = current_schema() AND table_name = :table AND column_name = :column"
            ),
            {"table": _FLEETS, "column": _COLUMN},
        ).first()
    )


def upgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    if not inspector.has_table(_FINDINGS):
        set_lock_timeout(conn)
        op.create_table(
            _FINDINGS,
            sa.Column("id", sa.String(64), nullable=False),
            sa.Column("check_id", sa.String(96), nullable=False),
            sa.Column("module", sa.String(16), nullable=False),
            sa.Column("subject", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
            sa.Column("category", sa.String(16), nullable=False),
            sa.Column("severity", sa.String(16), nullable=False),
            sa.Column("status", sa.String(16), nullable=False),
            sa.Column("evidence", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
            sa.Column("recommendation", sa.Text(), nullable=False, server_default=sa.text("''")),
            sa.Column("doc_link", sa.String(512), nullable=False, server_default=sa.text("''")),
            sa.Column("pass_streak", sa.Integer(), nullable=False, server_default=sa.text("0")),
            sa.Column("first_seen", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
            sa.Column("last_seen", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
            sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("dismissal", postgresql.JSONB(), nullable=True),
            sa.PrimaryKeyConstraint("id"),
            sa.CheckConstraint(f"module IN {_MODULES}", name="ck_health_check_findings_module_valid"),
            sa.CheckConstraint(
                "category IN ('availability', 'performance', 'capacity', 'configuration', 'security', "
                "'licence', 'coverage', 'cost')",
                name="ck_health_check_findings_category_valid",
            ),
            sa.CheckConstraint(
                "severity IN ('critical', 'warning', 'info')", name="ck_health_check_findings_severity_valid"
            ),
            sa.CheckConstraint(
                "status IN ('open', 'resolved', 'dismissed')", name="ck_health_check_findings_status_valid"
            ),
            if_not_exists=True,
        )
        op.create_index(
            "ix_health_check_findings_module_status", _FINDINGS, ["module", "status"], if_not_exists=True
        )
        op.create_index("ix_health_check_findings_check", _FINDINGS, ["check_id"], if_not_exists=True)
    if not inspector.has_table(_DAYS):
        set_lock_timeout(conn)
        op.create_table(
            _DAYS,
            sa.Column("day", sa.Date(), nullable=False),
            sa.Column("module", sa.String(16), nullable=False),
            sa.Column("score", sa.Integer(), nullable=False),
            sa.Column("critical", sa.Integer(), nullable=False, server_default=sa.text("0")),
            sa.Column("warning", sa.Integer(), nullable=False, server_default=sa.text("0")),
            sa.Column("info", sa.Integer(), nullable=False, server_default=sa.text("0")),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
            sa.PrimaryKeyConstraint("day", "module"),
            sa.CheckConstraint(f"module IN {_MODULES}", name="ck_health_score_days_module_valid"),
            if_not_exists=True,
        )
    if inspector.has_table(_FLEETS) and not _has_column(conn):
        terminate_lock_holders(conn, _FLEETS)
        set_lock_timeout(conn)
        op.execute(f"ALTER TABLE {_FLEETS} ADD COLUMN IF NOT EXISTS {_COLUMN} BOOLEAN NOT NULL DEFAULT false")


def downgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    if inspector.has_table(_FLEETS):
        terminate_lock_holders(conn, _FLEETS)
        set_lock_timeout(conn)
        op.execute(f"ALTER TABLE {_FLEETS} DROP COLUMN IF EXISTS {_COLUMN}")
    for table in (_DAYS, _FINDINGS):
        if inspector.has_table(table):
            terminate_lock_holders(conn, table)
            set_lock_timeout(conn)
            op.drop_table(table)
