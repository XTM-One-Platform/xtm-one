"""Platform Health analysis layer: trace source, analyses and the spend ceiling ledger

Slice S9 of issue #101 (design ``dev-docs/platform-health.md`` section 6):

* ``llm_traces.source`` accepts ``platform_health``: the check constraint is
  dropped and re-added ``NOT VALID`` with the new value in the migration
  transaction, then validated by ``VALIDATE CONSTRAINT`` in its own transaction
  (``autocommit_block``, after the swap has committed and released its lock), so
  the table scan runs under SHARE UPDATE EXCLUSIVE alone and blocks neither
  reads nor writes: no constraint is left unvalidated after an upgrade;
* ``health_module_analyses``: the latest analysis per module;
* ``health_analysis_spend``: the analysis ceiling ledger, one row per day and month.

Nothing is backfilled. Lock profile: the constraint swap takes an ACCESS
EXCLUSIVE lock on ``llm_traces`` under ``set_lock_timeout`` after
``terminate_lock_holders``, held only until the migration transaction commits
(catalog changes and two empty tables, no scan); the validation scan runs after
that commit. The two new tables carry no foreign key. Re-running the body after
an interruption is safe: the swap is guarded, and ``VALIDATE CONSTRAINT`` on a
validated constraint is a no-op. The column sets mirror ``app/models/health_analysis.py``, so a
fresh install's ``create_all`` and this body agree.

Revision ID: h126healthanalysis01
Revises: h125healthsnap01
Create Date: 2026-11-20 13:30:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.core.migration_ddl import set_lock_timeout, terminate_lock_holders

revision: str = "h126healthanalysis01"
# NOT a constant: must always be main's CURRENT head, re-pointed on every rebase
# (backend/AGENTS.md > "Database migrations (safety)", rule 1).
down_revision: str | Sequence[str] | None = "h125healthsnap01"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TRACES = "llm_traces"
_CONSTRAINT = "ck_llm_traces_source_valid"
_ANALYSES = "health_module_analyses"
_SPEND = "health_analysis_spend"

_NEW_CHECK = (
    "source IN ('agent-chat', 'general-chat', 'assignment', 'flow', 'memory', 'evaluation', 'proxy', 'platform_health')"
)
_OLD_CHECK = "source IN ('agent-chat', 'general-chat', 'assignment', 'flow', 'memory', 'evaluation', 'proxy')"


def _has_table(conn: sa.engine.Connection, table: str) -> bool:
    return bool(
        conn.execute(
            sa.text(
                "SELECT 1 FROM information_schema.tables WHERE table_schema = current_schema() AND table_name = :n"
            ),
            {"n": table},
        ).scalar()
    )


def _swap_check(conn: sa.engine.Connection, check_sql: str) -> None:
    """Drop and re-add the check ``NOT VALID``: catalog only, no table scan."""
    terminate_lock_holders(conn, _TRACES)
    set_lock_timeout(conn)
    # Literal DDL from module constants, never user input (ruff S608).
    op.execute(f"ALTER TABLE {_TRACES} DROP CONSTRAINT IF EXISTS {_CONSTRAINT}")
    op.execute(f"ALTER TABLE {_TRACES} ADD CONSTRAINT {_CONSTRAINT} CHECK ({check_sql}) NOT VALID")


def _validate_check() -> None:
    """Validate the check in its own transaction, after the swap committed.

    ``autocommit_block`` commits the migration transaction first, which releases
    the ACCESS EXCLUSIVE lock the swap took; the scan then holds SHARE UPDATE
    EXCLUSIVE alone, so trace reads and writes of the serving replicas go on
    while it runs. Bounded by the Alembic connection's statement timeout.
    """
    with op.get_context().autocommit_block():
        op.execute(f"ALTER TABLE {_TRACES} VALIDATE CONSTRAINT {_CONSTRAINT}")


def upgrade() -> None:
    conn = op.get_bind()
    has_traces = _has_table(conn, _TRACES)
    if has_traces:
        _swap_check(conn, _NEW_CHECK)
    set_lock_timeout(conn)
    op.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {_ANALYSES} (
            module VARCHAR(16) PRIMARY KEY,
            finding_hash VARCHAR(64) NOT NULL,
            status VARCHAR(16) NOT NULL,
            reason VARCHAR(64),
            summary TEXT NOT NULL DEFAULT '',
            model VARCHAR(200),
            tokens BIGINT NOT NULL DEFAULT 0,
            cost_usd NUMERIC(16, 6) NOT NULL DEFAULT 0,
            trigger VARCHAR(16) NOT NULL DEFAULT 'schedule',
            analysed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT ck_health_module_analyses_status_valid
                CHECK (status IN ('completed', 'refused', 'failed'))
        )
        """
    )
    op.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {_SPEND} (
            period VARCHAR(12) PRIMARY KEY,
            reserved_tokens BIGINT NOT NULL DEFAULT 0,
            reserved_cost_usd NUMERIC(16, 6) NOT NULL DEFAULT 0,
            spent_tokens BIGINT NOT NULL DEFAULT 0,
            spent_cost_usd NUMERIC(16, 6) NOT NULL DEFAULT 0,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    if has_traces:
        # Last: every stored row satisfies the superset; validated outside the
        # transaction that holds the swap's lock.
        _validate_check()


def downgrade() -> None:
    conn = op.get_bind()
    set_lock_timeout(conn)
    op.execute(f"DROP TABLE IF EXISTS {_SPEND}")
    op.execute(f"DROP TABLE IF EXISTS {_ANALYSES}")
    if _has_table(conn, _TRACES):
        # NOT VALID: the platform_health rows already stored are kept (traces are
        # never deleted by a downgrade); only new rows are checked.
        _swap_check(conn, _OLD_CHECK)
