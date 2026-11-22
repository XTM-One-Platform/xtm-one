"""health_check_findings: open after a sustained failure, alert after a window (#958)

A restart of XTM One or of a module opened critical findings and mailed them at
once. A finding now opens on its second failing read in a row (``fail_streak``,
the ``pending`` status before), its immediate email goes out once it has been open
for the alert window (``opened_at``) and at most once per open episode
(``alerted_at``); a module restart is stamped on its snapshot (``restarted_at``).

Lock-light: nullable or constant-default ``ADD COLUMN`` (catalog changes on
PostgreSQL 11+, no rewrite) and the status check constraint swapped. Open rows
get ``opened_at = first_seen`` and ``alerted_at = last_seen`` so nothing already
open mails again after the upgrade. Re-running after an interruption is a no-op
(columns probed first).

Revision ID: c958healthsustain1
Revises: c942decisions01
Create Date: 2026-11-21 19:10:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.core.migration_ddl import set_lock_timeout, terminate_lock_holders

revision: str = "c958healthsustain1"
# NOT a constant: must always be main's CURRENT head, re-pointed whenever main
# gains a migration (backend/AGENTS.md > "Database migrations (safety)", rule 1).
down_revision: str | Sequence[str] | None = "c942decisions01"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_FINDINGS = "health_check_findings"
_SNAPSHOTS = "platform_health_snapshots"
_STATUS_CK = "ck_health_check_findings_status_valid"


def _columns(conn: sa.engine.Connection, table: str) -> set[str]:
    rows = conn.execute(
        sa.text(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = current_schema() AND table_name = :t"
        ),
        {"t": table},
    ).scalars()
    return set(rows)


def upgrade() -> None:
    conn = op.get_bind()
    findings = _columns(conn, _FINDINGS)
    if findings and "fail_streak" not in findings:
        terminate_lock_holders(conn, _FINDINGS)
        set_lock_timeout(conn)
        op.add_column(_FINDINGS, sa.Column("fail_streak", sa.Integer(), nullable=False, server_default=sa.text("0")))
        op.add_column(_FINDINGS, sa.Column("opened_at", sa.DateTime(timezone=True), nullable=True))
        op.add_column(_FINDINGS, sa.Column("alerted_at", sa.DateTime(timezone=True), nullable=True))
        op.drop_constraint(_STATUS_CK, _FINDINGS, type_="check")
        op.create_check_constraint(_STATUS_CK, _FINDINGS, "status IN ('open', 'resolved', 'dismissed', 'pending')")
        # What is open today was already mailed (or not) under the old rule: never again.
        conn.execute(
            sa.text(
                f"UPDATE {_FINDINGS} SET opened_at = first_seen, alerted_at = last_seen "  # noqa: S608 (constant)
                "WHERE status = 'open'"
            )
        )
    snapshots = _columns(conn, _SNAPSHOTS)
    if snapshots and "restarted_at" not in snapshots:
        terminate_lock_holders(conn, _SNAPSHOTS)
        set_lock_timeout(conn)
        op.add_column(_SNAPSHOTS, sa.Column("restarted_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    conn = op.get_bind()
    if "restarted_at" in _columns(conn, _SNAPSHOTS):
        terminate_lock_holders(conn, _SNAPSHOTS)
        set_lock_timeout(conn)
        op.drop_column(_SNAPSHOTS, "restarted_at")
    if "fail_streak" in _columns(conn, _FINDINGS):
        terminate_lock_holders(conn, _FINDINGS)
        set_lock_timeout(conn)
        conn.execute(sa.text(f"DELETE FROM {_FINDINGS} WHERE status = 'pending'"))  # noqa: S608 (constant)
        op.drop_constraint(_STATUS_CK, _FINDINGS, type_="check")
        op.create_check_constraint(_STATUS_CK, _FINDINGS, "status IN ('open', 'resolved', 'dismissed')")
        op.drop_column(_FINDINGS, "alerted_at")
        op.drop_column(_FINDINGS, "opened_at")
        op.drop_column(_FINDINGS, "fail_streak")
