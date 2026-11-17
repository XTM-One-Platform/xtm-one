"""add fleets.budget_config and fleets.max_iterations

Fleets phase 6: the HOME fleet carries the run defaults of its
members. ``budget_config`` is the partial run-budget override of the agent
Budget tab (``budget_policy.BudgetOverrides.to_dict()``), ``max_iterations``
the iteration cap; both NULL = no fleet default, i.e. byte-identical
behaviour to before. An agent's own value always wins over its fleet's, and
the platform ceiling bounds both (the authority rule of the agents API).

Two nullable ``ADD COLUMN`` without default (metadata-only), following the
lock-safe recipe: idempotency probe, ``terminate_lock_holders``,
``set_lock_timeout``, then the guarded DDL. Fresh installs run ``create_all``
+ ``stamp head``: the body never executes there.

Revision ID: f7602fltbdg1
Revises: m7544heads01
Create Date: 2026-11-02 23:45:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.core.migration_ddl import set_lock_timeout, terminate_lock_holders

revision: str = "f7602fltbdg1"
# NOT a constant: must always be main's CURRENT head, re-pointed whenever main
# gains a migration (backend/AGENTS.md > "Database migrations (safety)", rule 1).
down_revision: str | Sequence[str] | None = "m7544heads01"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "fleets"
_COLUMNS: tuple[tuple[str, str], ...] = (
    ("budget_config", "JSON"),
    ("max_iterations", "INTEGER"),
)


def _missing(conn) -> list[tuple[str, str]]:
    present = {
        row[0]
        for row in conn.execute(
            sa.text(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema = current_schema() AND table_name = :table"
            ),
            {"table": _TABLE},
        )
    }
    return [(name, ddl) for name, ddl in _COLUMNS if name not in present]


def upgrade() -> None:
    conn = op.get_bind()
    missing = _missing(conn)
    if not missing:
        return
    terminate_lock_holders(conn, _TABLE)
    set_lock_timeout(conn)
    for name, ddl in missing:
        op.execute(f"ALTER TABLE {_TABLE} ADD COLUMN IF NOT EXISTS {name} {ddl}")


def downgrade() -> None:
    conn = op.get_bind()
    absent = {name for name, _ in _missing(conn)}
    present = [name for name, _ in _COLUMNS if name not in absent]
    if not present:
        return
    terminate_lock_holders(conn, _TABLE)
    set_lock_timeout(conn)
    for name in present:
        op.execute(f"ALTER TABLE {_TABLE} DROP COLUMN IF EXISTS {name}")
