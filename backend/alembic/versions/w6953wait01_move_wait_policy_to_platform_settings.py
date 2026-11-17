"""move the wait policy to platform_settings, add per-agent and per-assignment overrides

The bounds of the ``wait`` tool bound every run but lived on
``general_assistant_config``. This adds them to ``platform_settings``
(``maximum_wait_time``, ``maximum_total_wait_time``,
``wait_unbounded_with_until``) and COPIES the values an administrator saved on
the General Assistant row, so nothing changes for a running install; it adds
the ``wait_policy`` JSON override on ``agents`` and ``assignments``.

Nullable columns with no default: the ``ADD COLUMN`` is metadata-only, NULL
reads as the defaults (or inherit). The copy is one UPDATE of the singleton
row. The old ``general_assistant_config`` columns are kept (no longer read)
and dropped in a later release, like the other moved fields.

Revision ID: w6953wait01
Revises: b6952wideps01
Create Date: 2026-10-31 22:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.core.migration_ddl import set_lock_timeout, terminate_lock_holders

revision: str = "w6953wait01"
# NOT a constant: must always be main's CURRENT head, re-pointed whenever main
# gains a migration (backend/AGENTS.md > "Database migrations (safety)", rule 1).
down_revision: str | Sequence[str] | None = "b6952wideps01"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_COLUMNS: tuple[tuple[str, str, str], ...] = (
    ("platform_settings", "maximum_wait_time", "INTEGER"),
    ("platform_settings", "maximum_total_wait_time", "INTEGER"),
    ("platform_settings", "wait_unbounded_with_until", "BOOLEAN"),
    ("agents", "wait_policy", "JSON"),
    ("assignments", "wait_policy", "JSON"),
)


def _columns(inspector, table: str) -> set[str] | None:
    if not inspector.has_table(table):
        return None
    return {column["name"] for column in inspector.get_columns(table)}


def upgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    for table, column, sql_type in _COLUMNS:
        existing = _columns(inspector, table)
        if existing is None or column in existing:
            continue
        terminate_lock_holders(conn, table)
        set_lock_timeout(conn)
        op.execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {column} {sql_type}")

    # Copy what an administrator saved on the General Assistant row, once
    # (only where the platform value is still unset, so a re-run never
    # overwrites a value saved in the new place).
    ga_columns = _columns(inspector, "general_assistant_config") or set()
    if {"maximum_wait_time", "maximum_total_wait_time"} <= ga_columns and _columns(
        inspector, "platform_settings"
    ) is not None:
        op.execute(
            "UPDATE platform_settings SET "
            "maximum_wait_time = COALESCE(platform_settings.maximum_wait_time, ga.maximum_wait_time), "
            "maximum_total_wait_time = COALESCE(platform_settings.maximum_total_wait_time, ga.maximum_total_wait_time) "
            "FROM (SELECT maximum_wait_time, maximum_total_wait_time FROM general_assistant_config LIMIT 1) AS ga"
        )


def downgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    for table, column, _sql_type in reversed(_COLUMNS):
        existing = _columns(inspector, table)
        if not existing or column not in existing:
            continue
        terminate_lock_holders(conn, table)
        set_lock_timeout(conn)
        op.execute(f"ALTER TABLE {table} DROP COLUMN IF EXISTS {column}")
