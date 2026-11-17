"""per-tool HITL approval: whitelist overrides, standing approvals, HITL default flip

Adds the schema behind per-tool human-in-the-loop approval:

* ``hitl_whitelist_overrides`` — the administrator overlay on top of the
  shipped default whitelist. The whitelist itself is NOT stored: effective
  membership is computed as ``(shipped_default - removals) | additions`` at read
  time, so there is deliberately no list content to migrate here.
* ``tool_approval_preferences`` — per-user standing "Yes, always" approvals.
* ``assignment_runs.pending_approvals`` / ``approval_decisions`` — structured
  proposals and the reviewer's decisions across a pause.
* ``integrations.requires_approval`` — one approval toggle per integration.
* ``agents.human_in_the_loop`` — default flipped FALSE -> TRUE, **and every
  existing agent backfilled**.

The backfill is the consequential part. Flipping the default alone would only
affect newly created agents: the column is NOT NULL with FALSE already stored on
every existing row, so the deployed fleet would stay ungated indefinitely. This
is an AI compliance requirement confirmed by the product owner, not a nicer
default, which is why it applies to existing rows.

``HITL_BACKFILL_OPT_OUT`` (env, read at migration time) skips the backfill and
preserves each agent's current value. Setting it is a deliberate, auditable
decision to defer that compliance requirement — it is not a routine upgrade
flag, and it has no effect on agents created after the upgrade.

Assignment-level overrides are deliberately untouched: ``assignments.
human_in_the_loop`` is nullable tri-state, and an explicit FALSE there is an
operator decision about one assignment rather than an unset default.

Idempotent: guarded by existence probes and a ``WHERE human_in_the_loop = false``
predicate, so re-running converges without redoing work. Follows the canonical
lock-safe recipe: idempotency probe -> terminate_lock_holders -> set_lock_timeout
-> guarded DDL.

Revision ID: a1h2itl0173
Revises: s1jit3265001
Create Date: 2026-08-13 12:00:00.000000
"""

import os
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.core.migration_ddl import set_lock_timeout, terminate_lock_holders

# revision identifiers, used by Alembic.
revision: str = "a1h2itl0173"
down_revision: str | None = "s1jit3265001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_OVERRIDES = "hitl_whitelist_overrides"
_PREFERENCES = "tool_approval_preferences"

# Read at migration time, never at runtime. This governs the one-time backfill
# only and must not become a runtime override of the HITL switch — runtime
# gating stays governed solely by the agent/assignment setting.
_OPT_OUT_ENV = "HITL_BACKFILL_OPT_OUT"


def _column_exists(conn, table: str, column: str) -> bool:
    return (
        conn.execute(
            sa.text(
                "SELECT 1 FROM information_schema.columns "
                "WHERE table_schema = current_schema() "
                "AND table_name = :table AND column_name = :column"
            ),
            {"table": table, "column": column},
        ).scalar()
        is not None
    )


def _backfill_opted_out() -> bool:
    return os.getenv(_OPT_OUT_ENV, "").strip().lower() in {"1", "true", "yes", "on"}


def upgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)

    # --- New tables ------------------------------------------------------
    if not inspector.has_table(_OVERRIDES):
        op.create_table(
            _OVERRIDES,
            sa.Column("id", sa.dialects.postgresql.UUID(as_uuid=True), primary_key=True),
            # UNIQUE: one decision per tool, so re-adding a removed tool flips
            # the row instead of leaving an ambiguous pair.
            sa.Column("tool_key", sa.String(255), nullable=False, unique=True),
            sa.Column("action", sa.String(10), nullable=False),
            sa.Column(
                "created_by_id",
                sa.dialects.postgresql.UUID(as_uuid=True),
                sa.ForeignKey("users.id", ondelete="SET NULL"),
                nullable=True,
            ),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.text("now()"),
            ),
            sa.Column("reason", sa.Text(), nullable=True),
        )

    if not inspector.has_table(_PREFERENCES):
        op.create_table(
            _PREFERENCES,
            sa.Column("id", sa.dialects.postgresql.UUID(as_uuid=True), primary_key=True),
            sa.Column(
                "user_id",
                sa.dialects.postgresql.UUID(as_uuid=True),
                sa.ForeignKey("users.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("tool_key", sa.String(255), nullable=False),
            sa.Column("tool_source", sa.String(100), nullable=False),
            sa.Column("schema_fingerprint", sa.String(64), nullable=False),
            sa.Column(
                "granted_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.text("now()"),
            ),
            sa.Column(
                "granted_by_admin_id",
                sa.dialects.postgresql.UUID(as_uuid=True),
                sa.ForeignKey("users.id", ondelete="SET NULL"),
                nullable=True,
            ),
            sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("revoked_reason", sa.String(30), nullable=True),
        )
        op.create_index(
            "ix_tool_approval_preferences_user_id", _PREFERENCES, ["user_id"]
        )
        op.create_index(
            "ix_tool_approval_preferences_user_active",
            _PREFERENCES,
            ["user_id", "revoked_at"],
        )
        # Partial: revoked rows accumulate as history without blocking a fresh
        # grant, and two concurrent "Yes, always" clicks converge via upsert.
        op.create_index(
            "uq_tool_approval_preferences_active",
            _PREFERENCES,
            ["user_id", "tool_key"],
            unique=True,
            postgresql_where=sa.text("revoked_at IS NULL"),
        )

    # --- New columns -----------------------------------------------------
    if inspector.has_table("assignment_runs"):
        if not _column_exists(conn, "assignment_runs", "pending_approvals"):
            terminate_lock_holders(conn, "assignment_runs")
            set_lock_timeout(conn)
            op.execute(
                "ALTER TABLE assignment_runs "
                "ADD COLUMN IF NOT EXISTS pending_approvals JSON"
            )
        # Persists whether a human started the run. ``user_id`` cannot answer
        # that — it always resolves to someone (trigger user, run-as, or owner),
        # so only a dedicated column distinguishes a manual run from a schedule.
        if not _column_exists(conn, "assignment_runs", "triggered_by_user_id"):
            terminate_lock_holders(conn, "assignment_runs")
            set_lock_timeout(conn)
            op.execute(
                "ALTER TABLE assignment_runs ADD COLUMN IF NOT EXISTS "
                "triggered_by_user_id UUID REFERENCES users(id) ON DELETE SET NULL"
            )
        if not _column_exists(conn, "assignment_runs", "approval_decisions"):
            terminate_lock_holders(conn, "assignment_runs")
            set_lock_timeout(conn)
            op.execute(
                "ALTER TABLE assignment_runs "
                "ADD COLUMN IF NOT EXISTS approval_decisions JSON"
            )

    if inspector.has_table("integrations") and not _column_exists(
        conn, "integrations", "requires_approval"
    ):
        terminate_lock_holders(conn, "integrations")
        set_lock_timeout(conn)
        op.execute(
            "ALTER TABLE integrations ADD COLUMN IF NOT EXISTS "
            "requires_approval BOOLEAN NOT NULL DEFAULT TRUE"
        )

    # --- HITL default flip + fleet backfill -------------------------------
    if not inspector.has_table("agents"):
        return

    terminate_lock_holders(conn, "agents")
    set_lock_timeout(conn)
    op.execute("ALTER TABLE agents ALTER COLUMN human_in_the_loop SET DEFAULT TRUE")

    if _backfill_opted_out():
        # Deliberate deferral: leave every existing agent exactly as it is. New
        # agents still gate, because the column default above already changed.
        return

    # Idempotent by predicate — a re-run touches nothing once converged, and an
    # interrupted run resumes cleanly. Only agent-level values move;
    # assignments.human_in_the_loop is nullable tri-state and an explicit FALSE
    # there is an operator decision this migration must not overwrite.
    op.execute("UPDATE agents SET human_in_the_loop = TRUE WHERE human_in_the_loop = false")


def downgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)

    if inspector.has_table("agents"):
        terminate_lock_holders(conn, "agents")
        set_lock_timeout(conn)
        op.execute(
            "ALTER TABLE agents ALTER COLUMN human_in_the_loop SET DEFAULT FALSE"
        )
        # Deliberately NOT reverting row values. Downgrade restores the old
        # default for new agents; it does not un-gate agents that a human may
        # since have relied on being gated. Flipping thousands of rows back to
        # FALSE would be a silent, unlogged loosening of a compliance posture.

    if inspector.has_table("integrations") and _column_exists(
        conn, "integrations", "requires_approval"
    ):
        terminate_lock_holders(conn, "integrations")
        set_lock_timeout(conn)
        op.execute("ALTER TABLE integrations DROP COLUMN IF EXISTS requires_approval")

    if inspector.has_table("assignment_runs"):
        terminate_lock_holders(conn, "assignment_runs")
        set_lock_timeout(conn)
        op.execute(
            "ALTER TABLE assignment_runs DROP COLUMN IF EXISTS approval_decisions"
        )
        op.execute(
            "ALTER TABLE assignment_runs DROP COLUMN IF EXISTS pending_approvals"
        )
        op.execute(
            "ALTER TABLE assignment_runs DROP COLUMN IF EXISTS triggered_by_user_id"
        )

    if inspector.has_table(_PREFERENCES):
        op.drop_table(_PREFERENCES)
    if inspector.has_table(_OVERRIDES):
        op.drop_table(_OVERRIDES)
