"""add TOTP two-factor authentication columns to users

Backs the authenticator-app 2FA feature:

* ``mfa_enabled`` — the only flag the login flow gates on. A secret can exist
  while enrollment is still unconfirmed, and that half-finished state must not
  lock anyone out, so the boolean is separate from the secret.
* ``mfa_secret`` — base32 TOTP secret, Fernet-encrypted at rest by the
  application layer (never a plaintext second factor in a database dump).
* ``mfa_confirmed_at`` — when enrollment was confirmed with a real code.
* ``mfa_last_used_step`` — highest TOTP time-step already spent, the replay
  guard. BIGINT because the step is unix-time/30 and INTEGER would overflow
  in 2038.
* ``mfa_recovery_codes`` — JSON array of ``{hash, used_at}`` entries (SHA-256
  hashes of single-use recovery codes).

Every column is added NULL-able except ``mfa_enabled``, which lands NOT NULL
with a ``false`` server default: "MFA off" is exactly the right reading for
every pre-existing account, and a constant default makes the ADD COLUMN
metadata-only on PG 11+ rather than a full table rewrite.

The ADD COLUMNs on the (live) ``users`` table still follow the canonical
lock-safe recipe used across this project: idempotency probe ->
terminate_lock_holders -> set_lock_timeout -> guarded DDL. The downgrade's
DROP statements take ACCESS EXCLUSIVE and follow the same recipe.

The column set mirrors ``app/models/user.py`` exactly: a fresh install runs
``create_all`` + stamp head while an existing install runs this body, and the
two must converge.

Revision ID: d5e8mfa02786
Revises: c4e1mcpdcr0001
Create Date: 2026-08-08 10:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.core.migration_ddl import set_lock_timeout, terminate_lock_holders

# revision identifiers, used by Alembic.
revision: str = "d5e8mfa02786"
down_revision: str | None = "c4e1mcpdcr0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_USERS = "users"

# (column, DDL type) in the order they should be added. Kept as a single
# source of truth so upgrade/downgrade can never drift apart.
_COLUMNS: tuple[tuple[str, str], ...] = (
    ("mfa_enabled", "BOOLEAN NOT NULL DEFAULT false"),
    ("mfa_secret", "TEXT"),
    ("mfa_confirmed_at", "TIMESTAMPTZ"),
    ("mfa_last_used_step", "BIGINT"),
    ("mfa_recovery_codes", "JSON"),
)
_COLUMN_NAMES = tuple(name for name, _ in _COLUMNS)


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

    # Fresh installs run create_all + stamp head; this body never executes there.
    if not sa.inspect(conn).has_table(_USERS):
        return

    missing = [c for c in _COLUMN_NAMES if not _column_exists(conn, _USERS, c)]
    if not missing:
        return

    terminate_lock_holders(conn, _USERS)
    set_lock_timeout(conn)
    for column, ddl_type in _COLUMNS:
        if column in missing:
            op.execute(
                f"ALTER TABLE {_USERS} ADD COLUMN IF NOT EXISTS {column} {ddl_type}"
            )


def downgrade() -> None:
    conn = op.get_bind()

    if not sa.inspect(conn).has_table(_USERS):
        return

    present = [c for c in _COLUMN_NAMES if _column_exists(conn, _USERS, c)]
    if not present:
        return

    terminate_lock_holders(conn, _USERS)
    set_lock_timeout(conn)
    for column in present:
        op.execute(f"ALTER TABLE {_USERS} DROP COLUMN IF EXISTS {column}")
