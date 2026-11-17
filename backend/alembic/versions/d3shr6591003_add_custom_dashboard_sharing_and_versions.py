"""Share custom dashboards, and keep their versions.

Two changes for the Dashboard Studio:

* ``custom_dashboards`` gains the three-tier visibility column set
  (``VisibilityColumns``: four nullable JSON columns, two booleans defaulting to
  false) and a b-tree on ``is_company_managed``, as ``presentations`` has it.
  Every existing dashboard stays private to its owner.
* ``custom_dashboard_versions``: one row per saved revision of a dashboard
  (``dashboard_id`` ON DELETE CASCADE, ``author_id`` ON DELETE SET NULL), for
  the history and its restore. Existing dashboards start their history at
  their next write.

Safety contract (backend/AGENTS.md > "Database migrations (safety)"):

* Idempotent - ``ADD COLUMN IF NOT EXISTS`` plus a convergence pass for the two
  booleans (``_ensure_model_columns`` may have added them bare-nullable on a
  database stamped without running bodies), ``CREATE TABLE IF NOT EXISTS``,
  guarded foreign keys, indexes built only when missing. Fresh installs run
  ``create_all`` + ``stamp head``.
* Lock-safe - the canonical recipe (probe, ``terminate_lock_holders``,
  ``set_lock_timeout``) before metadata-only DDL on ``custom_dashboards``
  (nullable JSON; a boolean with a constant default is metadata-only on PG 11+).
  The new table is created without inline foreign keys; they are added
  ``NOT VALID`` on the empty table and validated separately. Indexes are plain
  builds (``create_index_in_place``) on a small and an empty table.

Revision ID: d3shr6591003
Revises: d2hom6591002
Create Date: 2026-10-23
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.core.migration_ddl import create_index_in_place, set_lock_timeout, terminate_lock_holders

revision: str = "d3shr6591003"
down_revision: str | None = "d2hom6591002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "custom_dashboards"
_VERSIONS = "custom_dashboard_versions"
_JSON_COLUMNS = ("shared_group_ids", "shared_group_permissions", "shared_user_ids", "shared_user_permissions")
_BOOL_COLUMNS = ("is_company_managed", "access_restricted")
_COMPANY_INDEX = "ix_custom_dashboards_is_company_managed"

_CREATE_VERSIONS = """
CREATE TABLE IF NOT EXISTS custom_dashboard_versions (
    id UUID PRIMARY KEY,
    dashboard_id UUID NOT NULL,
    revision INTEGER NOT NULL,
    title VARCHAR(200) NOT NULL,
    widget_count INTEGER NOT NULL DEFAULT 0,
    spec JSON NOT NULL,
    source VARCHAR(20) NOT NULL,
    summary VARCHAR(500),
    restored_from_revision INTEGER,
    author_id UUID,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL,
    CONSTRAINT uq_custom_dashboard_versions_revision UNIQUE (dashboard_id, revision)
)
"""

# (constraint name, literal DDL): spelled out, never assembled from variables (S608).
_FOREIGN_KEYS: tuple[tuple[str, str], ...] = (
    (
        "custom_dashboard_versions_dashboard_id_fkey",
        "ALTER TABLE custom_dashboard_versions ADD CONSTRAINT custom_dashboard_versions_dashboard_id_fkey "
        "FOREIGN KEY (dashboard_id) REFERENCES custom_dashboards(id) ON DELETE CASCADE NOT VALID",
    ),
    (
        "custom_dashboard_versions_author_id_fkey",
        "ALTER TABLE custom_dashboard_versions ADD CONSTRAINT custom_dashboard_versions_author_id_fkey "
        "FOREIGN KEY (author_id) REFERENCES users(id) ON DELETE SET NULL NOT VALID",
    ),
)

_VERSION_INDEXES: tuple[tuple[str, str], ...] = (
    ("ix_custom_dashboard_versions_dashboard_id", "(dashboard_id)"),
    ("ix_custom_dashboard_versions_dashboard_created", "(dashboard_id, created_at)"),
)


def _column_state(conn, table: str, column: str):
    """``(is_nullable, column_default)`` of a column, or ``None`` when missing."""
    return conn.execute(
        sa.text(
            "SELECT is_nullable, column_default FROM information_schema.columns "
            "WHERE table_schema = current_schema() AND table_name = :table AND column_name = :column"
        ),
        {"table": table, "column": column},
    ).one_or_none()


def _table_exists(conn, table: str) -> bool:
    return bool(
        conn.execute(
            sa.text(
                "SELECT 1 FROM information_schema.tables WHERE table_schema = current_schema() AND table_name = :table"
            ),
            {"table": table},
        ).scalar()
    )


def _index_exists(conn, name: str) -> bool:
    return bool(
        conn.execute(
            sa.text("SELECT 1 FROM pg_indexes WHERE schemaname = current_schema() AND indexname = :name"),
            {"name": name},
        ).scalar()
    )


def _constraint_exists(conn, name: str) -> bool:
    return bool(conn.execute(sa.text("SELECT 1 FROM pg_constraint WHERE conname = :name"), {"name": name}).scalar())


def _bool_converged(state) -> bool:
    return state is not None and state[0] == "NO" and state[1] is not None


def _upgrade_sharing_columns(conn) -> None:
    needs_json = [col for col in _JSON_COLUMNS if _column_state(conn, _TABLE, col) is None]
    bool_states = {col: _column_state(conn, _TABLE, col) for col in _BOOL_COLUMNS}
    if not needs_json and all(_bool_converged(state) for state in bool_states.values()):
        return  # already converged - no lock taken
    terminate_lock_holders(conn, _TABLE)
    set_lock_timeout(conn)
    for col in needs_json:
        op.execute(f"ALTER TABLE {_TABLE} ADD COLUMN IF NOT EXISTS {col} JSON")
    for col, state in bool_states.items():
        if state is None:
            op.execute(f"ALTER TABLE {_TABLE} ADD COLUMN IF NOT EXISTS {col} BOOLEAN NOT NULL DEFAULT FALSE")
        elif not _bool_converged(state):
            # Bare-nullable from the ``_ensure_model_columns`` safety net: backfill first.
            op.execute(f"UPDATE {_TABLE} SET {col} = FALSE WHERE {col} IS NULL")  # noqa: S608
            op.execute(f"ALTER TABLE {_TABLE} ALTER COLUMN {col} SET DEFAULT FALSE")
            op.execute(f"ALTER TABLE {_TABLE} ALTER COLUMN {col} SET NOT NULL")


def upgrade() -> None:
    conn = op.get_bind()
    if not _table_exists(conn, _TABLE):
        return  # a fresh install: create_all made both tables
    _upgrade_sharing_columns(conn)

    if not _table_exists(conn, _VERSIONS):
        set_lock_timeout(conn)
        conn.execute(sa.text(_CREATE_VERSIONS))
    for name, ddl in _FOREIGN_KEYS:
        if not _constraint_exists(conn, name):
            terminate_lock_holders(conn, _VERSIONS)
            set_lock_timeout(conn)
            conn.execute(sa.text(ddl))
        set_lock_timeout(conn)
        conn.execute(sa.text(f"ALTER TABLE {_VERSIONS} VALIDATE CONSTRAINT {name}"))

    with op.get_context().autocommit_block():
        if not _index_exists(conn, _COMPANY_INDEX):
            create_index_in_place(conn, _COMPANY_INDEX, _TABLE, "(is_company_managed)")
        for name, columns in _VERSION_INDEXES:
            if not _index_exists(conn, name):
                create_index_in_place(conn, name, _VERSIONS, columns)


def downgrade() -> None:
    conn = op.get_bind()
    if _table_exists(conn, _VERSIONS):
        terminate_lock_holders(conn, _VERSIONS)
        set_lock_timeout(conn)
        conn.execute(sa.text(f"DROP TABLE IF EXISTS {_VERSIONS}"))
    if not _table_exists(conn, _TABLE):
        return
    present = [col for col in (*_JSON_COLUMNS, *_BOOL_COLUMNS) if _column_state(conn, _TABLE, col) is not None]
    if not present and not _index_exists(conn, _COMPANY_INDEX):
        return
    terminate_lock_holders(conn, _TABLE)
    set_lock_timeout(conn)
    conn.execute(sa.text(f"DROP INDEX IF EXISTS {_COMPANY_INDEX}"))
    for col in present:
        conn.execute(sa.text(f"ALTER TABLE {_TABLE} DROP COLUMN IF EXISTS {col}"))
