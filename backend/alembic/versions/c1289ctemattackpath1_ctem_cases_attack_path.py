"""ctem_cases: the attack-path reach written by triage (CTEM-23)

What a finding on the affected asset reaches along the OpenAEV attack paths,
read from OpenCRQ by the triage step and written on the case:
``{origin, hops, reached, primary, at}``, ``{}`` until the first read.

Lock-light: one ``ADD COLUMN`` with a constant default, a catalog change on
PostgreSQL 11+, no rewrite. Re-running after an interruption is a no-op (the
column is probed first).

Revision ID: c1289ctemattackpath1
Revises: o1134dir01
Create Date: 2026-11-22 21:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.core.migration_ddl import set_lock_timeout, terminate_lock_holders

revision: str = "c1289ctemattackpath1"
# NOT a constant: must always be main's CURRENT head, re-pointed whenever main
# gains a migration (backend/AGENTS.md > "Database migrations (safety)", rule 1).
down_revision: str | Sequence[str] | None = "o1134dir01"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_CASES = "ctem_cases"
_COLUMN = "attack_path"


def _columns(conn: sa.engine.Connection) -> set[str]:
    rows = conn.execute(
        sa.text(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = current_schema() AND table_name = :t"
        ),
        {"t": _CASES},
    ).scalars()
    return set(rows)


def upgrade() -> None:
    conn = op.get_bind()
    present = _columns(conn)
    if not present or _COLUMN in present:  # no table (fresh installs run create_all) or already added
        return
    terminate_lock_holders(conn, _CASES)
    set_lock_timeout(conn)
    op.add_column(_CASES, sa.Column(_COLUMN, sa.JSON(), nullable=False, server_default=sa.text("'{}'")))


def downgrade() -> None:
    conn = op.get_bind()
    if _COLUMN not in _columns(conn):
        return
    terminate_lock_holders(conn, _CASES)
    set_lock_timeout(conn)
    op.drop_column(_CASES, _COLUMN)
