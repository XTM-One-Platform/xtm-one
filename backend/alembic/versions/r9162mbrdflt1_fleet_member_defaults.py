"""fleet member defaults: fleet-level settings, member rows inherit (R1 of #9162)

``fleets`` gains ``default_context_inclusion`` and
``default_collaboration_instruction``; ``fleet_members.context_inclusion`` and
``collaboration_instruction`` become nullable, NULL meaning "inherits the
fleet default" (dev-docs/ctem-fleets.md section 10.3).

Backfill (decision R-D1 = B, section 10.8): per fleet and per field, the most
common value among the connected rows becomes the fleet default when at least
two rows share it and no other value is as common; otherwise the column default
stays. Every row equal to the fleet default is then set to NULL and inherits
it; only the rows that differ keep an override. Every effective value is the
same before and after (``tests/test_fleet_member_defaults_migration_postgres.py``
asserts it). Home member rows never carried a meaning for these fields and are
set to NULL.

The reduction is written here, not imported from ``app.services.fleets``: a
migration keeps meaning what it meant on the day it ran.

Downgrade writes every effective value back on the rows, restores NOT NULL and
the column defaults, and drops the fleet columns.

Revision ID: r9162mbrdflt1
Revises: c9179sidebarws1
Create Date: 2026-11-06 12:10:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.core.migration_ddl import set_lock_timeout, terminate_lock_holders

revision: str = "r9162mbrdflt1"
down_revision: str | Sequence[str] | None = "c9179sidebarws1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_FIELDS: tuple[tuple[str, str, str], ...] = (
    # (member column, fleet column, column default)
    ("context_inclusion", "default_context_inclusion", "none"),
    ("collaboration_instruction", "default_collaboration_instruction", ""),
)


def _reduce(values: list[str], fallback: str) -> str:
    """The fleet default *values* reduce to: a unique most common value shared by two rows, else *fallback*."""
    counts: dict[str, int] = {}
    for value in values:
        counts[value] = counts.get(value, 0) + 1
    if not counts:
        return fallback
    best = max(counts.values())
    winners = [v for v, n in counts.items() if n == best]
    if best >= 2 and len(winners) == 1:
        return winners[0]
    return fallback


def _has_column(conn, table: str, column: str) -> bool:
    return bool(
        conn.execute(
            sa.text(
                "SELECT 1 FROM information_schema.columns "
                "WHERE table_schema = current_schema() AND table_name = :t AND column_name = :c"
            ),
            {"t": table, "c": column},
        ).scalar()
    )


def upgrade() -> None:
    conn = op.get_bind()
    terminate_lock_holders(conn, "fleets")
    terminate_lock_holders(conn, "fleet_members")
    set_lock_timeout(conn)
    if not _has_column(conn, "fleets", "default_context_inclusion"):
        conn.execute(
            sa.text(
                "ALTER TABLE fleets ADD COLUMN IF NOT EXISTS default_context_inclusion VARCHAR(10) "
                "NOT NULL DEFAULT 'none'"
            )
        )
        conn.execute(
            sa.text(
                "ALTER TABLE fleets ADD CONSTRAINT ck_fleets_default_context_inclusion "
                "CHECK (default_context_inclusion IN ('none', 'summary', 'full'))"
            )
        )
    conn.execute(
        sa.text(
            "ALTER TABLE fleets ADD COLUMN IF NOT EXISTS default_collaboration_instruction TEXT NOT NULL DEFAULT ''"
        )
    )
    for column, _, _ in _FIELDS:
        conn.execute(sa.text(f"ALTER TABLE fleet_members ALTER COLUMN {column} DROP NOT NULL"))
        conn.execute(sa.text(f"ALTER TABLE fleet_members ALTER COLUMN {column} DROP DEFAULT"))

    rows = conn.execute(
        sa.text(
            "SELECT fleet_id, context_inclusion, collaboration_instruction FROM fleet_members "
            "WHERE role = 'connected'"
        )
    ).all()
    by_fleet: dict[object, list[tuple[str | None, str | None]]] = {}
    for fleet_id, inclusion, instruction in rows:
        by_fleet.setdefault(fleet_id, []).append((inclusion, instruction))
    for fleet_id, values in by_fleet.items():
        for index, (column, fleet_column, fallback) in enumerate(_FIELDS):
            present = [v[index] if v[index] is not None else fallback for v in values]
            default = _reduce(present, fallback)
            conn.execute(
                sa.text(f"UPDATE fleets SET {fleet_column} = :v WHERE id = :f"),  # noqa: S608
                {"v": default, "f": fleet_id},
            )
            conn.execute(
                sa.text(
                    f"UPDATE fleet_members SET {column} = NULL "  # noqa: S608
                    f"WHERE fleet_id = :f AND role = 'connected' AND COALESCE({column}, :fb) = :v"
                ),
                {"f": fleet_id, "v": default, "fb": fallback},
            )
    for column, _, _ in _FIELDS:
        conn.execute(sa.text(f"UPDATE fleet_members SET {column} = NULL WHERE role = 'member'"))  # noqa: S608


def downgrade() -> None:
    conn = op.get_bind()
    terminate_lock_holders(conn, "fleet_members")
    set_lock_timeout(conn)
    for column, fleet_column, fallback in _FIELDS:
        conn.execute(
            sa.text(
                f"UPDATE fleet_members m SET {column} = COALESCE(f.{fleet_column}, :fb) "  # noqa: S608
                f"FROM fleets f WHERE m.fleet_id = f.id AND m.{column} IS NULL AND m.role = 'connected'"
            ),
            {"fb": fallback},
        )
        conn.execute(
            sa.text(f"UPDATE fleet_members SET {column} = :fb WHERE {column} IS NULL"),  # noqa: S608
            {"fb": fallback},
        )
        conn.execute(sa.text(f"ALTER TABLE fleet_members ALTER COLUMN {column} SET DEFAULT '{fallback}'"))
        conn.execute(sa.text(f"ALTER TABLE fleet_members ALTER COLUMN {column} SET NOT NULL"))
    conn.execute(sa.text("ALTER TABLE fleets DROP CONSTRAINT IF EXISTS ck_fleets_default_context_inclusion"))
    conn.execute(sa.text("ALTER TABLE fleets DROP COLUMN IF EXISTS default_context_inclusion"))
    conn.execute(sa.text("ALTER TABLE fleets DROP COLUMN IF EXISTS default_collaboration_instruction"))
