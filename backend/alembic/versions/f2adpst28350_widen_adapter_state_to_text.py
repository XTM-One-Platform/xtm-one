"""widen orchestration_executions.adapter_state to TEXT

The autonomous OpenAEV orchestrator adapter does not store a short sub-state
label in ``adapter_state`` - it serializes a small JSON blob carrying the cycle
counter, last mode, and (since the forward-progress guard) a no-progress streak:

    {"cycle": 1, "mode": "continue", "no_progress_streak": 0}   # 57 chars

The column was ``VARCHAR(50)``, so that value overflowed and the reconciler's
``UPDATE orchestration_executions SET adapter_state = ...`` failed with
``StringDataRightTruncationError: value too long for type character varying(50)``
- every autonomous run then stalled, unable to persist its cycle state.

``adapter_state`` is an opaque, adapter-defined value, not a bounded enum, so it
becomes ``TEXT`` to match how it is actually used (mirrors ``app/models/orchestration.py``).

Lock safety: ``varchar(n) -> text`` is a metadata-only change on PostgreSQL (same
on-disk representation, the length check is only dropped), so no table rewrite -
but it still takes a brief ``ACCESS EXCLUSIVE`` lock, hence the canonical
recipe: idempotency probe (early-return when already TEXT / table absent) ->
terminate_lock_holders -> set_lock_timeout -> guarded DDL.

Revision ID: f2adpst28350
Revises: d5e8mfa02786
Create Date: 2026-08-12 09:30:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.core.migration_ddl import set_lock_timeout, terminate_lock_holders

# revision identifiers, used by Alembic.
revision: str = "f2adpst28350"
down_revision: str | None = "d5e8mfa02786"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "orchestration_executions"
_COLUMN = "adapter_state"


def _char_max_length(conn) -> int | None:
    """Return the column's ``character_maximum_length`` (NULL/None for TEXT)."""
    return conn.execute(
        sa.text(
            "SELECT character_maximum_length FROM information_schema.columns "
            "WHERE table_schema = current_schema() "
            "AND table_name = :table AND column_name = :column"
        ),
        {"table": _TABLE, "column": _COLUMN},
    ).scalar()


def upgrade() -> None:
    conn = op.get_bind()

    # Fresh installs run create_all + stamp head; this body never executes there.
    if not sa.inspect(conn).has_table(_TABLE):
        return

    # Already TEXT (unbounded) -> nothing to do, take no lock.
    if _char_max_length(conn) is None:
        return

    terminate_lock_holders(conn, _TABLE)
    set_lock_timeout(conn)
    op.execute(f"ALTER TABLE {_TABLE} ALTER COLUMN {_COLUMN} TYPE TEXT")


def downgrade() -> None:
    conn = op.get_bind()

    if not sa.inspect(conn).has_table(_TABLE):
        return

    # Already bounded -> nothing to do.
    if _char_max_length(conn) is not None:
        return

    terminate_lock_holders(conn, _TABLE)
    set_lock_timeout(conn)
    # left(...) guards against rows that already exceed 50 chars (the very
    # overflow this migration exists to allow); a plain cast would error.
    op.execute(
        f"ALTER TABLE {_TABLE} ALTER COLUMN {_COLUMN} TYPE VARCHAR(50) "
        f"USING left({_COLUMN}, 50)"
    )
