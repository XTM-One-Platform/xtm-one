"""enforce "the default workspace is never company-managed" in the database

The service refuses making the default workspace company-managed and making a
company-managed workspace the default, but each check read the row without a
lock: an admin's ``PUT {is_company_managed: true}`` and the owner's
``POST /{id}/default`` could interleave and leave a row with both flags. The
two other invariants of the entity are already enforced by the database; this
adds the third, ``ck_workspaces_default_not_company_managed``:
``NOT (is_default AND is_company_managed)``.

An earlier version let a company-managed workspace stay its owner's default,
so a row may already break the check. It is repaired first: it stops being
the default, and ``ensure_default_workspace`` gives its owner a fresh default
on their next listing (the slug collision with the old "general" is handled
there). Its company-managed state, which other people may rely on, is kept.

Safety contract (backend/AGENTS.md > "Database migrations (safety)"):

* Idempotent - the constraint is probed first; a re-run with the constraint
  present and valid takes no lock, and every statement is guarded.
* Lock-safe - the canonical recipe (probe, ``terminate_lock_holders``,
  ``set_lock_timeout``) before the ``ALTER``. The check is added ``NOT VALID``
  first, so every write from then on is checked, then the legacy rows are
  repaired and the check validated. The repair touches only the rows breaking
  the check (at most one per owner, by ``uq_workspaces_default_per_user``) and
  ``workspaces`` holds a handful of rows per user, so neither the update nor
  the validation is an unbounded scan of a large table.
* Reversible - the downgrade drops the check. The repaired rows stay repaired:
  the state they left was the invalid one, and nothing reads it back.

Revision ID: k5041wsdfcm01
Revises: o3rgctx8987
Create Date: 2026-10-24 12:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.core.migration_ddl import set_lock_timeout, terminate_lock_holders

revision: str = "k5041wsdfcm01"
# NOT a constant: must always be main's CURRENT head, re-pointed whenever main
# gains a migration (backend/AGENTS.md > "Database migrations (safety)", rule 1).
down_revision: str | Sequence[str] | None = "o3rgctx8987"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "workspaces"
_CHECK = "ck_workspaces_default_not_company_managed"


def _constraint_state(conn) -> str | None:
    """``valid``, ``not_valid`` or ``None`` (absent) for the check."""
    row = conn.execute(
        sa.text(
            "SELECT convalidated FROM pg_constraint "
            "WHERE conname = :name AND conrelid = to_regclass(:table)"
        ),
        {"name": _CHECK, "table": _TABLE},
    ).first()
    if row is None:
        return None
    return "valid" if row[0] else "not_valid"


def upgrade() -> None:
    conn = op.get_bind()
    state = _constraint_state(conn)
    if state == "valid":
        return
    if state is None:
        terminate_lock_holders(conn, _TABLE)
        set_lock_timeout(conn)
        conn.execute(
            sa.text(
                "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint "
                "WHERE conname = 'ck_workspaces_default_not_company_managed' "
                "AND conrelid = to_regclass('workspaces')) THEN "
                "ALTER TABLE workspaces ADD CONSTRAINT ck_workspaces_default_not_company_managed "
                "CHECK (NOT (is_default AND is_company_managed)) NOT VALID; "
                "END IF; END $$;"
            )
        )
    conn.execute(
        sa.text(
            "UPDATE workspaces SET is_default = false "
            "WHERE is_default AND is_company_managed"
        )
    )
    conn.execute(sa.text(f"ALTER TABLE {_TABLE} VALIDATE CONSTRAINT {_CHECK}"))


def downgrade() -> None:
    conn = op.get_bind()
    if _constraint_state(conn) is None:
        return
    terminate_lock_holders(conn, _TABLE)
    set_lock_timeout(conn)
    conn.execute(sa.text(f"ALTER TABLE {_TABLE} DROP CONSTRAINT IF EXISTS {_CHECK}"))
