"""add file_attachments.image_meta

The geometry of a stored image a deck needs before drawing it: the size it was
read at, the box of its non-transparent pixels and the mean opacity inside it
(``app.services.image_geometry``). A logo wall sizes the MARK, not the file -
a logo exported on a padded artboard otherwise draws at half the height the
deck asks for - and the renderer must know the mark's box before the image
loads, on screen, in the PDF and in the DOM the editable PPTX is measured
from. Read once when the bytes are stored and kept here. Nullable, no default:
every existing row reads as "not read yet" and is read on the next deck save
that references it.

Lock profile: a nullable ``ADD COLUMN`` with no default is metadata-only on
PG 11+, but the ALTER still takes ACCESS EXCLUSIVE on ``file_attachments`` - a
HOT table (every chat upload and generated file is a row here), so any open
transaction that has merely read it queues the ALTER behind itself in a rolling
deploy. Canonical pre-flight: idempotency probe -> terminate_lock_holders ->
set_lock_timeout -> guarded DDL.

Revision ID: b3818logo001
Revises: s2528paused01
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.core.migration_ddl import set_lock_timeout, terminate_lock_holders

# revision identifiers, used by Alembic.
revision: str = "b3818logo001"
down_revision: str | None = "s2528paused01"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "file_attachments"
_COLUMN = "image_meta"


def _column_exists(conn, table: str, column: str) -> bool:
    # Scoped to the active schema - information_schema.columns spans ALL
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
    # 1. Idempotency probe - steady-state re-runs take NO lock.
    if _column_exists(conn, _TABLE, _COLUMN):
        return
    # 2. Pre-flight: kill same-role lock holders on the table the DDL locks.
    terminate_lock_holders(conn, _TABLE)
    # 3. Safety belt: fail fast (55P03) instead of wedging the lock queue.
    set_lock_timeout(conn)
    # 4. Guarded DDL - nullable, no default: metadata-only, no rewrite.
    op.execute(f"ALTER TABLE {_TABLE} ADD COLUMN IF NOT EXISTS {_COLUMN} JSON")


def downgrade() -> None:
    conn = op.get_bind()
    if not _column_exists(conn, _TABLE, _COLUMN):
        return
    terminate_lock_holders(conn, _TABLE)
    set_lock_timeout(conn)
    op.execute(f"ALTER TABLE {_TABLE} DROP COLUMN IF EXISTS {_COLUMN}")
