"""re-land the per-tool HITL approval schema on top of the revert

``d4und0173`` cancelled the whole approval schema after the feature was reverted.
This branch brings the feature back, so the schema has to come back too
— and it cannot come back by editing the three original migrations: every
platform that upgraded through main is already stamped past them, so their
bodies will never run again. Hence a forward migration that re-creates exactly
what ``d4und0173`` dropped.

Fresh installs never run this body (``create_all`` builds the shapes from the
models, then Alembic stamps head). It matters only for a deployment coming from
a main that had already applied the revert.

No agent row is touched (maintainer decision, 2026-09-27): the per-tool gate
has its own switch, added off on an upgrade by ``j1atc0173``.
``integrations.requires_approval`` comes back NOT NULL DEFAULT TRUE; it only
matters once an agent's gate is on.

A platform stamped before the revert release runs ``d4und0173`` and this body in
one startup, so ``pending_approvals`` is dropped and re-created empty under any
run that was paused at the gate. Those rows cannot be answered any more — the
proposals they carried are gone — so they are failed here rather than left
waiting forever on a decision no endpoint will accept.

Idempotent: every create is probed, every DDL is ``IF EXISTS`` / ``IF NOT
EXISTS``. Follows the canonical lock-safe
recipe: idempotency probe -> terminate_lock_holders -> set_lock_timeout ->
guarded DDL.

Revision ID: e5rel3298
Revises: a4996mcsrc01
Create Date: 2026-09-03 09:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.core.migration_ddl import set_lock_timeout, terminate_lock_holders

# revision identifiers, used by Alembic.
revision: str = "e5rel3298"
# Re-parented onto main's head each time main is merged in:
# both were cut from the same branchpoint, leaving two heads. Chained rather than
# joined with a merge revision because the Migrations job walks the history with
# ``upgrade +1``, which is undefined at a branch point — the lesson recorded in
# ``f7b3mrg0001``. Safe to re-parent: this chain is unreleased, so no deployment
# is stamped inside it.
down_revision: str | Sequence[str] | None = "s5013fstate01"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_OVERRIDES = "hitl_whitelist_overrides"
_PREFERENCES = "tool_approval_preferences"
_PLATFORM_REG = "platform_registrations"

_ASSIGNMENT_RUN_COLUMNS = (
    "approval_decisions",
    "pending_approvals",
    "triggered_by_user_id",
)
_PLATFORM_REG_COLUMNS = (
    "approval_support_seen_at",
    "approval_policy_set_by",
    "approval_policy",
    "supports_approval_prompts",
)

def _column_exists(conn, table: str, column: str) -> bool:
    return (
        conn.execute(
            sa.text(
                "SELECT 1 FROM information_schema.columns "
                "WHERE table_schema = current_schema() "
                "AND table_name = :table AND column_name = :column"
            ),
            {"table": table, "column": column},
        ).first()
        is not None
    )


def upgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)

    # --- Feature tables ----------------------------------------------------
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
        op.create_index("ix_tool_approval_preferences_user_id", _PREFERENCES, ["user_id"])
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

    # --- assignment_runs columns -------------------------------------------
    if inspector.has_table("assignment_runs"):
        missing = [c for c in _ASSIGNMENT_RUN_COLUMNS if not _column_exists(conn, "assignment_runs", c)]
        if missing:
            terminate_lock_holders(conn, "assignment_runs")
            set_lock_timeout(conn)
            if "pending_approvals" in missing:
                op.execute("ALTER TABLE assignment_runs ADD COLUMN IF NOT EXISTS pending_approvals JSON")
                # The gate is the only writer of this question prefix, which is
                # what separates a stranded gated pause from a classic
                # ``request_human_input`` wait — the latter is still answerable
                # with free text and must not be touched.
                op.execute(
                    """
                    UPDATE assignment_runs
                       SET status = 'failed',
                           error = 'Approval request lost to the schema revert; this run can no longer be answered.',
                           failure_cause = 'engine_abandoned',
                           failure_determination = 'structural',
                           completed_at = COALESCE(completed_at, now())
                     WHERE status = 'awaiting_human'
                       AND pending_approvals IS NULL
                       AND human_input_question LIKE 'Approval required before running:%'
                    """
                )
            if "approval_decisions" in missing:
                op.execute("ALTER TABLE assignment_runs ADD COLUMN IF NOT EXISTS approval_decisions JSON")
            if "triggered_by_user_id" in missing:
                op.execute(
                    "ALTER TABLE assignment_runs ADD COLUMN IF NOT EXISTS "
                    "triggered_by_user_id UUID REFERENCES users(id) ON DELETE SET NULL"
                )

    # --- integrations.requires_approval ------------------------------------
    if inspector.has_table("integrations") and not _column_exists(conn, "integrations", "requires_approval"):
        terminate_lock_holders(conn, "integrations")
        set_lock_timeout(conn)
        op.execute("ALTER TABLE integrations ADD COLUMN IF NOT EXISTS requires_approval BOOLEAN NOT NULL DEFAULT TRUE")

    # --- platform_registrations approval columns ---------------------------
    # A deployment that never connected a Suite platform may not have the table.
    if inspector.has_table(_PLATFORM_REG):
        missing = [c for c in _PLATFORM_REG_COLUMNS if not _column_exists(conn, _PLATFORM_REG, c)]
        if missing:
            terminate_lock_holders(conn, _PLATFORM_REG)
            set_lock_timeout(conn)
            if "supports_approval_prompts" in missing:
                op.execute(f"ALTER TABLE {_PLATFORM_REG} ADD COLUMN IF NOT EXISTS supports_approval_prompts BOOLEAN")
            if "approval_policy" in missing:
                op.execute(
                    f"ALTER TABLE {_PLATFORM_REG} ADD COLUMN IF NOT EXISTS "
                    "approval_policy VARCHAR(20) NOT NULL DEFAULT 'require'"
                )
            if "approval_policy_set_by" in missing:
                op.execute(
                    f"ALTER TABLE {_PLATFORM_REG} ADD COLUMN IF NOT EXISTS "
                    "approval_policy_set_by VARCHAR(20) NOT NULL DEFAULT 'default'"
                )
            if "approval_support_seen_at" in missing:
                op.execute(f"ALTER TABLE {_PLATFORM_REG} ADD COLUMN IF NOT EXISTS approval_support_seen_at TIMESTAMPTZ")


def downgrade() -> None:
    """Cancel the re-land again — mirrors ``d4und0173.upgrade``."""
    conn = op.get_bind()
    inspector = sa.inspect(conn)

    # ``DROP TABLE`` takes ACCESS EXCLUSIVE like any other lock-taking DDL, so
    # it gets the canonical preflight too. Without it a rolling rollback queues
    # the drop behind an old replica's open transaction on either table, and
    # every later reader queues behind the drop.
    for table in (_PREFERENCES, _OVERRIDES):
        if not inspector.has_table(table):
            continue
        terminate_lock_holders(conn, table)
        set_lock_timeout(conn)
        op.execute(f"DROP TABLE IF EXISTS {table}")

    if inspector.has_table("assignment_runs"):
        stale = [c for c in _ASSIGNMENT_RUN_COLUMNS if _column_exists(conn, "assignment_runs", c)]
        if stale:
            terminate_lock_holders(conn, "assignment_runs")
            set_lock_timeout(conn)
            for column in stale:
                op.execute(f"ALTER TABLE assignment_runs DROP COLUMN IF EXISTS {column}")

    if inspector.has_table("integrations") and _column_exists(conn, "integrations", "requires_approval"):
        terminate_lock_holders(conn, "integrations")
        set_lock_timeout(conn)
        op.execute("ALTER TABLE integrations DROP COLUMN IF EXISTS requires_approval")

    if inspector.has_table(_PLATFORM_REG):
        stale = [c for c in _PLATFORM_REG_COLUMNS if _column_exists(conn, _PLATFORM_REG, c)]
        if stale:
            terminate_lock_holders(conn, _PLATFORM_REG)
            set_lock_timeout(conn)
            for column in stale:
                op.execute(f"ALTER TABLE {_PLATFORM_REG} DROP COLUMN IF EXISTS {column}")
