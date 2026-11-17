"""browser profiles renew their session by themselves by default

Adds ``browser_profiles.signs_in_unattended`` (nullable boolean, no default:
catalogue-only): whether the last sign-in with the stored credentials
completed without a person, which ``profiles.renews_by_itself`` reads beside
a TOTP secret and a saved identity-provider session.

Moves ``keepalive_minutes`` to a default of 240 minutes, so the keep-alive
probes and refreshes every new profile's session by itself, and moves the
profiles still at the old default 0 to 240 (the table holds one row per
application sign-in: a bounded update). A profile set to another interval
keeps it.

Canonical lock-safe recipe (rule 5): idempotency probe, then
``terminate_lock_holders`` and ``set_lock_timeout`` before each DDL. The
downgrade drops the column and puts the default back to 0; the rows moved
to 240 keep it (the old state carried no choice of their own).

Revision ID: b7059renew01
Revises: f7030fleetsga
Create Date: 2026-11-01 12:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.core.migration_ddl import set_lock_timeout, terminate_lock_holders

revision: str = "b7059renew01"
# NOT a constant: must always be main's CURRENT head, re-pointed whenever main
# gains a migration (backend/AGENTS.md > "Database migrations (safety)", rule 1).
down_revision: str | Sequence[str] | None = "f7030fleetsga"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "browser_profiles"
_COLUMN = "signs_in_unattended"
_KEEPALIVE_DEFAULT = 240


def _column_exists(conn, column: str) -> bool:
    return bool(
        conn.execute(
            sa.text(
                "SELECT 1 FROM information_schema.columns "
                "WHERE table_schema = current_schema() "
                "AND table_name = :table AND column_name = :column"
            ),
            {"table": _TABLE, "column": column},
        ).scalar()
    )


def _keepalive_default(conn) -> str | None:
    return conn.execute(
        sa.text(
            "SELECT column_default FROM information_schema.columns "
            "WHERE table_schema = current_schema() "
            "AND table_name = :table AND column_name = 'keepalive_minutes'"
        ),
        {"table": _TABLE},
    ).scalar()


def upgrade() -> None:
    conn = op.get_bind()
    if not sa.inspect(conn).has_table(_TABLE):
        return
    if not _column_exists(conn, _COLUMN):
        terminate_lock_holders(conn, _TABLE)
        set_lock_timeout(conn)
        op.execute(f"ALTER TABLE {_TABLE} ADD COLUMN IF NOT EXISTS {_COLUMN} BOOLEAN")
    if str(_keepalive_default(conn) or "").strip("'") != str(_KEEPALIVE_DEFAULT):
        terminate_lock_holders(conn, _TABLE)
        set_lock_timeout(conn)
        op.execute(f"ALTER TABLE {_TABLE} ALTER COLUMN keepalive_minutes SET DEFAULT {_KEEPALIVE_DEFAULT}")
        # Once, with the default: a person who sets 0 afterwards keeps it on a re-run.
        set_lock_timeout(conn)
        move = sa.text("UPDATE browser_profiles SET keepalive_minutes = :minutes WHERE keepalive_minutes = 0")
        op.execute(move.bindparams(minutes=_KEEPALIVE_DEFAULT))


def downgrade() -> None:
    conn = op.get_bind()
    if not sa.inspect(conn).has_table(_TABLE):
        return
    if _column_exists(conn, _COLUMN):
        terminate_lock_holders(conn, _TABLE)
        set_lock_timeout(conn)
        op.execute(f"ALTER TABLE {_TABLE} DROP COLUMN IF EXISTS {_COLUMN}")
    if str(_keepalive_default(conn) or "").strip("'") != "0":
        terminate_lock_holders(conn, _TABLE)
        set_lock_timeout(conn)
        op.execute(f"ALTER TABLE {_TABLE} ALTER COLUMN keepalive_minutes SET DEFAULT 0")
