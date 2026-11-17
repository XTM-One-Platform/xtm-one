"""keep every existing fleet's level on rollout; add the instructions-governed position

Fixes the data step of ``p10153fltpin1`` (#10164) before it reaches an
existing instance (#10168, slice B2-I of #10000). That step moved every custom
fleet that declares no gate from ``full_gates`` to ``autonomous`` with
``approval_pins = {}``. Its gates did not change, but the autonomy block of
the members' run prompt (``fleet_autonomy.fleet_autonomy_block``) then read
"Full autonomous" instead of "Full human gates": a rollout changed the run
prompt of existing fleets. Rule of B2-I: no level moved, no gate added, no
prompt block changed on any existing fleet.

Data step: the rows ``p10153fltpin1`` moved (custom, no declared gate,
``autonomous`` with ``{}`` pins) go back to ``full_gates`` with
``approval_pins`` NULL, their exact state before it. A fleet a person moved
since through ``PUT /fleets/{id}/autonomy`` is told apart by its audit row
(``autonomy_level_change``) and kept.

Columns (nullable, no default, metadata-only):
- ``instructions_position``: where the fleet's instructions (persona,
  guardrails, skills) put it on the four-level scale, between-step positions
  allowed (``fleet_autonomy.INSTRUCTIONS_POSITIONS``). Display and statement
  only: never a platform gate.
- ``instructions_position_announced``: whether the members are told it in
  their run prompt (a person moved it). NULL/false = display only.

Lock-safe per the canonical recipe. Fresh installs run ``create_all`` +
``stamp head``: the body never executes there.

Revision ID: q10168fltinst1
Revises: p10153fltpin1
Create Date: 2026-11-08 20:30:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.core.migration_ddl import set_lock_timeout, terminate_lock_holders

revision: str = "q10168fltinst1"
# NOT a constant: must always be main's CURRENT head, re-pointed on every rebase
# (backend/AGENTS.md > "Database migrations (safety)", rule 1).
down_revision: str | Sequence[str] | None = "p10153fltpin1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "fleets"
_COLUMNS = ("instructions_position", "instructions_position_announced")

#: The rows the p10153fltpin1 data step moved, minus any fleet whose level a
#: person changed since (audit row). Static, no interpolation. The
#: ``created_at`` bound is the merge of #10164 (squash de530a2b): a fleet
#: created after it was created ``autonomous`` by ``services.fleets.create_fleet``
#: on purpose and is kept.
_RESTORE_SQL = (
    "UPDATE fleets SET autonomy_level = 'full_gates', approval_pins = NULL "
    "WHERE is_builtin = false AND autonomy_level = 'autonomous' "
    "AND created_at < '2026-11-08 20:14:48+00' "
    "AND approval_pins IS NOT NULL AND approval_pins::text = '{}' "
    "AND (approval_gates IS NULL OR json_typeof(approval_gates) <> 'array' "
    "OR json_array_length(approval_gates) = 0) "
    "AND (documentation IS NULL "
    "OR json_typeof(documentation -> 'structured' -> 'gates') IS DISTINCT FROM 'array' "
    "OR json_array_length(documentation -> 'structured' -> 'gates') = 0) "
    "AND NOT EXISTS (SELECT 1 FROM audit_logs a WHERE a.entity_type = 'fleet' "
    "AND a.entity_id = fleets.id AND a.action = 'autonomy_level_change')"
)


def _missing(conn) -> list[str]:
    found = {
        row[0]
        for row in conn.execute(
            sa.text(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema = current_schema() AND table_name = :table"
            ),
            {"table": _TABLE},
        )
    }
    return [c for c in _COLUMNS if c not in found]


def upgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    if not inspector.has_table(_TABLE):
        return
    missing = _missing(conn)
    terminate_lock_holders(conn, _TABLE)
    set_lock_timeout(conn)
    if "instructions_position" in missing:
        op.execute(f"ALTER TABLE {_TABLE} ADD COLUMN IF NOT EXISTS instructions_position VARCHAR(20)")
    if "instructions_position_announced" in missing:
        op.execute(f"ALTER TABLE {_TABLE} ADD COLUMN IF NOT EXISTS instructions_position_announced BOOLEAN")
    if inspector.has_table("audit_logs"):
        op.execute(sa.text(_RESTORE_SQL))


def downgrade() -> None:
    conn = op.get_bind()
    if not sa.inspect(conn).has_table(_TABLE):
        return
    terminate_lock_holders(conn, _TABLE)
    set_lock_timeout(conn)
    # The restored rows keep their pre-#10164 state: p10153fltpin1's own
    # downgrade reads them the same way.
    for column in _COLUMNS:
        op.execute(f"ALTER TABLE {_TABLE} DROP COLUMN IF EXISTS {column}")
