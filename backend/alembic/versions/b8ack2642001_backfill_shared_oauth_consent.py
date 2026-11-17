"""record consent for OAuth integrations already shared with their token

``b7f1e6a93c02`` added ``integrations.oauth_share_acknowledged`` as FALSE for
every existing row and nothing stripped the tokens of integrations that were
already shared (group, per-user grant or company-managed). On those rows the
owner's token kept working for everyone the integration is shared with, in
every path that did not yet apply the personal-token rule; this release applies it
everywhere, which would have cut them off at deploy.

By maintainer decision nothing that works before this release may stop working after
it, so this records the acknowledgement once, on the owners' behalf, for every
shared OAuth integration holding a token today. Rows already acknowledged,
unshared rows, token-less rows and non-OAuth rows are left alone, so a re-run
is a no-op. Fresh installs run ``create_all`` + ``stamp head``: the body never
executes there and there is nothing to backfill.

No DDL, so nothing lock-taking. The UPDATE runs in bounded batches, each
committed on its own (``autocommit_block``), so no transaction holds more than
``_BATCH_SIZE`` row locks; ``lock_timeout`` bounds each batch's wait behind a
row lock. An interrupted run resumes where it stopped: an acknowledged row no
longer matches.

Revision ID: b8ack2642001
Revises: n1044impr001
Create Date: 2026-09-26 19:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.core.migration_ddl import DEFAULT_LOCK_TIMEOUT

# revision identifiers, used by Alembic.
revision: str = "b8ack2642001"
down_revision: str | Sequence[str] | None = "n1044impr001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "integrations"

#: The rows this migration acknowledges; also what the PR body tells operators
#: to run beforehand to list them.
SHARED_OAUTH_ROWS = (
    "auth_type = 'oauth'"
    " AND oauth_share_acknowledged IS NOT TRUE"
    " AND access_token_enc IS NOT NULL"
    " AND (is_company_managed IS TRUE"
    " OR COALESCE(CAST(shared_group_ids AS TEXT), 'null') NOT IN ('null', '[]')"
    " OR COALESCE(CAST(shared_user_ids AS TEXT), 'null') NOT IN ('null', '[]'))"
)


_BATCH_SIZE = 500


def backfill_in_batches(conn, batch_size: int = _BATCH_SIZE) -> int:
    """Acknowledge the matching rows *batch_size* at a time; returns how many.

    Meant for an autocommit connection, where every statement is its own
    transaction: ``lock_timeout`` is therefore set for the session and put back
    afterwards (``SET LOCAL`` would not outlive the statement).
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
                    f"UPDATE {_TABLE} SET oauth_share_acknowledged = TRUE "  # noqa: S608
                    f"WHERE id IN (SELECT id FROM {_TABLE} WHERE {SHARED_OAUTH_ROWS} "
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
    if not {"oauth_share_acknowledged", "shared_user_ids"} <= columns:
        return
    with op.get_context().autocommit_block():
        backfill_in_batches(op.get_bind())


def downgrade() -> None:
    # Which rows were acknowledged by this migration rather than by their
    # owners is not recorded, so there is nothing safe to undo.
    pass
