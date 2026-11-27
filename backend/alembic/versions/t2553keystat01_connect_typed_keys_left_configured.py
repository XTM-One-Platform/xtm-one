"""connect the API key integrations an OAuth past left pre-configured

An integration created with OAuth gets ``status = 'configured'`` until its
first authorization. Switched to an API key (or a bot token) with the key
saved, the update never recomputed the status, so the row stayed
``configured`` ("Pre-configured" on its page) with a usable key stored
(#2553). The update now applies one rule after every auth-type switch; this
moves the rows already stuck to ``connected``, the status a typed key gets
when it is saved. A failed test still shows: its verdict lives in
``connection_test_status``, which this leaves alone.

No DDL, so nothing lock-taking. The UPDATE runs in bounded batches, each
committed on its own (``autocommit_block``), under ``lock_timeout``; a re-run
matches nothing. Fresh installs run ``create_all`` + ``stamp head``: the body
never executes there and there is nothing to repair.

Revision ID: t2553keystat01
Revises: t1930dashsnap01
Create Date: 2026-11-27 04:45:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.core.migration_ddl import DEFAULT_LOCK_TIMEOUT

revision: str = "t2553keystat01"
# NOT a constant: must always be main's CURRENT head, re-pointed whenever main
# gains a migration (backend/AGENTS.md > "Database migrations (safety)", rule 1).
down_revision: str | Sequence[str] | None = "t1930dashsnap01"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "integrations"

#: The rows repaired: a typed key stored under a status an OAuth past left.
STUCK_TYPED_KEY_ROWS = "auth_type IN ('api_key', 'bot_token') AND status = 'configured' AND api_key_enc IS NOT NULL"

_BATCH_SIZE = 500


def repair_in_batches(conn: sa.engine.Connection, batch_size: int = _BATCH_SIZE) -> int:
    """Connect the matching rows *batch_size* at a time; returns how many.

    Meant for an autocommit connection: ``lock_timeout`` is set for the
    session and put back afterwards.
    """
    previous = conn.execute(sa.text("SELECT current_setting('lock_timeout')")).scalar()
    conn.execute(sa.text("SELECT set_config('lock_timeout', :t, false)"), {"t": DEFAULT_LOCK_TIMEOUT})
    total = 0
    try:
        while True:
            result = conn.execute(
                sa.text(
                    f"UPDATE {_TABLE} SET status = 'connected' "  # noqa: S608
                    f"WHERE id IN (SELECT id FROM {_TABLE} WHERE {STUCK_TYPED_KEY_ROWS} "
                    "ORDER BY id LIMIT :n)"
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
    if not inspector.has_table(_TABLE):
        return
    columns = {c["name"] for c in inspector.get_columns(_TABLE)}
    if not {"auth_type", "status", "api_key_enc"} <= columns:
        return
    with op.get_context().autocommit_block():
        repair_in_batches(op.get_bind())


def downgrade() -> None:
    # Which rows this repaired is not recorded, and ``connected`` is what a
    # stored key gives anyway: there is nothing safe to undo.
    pass
