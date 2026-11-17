"""drop the focus mode and development AI adoption columns (#11380)

``x11380devadopt1`` (xtm-one #11387) was the EXPAND phase of the focus mode and
development AI adoption removal: it dropped the two tables only administrator
paths read and left the columns below in place, unmapped and inert, because the
release before it maps them on every authenticated request (the user row, the
group rows, the platform settings singleton) and its replicas keep serving
during a rolling deploy (dev-docs/reference/migrations-safety.md, rule 4). This
is the CONTRACT phase. It drops:

* ``users.standalone_mode``, ``users.standalone_mode_chosen`` and
  ``users.focus_mode_policy``;
* ``groups.focus_mode_policy``;
* the ``platform_settings`` GitHub Copilot, Cursor and AI metrics sync columns:
  ``github_copilot_configs``, ``github_copilot_enterprise``,
  ``github_copilot_client_id_enc``, ``github_copilot_client_secret_enc``,
  ``github_copilot_access_token_enc``, ``github_copilot_refresh_token_enc``,
  ``github_copilot_token_expires_at``, ``cursor_api_key_enc``,
  ``cursor_is_enterprise``, ``ai_metrics_sync_enabled`` and
  ``dev_adoption_group_ids``.

Nothing reads them any more: no model maps them and no application code names
them (pinned by tests/test_focus_mode_contract_migration.py).

Whether a replica may still read them depends on where the upgrade started, as
for ``c2797toudrop1``. An install upgrading from a release that carries the
expand revision replaces replicas that never map these columns: the body drops
them. An install that jumps here from an older release applies the expand and
this revision in the same run while its old replicas still select the columns
until the rollout replaces them: the body keeps them, inert, as the expand
release carries them. A run whose start is unknown keeps them too. A run that
started from an empty database has no old replica and drops them. No migration
between the expand revision and this one commits mid-run, so no resumed run can
start past the expand while an old replica serves.

Lock profile: ``DROP COLUMN`` is metadata-only but takes ACCESS EXCLUSIVE on
each table, ``users`` the hottest. Canonical recipe per table: idempotency probe
-> terminate_lock_holders -> set_lock_timeout -> guarded DDL, one ``ALTER
TABLE`` per table.

The downgrade recreates every column with the type, nullability and server
default the baseline and ``w5foc3314005`` gave it. The dropped values are not
recoverable (Sam, 12 Nov 2026: "fully remove").

Revision ID: x11380focdrop1
Revises: x11380devadopt1
"""

import logging
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.core.migration_ddl import (
    applied_before_this_run,
    run_starting_heads,
    set_lock_timeout,
    terminate_lock_holders,
)

# revision identifiers, used by Alembic.
revision: str = "x11380focdrop1"
# NOT a constant: must always be main's CURRENT head, re-pointed on every rebase
# (backend/AGENTS.md > "Database migrations (safety)", rule 1).
down_revision: str | None = "x11380devadopt1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

logger = logging.getLogger(__name__)

# Spelled as a literal, never imported: a revision must keep meaning what it
# meant on the day it ran. The release carrying it no longer maps the columns.
_EXPAND_REVISION = "x11380devadopt1"

# (table, column, DDL type the downgrade recreates), per table in drop order.
_COLUMNS: tuple[tuple[str, str, str], ...] = (
    ("users", "standalone_mode", "BOOLEAN NOT NULL DEFAULT false"),
    ("users", "standalone_mode_chosen", "BOOLEAN NOT NULL DEFAULT false"),
    ("users", "focus_mode_policy", "VARCHAR(20)"),
    ("groups", "focus_mode_policy", "VARCHAR(20)"),
    ("platform_settings", "github_copilot_configs", "JSON"),
    ("platform_settings", "github_copilot_enterprise", "VARCHAR(255)"),
    ("platform_settings", "github_copilot_client_id_enc", "TEXT"),
    ("platform_settings", "github_copilot_client_secret_enc", "TEXT"),
    ("platform_settings", "github_copilot_access_token_enc", "TEXT"),
    ("platform_settings", "github_copilot_refresh_token_enc", "TEXT"),
    ("platform_settings", "github_copilot_token_expires_at", "TIMESTAMPTZ"),
    ("platform_settings", "cursor_api_key_enc", "TEXT"),
    ("platform_settings", "cursor_is_enterprise", "BOOLEAN NOT NULL DEFAULT false"),
    ("platform_settings", "ai_metrics_sync_enabled", "BOOLEAN NOT NULL DEFAULT true"),
    ("platform_settings", "dev_adoption_group_ids", "JSON"),
)


def _tables() -> list[str]:
    seen: list[str] = []
    for table, _column, _ddl in _COLUMNS:
        if table not in seen:
            seen.append(table)
    return seen


def _present_columns(conn, table: str) -> set[str]:
    # Scoped to the active schema: information_schema spans ALL schemas.
    rows = conn.execute(
        sa.text(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = current_schema() AND table_name = :table"
        ),
        {"table": table},
    ).fetchall()
    return {row[0] for row in rows}


def _old_readers_may_serve(migration_context) -> bool:
    """May a replica this rollout has not replaced yet still map the columns?

    Only a release before the expand revision maps them. An empty starting
    point has no replica; an unknown one cannot be proven safe.
    """
    starting_heads = run_starting_heads(migration_context)
    if starting_heads is None:
        return True
    if not starting_heads:
        return False
    return applied_before_this_run(migration_context, _EXPAND_REVISION) is not True


def upgrade() -> None:
    conn = op.get_bind()
    # 1. Idempotency probe: fresh installs and re-runs take NO lock.
    to_drop: dict[str, list[str]] = {}
    for table in _tables():
        present = _present_columns(conn, table)
        columns = [column for t, column, _ddl in _COLUMNS if t == table and column in present]
        if columns:
            to_drop[table] = columns
    if not to_drop:
        return
    migration_context = op.get_context()
    if _old_readers_may_serve(migration_context):
        starting_heads = run_starting_heads(migration_context)
        logger.warning(
            "Keeping the inert focus mode and development AI adoption columns (%s): "
            "this run started at %s, which may be an upgrade from a release that "
            "still maps them, and its replicas may still be serving",
            ", ".join(f"{table}.{column}" for table, columns in to_drop.items() for column in columns),
            ", ".join(starting_heads) if starting_heads else "an unknown revision",
        )
        return
    for table, columns in to_drop.items():
        # 2. Pre-flight, 3. safety belt, 4. guarded DDL, per table.
        terminate_lock_holders(conn, table)
        set_lock_timeout(conn)
        drops = ", ".join(f"DROP COLUMN IF EXISTS {column}" for column in columns)
        op.execute(f"ALTER TABLE {table} {drops}")


def downgrade() -> None:
    conn = op.get_bind()
    for table in _tables():
        present = _present_columns(conn, table)
        if not present:
            # No such table (an install that never had it): nothing to rebuild.
            continue
        missing = [(column, ddl) for t, column, ddl in _COLUMNS if t == table and column not in present]
        if not missing:
            continue
        terminate_lock_holders(conn, table)
        set_lock_timeout(conn)
        adds = ", ".join(f"ADD COLUMN IF NOT EXISTS {column} {ddl}" for column, ddl in missing)
        op.execute(f"ALTER TABLE {table} {adds}")
