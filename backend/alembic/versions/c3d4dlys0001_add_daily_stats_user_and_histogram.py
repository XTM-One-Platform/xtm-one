"""add llm_daily_stats.user_id + duration_histogram and re-key the unique constraint

The ``llm_daily_stats`` rollup outlives the raw-trace retention purge, so it is
what every observability KPI beyond 7 days is served from. Two gaps made it
disagree with the live path:

* No ``user_id``: the rollup could not express "this row is *your* general-chat
  traffic", so the read path had to drop every ``agent_id IS NULL`` row for
  non-admins and per-user cost was unanswerable past retention. The grouping key
  therefore gains ``user_id``, which means the UNIQUE constraint must gain it too
  - otherwise two groups differing only by user collide and the aggregator's
  ``ON CONFLICT DO NOTHING`` silently drops one.
* No mergeable latency state: ``p50``/``p95`` stored per group were recombined
  with a ``trace_count``-weighted mean, which is not a percentile.
  ``duration_histogram`` stores log-spaced bucket counts, which DO add (see
  ``app/services/tracing/latency_histogram.py``).

Both columns are nullable with no default, so the ``ADD COLUMN`` is metadata-only
(no table rewrite). Rows written before this migration keep ``NULL`` in both: the
read path treats a NULL ``user_id`` as "not attributable to a viewer" (exactly
the pre-migration behaviour, so no non-admin loses a row they could already see)
and falls back to the stored percentiles when ``duration_histogram`` is NULL.

The constraint swap is a DROP + ADD rather than an in-place change (PostgreSQL
has no ``ALTER CONSTRAINT`` for column sets). The replacement's index is built
in-place: ``llm_daily_stats`` holds one row per (day, agent, model, source)
group - thousands, not millions - so the build is milliseconds, and the
canonical lock recipe (probe -> terminate_lock_holders -> set_lock_timeout ->
guarded DDL) keeps a rolling deploy's readers from wedging the lock queue.

Because the constraint is RENAMED, a previous-version pod still serving during
the rolling deploy names the old constraint in its aggregator's ``ON CONFLICT``
and its next aggregation attempt fails. That is transient and harmless: the
aggregator logs the error, writes nothing, and the day is picked up by the next
tick (on a pod that has the new code). Nothing is lost - the purge refuses to
delete a day that has no rollup.

Revision ID: c3d4dlys0001
Revises: a4c8tou10001
Create Date: 2026-07-30 23:30:00.000000
"""
# ruff: noqa: S608 - the only value interpolated into any statement here is this
# module's own table-name constant; nothing in these bodies comes from input.

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.core.migration_ddl import set_lock_timeout, terminate_lock_holders

# revision identifiers, used by Alembic.
revision: str = "c3d4dlys0001"
down_revision: str | None = "a4c8tou10001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "llm_daily_stats"
_OLD_CONSTRAINT = "uq_llm_daily_stats_date_agent_model_source"
_NEW_CONSTRAINT = "uq_llm_daily_stats_date_agent_model_source_user"
_USER_INDEX = "ix_llm_daily_stats_user_id"


def _table_exists(conn, table: str) -> bool:
    # Scope the probe to the active schema - information_schema spans ALL
    # schemas, so a same-named table elsewhere must not drive the decision.
    return bool(
        conn.execute(
            sa.text(
                "SELECT 1 FROM information_schema.tables "
                "WHERE table_schema = current_schema() AND table_name = :table"
            ),
            {"table": table},
        ).scalar()
    )


def _column_exists(conn, table: str, column: str) -> bool:
    return bool(
        conn.execute(
            sa.text(
                "SELECT 1 FROM information_schema.columns "
                "WHERE table_schema = current_schema() "
                "  AND table_name = :table AND column_name = :column"
            ),
            {"table": table, "column": column},
        ).scalar()
    )


def _constraint_exists(conn, table: str, constraint: str) -> bool:
    return bool(
        conn.execute(
            sa.text(
                "SELECT 1 FROM pg_constraint c "
                "JOIN pg_class t ON t.oid = c.conrelid "
                "JOIN pg_namespace n ON n.oid = t.relnamespace "
                "WHERE n.nspname = current_schema() "
                "  AND t.relname = :table AND c.conname = :constraint"
            ),
            {"table": table, "constraint": constraint},
        ).scalar()
    )


def upgrade() -> None:
    conn = op.get_bind()
    # A fresh install ran create_all from the model and was stamped, so the table
    # already carries the final shape - nothing to do, and no lock taken.
    if not _table_exists(conn, _TABLE):
        return
    needs_columns = not _column_exists(conn, _TABLE, "user_id") or not _column_exists(
        conn, _TABLE, "duration_histogram"
    )
    needs_constraint = not _constraint_exists(conn, _TABLE, _NEW_CONSTRAINT)
    if not needs_columns and not needs_constraint:
        return

    terminate_lock_holders(conn, _TABLE)
    set_lock_timeout(conn)

    if needs_columns:
        op.execute(f"ALTER TABLE {_TABLE} ADD COLUMN IF NOT EXISTS user_id UUID")
        op.execute(f"ALTER TABLE {_TABLE} ADD COLUMN IF NOT EXISTS duration_histogram JSONB")
        op.execute(f"CREATE INDEX IF NOT EXISTS {_USER_INDEX} ON {_TABLE} (user_id)")

    if needs_constraint:
        # Order matters: the old constraint is a strict subset of the new key, so
        # dropping it first avoids holding two overlapping unique indexes, and
        # nothing can insert a now-legal duplicate in between (the aggregator
        # skips any date that already has a row, and the whole migration runs
        # under Alembic's single transaction).
        op.execute(f"ALTER TABLE {_TABLE} DROP CONSTRAINT IF EXISTS {_OLD_CONSTRAINT}")
        op.execute(
            f"ALTER TABLE {_TABLE} ADD CONSTRAINT {_NEW_CONSTRAINT} "
            "UNIQUE NULLS NOT DISTINCT (date, agent_id, model, source, user_id)"
        )


def downgrade() -> None:
    conn = op.get_bind()
    if not _table_exists(conn, _TABLE):
        return

    terminate_lock_holders(conn, _TABLE)
    set_lock_timeout(conn)

    # Restore the narrower key first. Rows written while this revision was live
    # can hold several users per (date, agent, model, source) group, which the
    # old constraint forbids - collapse them into one row per old-key group,
    # summing the additive counters, before re-adding it. Latency/feedback
    # averages are re-derived as trace_count-weighted means, which is exactly the
    # (approximate) shape the pre-revision read path expected.
    if not _constraint_exists(conn, _TABLE, _OLD_CONSTRAINT):
        op.execute(f"ALTER TABLE {_TABLE} DROP CONSTRAINT IF EXISTS {_NEW_CONSTRAINT}")
        op.execute(
            f"""
            WITH merged AS (
                SELECT
                    date, agent_id, model, source,
                    MIN(id::text)::uuid AS keep_id,
                    SUM(trace_count) AS trace_count,
                    SUM(error_count) AS error_count,
                    SUM(total_input_tokens) AS total_input_tokens,
                    SUM(total_output_tokens) AS total_output_tokens,
                    SUM(total_cost) AS total_cost,
                    SUM(avg_duration_ms * trace_count)
                        / NULLIF(SUM(trace_count) FILTER (WHERE avg_duration_ms IS NOT NULL), 0)
                        AS avg_duration_ms,
                    SUM(p50_duration_ms * trace_count)
                        / NULLIF(SUM(trace_count) FILTER (WHERE p50_duration_ms IS NOT NULL), 0)
                        AS p50_duration_ms,
                    SUM(p95_duration_ms * trace_count)
                        / NULLIF(SUM(trace_count) FILTER (WHERE p95_duration_ms IS NOT NULL), 0)
                        AS p95_duration_ms,
                    SUM(avg_feedback_score * trace_count)
                        / NULLIF(SUM(trace_count) FILTER (WHERE avg_feedback_score IS NOT NULL), 0)
                        AS avg_feedback_score
                FROM {_TABLE}
                GROUP BY date, agent_id, model, source
                HAVING COUNT(*) > 1
            )
            UPDATE {_TABLE} s
            SET trace_count = m.trace_count,
                error_count = m.error_count,
                total_input_tokens = m.total_input_tokens,
                total_output_tokens = m.total_output_tokens,
                total_cost = m.total_cost,
                avg_duration_ms = m.avg_duration_ms,
                p50_duration_ms = m.p50_duration_ms,
                p95_duration_ms = m.p95_duration_ms,
                avg_feedback_score = m.avg_feedback_score
            FROM merged m
            WHERE s.id = m.keep_id
            """
        )
        op.execute(
            f"""
            DELETE FROM {_TABLE} s
            USING (
                SELECT date, agent_id, model, source, MIN(id::text)::uuid AS keep_id
                FROM {_TABLE}
                GROUP BY date, agent_id, model, source
            ) k
            WHERE s.date = k.date
              AND s.agent_id IS NOT DISTINCT FROM k.agent_id
              AND s.model = k.model
              AND s.source = k.source
              AND s.id <> k.keep_id
            """
        )
        op.execute(
            f"ALTER TABLE {_TABLE} ADD CONSTRAINT {_OLD_CONSTRAINT} "
            "UNIQUE NULLS NOT DISTINCT (date, agent_id, model, source)"
        )

    op.execute(f"DROP INDEX IF EXISTS {_USER_INDEX}")
    op.execute(f"ALTER TABLE {_TABLE} DROP COLUMN IF EXISTS duration_histogram")
    op.execute(f"ALTER TABLE {_TABLE} DROP COLUMN IF EXISTS user_id")
