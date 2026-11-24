"""delete the OpenCRQ primary-asset sync older builds saved by default (#1883)

Older builds saved a daily ``crown_jewels`` / ``opencrq`` sync by themselves on
every platform where an administrator could read OpenCRQ (create audit with
``details.default`` true). Nothing is pre-provisioned any more: an
administrator adds that source from Inventory > Sources like any other.

This migration deletes such a sync when no person has edited it since (no
``update`` or ``toggle`` audit on it), as an administrator's delete in its
default ``leave`` mode does: the sync row goes, a ``delete`` audit entry is
written in the name of the account that owned the default, and the primary
assets it imported stay as they are (still marked ``source: opencrq``). A sync a
person created or edited stays. Data-only and idempotent; no downgrade.

Revision ID: n1883nodefsync01
Revises: c1748ctemgrp01
Create Date: 2026-11-24 12:45:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "n1883nodefsync01"
# NOT a constant: must always be main's CURRENT head, re-pointed whenever main
# gains a migration (backend/AGENTS.md > "Database migrations (safety)", rule 1).
down_revision: str | Sequence[str] | None = "c1748ctemgrp01"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# The syncs the former default created that no person changed since.
_DEFAULTS = sa.text(
    """
    SELECT s.id, a.user_id, a.user_email, s.section, s.source
    FROM organization_context_syncs s
    JOIN audit_logs a
      ON a.entity_type = 'organization_context_sync'
     AND a.entity_id = s.id
     AND a.action = 'create'
     AND (a.details ->> 'default') = 'true'
    WHERE s.section = 'crown_jewels' AND s.source = 'opencrq'
      AND NOT EXISTS (
        SELECT 1 FROM audit_logs e
        WHERE e.entity_type = 'organization_context_sync'
          AND e.entity_id = s.id
          AND e.action IN ('update', 'toggle')
      )
    """
)

_AUDIT = sa.text(
    """
    INSERT INTO audit_logs (id, user_id, user_email, action, entity_type, entity_id, entity_name, summary, details, created_at)
    VALUES (gen_random_uuid(), :user_id, :user_email, 'delete', 'organization_context_sync', :id, :name, :summary,
            CAST(:details AS json), now())
    """
)


def upgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    if not (inspector.has_table("organization_context_syncs") and inspector.has_table("audit_logs")):
        return
    for row in conn.execute(_DEFAULTS).mappings().all():
        conn.execute(
            _AUDIT,
            {
                "user_id": row["user_id"],
                "user_email": row["user_email"],
                "id": row["id"],
                "name": f"{row['section']} from {row['source']}",
                "summary": "Default primary-asset sync from OpenCRQ deleted (nothing is pre-provisioned), its rows kept",
                "details": '{"rows": "leave", "migration": "n1883nodefsync01"}',
            },
        )
        conn.execute(sa.text("DELETE FROM organization_context_syncs WHERE id = :id"), {"id": row["id"]})


def downgrade() -> None:
    # Data-only: a deleted default is never recreated (nothing is pre-provisioned).
    pass
