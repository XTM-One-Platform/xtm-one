"""add agents.budget_config — the per-agent run budget

The run budget had exactly one configuration surface: the
platform environment. Every agent on a deployment shared one ceiling, so the
ceiling ended up sized for the most expensive agent and stopped bounding the
cheap ones.

``budget_config`` is the per-agent override: a partial JSON policy object
(``{"max_cost_usd": 0.5, "fallback_enabled": true, ...}``) shaped like
``app.services.budget_policy.BudgetOverrides.to_dict()``. Only the keys an
agent pins are stored; the rest keep resolving to the platform values, so an
agent that cares about money alone does not have to restate the token,
tool-call and duration limits and then drift from them.

NULL (the default, and what every existing row reads as) means "no override" —
byte-identical behaviour to before this column existed. Nothing is backfilled:
inventing a per-agent budget for agents nobody budgeted would silently start
bounding runs that are unbounded today, which is precisely the surprise a
spend feature must not spring.

Authority is NOT in the schema. A value above the platform ceiling can only be
written by an admin (``budget_policy.ceiling_violations``, enforced at the API
edge), which is what makes the env setting a budget a user cannot exceed on
their own. A CHECK constraint could not express that rule — it depends on who
is writing, not on the value.

Additive and lock-light: ``ADD COLUMN`` of a nullable column with no default is
metadata-only on every supported PG. Old replicas during a rolling deploy never
select a column they do not know about.

Revision ID: b1bud3170001
Revises: ai2skipped02
Create Date: 2026-08-28 10:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.core.migration_ddl import set_lock_timeout, terminate_lock_holders

revision: str = "b1bud3170001"
down_revision: str | Sequence[str] | None = "ai2skipped02"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "agents"
_COLUMN = "budget_config"
_DDL = "JSON"


def _has_table(conn, name: str) -> bool:
    return bool(
        conn.execute(
            sa.text(
                "SELECT 1 FROM information_schema.tables "
                "WHERE table_schema = current_schema() AND table_name = :t"
            ),
            {"t": name},
        ).scalar()
    )


def _has_column(conn, table: str, column: str) -> bool:
    return bool(
        conn.execute(
            sa.text(
                "SELECT 1 FROM information_schema.columns "
                "WHERE table_schema = current_schema() "
                "AND table_name = :t AND column_name = :c"
            ),
            {"t": table, "c": column},
        ).scalar()
    )


def upgrade() -> None:
    conn = op.get_bind()
    if not _has_table(conn, _TABLE):
        return
    # Steady-state re-run (a fresh install stamped head, a replica retried)
    # takes no lock at all.
    if _has_column(conn, _TABLE, _COLUMN):
        return

    terminate_lock_holders(conn, _TABLE)
    set_lock_timeout(conn)
    op.execute(
        sa.text(f"ALTER TABLE {_TABLE} ADD COLUMN IF NOT EXISTS {_COLUMN} {_DDL}")
    )


def downgrade() -> None:
    conn = op.get_bind()
    if not _has_table(conn, _TABLE):
        return
    if not _has_column(conn, _TABLE, _COLUMN):
        return

    terminate_lock_holders(conn, _TABLE)
    set_lock_timeout(conn)
    op.execute(sa.text(f"ALTER TABLE {_TABLE} DROP COLUMN IF EXISTS {_COLUMN}"))
