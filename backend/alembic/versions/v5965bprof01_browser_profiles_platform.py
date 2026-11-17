"""make browser profiles a platform feature: agent and General Assistant bindings, the security option

Browser profiles leave the Video Studio: every browser of the
platform can open one, the agent ``browser`` tool included.

* ``agents.browser_profile_ids`` / ``agents.browser_profile_read_only``: the
  profiles an agent's ``browser`` tool may open, and those it opens read-only;
* ``users.ga_browser_profile_ids``: the profiles the person's General
  Assistant may open;
* the security option ``video_studio_private_targets`` becomes
  ``browser_private_targets``: an administrator's stored value moves to the
  new key (``platform_settings.security_options``).

The columns are nullable JSON without a default, so each ``ADD COLUMN`` is
metadata-only and needs no backfill.

Revision ID: v5965bprof01
Revises: v5961probe01
Create Date: 2026-10-18 18:00:00.000000
"""

import json
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.core.migration_ddl import set_lock_timeout, terminate_lock_holders

revision: str = "v5965bprof01"
down_revision: str | None = "v5961probe01"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_COLUMNS: dict[str, tuple[str, ...]] = {
    "agents": ("browser_profile_ids", "browser_profile_read_only"),
    "users": ("ga_browser_profile_ids",),
}
_OLD_OPTION = "video_studio_private_targets"
_NEW_OPTION = "browser_private_targets"


def _add_columns(conn: sa.Connection, table: str, columns: tuple[str, ...]) -> None:
    inspector = sa.inspect(conn)
    if not inspector.has_table(table):
        return
    present = {c["name"] for c in inspector.get_columns(table)}
    missing = [name for name in columns if name not in present]
    if not missing:
        return
    terminate_lock_holders(conn, table)
    set_lock_timeout(conn)
    additions = ", ".join(f"ADD COLUMN IF NOT EXISTS {name} JSON" for name in missing)
    op.execute(f"ALTER TABLE {table} {additions}")


def _drop_columns(conn: sa.Connection, table: str, columns: tuple[str, ...]) -> None:
    inspector = sa.inspect(conn)
    if not inspector.has_table(table):
        return
    present = {c["name"] for c in inspector.get_columns(table)}
    there = [name for name in columns if name in present]
    if not there:
        return
    terminate_lock_holders(conn, table)
    set_lock_timeout(conn)
    drops = ", ".join(f"DROP COLUMN IF EXISTS {name}" for name in there)
    op.execute(f"ALTER TABLE {table} {drops}")


def _rename_stored_option(conn: sa.Connection, old: str, new: str) -> None:
    """Move an administrator's stored value from *old* to *new*, unless *new* is already set."""
    if not sa.inspect(conn).has_table("platform_settings"):
        return
    set_lock_timeout(conn)
    rows = conn.execute(
        sa.text("SELECT id, security_options FROM platform_settings WHERE security_options IS NOT NULL")
    ).all()
    for row_id, raw in rows:
        try:
            values = json.loads(raw) if isinstance(raw, str) else raw
        except ValueError:
            continue
        if not isinstance(values, dict) or old not in values:
            continue
        moved = {k: v for k, v in values.items() if k != old}
        moved.setdefault(new, values[old])
        conn.execute(
            sa.text("UPDATE platform_settings SET security_options = CAST(:values AS JSON) WHERE id = :id"),
            {"values": json.dumps(moved), "id": row_id},
        )


def upgrade() -> None:
    conn = op.get_bind()
    for table, columns in _COLUMNS.items():
        _add_columns(conn, table, columns)
    _rename_stored_option(conn, _OLD_OPTION, _NEW_OPTION)


def downgrade() -> None:
    conn = op.get_bind()
    _rename_stored_option(conn, _NEW_OPTION, _OLD_OPTION)
    for table, columns in _COLUMNS.items():
        _drop_columns(conn, table, columns)
