"""file the fleet members' unfiled work items in their fleet's workspace

A work item created by a home member of a fleet with no
workspace named is filed in the workspace hosting that fleet. This backfills
the items created before: an unfiled work item whose creator (else its owner)
is a current home member of a live fleet is filed in that fleet's workspace.

Only where filing widens nobody's reach beyond what the workspace already
grants: the item belongs to the workspace's owner, or both are company-managed.
Anything else (an item of another user, a private item for a company-managed
workspace) stays unfiled and is still counted and listed by the live resolver
(``workspace_scope_clause``). An archived workspace or fleet takes nothing.
Moving an agent to another fleet later moves nothing: this runs once.

No DDL. The UPDATE runs in bounded batches, each committed on its own
(``autocommit_block``), so no transaction holds more than ``_BATCH_SIZE`` row
locks; ``lock_timeout`` bounds each batch's wait. Idempotent: a filed item no
longer matches, so an interrupted run resumes and a re-run is a no-op. Fresh
installs run ``create_all`` + ``stamp head``: the body never executes there.

Revision ID: w7245filefleet1
Revises: f7065rolelbl
Create Date: 2026-11-02 03:30:00.000000
"""

import logging
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.core.migration_ddl import DEFAULT_LOCK_TIMEOUT

revision: str = "w7245filefleet1"
# NOT a constant: must always be main's CURRENT head, re-pointed whenever main
# gains a migration (backend/AGENTS.md > "Database migrations (safety)", rule 1).
down_revision: str | Sequence[str] | None = "f7065rolelbl"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

logger = logging.getLogger("alembic.runtime.migration")

_BATCH_SIZE = 500

#: The hosting workspace of each unfiled item to file, one row per item: its
#: creator's home fleet, else its owner's (creator first, as the create path
#: files by creator). ``DISTINCT ON`` keeps one fleet per item.
_TARGETS = """
SELECT DISTINCT ON (wi.id) wi.id AS item_id, f.workspace_id AS workspace_id
FROM work_items wi
JOIN fleet_members fm
  ON fm.role = 'member'
 AND fm.agent_id IN (wi.created_by_agent_id, wi.owner_agent_id)
JOIN fleets f ON f.id = fm.fleet_id AND f.is_archived IS FALSE
JOIN workspaces w ON w.id = f.workspace_id AND w.is_archived IS FALSE
WHERE wi.workspace_id IS NULL
  AND (
    (wi.user_id IS NOT NULL AND wi.user_id = w.user_id)
    OR (wi.is_company_managed IS TRUE AND w.is_company_managed IS TRUE)
  )
ORDER BY wi.id, (fm.agent_id = wi.created_by_agent_id) DESC
LIMIT :n
"""


def file_in_batches(conn, batch_size: int = _BATCH_SIZE) -> int:
    """File the matching items *batch_size* at a time; returns how many.

    Meant for an autocommit connection: ``lock_timeout`` is set for the
    session and put back afterwards (``SET LOCAL`` would not outlive the
    statement).
    """
    previous = conn.execute(sa.text("SELECT current_setting('lock_timeout')")).scalar()
    conn.execute(
        sa.text("SELECT set_config('lock_timeout', :t, false)"), {"t": DEFAULT_LOCK_TIMEOUT}
    )
    total = 0
    try:
        while True:
            result = conn.execute(
                sa.text(
                    "UPDATE work_items SET workspace_id = t.workspace_id "  # noqa: S608
                    f"FROM ({_TARGETS}) AS t "
                    "WHERE work_items.id = t.item_id AND work_items.workspace_id IS NULL"
                ),
                {"n": batch_size},
            )
            if not result.rowcount:
                return total
            total += result.rowcount
    finally:
        conn.execute(sa.text("SELECT set_config('lock_timeout', :t, false)"), {"t": previous})


def upgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    if not all(inspector.has_table(t) for t in ("work_items", "fleets", "fleet_members", "workspaces")):
        return
    with op.get_context().autocommit_block():
        filed = file_in_batches(op.get_bind())
    logger.info("w7245filefleet1: filed %d fleet work item(s) in their hosting workspace", filed)


def downgrade() -> None:
    # Which items this migration filed, rather than their creators, is not
    # recorded, so there is nothing safe to undo.
    pass
