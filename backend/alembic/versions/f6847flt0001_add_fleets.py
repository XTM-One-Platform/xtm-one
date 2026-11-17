"""add the fleets and fleet_members tables

Purely additive: two new tables, nothing altered, no row written. On upgrade
every agent is in no fleet (the playground) and nothing anyone sees changes.

**Lock safety.** Nothing can hold a lock on a table that does not exist yet,
but the foreign keys lock the REFERENCED tables (``users``, ``workspaces``,
``agents``) under SHARE ROW EXCLUSIVE, so the DDL runs under
``set_lock_timeout`` (``backend/AGENTS.md`` > "Database migrations (safety)",
like ``w1wsp3314001``). No ``terminate_lock_holders``: that lock does not
conflict with readers, and the pre-flight would kill every session that merely
read an agent. The indexes are built on the brand-new, empty tables, so they
are plain in-transaction builds.

**Idempotent.** A fresh install runs ``create_all`` + ``stamp head`` and never
executes this body; the migrations job re-runs it on an upgraded database. The
probe returns early when ``fleets`` exists.

**The partial uniques are the point.** ``uq_fleet_members_home_agent`` is the
"one home fleet per agent" rule; ``uq_fleets_slug_per_user`` the per-owner
slug, written with its ``user_id IS NOT NULL`` predicate like the workspaces'.
The CHECK texts are the model's, character for character:
``tests/test_workspaces_postgres.py`` compares both install paths.

Revision ID: f6847flt0001
Revises: e6879wirun01
Create Date: 2026-10-31 03:45:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from app.core.migration_ddl import set_lock_timeout

revision: str = "f6847flt0001"
# NOT a constant: must always be main's CURRENT head, re-pointed whenever main
# gains a migration (backend/AGENTS.md > "Database migrations (safety)", rule 1).
down_revision: str | Sequence[str] | None = "e6879wirun01"
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


def _timestamps() -> list[sa.Column]:
    return [
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    ]


def upgrade() -> None:
    conn = op.get_bind()
    if _has_table(conn, "fleets"):
        return
    set_lock_timeout(conn)

    op.create_table(
        "fleets",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column(
            "workspace_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("workspaces.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("slug", sa.String(200), nullable=False),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        sa.Column("icon", sa.String(50), nullable=False, server_default="Network"),
        sa.Column("color", sa.String(30), nullable=False, server_default=""),
        sa.Column(
            "orchestrator_agent_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("agents.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("skills", postgresql.JSON(), nullable=True),
        sa.Column("accepted_handover_types", postgresql.JSON(), nullable=True),
        sa.Column("is_archived", sa.Boolean(), nullable=False, server_default=sa.false()),
        # Three-tier visibility, in full (`VisibilityColumns`).
        sa.Column("is_company_managed", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("shared_group_ids", postgresql.JSON(), nullable=True),
        sa.Column("shared_group_permissions", postgresql.JSON(), nullable=True),
        sa.Column("shared_user_ids", postgresql.JSON(), nullable=True),
        sa.Column("shared_user_permissions", postgresql.JSON(), nullable=True),
        sa.Column("access_restricted", sa.Boolean(), nullable=False, server_default=sa.false()),
        *_timestamps(),
    )
    op.create_index("ix_fleets_user_id", "fleets", ["user_id"])
    op.create_index("ix_fleets_workspace_id", "fleets", ["workspace_id"])
    op.create_index("ix_fleets_orchestrator_agent_id", "fleets", ["orchestrator_agent_id"])
    op.create_index("ix_fleets_is_company_managed", "fleets", ["is_company_managed"])
    op.create_index(
        "uq_fleets_slug_per_user",
        "fleets",
        ["user_id", "slug"],
        unique=True,
        postgresql_where=sa.text("user_id IS NOT NULL"),
    )

    op.create_table(
        "fleet_members",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "fleet_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("fleets.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "agent_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("agents.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column(
            "target_fleet_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("fleets.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column("role", sa.String(20), nullable=False, server_default="member"),
        sa.Column(
            "parent_member_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("fleet_members.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("is_exposed", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("collaboration_instruction", sa.Text(), nullable=False, server_default=""),
        sa.Column("context_inclusion", sa.String(10), nullable=False, server_default="none"),
        *_timestamps(),
        sa.CheckConstraint(
            "(role = 'member' AND agent_id IS NOT NULL AND target_fleet_id IS NULL)"
            " OR (role = 'connected' AND parent_member_id IS NULL"
            " AND (agent_id IS NULL) <> (target_fleet_id IS NULL))",
            name="ck_fleet_members_role_shape",
        ),
        sa.CheckConstraint(
            "target_fleet_id IS NULL OR target_fleet_id <> fleet_id",
            name="ck_fleet_members_not_self",
        ),
        sa.CheckConstraint(
            "context_inclusion IN ('none', 'summary', 'full')",
            name="ck_fleet_members_context_inclusion",
        ),
    )
    op.create_index("ix_fleet_members_fleet_id", "fleet_members", ["fleet_id"])
    op.create_index("ix_fleet_members_agent_id", "fleet_members", ["agent_id"])
    op.create_index("ix_fleet_members_target_fleet_id", "fleet_members", ["target_fleet_id"])
    op.create_index("ix_fleet_members_parent_member_id", "fleet_members", ["parent_member_id"])
    op.create_index(
        "uq_fleet_members_home_agent",
        "fleet_members",
        ["agent_id"],
        unique=True,
        postgresql_where=sa.text("role = 'member'"),
    )
    op.create_index(
        "uq_fleet_members_fleet_agent",
        "fleet_members",
        ["fleet_id", "agent_id"],
        unique=True,
        postgresql_where=sa.text("agent_id IS NOT NULL"),
    )
    op.create_index(
        "uq_fleet_members_fleet_target",
        "fleet_members",
        ["fleet_id", "target_fleet_id"],
        unique=True,
        postgresql_where=sa.text("target_fleet_id IS NOT NULL"),
    )


def downgrade() -> None:
    conn = op.get_bind()
    # The indexes and constraints fall with their tables. Dropping a table
    # takes an ACCESS EXCLUSIVE lock on the tables its foreign keys reference.
    set_lock_timeout(conn)
    if _has_table(conn, "fleet_members"):
        op.drop_table("fleet_members")
    if _has_table(conn, "fleets"):
        op.drop_table("fleets")
