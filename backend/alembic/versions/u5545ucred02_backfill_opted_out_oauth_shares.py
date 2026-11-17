"""keep the opt-out of OAuth integrations already shared without their account

An OAuth integration shared with ``Don't share my connected account`` kept no
record of the choice: its token was removed, and its owner's next
authorization lent the new token to everyone it is shared with. The choice is
now ``integrations.credential_mode = 'per_user'`` (``u5545ucred01``): the
owner's account stays theirs, and everyone else authorizes their own.

This moves the integrations shared that way today - OAuth, shared (group,
per-user grant or company-managed), the consent not given, no token stored
(the opt-out removed it) - to ``per_user``, so authorizing one of them again
keeps the owner's choice. A shared OAuth integration whose owner consented is
left ``shared``; so is one still holding a token nobody consented to share:
what it is bound to uses it as bound today, and moving it would take
that away at deploy - nothing that works before this revision may stop
working (the ``b8ack2642001`` rule). Every other row is left too.
A re-run is a no-op: a moved row no longer matches. Fresh installs run
``create_all`` + ``stamp head``: the body never executes there.

No DDL. The UPDATE runs in bounded batches, each committed on its own
(``autocommit_block``), with ``lock_timeout`` bounding each batch's wait
behind a row lock - the ``b8ack2642001`` recipe.

Revision ID: u5545ucred02
Revises: u5545ucred01
Create Date: 2026-10-12 10:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.core.migration_ddl import DEFAULT_LOCK_TIMEOUT

revision: str = "u5545ucred02"
down_revision: str | Sequence[str] | None = "u5545ucred01"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "integrations"

#: The integrations shared without their connected account today.
OPTED_OUT_OAUTH_SHARES = (
    "auth_type = 'oauth'"
    " AND oauth_share_acknowledged IS NOT TRUE"
    " AND credential_mode = 'shared'"
    " AND access_token_enc IS NULL AND refresh_token_enc IS NULL"
    " AND (is_company_managed IS TRUE"
    " OR COALESCE(CAST(shared_group_ids AS TEXT), 'null') NOT IN ('null', '[]')"
    " OR COALESCE(CAST(shared_user_ids AS TEXT), 'null') NOT IN ('null', '[]'))"
)

_BATCH_SIZE = 500


def backfill_in_batches(conn, batch_size: int = _BATCH_SIZE) -> int:
    """Move the matching rows *batch_size* at a time; returns how many."""
    previous = conn.execute(sa.text("SELECT current_setting('lock_timeout')")).scalar()
    conn.execute(
        sa.text("SELECT set_config('lock_timeout', :t, false)"), {"t": DEFAULT_LOCK_TIMEOUT}
    )
    total = 0
    try:
        while True:
            result = conn.execute(
                sa.text(
                    f"UPDATE {_TABLE} SET credential_mode = 'per_user' "  # noqa: S608
                    f"WHERE id IN (SELECT id FROM {_TABLE} WHERE {OPTED_OUT_OAUTH_SHARES} "
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
    if not {"credential_mode", "oauth_share_acknowledged", "shared_user_ids"} <= columns:
        return
    with op.get_context().autocommit_block():
        backfill_in_batches(op.get_bind())


def downgrade() -> None:
    # The column is ``u5545ucred01``'s, which drops it on its own downgrade:
    # before this revision it carried no choice to put back.
    pass
