"""add per-user sharing grants and company-managed access restriction

Sharing was expressible only as a list of *group* ids, so "share this agent
with Alice" required inventing a two-person group. This adds the per-user
counterpart of the existing group columns to every entity that carries
``shared_group_ids``:

* ``shared_user_ids``         - JSON array of User UUIDs, mirroring ``shared_group_ids``
* ``shared_user_permissions`` - JSON ``{user_id: permission}``, mirroring ``shared_group_permissions``
* ``access_restricted``       - BOOLEAN; when set, a company-managed entity is
  visible only to its explicit grants instead of to everyone

Both principal kinds are unioned in ``app.api.v1.helpers.user_visible_filter``,
so all of its call sites inherit per-user sharing without a signature change.

``evaluations`` additionally gains ``shared_group_permissions``: it was the
only shared entity without it, which would have left its group grants untiered
while its user grants were tiered. NULL preserves the existing behaviour
(``get_group_permission`` already defaults a missing entry to ``read_only``).

Two GIN indexes are created on ``(shared_user_ids::jsonb)``, for ``agents``
(resolved on every chat message, picker render and catalog query) and
``integrations`` (parity with its existing group index). The remaining tables
are small, rarely-queried config tables where the index maintenance would cost
more than the sequential scan it saves. Both are built ``CONCURRENTLY`` in an
autocommit block — these are hot tables, and a plain build would hold a write
lock for its whole duration.

Idempotent throughout — ``ADD COLUMN IF NOT EXISTS`` plus an explicit
convergence pass for ``access_restricted``. The convergence pass is required
because ``app/core/migrations.py::_ensure_model_columns`` adds model-declared
columns "as nullable without defaults or constraints" ahead of the real
migration on databases that were stamped without running migration bodies —
exactly the gap that hit ``oauth_share_acknowledged``.

Chains onto ``e7a3wait2698``, which merged one minute earlier.  This revision
was cut from ``a7f3run0003`` and merged after two other revisions had already
moved the tip, leaving the history with three heads; it is re-pointed onto
the newest one.  Order carries no consequence: the three forked revisions touch
disjoint objects (the ``shared_user_*`` columns here, ``general_assistant_config``
for the wait bounds, the ``nova_*`` tables for Nova), and the ambiguous head has
kept ``alembic upgrade head`` from running at all, so no database is stamped at
any of them.

Revision ID: d4e5ushr1881
Revises: e7a3wait2698
Create Date: 2026-08-14 12:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.core.migration_ddl import set_lock_timeout, terminate_lock_holders

# revision identifiers, used by Alembic.
revision: str = "d4e5ushr1881"
down_revision: str | None = "e7a3wait2698"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: Every table carrying ``shared_group_ids``. Kept explicit rather than derived
#: from ``Base.metadata`` so the migration describes a fixed historical schema
#: and cannot silently widen when a future model gains the column.
_TABLES = (
    "agents",
    "agent_skills",
    "channel_bots",
    "custom_tools",
    "datasets",
    "evaluations",
    "integrations",
    "knowledge_bases",
    "mcp_servers",
    "prompts",
    "runners",
    "template_containers",
    "variables",
)

#: ``(table, index_name)`` for the GIN indexes over ``(shared_user_ids::jsonb)``.
_GIN_INDEXES = (
    ("agents", "ix_agents_shared_user_ids_jsonb_gin"),
    ("integrations", "ix_integrations_shared_user_ids_jsonb_gin"),
)

_JSON_COLUMNS = ("shared_user_ids", "shared_user_permissions")
_BOOL_COLUMN = "access_restricted"


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

    for table in _TABLES:
        # Fresh installs run create_all (already correct per the models) and
        # stamp head, so this body never executes for them.
        if table not in tables_in_db:
            continue

        needs_json = [
            col for col in _JSON_COLUMNS if _column_state(conn, table, col) is None
        ]
        bool_state = _column_state(conn, table, _BOOL_COLUMN)
        # ``evaluations`` is the only table that also needs the group-permission
        # column; everywhere else this probe finds it already present.
        needs_group_perms = (
            _column_state(conn, table, "shared_group_permissions") is None
        )
        bool_converged = bool_state is not None and bool_state[0] == "NO" and bool_state[1] is not None

        if not needs_json and not needs_group_perms and bool_converged:
            continue  # already converged - no lock taken for this table

        terminate_lock_holders(conn, table)
        set_lock_timeout(conn)

        for col in needs_json:
            op.execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {col} JSON")
        if needs_group_perms:
            op.execute(
                f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS shared_group_permissions JSON"
            )

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

    # Built CONCURRENTLY: ``agents`` and ``integrations`` are hot tables, and a
    # plain index build holds a write lock for its whole duration. CONCURRENTLY
    # must run outside a transaction and must NOT inherit the ``lock_timeout``
    # set above — the build legitimately waits on open snapshots — hence the
    # autocommit block and the explicit timeout reset (AGENTS.md, "Index
    # idiom").
    for table, index_name in _GIN_INDEXES:
        if table not in tables_in_db:
            continue
        with op.get_context().autocommit_block():
            op.execute("SET lock_timeout = 0")
            op.execute("SET statement_timeout = 0")
            op.execute(
                f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {index_name} ON {table} "
                "USING gin ((shared_user_ids::jsonb))"
            )


def downgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    tables_in_db = set(inspector.get_table_names())

    for _table, index_name in _GIN_INDEXES:
        with op.get_context().autocommit_block():
            op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {index_name}")

    for table in _TABLES:
        if table not in tables_in_db:
            continue
        terminate_lock_holders(conn, table)
        set_lock_timeout(conn)
        for col in (*_JSON_COLUMNS, _BOOL_COLUMN):
            op.execute(f"ALTER TABLE {table} DROP COLUMN IF EXISTS {col}")

    # ``evaluations.shared_group_permissions`` is intentionally NOT dropped:
    # this migration only creates it where it was missing, and dropping it on
    # downgrade would destroy permission data on any database that acquired the
    # column through another path.
