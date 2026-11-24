"""seeded and platform-created assignments carry no AI label (#1735)

The AI label is a prompt: ``assignment_helpers.DEFAULT_AI_LABEL_PROMPT`` is
appended to the system prompt when ``assignments.ai_label`` is true, and the
model then prefixes work item titles, notes and decision cards with
"[AI-generated]". The seed and the platform's own auto-create paths now set it
off; this switches it off on the rows they already wrote, and only on those:

* every assignment of a built-in agent (``agents.is_builtin``): the built-in
  desks, the seeded fleets and the Platform Health fleet. The seed owns these
  rows and rewrites them on every boot, so no person's choice is lost;
* a fleet orchestrator's "Weekly fleet health check" and "Fleet health
  answered" (``services/fleets.py``), keyed on their name and trigger;
* an organization context fill schedule
  (``services/organization_context_fill_agent.py``), keyed on its name and the
  ``org-fill:`` tag of its agent.

An assignment a person created keeps its label. Data only, no DDL: bounded
batches of row updates, a no-op on re-run (rule 2). The downgrade is a no-op:
the old value was the column default, not a choice anyone made (rule 6).

Revision ID: x1735noailabel01
Revises: w1665intreason01
Create Date: 2026-11-24 07:10:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "x1735noailabel01"
# NOT a constant: must always be main's CURRENT head, re-pointed whenever main
# gains a migration (backend/AGENTS.md > "Database migrations (safety)", rule 1).
down_revision: str | Sequence[str] | None = "w1665intreason01"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_BATCH = 500

#: One bounded batch of the assignments the platform seeds or creates itself (see the docstring).
_UPDATE_BATCH = sa.text(
    """
    UPDATE assignments SET ai_label = FALSE, ai_label_prompt = NULL
    WHERE id IN (
        SELECT a.id FROM assignments a
        WHERE a.ai_label IS TRUE
        AND (
            a.agent_id IN (SELECT g.id FROM agents g WHERE g.is_builtin IS TRUE)
            OR (a.name = 'Weekly fleet health check' AND a.trigger_type = 'schedule')
            OR (a.name = 'Fleet health answered' AND a.trigger_type = 'platform_event')
            OR (
                a.name LIKE :fill_name
                AND a.agent_id IN (SELECT g.id FROM agents g WHERE CAST(g.tags AS TEXT) LIKE :fill_tag)
            )
        )
        LIMIT :batch
    )
    """
)


def _tables_present(conn: sa.engine.Connection) -> bool:
    inspector = sa.inspect(conn)
    return inspector.has_table("assignments") and inspector.has_table("agents")


def upgrade() -> None:
    conn = op.get_bind()
    if not _tables_present(conn):
        return
    while True:
        updated = conn.execute(
            _UPDATE_BATCH,
            {"fill_name": "Keep my assets in sync from %", "fill_tag": "%org-fill:%", "batch": _BATCH},
        ).rowcount
        if not updated or updated < _BATCH:
            return


def downgrade() -> None:
    # The label was the column default on these rows, never a person's choice.
    return
