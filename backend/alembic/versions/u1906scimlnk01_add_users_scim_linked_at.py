"""users.scim_linked_at: since when SCIM manages an account, externalId or not

``scim_external_id`` only marks the accounts whose identity provider sent an
``externalId``, which SCIM 2.0 leaves optional, so an account SCIM created or
linked by ``userName`` alone read as local: its owner could change its email,
and the next SCIM write put the identity provider's address back. SCIM now
stamps ``scim_linked_at`` on every account it creates, links or updates
(``api/v1/scim.py``).

The backfill stamps the accounts SCIM already manages: those with a
``scim_external_id``, and those a SCIM write audited (``details`` carries the
``scim_token_id`` of every one). An account whose SCIM audit rows the retention
already purged is stamped by the identity provider's next write. A re-run is
a no-op: a stamped row no longer matches. Fresh installs run ``create_all`` +
``stamp head``: the body never executes there.

Lock profile: the column is nullable without a default (catalogue-only),
added after the lock pre-flight under the lock timeout. The backfill walks
``users`` by id, 500 rows at a time, each batch committed on its own
(``autocommit_block``) under ``lock_timeout`` - the ``b8ack2642001`` recipe.
The downgrade drops the column, which carried nothing before this revision.

Revision ID: u1906scimlnk01
Revises: f2580fleetval01
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.core.migration_ddl import DEFAULT_LOCK_TIMEOUT, column_exists, set_lock_timeout, terminate_lock_holders

revision: str = "u1906scimlnk01"
# NOT a constant: must always be main's CURRENT head, re-pointed whenever main
# gains a migration (backend/AGENTS.md > "Database migrations (safety)", rule 1).
down_revision: str | Sequence[str] | None = "f2580fleetval01"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "users"
_COLUMN = "scim_linked_at"
_BATCH_SIZE = 500

#: An account SCIM manages today: an externalId, or a SCIM write audited on it.
SCIM_MANAGED = (
    "u.scim_external_id IS NOT NULL OR EXISTS ("
    "SELECT 1 FROM audit_logs a WHERE a.entity_type = 'user' AND a.entity_id = u.id"
    " AND CAST(a.details AS jsonb) -> 'scim_token_id' IS NOT NULL)"
)


def backfill_in_batches(conn, batch_size: int = _BATCH_SIZE) -> int:
    """Stamp the accounts SCIM manages, walking ``users`` *batch_size* ids at a time; returns how many."""
    previous = conn.execute(sa.text("SELECT current_setting('lock_timeout')")).scalar()
    conn.execute(sa.text("SELECT set_config('lock_timeout', :t, false)"), {"t": DEFAULT_LOCK_TIMEOUT})
    total = 0
    after = None
    try:
        while True:
            last = conn.execute(
                sa.text(
                    f"SELECT max(id::text)::uuid FROM (SELECT id FROM {_TABLE} "  # noqa: S608
                    "WHERE (CAST(:after AS uuid) IS NULL OR id > CAST(:after AS uuid)) ORDER BY id LIMIT :n) batch"
                ),
                {"after": after, "n": batch_size},
            ).scalar()
            if last is None:
                return total
            result = conn.execute(
                sa.text(
                    f"UPDATE {_TABLE} u SET {_COLUMN} = now() "  # noqa: S608
                    "WHERE (CAST(:after AS uuid) IS NULL OR u.id > CAST(:after AS uuid)) AND u.id <= CAST(:last AS uuid) "
                    f"AND u.{_COLUMN} IS NULL AND ({SCIM_MANAGED})"
                ),
                {"after": after, "last": str(last)},
            )
            total += result.rowcount or 0
            after = str(last)
    finally:
        conn.execute(sa.text("SELECT set_config('lock_timeout', :t, false)"), {"t": previous})


def upgrade() -> None:
    conn = op.get_bind()
    if not column_exists(conn, _TABLE, _COLUMN):
        terminate_lock_holders(conn, _TABLE)
        set_lock_timeout(conn)
        op.execute(f"ALTER TABLE {_TABLE} ADD COLUMN IF NOT EXISTS {_COLUMN} TIMESTAMP WITH TIME ZONE")
    with op.get_context().autocommit_block():
        backfill_in_batches(op.get_bind())


def downgrade() -> None:
    conn = op.get_bind()
    if not column_exists(conn, _TABLE, _COLUMN):
        return
    terminate_lock_holders(conn, _TABLE)
    set_lock_timeout(conn)
    op.execute(f"ALTER TABLE {_TABLE} DROP COLUMN IF EXISTS {_COLUMN}")
