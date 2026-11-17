"""move Terms of Use acceptance from per-user to a single platform-wide record

Reworks the Terms of Use gate so acceptance binds the whole platform,
not each user:

* ``platform_settings.tou_accepted_version`` / ``tou_accepted_at`` /
  ``tou_accepted_by_user_id`` — the single platform-wide acceptance the request
  gate now reads (one admin accepts on behalf of the org; the audit log carries
  the who/when/version trail).

This is deliberately the EXPAND phase only (rolling-deploy safety): the
legacy per-user ``users.tou_accepted_version`` / ``users.tou_accepted_at``
columns and the ``terms_acceptances`` history table are NOT dropped here.
Release 1.260809.0 replicas still map the ``users`` columns in the ORM (every
user load selects them) and still write ``terms_acceptances`` on accept —
dropping them in the release that ships this migration would fail every
authenticated request on the old replicas for the whole rollout window.
Leaving them in place (unmapped and inert to the new code) also means a
rollback restores the previous per-user behaviour with its data intact. The
CONTRACT phase — dropping the legacy schema once no older replicas can
exist — is ``c2797toudrop1``. Until then, migrated installs carry the inert legacy schema
while ``create_all`` fresh installs never grow it; that divergence is the
standard expand/contract trade-off and is harmless because nothing reads it.

The ``ADD COLUMN`` here takes ACCESS EXCLUSIVE on a live table, so it follows
the canonical lock-safe recipe: idempotency probe -> early return ->
terminate_lock_holders -> set_lock_timeout -> guarded (``IF [NOT] EXISTS``)
DDL. ``platform_settings`` is the singleton that took production down on
2026-07-05 by skipping this recipe, so its ``ADD COLUMN`` is not exempt. The
added column set mirrors ``app/models/platform_settings.py`` exactly so
``create_all`` fresh installs and this body converge on the new columns.

Revision ID: c5g9tou20001
Revises: b2r7rep2720
Create Date: 2026-08-09 13:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.core.migration_ddl import set_lock_timeout, terminate_lock_holders

# revision identifiers, used by Alembic.
revision: str = "c5g9tou20001"
down_revision: str | None = "b2r7rep2720"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "terms_acceptances"
_USERS = "users"
_PS = "platform_settings"
_USER_COLUMNS = ("tou_accepted_version", "tou_accepted_at")
_PS_COLUMNS = ("tou_accepted_version", "tou_accepted_at", "tou_accepted_by_user_id")
_PS_COLUMN_TYPES = (
    ("tou_accepted_version", "VARCHAR(50)"),
    ("tou_accepted_at", "TIMESTAMPTZ"),
    ("tou_accepted_by_user_id", "UUID"),
)


def _table_exists(conn, table: str) -> bool:
    # Scope the probe to the active schema — information_schema spans ALL
    # schemas, so a same-named table elsewhere must not skew the probe.
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

    # Add the platform-wide acceptance columns (constant-defaultless =
    # metadata-only on PG 11+), lock-safe on the singleton table.
    #
    # EXPAND phase only: the legacy per-user columns on ``users`` and the
    # ``terms_acceptances`` table are intentionally NOT dropped — previous-
    # release replicas still select/write them during a rolling deploy, and
    # keeping them makes a rollback lossless. The contract-phase drop ships
    # separately once no older replicas can exist (``c2797toudrop1``).
    if sa.inspect(conn).has_table(_PS):
        ps_missing = [c for c in _PS_COLUMNS if not _column_exists(conn, _PS, c)]
        if ps_missing:
            terminate_lock_holders(conn, _PS)
            set_lock_timeout(conn)
            for column, ddl_type in _PS_COLUMN_TYPES:
                if column in ps_missing:
                    op.execute(
                        f"ALTER TABLE {_PS} ADD COLUMN IF NOT EXISTS {column} {ddl_type}"
                    )


def downgrade() -> None:
    conn = op.get_bind()

    # On a migrated install steps 1 and 2 are no-ops: the expand-only upgrade
    # never dropped the legacy per-user schema, so a rollback finds it intact,
    # data included. They only materialise on a fresh install (create_all from
    # the new models, then downgraded below head), where the prior shape must
    # be rebuilt for the previous-release code to run.

    # 1. Recreate the per-user history table + its index.
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
    op.execute(
        f"CREATE INDEX IF NOT EXISTS ix_{_TABLE}_user_id ON {_TABLE} (user_id)"
    )

    # 2. Recreate the denormalised per-user columns (NULL = never accepted).
    if sa.inspect(conn).has_table(_USERS):
        missing = [c for c in _USER_COLUMNS if not _column_exists(conn, _USERS, c)]
        if missing:
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

    # 3. Drop the platform-wide acceptance columns.
    if sa.inspect(conn).has_table(_PS):
        present = [c for c in _PS_COLUMNS if _column_exists(conn, _PS, c)]
        if present:
            terminate_lock_holders(conn, _PS)
            set_lock_timeout(conn)
            for column in present:
                op.execute(f"ALTER TABLE {_PS} DROP COLUMN IF EXISTS {column}")
