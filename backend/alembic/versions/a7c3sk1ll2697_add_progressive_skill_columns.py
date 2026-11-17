"""add progressive skill disclosure columns

Adds two nullable JSON columns:

  - ``agents.skill_load_modes`` — per-skill load mode map
    ``{"<skill_uuid>": "progressive" | "always"}``.  NULL / missing key means
    "progressive" (catalog only, body pulled in by ``load_skill``); ``"always"``
    pins the body inline, which is the earlier behaviour kept as an escape
    hatch for the single-critical-skill agent.
  - ``evaluation_runs.skill_versions`` — ``{"<skill_uuid>": <version_number>}``
    snapshot of the skill versions active when the run was created, so a
    versioned replay resolves the pinned body instead of whatever the skill
    has been re-fetched to since.

Both are nullable ``ADD COLUMN`` with no default (metadata-only on PG 11+) and
follow the canonical lock-safe recipe: idempotency probe ->
terminate_lock_holders -> set_lock_timeout -> guarded DDL.  No backfill: NULL
is the correct value for every existing row (no pinned skills, no snapshot).

Revision ID: a7c3sk1ll2697
Revises: c3d4dlys0001
Create Date: 2026-07-31 09:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.core.migration_ddl import set_lock_timeout, terminate_lock_holders

# revision identifiers, used by Alembic.
revision: str = "a7c3sk1ll2697"
# NOT a constant: this must always be main's CURRENT head (so far: f7p1res2637 ->
# c3d4dlys0001). Two PRs both landed on `f7p1res2637` within minutes of
# each other, which forked the graph into two heads — invisible to git, since
# neither PR touched the other's files. `command.upgrade(cfg, "head")` in the
# startup runner is singular and refuses to run at all while main is forked, so
# no deployment (and no local dev stack) can migrate.
#
# Re-pointed onto the *other* branch's tip rather than the reverse: the daily-
# stats branch already had two revisions chained behind it, so moving it would
# have made this one a second child again and simply re-forked the graph a node
# earlier. The chain has to be linear, not merely single-headed — a merge
# revision rejoins the heads but leaves the middle branched, which the
# Migrations job's sequential `upgrade +1` rejects with "Ambiguous walk"
# (backend/AGENTS.md > "Database migrations (safety)", rule 1).
#
# Safe to move: the two ADD COLUMNs below touch `agents` and `evaluation_runs`,
# neither of which the revisions now in front of it (knowledge-base read-only
# flag, terms-of-use acceptance, daily-stats columns) create or alter.
down_revision: str | None = "c3d4dlys0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# (table, column, DDL type)
_COLUMNS: tuple[tuple[str, str, str], ...] = (
    ("agents", "skill_load_modes", "JSON"),
    ("evaluation_runs", "skill_versions", "JSON"),
)


def _column_exists(conn, table: str, column: str) -> bool:
    # Scope the probe to the active schema — information_schema.columns spans
    # ALL schemas, so a same-named table elsewhere must not make the probe
    # skip the ADD COLUMN in the schema being migrated.
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

    for table, column, ddl_type in _COLUMNS:
        # 1. Idempotency probe — steady-state re-runs take NO lock.
        if _column_exists(conn, table, column):
            continue
        # 2. Pre-flight: kill same-role lock holders so the ALTER is instant.
        terminate_lock_holders(conn, table)
        # 3. Safety belt: fail fast (55P03) instead of wedging the lock queue.
        set_lock_timeout(conn)
        # 4. Guarded, default-less (metadata-only) DDL.
        op.execute(
            f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {column} {ddl_type}"
        )


def downgrade() -> None:
    conn = op.get_bind()

    for table, column, _ddl_type in _COLUMNS:
        if not _column_exists(conn, table, column):
            continue
        terminate_lock_holders(conn, table)
        set_lock_timeout(conn)
        op.execute(f"ALTER TABLE {table} DROP COLUMN IF EXISTS {column}")
