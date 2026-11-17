"""add terms of use acceptance (table + denormalised user columns)

Backs the Terms of Use acceptance gate:

* ``terms_acceptances`` — one row per (user, ToU version) explicitly accepted.
  The ``uq_terms_acceptances_user_version`` unique constraint is what makes
  ``POST /auth/terms/accept`` idempotent (``ON CONFLICT DO NOTHING``).
* ``users.tou_accepted_version`` / ``users.tou_accepted_at`` — denormalised
  current state, so the per-request gate in ``app.api.deps`` reads an
  already-loaded row instead of querying the history table on every call.

Both columns are added NULL-able with no default: "never accepted" is exactly
what every pre-existing account should read as, because the gate covers existing
users too (no implicit backfill — nobody clicked).

``CREATE TABLE IF NOT EXISTS`` takes no lock on any existing relation, so the
table half needs no lock pre-flight. The two ``ADD COLUMN``s on the (live)
``users`` table are metadata-only on PG 11+ but still follow the canonical
lock-safe recipe: idempotency probe -> terminate_lock_holders ->
set_lock_timeout -> guarded DDL. The downgrade's ``DROP`` statements take
ACCESS EXCLUSIVE and follow the same recipe.

The column set mirrors the ORM models exactly (``app/models/terms_acceptance.py``
and ``app/models/user.py``): a fresh install runs ``create_all`` + stamp head
while an existing install runs this body, and the two must converge.

Revision ID: a4c8tou10001
Revises: a4c8rdonly1863
Create Date: 2026-07-30 12:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.core.migration_ddl import set_lock_timeout, terminate_lock_holders

# revision identifiers, used by Alembic.
revision: str = "a4c8tou10001"
down_revision: str | None = "a4c8rdonly1863"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "terms_acceptances"
_USERS = "users"
_USER_COLUMNS = ("tou_accepted_version", "tou_accepted_at")


def _table_exists(conn, table: str) -> bool:
    # Scope the probe to the active schema — information_schema spans ALL
    # schemas, so a same-named table elsewhere must not make the probe skip
    # the CREATE in the schema being migrated.
    return bool(
        conn.execute(
            sa.text(
                "SELECT 1 FROM information_schema.tables "
                "WHERE table_schema = current_schema() AND table_name = :table"
            ),
            {"table": table},
        ).scalar()
    )


def _column_exists(conn, table: str, column: str) -> bool:
    return bool(
        conn.execute(
            sa.text(
                "SELECT 1 FROM information_schema.columns "
                "WHERE table_schema = current_schema() "
                "AND table_name = :table AND column_name = :column"
            ),
            {"table": table, "column": column},
        ).scalar()
    )


def upgrade() -> None:
    conn = op.get_bind()

    if not _table_exists(conn, _TABLE):
        op.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {_TABLE} (
                id UUID PRIMARY KEY,
                user_id UUID NOT NULL REFERENCES {_USERS}(id) ON DELETE CASCADE,
                tou_version VARCHAR(50) NOT NULL,
                accepted_at TIMESTAMPTZ NOT NULL,
                CONSTRAINT uq_terms_acceptances_user_version
                    UNIQUE (user_id, tou_version)
            )
            """
        )

    # Deliberately OUTSIDE the "table did not exist" branch. PostgreSQL has
    # transactional DDL, so a crash between the CREATE TABLE above and this
    # statement rolls both back and the table is simply absent — but that is a
    # property of how this migration happens to be run, not of the statement
    # itself. Creating the index unconditionally means the table can never end
    # up present-but-unindexed no matter how it got there (a half-applied
    # deploy, a hand-created table, a restore). ``IF NOT EXISTS`` makes the
    # normal path a no-op, and the relation was created in this transaction on
    # the fresh path, so nothing can be waiting on it and CONCURRENTLY is
    # unnecessary.
    op.execute(
        f"CREATE INDEX IF NOT EXISTS ix_{_TABLE}_user_id ON {_TABLE} (user_id)"
    )

    # Fresh installs run create_all + stamp head; the users half never
    # executes there either.
    if not sa.inspect(conn).has_table(_USERS):
        return

    missing = [c for c in _USER_COLUMNS if not _column_exists(conn, _USERS, c)]
    if not missing:
        return

    terminate_lock_holders(conn, _USERS)
    set_lock_timeout(conn)
    for column, ddl_type in (
        ("tou_accepted_version", "VARCHAR(50)"),
        ("tou_accepted_at", "TIMESTAMPTZ"),
    ):
        if column in missing:
            op.execute(
                f"ALTER TABLE {_USERS} ADD COLUMN IF NOT EXISTS {column} {ddl_type}"
            )


def downgrade() -> None:
    conn = op.get_bind()

    if sa.inspect(conn).has_table(_USERS):
        present = [c for c in _USER_COLUMNS if _column_exists(conn, _USERS, c)]
        if present:
            terminate_lock_holders(conn, _USERS)
            set_lock_timeout(conn)
            for column in present:
                op.execute(
                    f"ALTER TABLE {_USERS} DROP COLUMN IF EXISTS {column}"
                )

    if _table_exists(conn, _TABLE):
        terminate_lock_holders(conn, _TABLE)
        set_lock_timeout(conn)
        op.execute(f"DROP TABLE IF EXISTS {_TABLE}")
