"""add ix_ctem_case_events_merged_from: a merged-away case id finds its survivor (#2289)

A case merged into another is deleted; the survivor records one ``merge`` event
whose ``from_value`` is the merged id. A read of the merged id (``get_ctem_case``,
the case tools, the start check of a queued ``ctem_case.opened`` run) looks that
event up by full id or by an 8+ character prefix. This partial index holds the
``merge`` rows only, keyed by ``from_value`` with ``varchar_pattern_ops`` so the
prefix ``LIKE 'abc%'`` is an index range scan.

Purely additive. Built in place by ``create_index_in_place`` inside an
autocommit block (never CONCURRENTLY, which hung a rollout on 2026-10-12): the
build is bounded by the lock and statement timeouts, and a failure leaves the
model index reconciliation to build it after the upgrade. The drop follows the
canonical recipe.

Revision ID: m2289mergedfrom1
Revises: r2214relsettle1
Create Date: 2026-11-25 17:10:00.000000
"""

from collections.abc import Sequence

from alembic import op
from app.core.migration_ddl import create_index_in_place, set_lock_timeout, terminate_lock_holders

revision: str = "m2289mergedfrom1"
# NOT a constant: must always be main's CURRENT head, re-pointed on every rebase
# (backend/AGENTS.md > "Database migrations (safety)", rule 1).
down_revision: str | Sequence[str] | None = "r2214relsettle1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "ctem_case_events"
_INDEX = "ix_ctem_case_events_merged_from"
_DEFINITION = "(from_value varchar_pattern_ops) WHERE kind = 'merge'"


def upgrade() -> None:
    conn = op.get_bind()
    with op.get_context().autocommit_block():
        create_index_in_place(conn, _INDEX, _TABLE, _DEFINITION)


def downgrade() -> None:
    conn = op.get_bind()
    terminate_lock_holders(conn, _TABLE)
    set_lock_timeout(conn)
    op.execute(f"DROP INDEX IF EXISTS {_INDEX}")
