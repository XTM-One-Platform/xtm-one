"""add fleets.approval_pins; a custom fleet that gates nothing reads its true level

The fleet autonomy cursor (#10153, slice B2 of #10000) acts on the computed
approval catalog of a custom fleet and carries per-action pins
(``{gate key: "wait" | "run"}``). ``approval_pins`` NOT NULL also turns the
computed catalog on for the fleet (``fleet_gate_proposals.fleet_effective_gates``).

Data step (C2, the stored level matches the level shown): a custom fleet with
no declared gate (no stored catalog, or an empty one, and no gate in its
documentation) gates nothing at any level today, yet stores ``full_gates``.
It is moved to ``autonomous`` with ``approval_pins = {}``: at ``autonomous``
nothing of its computed catalog waits, so no behaviour changes, and the cursor
shows the position that holds. A custom fleet that declares gates keeps its
level and ``approval_pins`` NULL (its declared gates alone, as before).

The ``ADD COLUMN`` is nullable with no default (metadata-only). The UPDATE is
bounded by the ``fleets`` table (one row per fleet, custom fleets only).
Lock-safe per the canonical recipe. Fresh installs run ``create_all`` +
``stamp head``: the body never executes there (a new fleet gets the same values
from ``services.fleets.create_fleet``).

Revision ID: p10153fltpin1
Revises: r10006roibf1
Create Date: 2026-11-08 19:40:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.core.migration_ddl import set_lock_timeout, terminate_lock_holders

revision: str = "p10153fltpin1"
# NOT a constant: must always be main's CURRENT head, re-pointed on every rebase
# (backend/AGENTS.md > "Database migrations (safety)", rule 1).
down_revision: str | Sequence[str] | None = "r10006roibf1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "fleets"
_COLUMN = "approval_pins"

#: Custom fleets that declare no gate: no stored catalog (or an empty one) and
#: no gate in the documentation's structured part.
_GATES_NOTHING = (
    "is_builtin = false "
    "AND (approval_gates IS NULL OR json_typeof(approval_gates) <> 'array' "
    "OR json_array_length(approval_gates) = 0) "
    "AND (documentation IS NULL "
    "OR json_typeof(documentation -> 'structured' -> 'gates') IS DISTINCT FROM 'array' "
    "OR json_array_length(documentation -> 'structured' -> 'gates') = 0)"
)
#: Static statements (no interpolation): the fleets that gate nothing move to
#: their true level with the computed catalog on; the downgrade moves them back.
_UPGRADE_SQL = (
    "UPDATE fleets SET autonomy_level = 'autonomous', approval_pins = '{}'::json "
    "WHERE approval_pins IS NULL AND is_builtin = false "
    "AND (approval_gates IS NULL OR json_typeof(approval_gates) <> 'array' "
    "OR json_array_length(approval_gates) = 0) "
    "AND (documentation IS NULL "
    "OR json_typeof(documentation -> 'structured' -> 'gates') IS DISTINCT FROM 'array' "
    "OR json_array_length(documentation -> 'structured' -> 'gates') = 0)"
)
_DOWNGRADE_SQL = (
    "UPDATE fleets SET autonomy_level = 'full_gates' "
    "WHERE autonomy_level = 'autonomous' AND is_builtin = false "
    "AND (approval_gates IS NULL OR json_typeof(approval_gates) <> 'array' "
    "OR json_array_length(approval_gates) = 0) "
    "AND (documentation IS NULL "
    "OR json_typeof(documentation -> 'structured' -> 'gates') IS DISTINCT FROM 'array' "
    "OR json_array_length(documentation -> 'structured' -> 'gates') = 0)"
)


def _has_column(conn) -> bool:
    return bool(
        conn.execute(
            sa.text(
                "SELECT 1 FROM information_schema.columns "
                "WHERE table_schema = current_schema() "
                "AND table_name = :table AND column_name = :column"
            ),
            {"table": _TABLE, "column": _COLUMN},
        ).first()
    )


def upgrade() -> None:
    conn = op.get_bind()
    if not sa.inspect(conn).has_table(_TABLE) or _has_column(conn):
        return
    terminate_lock_holders(conn, _TABLE)
    set_lock_timeout(conn)
    op.execute(f"ALTER TABLE {_TABLE} ADD COLUMN IF NOT EXISTS {_COLUMN} JSON")
    op.execute(sa.text(_UPGRADE_SQL))


def downgrade() -> None:
    conn = op.get_bind()
    if not sa.inspect(conn).has_table(_TABLE) or not _has_column(conn):
        return
    terminate_lock_holders(conn, _TABLE)
    set_lock_timeout(conn)
    # A fleet that gated nothing goes back to the old default; any other keeps its level.
    op.execute(sa.text(_DOWNGRADE_SQL))
    op.execute(f"ALTER TABLE {_TABLE} DROP COLUMN IF EXISTS {_COLUMN}")
