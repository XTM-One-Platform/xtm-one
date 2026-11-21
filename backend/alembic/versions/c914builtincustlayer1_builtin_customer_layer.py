"""built-in agents: the customer layer columns

``agents.customer_notes`` (organization notes appended after a built-in's
seeded persona) and ``agents.customer_fields`` (the customer-layer fields a
person set on a built-in, which the seed keeps). Issue #914.

Lock profile: two nullable columns with no default, a catalog-only change.
Re-running the body after an interruption is safe (``IF NOT EXISTS``).

Revision ID: c914builtincustlayer1
Revises: h128healthmaspend01
Create Date: 2026-11-21 16:00:00.000000
"""

from collections.abc import Sequence

from alembic import op
from app.core.migration_ddl import set_lock_timeout

revision: str = "c914builtincustlayer1"
# NOT a constant: must always be main's CURRENT head, re-pointed whenever main
# gains a migration (backend/AGENTS.md > "Database migrations (safety)", rule 1).
down_revision: str | Sequence[str] | None = "h128healthmaspend01"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    set_lock_timeout(op.get_bind())
    op.execute("ALTER TABLE agents ADD COLUMN IF NOT EXISTS customer_notes TEXT")
    op.execute("ALTER TABLE agents ADD COLUMN IF NOT EXISTS customer_fields JSON")


def downgrade() -> None:
    set_lock_timeout(op.get_bind())
    op.execute("ALTER TABLE agents DROP COLUMN IF EXISTS customer_fields")
    op.execute("ALTER TABLE agents DROP COLUMN IF EXISTS customer_notes")
