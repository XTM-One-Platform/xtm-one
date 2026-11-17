"""drop the development AI adoption tables and rows (#11380)

AI Adoption tracks agentic AI on the platform alone: the GitHub Copilot and
Cursor integrations, their keys and the identity mapping between platform users
and GitHub / Cursor identities are removed from the code in the same release.
This drops:

* the ``ai_external_keys`` table (the GitHub Copilot and Cursor keys, Fernet
  encrypted), with its indexes;
* the ``ai_user_mappings`` table (platform user to GitHub login / Cursor email),
  with its unique constraint and index;
* every ``ai_adoption_metrics`` row of the ``github_copilot`` and ``cursor``
  providers (the platform's own ``copilot_platform`` rows stay).

Data loss is accepted (Sam, 12 Nov 2026: "fully remove").

What this does NOT drop yet, on purpose: ``users.standalone_mode``,
``users.standalone_mode_chosen``, ``users.focus_mode_policy``,
``groups.focus_mode_policy`` and the ``platform_settings`` AI adoption columns.
The release before this one maps them on every authenticated request (the user
row, the platform settings singleton), and its replicas keep serving during a
rolling deploy, so dropping them here would fail those requests until the
rollout ends (dev-docs/reference/migrations-safety.md, rule 4). This release no
longer maps them: they are inert, and the CONTRACT migration of the next release
drops them, as ``c2797toudrop1`` did for the per-user Terms of Use schema.

The two tables are read by the release before only on administrator paths
(Settings > AI Adoption, the hourly collector, which logs and goes on): a
request of an old replica on them during the rollout answers an error, nothing
else does.

Lock profile: dropping a table that references ``users`` removes its foreign
key's triggers, which takes ACCESS EXCLUSIVE on ``users`` for a moment. The
canonical recipe: idempotency probe -> terminate_lock_holders -> set_lock_timeout
-> guarded DDL. The row delete is bounded by the providers' rows, on an indexed
column, and takes row locks only.

The downgrade recreates both tables empty, as their models declared them. The
dropped rows and the deleted metric rows are not recoverable.

Revision ID: x11380devadopt1
Revises: x11033jirascope1
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.core.migration_ddl import set_lock_timeout, terminate_lock_holders

# revision identifiers, used by Alembic.
revision: str = "x11380devadopt1"
down_revision: str | None = "x11033jirascope1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_KEYS = "ai_external_keys"
_MAPPINGS = "ai_user_mappings"
_METRICS = "ai_adoption_metrics"
_USERS = "users"
_DEV_PROVIDERS = ("github_copilot", "cursor")


def _table_exists(conn, table: str) -> bool:
    # Scoped to the active schema: information_schema spans ALL schemas.
    return bool(
        conn.execute(
            sa.text(
                "SELECT 1 FROM information_schema.tables "
                "WHERE table_schema = current_schema() AND table_name = :table"
            ),
            {"table": table},
        ).scalar()
    )


def upgrade() -> None:
    conn = op.get_bind()
    if _table_exists(conn, _METRICS):
        op.execute(
            sa.text("DELETE FROM ai_adoption_metrics WHERE provider IN :providers").bindparams(
                sa.bindparam("providers", value=_DEV_PROVIDERS, expanding=True)
            )
        )
    # 1. Idempotency probe: fresh installs and re-runs take NO lock.
    present = [table for table in (_KEYS, _MAPPINGS) if _table_exists(conn, table)]
    if not present:
        return
    # 2. Pre-flight on every table the DDL locks: each table, and ``users``
    #    for its foreign key's triggers.
    for table in present:
        terminate_lock_holders(conn, table)
    terminate_lock_holders(conn, _USERS)
    # 3. Safety belt: fail fast (55P03) instead of wedging the lock queue.
    set_lock_timeout(conn)
    # 4. Guarded DDL.
    for table in present:
        op.execute(f"DROP TABLE IF EXISTS {table}")


def downgrade() -> None:
    conn = op.get_bind()
    missing = [table for table in (_KEYS, _MAPPINGS) if not _table_exists(conn, table)]
    if not missing:
        return
    # A new table's foreign key takes SHARE ROW EXCLUSIVE on ``users``.
    terminate_lock_holders(conn, _USERS)
    set_lock_timeout(conn)
    if _KEYS in missing:
        op.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {_KEYS} (
                id UUID PRIMARY KEY,
                user_id UUID REFERENCES {_USERS}(id) ON DELETE CASCADE,
                provider VARCHAR(50) NOT NULL,
                key_type VARCHAR(50) NOT NULL,
                name VARCHAR(255) NOT NULL,
                key_enc TEXT NOT NULL,
                key_prefix VARCHAR(30) NOT NULL,
                config JSON,
                is_active BOOLEAN NOT NULL,
                last_synced_at TIMESTAMPTZ,
                sync_error TEXT,
                created_at TIMESTAMPTZ,
                updated_at TIMESTAMPTZ
            )
            """
        )
        op.execute(f"CREATE INDEX IF NOT EXISTS ix_{_KEYS}_user_id ON {_KEYS} (user_id)")
        op.execute(f"CREATE INDEX IF NOT EXISTS ix_{_KEYS}_provider ON {_KEYS} (provider)")
    if _MAPPINGS in missing:
        op.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {_MAPPINGS} (
                id UUID PRIMARY KEY,
                user_id UUID NOT NULL REFERENCES {_USERS}(id) ON DELETE CASCADE,
                provider VARCHAR(50) NOT NULL,
                external_id VARCHAR(255) NOT NULL,
                created_at TIMESTAMPTZ,
                CONSTRAINT uq_ai_user_mapping_provider_external_id UNIQUE (provider, external_id)
            )
            """
        )
        op.execute(f"CREATE INDEX IF NOT EXISTS ix_{_MAPPINGS}_user_id ON {_MAPPINGS} (user_id)")
