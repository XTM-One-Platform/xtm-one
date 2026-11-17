"""add a browser profile's sign-in timeouts and the last probe of its saved session

A profile's session status is the answer of a probe from now on: the
start URL opened in the saved session and checked to render the application,
not the presence of cookies. The probe's time and result are kept on the
profile (``last_probe_at``, ``last_probe_result``), and the sign-in's bounds
become the profile's own:

* ``sign_in_timeout_seconds`` (120): the flow's inactivity timeout, counted
  from the last navigation;
* ``session_save_timeout_seconds`` (30): reading the session out of the
  browser and writing it;
* ``mfa_timeout_seconds`` (300): how long an MFA challenge keeps its browser
  open for a person.

The timeouts are ``NOT NULL`` with a constant default and the probe columns
nullable, so each ``ADD COLUMN`` is metadata-only and needs no backfill.

Revision ID: v5961probe01
Revises: v5955jobst01
Create Date: 2026-10-18 13:30:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.core.migration_ddl import set_lock_timeout, terminate_lock_holders

revision: str = "v5961probe01"
down_revision: str | None = "v5955jobst01"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "browser_profiles"
_COLUMNS: tuple[tuple[str, str], ...] = (
    ("sign_in_timeout_seconds", "INTEGER NOT NULL DEFAULT 120"),
    ("session_save_timeout_seconds", "INTEGER NOT NULL DEFAULT 30"),
    ("mfa_timeout_seconds", "INTEGER NOT NULL DEFAULT 300"),
    ("last_probe_at", "TIMESTAMP WITH TIME ZONE"),
    ("last_probe_result", "JSON"),
)


def upgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    if not inspector.has_table(_TABLE):
        return
    present = {c["name"] for c in inspector.get_columns(_TABLE)}
    missing = [(name, kind) for name, kind in _COLUMNS if name not in present]
    if not missing:
        return
    terminate_lock_holders(conn, _TABLE)
    set_lock_timeout(conn)
    additions = ", ".join(f"ADD COLUMN IF NOT EXISTS {name} {kind}" for name, kind in missing)
    op.execute(f"ALTER TABLE {_TABLE} {additions}")


def downgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    if not inspector.has_table(_TABLE):
        return
    present = {c["name"] for c in inspector.get_columns(_TABLE)}
    there = [name for name, _kind in _COLUMNS if name in present]
    if not there:
        return
    # The previous code knows no "unverified" session: it read a saved session as active.
    op.execute("UPDATE browser_profiles SET session_status = 'active' WHERE session_status = 'unverified'")
    terminate_lock_holders(conn, _TABLE)
    set_lock_timeout(conn)
    drops = ", ".join(f"DROP COLUMN IF EXISTS {name}" for name in there)
    op.execute(f"ALTER TABLE {_TABLE} {drops}")
