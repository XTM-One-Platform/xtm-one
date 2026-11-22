"""backfill the decisions of the human gates opened before c942decisions01 (#842, S2b)

``dev-docs/human-gates.md`` section 8 (G2), step 2: every gate still open
(a run awaiting a person, a pending tool approval, a flow parked on an
Ask-user node, a work item waiting for a person) gets its ``decisions`` row
and an ``asked`` event stamped ``backfill``, built by
``app.services.decision_backfill`` with the dual write's own rules. A gate
already answered, or already holding an open decision, is skipped: a re-run
writes nothing. Batched by keyset, no DDL. The count check is logged after
the copy and repeated by ``GET /api/v1/decisions/backfill``.

Downgrade: removes the open decisions this backfill wrote (their ``asked``
event says ``backfill``); every engine still holds its own pause, so no open
question is lost, and answered rows are kept as the audit.

Revision ID: c943decisionsbf1
Revises: c958healthsustain1
Create Date: 2026-11-22 01:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "c943decisionsbf1"
# NOT a constant: must always be main's CURRENT head, re-pointed whenever main
# gains a migration (backend/AGENTS.md > "Database migrations (safety)", rule 1).
down_revision: str | Sequence[str] | None = "c958healthsustain1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    from app.core.migration_ddl import set_lock_timeout
    from app.services import decision_backfill

    conn = op.get_bind()
    if not decision_backfill.has_decisions_table(conn):
        return
    set_lock_timeout(conn)
    written = decision_backfill.backfill(conn)
    decision_backfill.log_report(decision_backfill.backfill_report(conn), written)


def downgrade() -> None:
    from app.core.migration_ddl import set_lock_timeout

    conn = op.get_bind()
    if not sa.inspect(conn).has_table("decisions"):
        return
    set_lock_timeout(conn)
    conn.execute(
        sa.text(
            "DELETE FROM decisions WHERE status = 'open' AND id IN ("
            "SELECT decision_id FROM decision_events WHERE event = 'asked' AND surface = 'backfill')"
        )
    )
