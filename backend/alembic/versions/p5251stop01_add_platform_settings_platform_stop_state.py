"""add the open platform emergency stop's state to platform_settings

``platform_stop_at``, ``platform_stop_times`` and ``platform_stop_ai_override``
hold the platform-wide emergency stop that is in effect (``services/panic.py``).
It was derived from the ``panic_stop`` / ``panic_resume`` rows of
``audit_logs``, which cascade with the user who pressed the stop, so deleting
that user closed the stop while its paused assignments and its AI override
stayed in place.

Backfill: one row. When the trail says a platform stop is open at upgrade time
(a platform ``panic_stop`` newer than the latest platform ``panic_resume``),
the singleton gets that sequence's state: the first stop's ``stopped_at`` and
pre-stop AI override, and every stop's ``stopped_at``. The read is bounded to
the platform stop rows (``action`` is indexed).

Lock profile: three nullable ``ADD COLUMN`` with no default are metadata-only
on PG 11+, but the ALTER takes ACCESS EXCLUSIVE on ``platform_settings``, so
it follows the canonical recipe (probe, ``terminate_lock_holders``,
``set_lock_timeout``, guarded DDL). The downgrade drops the columns the same
way.

Revision ID: p5251stop01
Revises: m5190mcptok01
Create Date: 2026-10-24 12:00:00.000000
"""

import json
from collections.abc import Sequence
from datetime import datetime

import sqlalchemy as sa

from alembic import op
from app.core.migration_ddl import set_lock_timeout, terminate_lock_holders

revision: str = "p5251stop01"
# NOT a constant: must always be main's CURRENT head, re-pointed whenever main
# gains a migration (backend/AGENTS.md > "Database migrations (safety)", rule 1).
down_revision: str | Sequence[str] | None = "m5190mcptok01"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "platform_settings"
_COLUMNS = {
    "platform_stop_at": "TIMESTAMP WITH TIME ZONE",
    "platform_stop_times": "JSON",
    "platform_stop_ai_override": "BOOLEAN",
}


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


def _parse(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return None


def _backfill_open_stop(conn) -> None:
    """Carry a platform stop open at upgrade time over to the new columns."""
    last_resume = conn.execute(
        sa.text(
            "SELECT max(created_at) FROM audit_logs "
            "WHERE action = 'panic_resume' AND entity_type = 'platform'"
        )
    ).scalar()
    query = (
        "SELECT created_at, details->>'stopped_at' AS stopped_at, "
        "details->>'ai_override_before_stop' AS ai_override "
        "FROM audit_logs WHERE action = 'panic_stop' AND entity_type = 'platform'"
    )
    params: dict = {}
    if last_resume is not None:
        query += " AND created_at > :last_resume"
        params["last_resume"] = last_resume
    rows = conn.execute(sa.text(query + " ORDER BY created_at ASC"), params).all()
    if not rows:
        return
    times = [_parse(row.stopped_at) or row.created_at for row in rows]
    override = {"true": True, "false": False}.get(rows[0].ai_override)
    conn.execute(
        sa.text(
            "UPDATE platform_settings SET platform_stop_at = :at, "
            "platform_stop_times = CAST(:times AS JSON), "
            "platform_stop_ai_override = :override "
            "WHERE platform_stop_at IS NULL"
        ),
        {
            "at": times[0],
            "times": json.dumps([t.isoformat() for t in times]),
            "override": override,
        },
    )


def upgrade() -> None:
    conn = op.get_bind()
    missing = [c for c in _COLUMNS if not _column_exists(conn, _TABLE, c)]
    # 1. Idempotency probe — steady-state re-runs take NO lock.
    if not missing:
        return
    # 2. Pre-flight: kill same-role lock holders on the table the DDL locks.
    terminate_lock_holders(conn, _TABLE)
    # 3. Safety belt: fail fast (55P03) instead of wedging the lock queue.
    set_lock_timeout(conn)
    # 4. Guarded DDL.
    for column in missing:
        op.execute(
            f"ALTER TABLE {_TABLE} ADD COLUMN IF NOT EXISTS {column} {_COLUMNS[column]}"
        )
    _backfill_open_stop(conn)


def downgrade() -> None:
    conn = op.get_bind()
    present = [c for c in _COLUMNS if _column_exists(conn, _TABLE, c)]
    if not present:
        return
    terminate_lock_holders(conn, _TABLE)
    set_lock_timeout(conn)
    for column in present:
        op.execute(f"ALTER TABLE {_TABLE} DROP COLUMN IF EXISTS {column}")
