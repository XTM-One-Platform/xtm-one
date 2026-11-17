"""remove per-tool HITL approval schema after the feature revert

The per-tool human-in-the-loop approval feature was
fully reverted. The revert removed the application code AND deleted
the three migration files that had shipped with it:

* ``a1h2itl0173`` — approval tables, run/integration columns, and the fleet
  backfill that flipped ``agents.human_in_the_loop`` to TRUE everywhere.
* ``b2p1lat0173`` — the three ``platform_registrations`` policy columns.
* ``c3q2obs0173`` — ``platform_registrations.approval_support_seen_at``.

Deleting migration files does not undo migrations: every platform deployed
from main while the feature was in is stamped at ``c3q2obs0173`` (Alembic
cannot even locate that revision without the files, so startup fails) and
still carries the whole schema — including a fleet of agents force-flipped to
``human_in_the_loop = TRUE``, which the reverted code now interprets as the
older pause-for-review behaviour on every assignment run.

The three files are therefore restored verbatim (history must stay intact),
and THIS migration cancels everything they did:

* drops ``tool_approval_preferences`` and ``hitl_whitelist_overrides``;
* drops ``assignment_runs.pending_approvals`` / ``approval_decisions`` /
  ``triggered_by_user_id`` (the run-as `triggered_by_user_id` kwarg in the
  engine is an in-memory parameter and never persisted this column);
* drops ``integrations.requires_approval``;
* drops the four ``platform_registrations`` approval columns;
* restores the ``agents.human_in_the_loop`` default to FALSE and resets every
  row to FALSE.

The row reset is deliberate and lossy in one narrow case: an agent whose
owner had switched HITL on BEFORE the feature shipped is reset along with the fleet,
because the feature's backfill flipped every row to TRUE and left nothing to tell
a deliberate TRUE from a backfilled one. Restoring the known-good fleet
posture (everything ungated, as before the feature) is the point of this migration;
operators who want an agent gated re-enable it explicitly.

Fresh installs never run this body (``create_all`` + ``stamp head`` builds
the clean schema directly). A platform upgrading straight from a build
from before the feature runs the three restored migrations and then this one in the same
serialized startup sequence, converging on the clean schema before the app
serves traffic.

Idempotent: every drop is probed / ``IF EXISTS`` and the row reset is
predicated, so re-running converges without redoing work. Follows the
canonical lock-safe recipe: idempotency probe -> terminate_lock_holders ->
set_lock_timeout -> guarded DDL.

Revision ID: d4und0173
Revises: c3q2obs0173
Create Date: 2026-09-02 10:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.core.migration_ddl import set_lock_timeout, terminate_lock_holders

# revision identifiers, used by Alembic.
revision: str = "d4und0173"
down_revision: str | None = "c3q2obs0173"
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
    # Dropping a table only locks that table; no pre-flight needed beyond the
    # existence probe (nothing else in the app touches them post-revert).
    if inspector.has_table(_PREFERENCES):
        op.execute(f"DROP TABLE IF EXISTS {_PREFERENCES}")
    if inspector.has_table(_OVERRIDES):
        op.execute(f"DROP TABLE IF EXISTS {_OVERRIDES}")

    # --- assignment_runs columns --------------------------------------------
    if inspector.has_table("assignment_runs"):
        stale = [
            c
            for c in _ASSIGNMENT_RUN_COLUMNS
            if _column_exists(conn, "assignment_runs", c)
        ]
        if stale:
            terminate_lock_holders(conn, "assignment_runs")
            set_lock_timeout(conn)
            for column in stale:
                op.execute(
                    f"ALTER TABLE assignment_runs DROP COLUMN IF EXISTS {column}"
                )

    # --- integrations.requires_approval --------------------------------------
    if inspector.has_table("integrations") and _column_exists(
        conn, "integrations", "requires_approval"
    ):
        terminate_lock_holders(conn, "integrations")
        set_lock_timeout(conn)
        op.execute("ALTER TABLE integrations DROP COLUMN IF EXISTS requires_approval")

    # --- platform_registrations approval columns ------------------------------
    if inspector.has_table(_PLATFORM_REG):
        stale = [
            c for c in _PLATFORM_REG_COLUMNS if _column_exists(conn, _PLATFORM_REG, c)
        ]
        if stale:
            terminate_lock_holders(conn, _PLATFORM_REG)
            set_lock_timeout(conn)
            for column in stale:
                op.execute(
                    f"ALTER TABLE {_PLATFORM_REG} DROP COLUMN IF EXISTS {column}"
                )

    # --- agents.human_in_the_loop: default back to FALSE + fleet reset --------
    if not inspector.has_table("agents"):
        return

    terminate_lock_holders(conn, "agents")
    set_lock_timeout(conn)
    op.execute("ALTER TABLE agents ALTER COLUMN human_in_the_loop SET DEFAULT FALSE")
    # Idempotent by predicate — see the module docstring for why this resets
    # every row rather than trying to tell deliberate TRUEs from backfilled ones.
    op.execute(
        "UPDATE agents SET human_in_the_loop = FALSE WHERE human_in_the_loop = true"
    )


def downgrade() -> None:
    """Recreate the approval schema shapes (structure only, no backfills)."""
    conn = op.get_bind()
    inspector = sa.inspect(conn)

    if inspector.has_table("agents"):
        terminate_lock_holders(conn, "agents")
        set_lock_timeout(conn)
        op.execute(
            "ALTER TABLE agents ALTER COLUMN human_in_the_loop SET DEFAULT TRUE"
        )

    if inspector.has_table(_PLATFORM_REG):
        terminate_lock_holders(conn, _PLATFORM_REG)
        set_lock_timeout(conn)
        op.execute(
            f"ALTER TABLE {_PLATFORM_REG} ADD COLUMN IF NOT EXISTS "
            "supports_approval_prompts BOOLEAN"
        )
        op.execute(
            f"ALTER TABLE {_PLATFORM_REG} ADD COLUMN IF NOT EXISTS "
            "approval_policy VARCHAR(20) NOT NULL DEFAULT 'require'"
        )
        op.execute(
            f"ALTER TABLE {_PLATFORM_REG} ADD COLUMN IF NOT EXISTS "
            "approval_policy_set_by VARCHAR(20) NOT NULL DEFAULT 'default'"
        )
        op.execute(
            f"ALTER TABLE {_PLATFORM_REG} ADD COLUMN IF NOT EXISTS "
            "approval_support_seen_at TIMESTAMPTZ"
        )

    if inspector.has_table("integrations"):
        terminate_lock_holders(conn, "integrations")
        set_lock_timeout(conn)
        op.execute(
            "ALTER TABLE integrations ADD COLUMN IF NOT EXISTS "
            "requires_approval BOOLEAN NOT NULL DEFAULT TRUE"
        )

    if inspector.has_table("assignment_runs"):
        terminate_lock_holders(conn, "assignment_runs")
        set_lock_timeout(conn)
        op.execute(
            "ALTER TABLE assignment_runs ADD COLUMN IF NOT EXISTS "
            "pending_approvals JSON"
        )
        op.execute(
            "ALTER TABLE assignment_runs ADD COLUMN IF NOT EXISTS "
            "approval_decisions JSON"
        )
        op.execute(
            "ALTER TABLE assignment_runs ADD COLUMN IF NOT EXISTS "
            "triggered_by_user_id UUID REFERENCES users(id) ON DELETE SET NULL"
        )

    if not inspector.has_table(_OVERRIDES):
        op.create_table(
            _OVERRIDES,
            sa.Column("id", sa.dialects.postgresql.UUID(as_uuid=True), primary_key=True),
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
        op.create_index(
            "uq_tool_approval_preferences_active",
            _PREFERENCES,
            ["user_id", "tool_key"],
            unique=True,
            postgresql_where=sa.text("revoked_at IS NULL"),
        )
