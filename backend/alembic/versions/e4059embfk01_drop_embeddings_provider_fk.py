"""drop the embeddings.embedding_provider_id foreign key

``embeddings.embedding_provider_id`` referenced ``llm_provider_configs.id`` with
``ON DELETE SET NULL``. Deleting a provider config therefore ran, inside the
``DELETE``, an ``UPDATE embeddings SET embedding_provider_id = NULL`` over every
chunk that config embedded: one long statement under the 30 s
``statement_timeout``, executed while the deletion holds its lock on every
provider config row. Nothing reads the column for a decision, like the
``platform_settings.*_provider_id`` references, which carry no FK either. The
column and its index stay; the scheduler nulls the id of a deleted config
afterwards, in bounded batches
(``llm_provider_deletion.clear_deleted_embedding_provider_references``), so the
end state is the one the cascade produced.

The constraint is found by definition (``embeddings.embedding_provider_id`` ->
``llm_provider_configs``), not by name: the baseline created it unnamed.

Lock profile: dropping a foreign key takes ACCESS EXCLUSIVE on both tables (the
RI triggers live on each), and both are hot - every ingestion writes
``embeddings``, every model call reads ``llm_provider_configs``. Canonical
pre-flight on both: idempotency probe -> terminate_lock_holders ->
set_lock_timeout -> guarded DDL. The downgrade restores the constraint
``NOT VALID``: no scan of ``embeddings``, and an id the sweep has not nulled yet
does not fail it.

Revision ID: e4059embfk01
Revises: s2977stale01
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.core.migration_ddl import set_lock_timeout, terminate_lock_holders

# revision identifiers, used by Alembic.
revision: str = "e4059embfk01"
down_revision: str | None = "s2977stale01"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "embeddings"
_REFERENCED = "llm_provider_configs"
_COLUMN = "embedding_provider_id"
_CONSTRAINT = "embeddings_embedding_provider_id_fkey"


def _provider_foreign_keys(conn) -> list[str]:
    """Names of every FK from ``embeddings.embedding_provider_id`` to the configs."""
    return list(
        conn.execute(
            sa.text(
                "SELECT con.conname FROM pg_constraint con "
                "JOIN pg_class rel ON rel.oid = con.conrelid "
                "JOIN pg_namespace ns ON ns.oid = rel.relnamespace "
                "JOIN pg_class ref ON ref.oid = con.confrelid "
                "JOIN pg_attribute att ON att.attrelid = con.conrelid "
                "  AND att.attnum = con.conkey[1] "
                "WHERE con.contype = 'f' "
                "  AND ns.nspname = current_schema() "
                "  AND rel.relname = :table "
                "  AND ref.relname = :referenced "
                "  AND array_length(con.conkey, 1) = 1 "
                "  AND att.attname = :column "
                "ORDER BY con.conname"
            ),
            {"table": _TABLE, "referenced": _REFERENCED, "column": _COLUMN},
        ).scalars()
    )


def _quoted(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def upgrade() -> None:
    conn = op.get_bind()
    # 1. Idempotency probe - steady-state re-runs take NO lock.
    names = _provider_foreign_keys(conn)
    if not names:
        return
    # 2. Pre-flight on both tables the DROP locks.
    terminate_lock_holders(conn, _TABLE)
    terminate_lock_holders(conn, _REFERENCED)
    # 3. Safety belt: fail fast (55P03) instead of wedging the lock queue.
    set_lock_timeout(conn)
    # 4. Guarded DDL.
    for name in names:
        op.execute(f"ALTER TABLE {_TABLE} DROP CONSTRAINT IF EXISTS {_quoted(name)}")


def downgrade() -> None:
    conn = op.get_bind()
    if _provider_foreign_keys(conn):
        return
    terminate_lock_holders(conn, _TABLE)
    terminate_lock_holders(conn, _REFERENCED)
    set_lock_timeout(conn)
    op.execute(
        f"ALTER TABLE {_TABLE} ADD CONSTRAINT {_CONSTRAINT} "
        f"FOREIGN KEY ({_COLUMN}) REFERENCES {_REFERENCED} (id) "
        "ON DELETE SET NULL NOT VALID"
    )
