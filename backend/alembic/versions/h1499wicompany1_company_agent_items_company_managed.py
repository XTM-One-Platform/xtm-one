"""open work items filed by a company-managed agent become company-managed (#1499)

A company-managed agent's work item was personal when the run that filed it
was not company-managed (a hidden-runs inbox dispatch, a CTEM case decision
parked for the scope user): it was listed under Mine of that user instead of
Company. From #1499 ``create_work_item`` follows the creating agent; this data
fix applies the same rule to the open items already written. ``user_id`` is
kept, so the person a decision waits for is unchanged.

One UPDATE on the open rows only (open, claimed, in_progress, blocked); done
and cancelled items keep their history. On a typical instance it touches the
open decision and hand-off items of the company-managed fleets, tens of rows.
Scheduled one-offs (``action_mode``) stay personal: they act as their person.
Not reversible by design (which rows were personal is not recorded):
downgrade is a no-op.

Revision ID: h1499wicompany1
Revises: f1526fleetrunbudget1
Create Date: 2026-11-23 11:50:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.core.migration_ddl import set_lock_timeout

revision: str = "h1499wicompany1"
# NOT a constant: must always be main's CURRENT head, re-pointed whenever main
# gains a migration (backend/AGENTS.md > "Database migrations (safety)", rule 1).
down_revision: str | Sequence[str] | None = "f1526fleetrunbudget1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    if not inspector.has_table("work_items") or not inspector.has_table("agents"):
        return
    set_lock_timeout(conn)
    op.execute(
        """
        UPDATE work_items AS w
           SET is_company_managed = true
          FROM agents AS a
         WHERE a.id = w.created_by_agent_id
           AND a.is_company_managed IS TRUE
           AND w.is_company_managed IS NOT TRUE
           AND w.action_mode IS NULL
           AND w.status IN ('open', 'claimed', 'in_progress', 'blocked')
        """
    )


def downgrade() -> None:
    # Which rows were personal is not recorded: nothing to restore.
    pass
