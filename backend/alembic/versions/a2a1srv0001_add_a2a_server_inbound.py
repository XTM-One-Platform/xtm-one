"""add A2A inbound server tables (a2a_exposures, a2a_tasks)

Purely additive: two new tables, one nullable FK on ``mcp_oauth_clients``,
and three nullable Entra-federation columns on ``a2a_exposures``.

Nothing is backfilled, and that is a requirement rather than an omission. The
specification states that on a fresh deployment *and on an upgrade of an
existing one*, zero agents are externally reachable until an explicit opt-in is
recorded. An upgrade therefore ends with ``a2a_exposures`` empty; any backfill
that "helpfully" exposed existing agents would silently put them on the
internet.

Rolling-deploy safety: no existing column changes type or nullability, so old
replicas simply ignore two tables they do not know about and a column they
never select. There is no prior state to misinterpret.

``mcp_oauth_clients.a2a_exposure_id`` is nullable because every pre-existing
client self-registered through RFC 7591 DCR and has no exposure - a NOT NULL
column would break all of them.

Revision ID: a2a1srv0001
Revises: g2h5flwshr1
Create Date: 2026-08-18 12:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from app.core.migration_ddl import set_lock_timeout, terminate_lock_holders

revision: str = "a2a1srv0001"
# This revision was born as the merge point of main's then-forked heads
# (f9u2fav0001 + g1novasrc00). Main has since merged its own fork and moved
# on, so this now chains onto main's tip like any ordinary revision — a
# rebase of this branch must re-point it at whatever head main carries.
down_revision: str | Sequence[str] | None = "g2h5flwshr1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _has_table(conn, name: str) -> bool:
    return bool(
        conn.execute(
            sa.text(
                "SELECT 1 FROM information_schema.tables "
                "WHERE table_schema = current_schema() AND table_name = :n"
            ),
            {"n": name},
        ).scalar()
    )


def _has_column(conn, table: str, column: str) -> bool:
    return bool(
        conn.execute(
            sa.text(
                "SELECT 1 FROM information_schema.columns "
                "WHERE table_schema = current_schema() "
                "AND table_name = :t AND column_name = :c"
            ),
            {"t": table, "c": column},
        ).scalar()
    )


def upgrade() -> None:
    conn = op.get_bind()

    # Idempotency probe: fresh installs run create_all + stamp head, and the
    # migrations CI job re-runs this revision against an already-upgraded
    # database. Both must be no-ops rather than errors.
    if not _has_table(conn, "a2a_exposures"):
        op.create_table(
            "a2a_exposures",
            sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
            sa.Column(
                "agent_id",
                postgresql.UUID(as_uuid=True),
                sa.ForeignKey("agents.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("card_token", sa.String(64), nullable=False),
            # The identity external work runs as, mirroring
            # Assignment.run_as_user_id. NOT NULL: an external task has no
            # invoking human to fall back to, so it is resolved once at
            # exposure time and never inferred later.
            sa.Column(
                "run_as_user_id",
                postgresql.UUID(as_uuid=True),
                sa.ForeignKey("users.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("allowed_tools", postgresql.JSON(), nullable=True),
            sa.Column(
                "exposed_by_user_id",
                postgresql.UUID(as_uuid=True),
                sa.ForeignKey("users.id", ondelete="SET NULL"),
                nullable=True,
            ),
            sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("revoked_reason", sa.String(40), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        )
        op.create_index(
            "ix_a2a_exposures_agent_id", "a2a_exposures", ["agent_id"]
        )
        op.create_index(
            "ix_a2a_exposures_run_as_user_id", "a2a_exposures", ["run_as_user_id"]
        )
        op.create_index(
            "ix_a2a_exposures_card_token", "a2a_exposures", ["card_token"], unique=True
        )
        # Partial unique index: at most one LIVE exposure per agent. Enforced
        # here rather than in application code so two concurrent grants on
        # different replicas cannot both win.
        op.create_index(
            "uq_a2a_exposures_live_agent",
            "a2a_exposures",
            ["agent_id"],
            unique=True,
            postgresql_where=sa.text("revoked_at IS NULL"),
        )

    if not _has_table(conn, "a2a_tasks"):
        op.create_table(
            "a2a_tasks",
            sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
            sa.Column("context_id", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column(
                "exposure_id",
                postgresql.UUID(as_uuid=True),
                sa.ForeignKey("a2a_exposures.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("client_id", sa.String(255), nullable=False),
            sa.Column(
                "run_id",
                postgresql.UUID(as_uuid=True),
                sa.ForeignKey("assignment_runs.id", ondelete="SET NULL"),
                nullable=True,
            ),
            sa.Column("message_id", sa.String(128), nullable=False),
            sa.Column("inbound_parts", postgresql.JSON(), nullable=True),
            sa.Column("rejected_reason", sa.String(40), nullable=True),
            sa.Column(
                "cancel_requested_at", sa.DateTime(timezone=True), nullable=True
            ),
            sa.Column(
                "lease_heartbeat_at", sa.DateTime(timezone=True), nullable=True
            ),
            sa.Column(
                "reaped", sa.Boolean(), nullable=False, server_default=sa.false()
            ),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        )
        op.create_index("ix_a2a_tasks_context_id", "a2a_tasks", ["context_id"])
        op.create_index("ix_a2a_tasks_exposure_id", "a2a_tasks", ["exposure_id"])
        op.create_index("ix_a2a_tasks_client_id", "a2a_tasks", ["client_id"])
        op.create_index("ix_a2a_tasks_run_id", "a2a_tasks", ["run_id"])
        op.create_index(
            "ix_a2a_tasks_lease_heartbeat_at", "a2a_tasks", ["lease_heartbeat_at"]
        )
        # Durable deduplication guarantee. Redis is the fast path and may be
        # lost; this constraint is what actually prevents a partner retry from
        # starting a second billable run.
        #
        # PARTIAL, excluding rejected rows: a submission refused for bad
        # content must not consume its message id, or the partner's corrected
        # re-send would collide with the rejection it is correcting.
        op.create_index(
            "uq_a2a_tasks_client_message",
            "a2a_tasks",
            ["client_id", "message_id"],
            unique=True,
            postgresql_where=sa.text("rejected_reason IS NULL"),
        )
        op.create_index(
            "ix_a2a_tasks_client_id_task", "a2a_tasks", ["client_id", "id"]
        )

    # Entra federation columns, added while this feature branch was still
    # unmerged — guarded separately from the CREATE TABLE above so a dev
    # database that ran an earlier cut of this revision converges by
    # re-running it, exactly like the CI idempotency re-run.
    for column in (
        sa.Column("entra_tenant_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("entra_client_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("entra_audience", sa.String(200), nullable=True),
    ):
        if not _has_column(conn, "a2a_exposures", column.name):
            terminate_lock_holders(conn, "a2a_exposures")
            set_lock_timeout(conn)
            op.add_column("a2a_exposures", column)

    if not _has_column(conn, "mcp_oauth_clients", "a2a_exposure_id"):
        # ``mcp_oauth_clients`` is an EXISTING, live table, so this ALTER
        # takes ACCESS EXCLUSIVE and can queue behind a long-running reader.
        # The canonical recipe (backend/AGENTS.md > "Database migrations
        # (safety)") exists because exactly this gap took production down on
        # 2026-07-05: kill same-role lock holders so the lock is acquired
        # instantly, then bound the wait so a migration can never hang the
        # table indefinitely. The two CREATE TABLEs above need none of this -
        # nothing can hold a lock on a table that does not exist yet.
        terminate_lock_holders(conn, "mcp_oauth_clients")
        set_lock_timeout(conn)
        op.add_column(
            "mcp_oauth_clients",
            sa.Column(
                "a2a_exposure_id",
                postgresql.UUID(as_uuid=True),
                sa.ForeignKey("a2a_exposures.id", ondelete="CASCADE"),
                nullable=True,
            ),
        )
        op.create_index(
            "ix_mcp_oauth_clients_a2a_exposure_id",
            "mcp_oauth_clients",
            ["a2a_exposure_id"],
        )


def downgrade() -> None:
    conn = op.get_bind()

    # Drop in reverse dependency order: the client FK references
    # a2a_exposures, and a2a_tasks references it too.
    if _has_column(conn, "mcp_oauth_clients", "a2a_exposure_id"):
        terminate_lock_holders(conn, "mcp_oauth_clients")
        set_lock_timeout(conn)
        op.drop_index(
            "ix_mcp_oauth_clients_a2a_exposure_id", table_name="mcp_oauth_clients"
        )
        op.drop_column("mcp_oauth_clients", "a2a_exposure_id")

    if _has_table(conn, "a2a_tasks"):
        op.drop_table("a2a_tasks")

    if _has_table(conn, "a2a_exposures"):
        # The Entra columns fall with the table; no separate drop needed.
        op.drop_table("a2a_exposures")
