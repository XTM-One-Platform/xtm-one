"""add the per-user sharing columns to the flow and nova tables

``flows``, ``flow_event_types``, ``nova_rules`` and ``flow_runs`` were created
carrying the three-tier visibility columns (``is_company_managed`` + ``shared_group_ids`` +
``shared_group_permissions``) but were missed by the sweep that added the
per-user counterpart everywhere else:

* ``shared_user_ids``         - JSON array of User UUIDs, mirroring ``shared_group_ids``
* ``shared_user_permissions`` - JSON ``{user_id: permission}``, mirroring ``shared_group_permissions``
* ``access_restricted``       - BOOLEAN; when set, a company-managed entity is
  visible only to its explicit grants instead of to everyone

That omission was not cosmetic. ``app.api.v1.helpers.user_visible_filter``
references both ``access_restricted`` and ``shared_user_ids`` unconditionally on
its non-admin branch (the admin branch short-circuits to ``TRUE``), so every
flow endpoint raised ``AttributeError: type object 'Flow' has no attribute
'access_restricted'`` — a 500 — for any non-admin caller. It stayed invisible
while the builder was feature-flagged and admin-only; shipping it unflagged
turned it into a hard 500 on ``/flows`` and on ``/chat`` (which reads
``/flows/chat-entries``) for every ordinary user. ``nova_rules`` carries the
same defect, still latent: ``GET /api/v1/nova-rules`` lists through the same
helper, so it 500s for the first non-admin caller that reaches it.

``flow_runs`` is here for a different reason — it never crashed, because it
re-implements the filter in Python rather than calling the helper. That copy
covered only the group tier, so a company flow narrowed to explicit grants
would have had its run journal readable by every authenticated user
(``_get_visible_run`` returned any company-managed run outright). The snapshot
now carries the audience columns it evaluates.

No GIN index here, by the same reasoning as that sweep (``d4e5ushr1881``): it indexed
``(shared_user_ids::jsonb)`` on ``agents`` and ``integrations`` only, because
the remaining tables are small, rarely-queried config tables where index
maintenance would cost more than the sequential scan it saves. Flows, event
type declarations and Nova rules are all a per-user handful listed from one
page.

Idempotent throughout — ``ADD COLUMN IF NOT EXISTS`` plus an explicit
convergence pass for ``access_restricted``, because
``app/core/migrations.py::_ensure_model_columns`` adds model-declared columns
"as nullable without defaults or constraints" ahead of the real migration on
databases stamped without running migration bodies. Since the models above now
declare these columns, that safety net WILL have created them bare-nullable on
any install that booted the new image before this revision ran — the
convergence pass is the path that actually executes there, not a theoretical one.

Lock safety: canonical recipe — idempotency probe + early return, then
``terminate_lock_holders`` + ``set_lock_timeout``, then metadata-only
``ADD COLUMN IF NOT EXISTS`` (nullable JSON → no rewrite; the boolean takes a
constant default, also metadata-only). The backfill in the convergence pass is
bounded by these two small tables.

Revision ID: g2h5flwshr1
Revises: f7h9runflw1
Create Date: 2026-08-22 13:30:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.core.migration_ddl import set_lock_timeout, terminate_lock_holders

# revision identifiers, used by Alembic.
revision: str = "g2h5flwshr1"
down_revision: str | None = "f7h9runflw1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_JSON_COLUMNS = ("shared_user_ids", "shared_user_permissions")
_BOOL_COLUMN = "access_restricted"

#: ``table -> JSON columns it needs``, for the tables carrying
#: ``shared_group_ids`` without the per-user set. Explicit rather than derived
#: from ``Base.metadata`` so this migration describes a fixed historical schema
#: and cannot silently widen when a future model gains the column.
#:
#: ``flow_runs`` takes only ``shared_user_ids``: it snapshots a flow's
#: visibility to answer "who may read this journal", never "what may they do",
#: so it mirrors the audience columns and none of the permission maps. Every
#: table takes ``access_restricted`` — a narrowed company entity must read as
#: narrowed wherever its audience is evaluated.
_TABLES = {
    "flows": _JSON_COLUMNS,
    "flow_event_types": _JSON_COLUMNS,
    "nova_rules": _JSON_COLUMNS,
    "flow_runs": ("shared_user_ids",),
}


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


def upgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    tables_in_db = set(inspector.get_table_names())

    for table, json_columns in _TABLES.items():
        # Fresh installs run create_all (already correct per the models) and
        # stamp head, so this body never executes for them.
        if table not in tables_in_db:
            continue

        needs_json = [
            col for col in json_columns if _column_state(conn, table, col) is None
        ]
        bool_state = _column_state(conn, table, _BOOL_COLUMN)
        bool_converged = (
            bool_state is not None and bool_state[0] == "NO" and bool_state[1] is not None
        )

        if not needs_json and bool_converged:
            continue  # already converged - no lock taken for this table

        terminate_lock_holders(conn, table)
        set_lock_timeout(conn)

        for col in needs_json:
            op.execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {col} JSON")

        if bool_state is None:
            op.execute(
                f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {_BOOL_COLUMN} "
                "BOOLEAN NOT NULL DEFAULT FALSE"
            )
        elif not bool_converged:
            # Created bare-nullable by the ``_ensure_model_columns`` safety net.
            # Backfill before SET NOT NULL - Postgres refuses the constraint
            # while any row is still NULL.
            op.execute(f"UPDATE {table} SET {_BOOL_COLUMN} = FALSE WHERE {_BOOL_COLUMN} IS NULL")  # noqa: S608
            op.execute(f"ALTER TABLE {table} ALTER COLUMN {_BOOL_COLUMN} SET DEFAULT FALSE")
            op.execute(f"ALTER TABLE {table} ALTER COLUMN {_BOOL_COLUMN} SET NOT NULL")


def downgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    tables_in_db = set(inspector.get_table_names())

    for table, json_columns in _TABLES.items():
        if table not in tables_in_db:
            continue
        # Probe before the lock recipe, mirroring ``upgrade``: a retry after a
        # complete or partial downgrade must not terminate lock holders and take
        # an ACCESS EXCLUSIVE lock to run DROP COLUMN IF EXISTS over columns
        # that are already gone.
        present = [
            col
            for col in (*json_columns, _BOOL_COLUMN)
            if _column_state(conn, table, col) is not None
        ]
        if not present:
            continue
        terminate_lock_holders(conn, table)
        set_lock_timeout(conn)
        for col in present:
            op.execute(f"ALTER TABLE {table} DROP COLUMN IF EXISTS {col}")
