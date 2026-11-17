"""add the three-tier visibility columns to presentations

A deck becomes a shareable entity: one person asks their assistant to draft
it, shares the link, and a colleague keeps working on it with their own agent.
That is the platform's existing sharing model, so ``presentations`` takes the
same column set every other shareable entity carries — and needs ALL of it,
because ``app.api.v1.helpers.user_visible_filter`` reads ``shared_user_ids``
and ``access_restricted`` unconditionally on its non-admin branch (see the
``g2h5flwshr1`` migration for the production 500 a partial set produces):

* ``is_company_managed``       - BOOLEAN NOT NULL DEFAULT FALSE; the published,
  read-only-for-everyone form of a deck
* ``shared_group_ids``         - JSON array of Group UUIDs
* ``shared_group_permissions`` - JSON ``{group_id: permission}``
* ``shared_user_ids``          - JSON array of User UUIDs
* ``shared_user_permissions``  - JSON ``{user_id: permission}``
* ``access_restricted``        - BOOLEAN NOT NULL DEFAULT FALSE; narrows a
  company-managed deck to its explicit grants

No GIN index, by the same reasoning as the per-user sharing migration: only ``agents`` and
``integrations`` index ``(shared_user_ids::jsonb)``; a user's decks are a
handful listed from one page, where index maintenance would cost more than
the sequential scan it saves. The plain b-tree on ``is_company_managed``
mirrors ``template_containers``.

Idempotent throughout — ``ADD COLUMN IF NOT EXISTS`` plus a convergence pass
for the two booleans, because ``app/core/migrations.py::_ensure_model_columns``
adds model-declared columns "as nullable without defaults or constraints"
ahead of the real migration on databases stamped without running migration
bodies. Any install that booted the new image before this revision ran has
the columns bare-nullable already; the convergence pass is the path that
executes there.

Lock safety: canonical recipe — idempotency probe + early return, then
``terminate_lock_holders`` + ``set_lock_timeout``, then metadata-only DDL
(nullable JSON → no rewrite; a boolean with a constant default is metadata-only
on PG 11+). The backfill in the convergence pass is bounded by a small table.

Revision ID: h2p3358share
Revises: f1p3358dflt
Create Date: 2026-09-06 14:30:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.core.migration_ddl import set_lock_timeout, terminate_lock_holders

# revision identifiers, used by Alembic.
revision: str = "h2p3358share"
down_revision: str | None = "f1p3358dflt"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "presentations"
_JSON_COLUMNS = (
    "shared_group_ids",
    "shared_group_permissions",
    "shared_user_ids",
    "shared_user_permissions",
)
_BOOL_COLUMNS = ("is_company_managed", "access_restricted")
_INDEX = "ix_presentations_is_company_managed"


def _column_state(conn, table: str, column: str):
    """Return ``(is_nullable, column_default)`` for a column, or ``None`` if missing."""
    return conn.execute(
        sa.text(
            "SELECT is_nullable, column_default FROM information_schema.columns "
            "WHERE table_schema = current_schema() "
            "AND table_name = :table AND column_name = :column"
        ),
        {"table": table, "column": column},
    ).one_or_none()


def _index_exists(conn, name: str) -> bool:
    return bool(
        conn.execute(
            sa.text(
                "SELECT 1 FROM pg_indexes "
                "WHERE schemaname = current_schema() AND indexname = :name"
            ),
            {"name": name},
        ).scalar()
    )


def _bool_converged(state) -> bool:
    return state is not None and state[0] == "NO" and state[1] is not None


def upgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    # Fresh installs run create_all (already correct per the model) and stamp
    # head, so this body never executes for them.
    if _TABLE not in set(inspector.get_table_names()):
        return

    needs_json = [
        col for col in _JSON_COLUMNS if _column_state(conn, _TABLE, col) is None
    ]
    bool_states = {col: _column_state(conn, _TABLE, col) for col in _BOOL_COLUMNS}
    bools_converged = all(_bool_converged(state) for state in bool_states.values())
    needs_index = not _index_exists(conn, _INDEX)

    if not needs_json and bools_converged and not needs_index:
        return  # already converged - no lock taken

    terminate_lock_holders(conn, _TABLE)
    set_lock_timeout(conn)

    for col in needs_json:
        op.execute(f"ALTER TABLE {_TABLE} ADD COLUMN IF NOT EXISTS {col} JSON")

    for col, state in bool_states.items():
        if state is None:
            op.execute(
                f"ALTER TABLE {_TABLE} ADD COLUMN IF NOT EXISTS {col} "
                "BOOLEAN NOT NULL DEFAULT FALSE"
            )
        elif not _bool_converged(state):
            # Created bare-nullable by the ``_ensure_model_columns`` safety net.
            # Backfill before SET NOT NULL - Postgres refuses the constraint
            # while any row is still NULL.
            op.execute(f"UPDATE {_TABLE} SET {col} = FALSE WHERE {col} IS NULL")  # noqa: S608
            op.execute(f"ALTER TABLE {_TABLE} ALTER COLUMN {col} SET DEFAULT FALSE")
            op.execute(f"ALTER TABLE {_TABLE} ALTER COLUMN {col} SET NOT NULL")

    if needs_index:
        # Plain (non-CONCURRENTLY) build is fine: the table is small and the
        # lock is already held for the column changes above.
        op.execute(
            f"CREATE INDEX IF NOT EXISTS {_INDEX} ON {_TABLE} (is_company_managed)"
        )


def downgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    if _TABLE not in set(inspector.get_table_names()):
        return
    # Probe before the lock recipe, mirroring ``upgrade``: a retry after a
    # complete or partial downgrade must not terminate lock holders to run
    # DROP COLUMN IF EXISTS over columns that are already gone.
    present = [
        col
        for col in (*_JSON_COLUMNS, *_BOOL_COLUMNS)
        if _column_state(conn, _TABLE, col) is not None
    ]
    has_index = _index_exists(conn, _INDEX)
    if not present and not has_index:
        return
    terminate_lock_holders(conn, _TABLE)
    set_lock_timeout(conn)
    if has_index:
        op.execute(f"DROP INDEX IF EXISTS {_INDEX}")
    for col in present:
        op.execute(f"ALTER TABLE {_TABLE} DROP COLUMN IF EXISTS {col}")
