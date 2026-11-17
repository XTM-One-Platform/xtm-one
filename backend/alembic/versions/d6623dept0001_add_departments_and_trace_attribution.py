"""add departments and the attribution of trace usage

Department-grade LLM analytics:

* ``departments`` and ``department_group_rules`` (the ordered group mapping),
  ``groups.kind``, ``users.directory_department`` and
  ``platform_settings.department_settings``;
* on ``llm_traces``, who a trace's usage counts for (``attributed_user_id``,
  ``attribution``), its department (``department_id``,
  ``department_resolved_at``, ``department_backfilled``), the agent's name
  (``agent_name_snapshot``) and the cached input tokens;
* the same on ``llm_daily_stats``, whose grain gains ``attributed_user_id``,
  ``attribution`` and ``department_id``;
* ``trace_attribution_backfill``, where the one-time stamping of the existing
  traces stands. The stamping itself runs in the trace maintenance, in bounded
  batches (``services/tracing/attribution_backfill.py``), never here.

Every added column is nullable or carries a constant default, so each ``ADD
COLUMN`` is metadata-only, and each ALTER follows the canonical recipe. The
rule table's foreign keys lock ``groups``, so its ``CREATE TABLE`` runs under
``set_lock_timeout``.

The rollup's unique constraint keeps its name (the aggregator of a replica
still on the older build names it in its ``ON CONFLICT``): its new unique index
is built in place first, then the constraint is moved onto it under the
recipe. The downgrade merges the rows the older grain would see twice (a day's
traces of one agent and model with no user, attributed to several people)
before it moves the constraint back; a merged row keeps its sums exactly, its
percentiles approximately (the read path flags a row without a histogram).

Revision ID: d6623dept0001
Revises: r1plc6535001
Create Date: 2026-10-27 12:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.core.migration_ddl import create_index_in_place, set_lock_timeout, terminate_lock_holders

revision: str = "d6623dept0001"
# NOT a constant: must always be the base branch's CURRENT head, re-pointed
# whenever it gains a migration (backend/AGENTS.md > "Database migrations
# (safety)", rule 1).
down_revision: str | Sequence[str] | None = "r1plc6535001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ROLLUP = "llm_daily_stats"
_GRAIN_CONSTRAINT = "uq_llm_daily_stats_date_agent_model_source_user"
_OLD_GRAIN = ("date", "agent_id", "model", "source", "user_id")
_NEW_GRAIN = (*_OLD_GRAIN, "attributed_user_id", "attribution", "department_id")
_NEW_GRAIN_INDEX = "uq_llm_daily_stats_grain_6623"
_OLD_GRAIN_INDEX = "uq_llm_daily_stats_grain_old"

#: Columns added to existing tables, with their DDL type.
_COLUMNS: dict[str, dict[str, str]] = {
    "groups": {"kind": "VARCHAR(20) NOT NULL DEFAULT 'auto'"},
    "users": {"directory_department": "VARCHAR(200)"},
    "platform_settings": {"department_settings": "JSON"},
    "llm_traces": {
        "attributed_user_id": "UUID",
        "attribution": "VARCHAR(20)",
        "department_id": "UUID",
        "department_resolved_at": "TIMESTAMPTZ",
        "department_backfilled": "BOOLEAN NOT NULL DEFAULT false",
        "agent_name_snapshot": "VARCHAR(255)",
        "cached_input_tokens": "INTEGER",
    },
    _ROLLUP: {
        "attributed_user_id": "UUID",
        "attribution": "VARCHAR(20)",
        "department_id": "UUID",
        "department_backfilled": "BOOLEAN NOT NULL DEFAULT false",
        "agent_name_snapshot": "VARCHAR(255)",
        "total_cached_input_tokens": "BIGINT NOT NULL DEFAULT 0",
    },
}


def _columns(conn, table: str) -> set[str]:
    inspector = sa.inspect(conn)
    if not inspector.has_table(table):
        return set()
    return {column["name"] for column in inspector.get_columns(table)}


def _grain(conn) -> tuple[str, ...] | None:
    """The columns of the rollup's unique constraint, in order; None without one."""
    row = conn.execute(
        sa.text(
            """
            SELECT array_agg(a.attname ORDER BY k.ordinality)
            FROM pg_constraint c
            JOIN LATERAL unnest(c.conkey) WITH ORDINALITY AS k(attnum, ordinality) ON true
            JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = k.attnum
            WHERE c.conname = :name AND c.conrelid = to_regclass(:table)
            """
        ),
        {"name": _GRAIN_CONSTRAINT, "table": _ROLLUP},
    ).scalar()
    return tuple(row) if row else None


def _move_grain(conn, columns: tuple[str, ...], index: str) -> None:
    """Put the rollup's unique constraint on *columns*: build *index* in place, then move the constraint onto it."""
    if _grain(conn) == columns:
        return
    with op.get_context().autocommit_block():
        create_index_in_place(
            conn,
            index,
            _ROLLUP,
            f"({', '.join(columns)}) NULLS NOT DISTINCT",
            unique=True,
            required=True,
        )
    terminate_lock_holders(conn, _ROLLUP)
    set_lock_timeout(conn)
    op.execute(
        f"ALTER TABLE {_ROLLUP} DROP CONSTRAINT IF EXISTS {_GRAIN_CONSTRAINT}, "
        f"ADD CONSTRAINT {_GRAIN_CONSTRAINT} UNIQUE USING INDEX {index}"
    )


def upgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)

    if not inspector.has_table("departments"):
        op.execute(
            """
            CREATE TABLE IF NOT EXISTS departments (
                id UUID PRIMARY KEY,
                name VARCHAR(200) NOT NULL UNIQUE,
                description TEXT,
                directory_values JSON,
                created_at TIMESTAMPTZ,
                updated_at TIMESTAMPTZ
            )
            """
        )
    if not inspector.has_table("department_group_rules"):
        # Its foreign keys take a SHARE ROW EXCLUSIVE lock on `groups`.
        set_lock_timeout(conn)
        op.execute(
            """
            CREATE TABLE IF NOT EXISTS department_group_rules (
                id UUID PRIMARY KEY,
                department_id UUID NOT NULL REFERENCES departments(id) ON DELETE CASCADE,
                group_id UUID NOT NULL REFERENCES groups(id) ON DELETE CASCADE,
                priority INTEGER NOT NULL DEFAULT 0,
                created_at TIMESTAMPTZ,
                CONSTRAINT uq_department_group_rules_group UNIQUE (group_id)
            )
            """
        )
        op.execute(
            "CREATE INDEX IF NOT EXISTS ix_department_group_rules_department_id "
            "ON department_group_rules (department_id)"
        )
    if not inspector.has_table("trace_attribution_backfill"):
        op.execute(
            """
            CREATE TABLE IF NOT EXISTS trace_attribution_backfill (
                id SMALLINT PRIMARY KEY,
                cursor_date DATE,
                completed_at TIMESTAMPTZ,
                department_rewrite_before TIMESTAMPTZ,
                updated_at TIMESTAMPTZ
            )
            """
        )

    for table, columns in _COLUMNS.items():
        existing = _columns(conn, table)
        missing = [name for name in columns if name not in existing]
        if not missing:
            continue
        terminate_lock_holders(conn, table)
        set_lock_timeout(conn)
        additions = ", ".join(f"ADD COLUMN IF NOT EXISTS {name} {columns[name]}" for name in missing)
        op.execute(f"ALTER TABLE {table} {additions}")

    _move_grain(conn, _NEW_GRAIN, _NEW_GRAIN_INDEX)


_OLD_KEY = ", ".join(_OLD_GRAIN)
_SAME_OLD_KEY = " AND ".join(f"s.{column} IS NOT DISTINCT FROM m.{column}" for column in _OLD_GRAIN)
#: The rows the older grain would hold twice, merged into one each. Built from
#: module constants, never from input.
_MERGE_OLD_GRAIN_SQL = (
    f"""
    CREATE TEMP TABLE merged_rollup_6623 ON COMMIT DROP AS
    SELECT
        {_OLD_KEY}, sum(trace_count) AS trace_count, sum(error_count) AS error_count,
        sum(total_input_tokens) AS total_input_tokens, sum(total_output_tokens) AS total_output_tokens,
        sum(total_cost) AS total_cost,
        sum(avg_duration_ms * trace_count)
            / nullif(sum(trace_count) FILTER (WHERE avg_duration_ms IS NOT NULL), 0) AS avg_duration_ms,
        max(p50_duration_ms) AS p50_duration_ms, max(p95_duration_ms) AS p95_duration_ms,
        sum(avg_feedback_score * trace_count)
            / nullif(sum(trace_count) FILTER (WHERE avg_feedback_score IS NOT NULL), 0) AS avg_feedback_score,
        min(created_at) AS created_at
    FROM {_ROLLUP}
    GROUP BY {_OLD_KEY} HAVING count(*) > 1
    """,  # noqa: S608
    f"DELETE FROM {_ROLLUP} s USING merged_rollup_6623 m WHERE {_SAME_OLD_KEY}",  # noqa: S608
    f"""
    INSERT INTO {_ROLLUP} (
        id, {_OLD_KEY}, trace_count, error_count, total_input_tokens, total_output_tokens, total_cost,
        avg_duration_ms, p50_duration_ms, p95_duration_ms, avg_feedback_score, created_at
    )
    SELECT
        gen_random_uuid(), {_OLD_KEY}, trace_count, error_count, total_input_tokens, total_output_tokens,
        total_cost, avg_duration_ms, p50_duration_ms, p95_duration_ms, avg_feedback_score, created_at
    FROM merged_rollup_6623
    """,  # noqa: S608
)


def downgrade() -> None:
    conn = op.get_bind()

    if "attributed_user_id" in _columns(conn, _ROLLUP):
        set_lock_timeout(conn)
        for statement in _MERGE_OLD_GRAIN_SQL:
            op.execute(statement)
        _move_grain(conn, _OLD_GRAIN, _OLD_GRAIN_INDEX)

    for table, columns in reversed(_COLUMNS.items()):
        existing = _columns(conn, table)
        present = [name for name in columns if name in existing]
        if not present:
            continue
        terminate_lock_holders(conn, table)
        set_lock_timeout(conn)
        removals = ", ".join(f"DROP COLUMN IF EXISTS {name}" for name in present)
        op.execute(f"ALTER TABLE {table} {removals}")

    inspector = sa.inspect(conn)
    if inspector.has_table("trace_attribution_backfill"):
        op.execute("DROP TABLE IF EXISTS trace_attribution_backfill")
    if inspector.has_table("department_group_rules"):
        # ACCESS EXCLUSIVE on `groups` to drop the FK trigger.
        set_lock_timeout(conn)
        op.execute("DROP TABLE IF EXISTS department_group_rules")
    if inspector.has_table("departments"):
        op.execute("DROP TABLE IF EXISTS departments")
