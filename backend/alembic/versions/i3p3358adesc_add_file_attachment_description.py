"""add file_attachments.description

What a template asset DEPICTS, for the agent that has to choose one. A template
container can hold dozens of predefined assets — brand logos, product logos, the
XTM platform diagram, the OpenAEV architecture, a CTEM loop — and an agent
composing a deck sees them only as ``[illustration] xtm-platform.svg``: a file
name is not a description, and the ``image`` block takes a file id, so an asset
the agent cannot tell apart is an asset it never places. The seed manifests
already carry a description per asset; this column is where it lands, and the
Templates UI / authoring tool let an admin describe their own uploads the same
way. Nullable, no default: every non-template attachment (chat uploads,
generated files) simply leaves it NULL.

Lock profile: a nullable ``ADD COLUMN`` with no default is metadata-only on
PG 11+, but the ALTER still takes ACCESS EXCLUSIVE on ``file_attachments`` — a
HOT table (every chat upload and generated file is a row here), so any open
transaction that has merely read it queues the ALTER behind itself in a rolling
deploy. Canonical pre-flight: idempotency probe -> terminate_lock_holders ->
set_lock_timeout -> guarded DDL.

Revision ID: i3p3358adesc
Revises: h2p3358share
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.core.migration_ddl import set_lock_timeout, terminate_lock_holders

# revision identifiers, used by Alembic.
revision: str = "i3p3358adesc"
down_revision: str | None = "h2p3358share"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "file_attachments"
_COLUMN = "description"


def _column_exists(conn, table: str, column: str) -> bool:
    # Scoped to the active schema — information_schema.columns spans ALL
    # schemas, so a same-named table elsewhere must not make the probe skip
    # the ADD COLUMN in the schema being migrated.
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
    # 1. Idempotency probe — steady-state re-runs take NO lock.
    if _column_exists(conn, _TABLE, _COLUMN):
        return
    # 2. Pre-flight: kill same-role lock holders on the table the DDL locks.
    terminate_lock_holders(conn, _TABLE)
    # 3. Safety belt: fail fast (55P03) instead of wedging the lock queue.
    set_lock_timeout(conn)
    # 4. Guarded DDL — nullable, no default: metadata-only, no rewrite.
    op.execute(f"ALTER TABLE {_TABLE} ADD COLUMN IF NOT EXISTS {_COLUMN} TEXT")


def downgrade() -> None:
    conn = op.get_bind()
    if not _column_exists(conn, _TABLE, _COLUMN):
        return
    terminate_lock_holders(conn, _TABLE)
    set_lock_timeout(conn)
    op.execute(f"ALTER TABLE {_TABLE} DROP COLUMN IF EXISTS {_COLUMN}")
