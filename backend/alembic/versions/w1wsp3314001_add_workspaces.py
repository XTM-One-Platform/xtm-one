"""add the workspaces table

Purely additive: one new table, nothing referencing it yet, no backfill.

The absence of a backfill is a requirement, not an omission. Every user's
default workspace is created lazily on first use by
``services.workspaces.ensure_default_workspace``, which is race-safe against
concurrent replicas. Creating one row per existing user here would be an
unbounded UPDATE-shaped write inside the migration transaction on a table
whose size is the user count — banned by the lock-light rules — and it would
also create rows for users who never open the feature.

**Lock safety.** Nothing can hold a lock on a table that does not exist yet,
but the foreign key to ``users`` locks the REFERENCED table, so the DDL runs
under ``set_lock_timeout`` (``backend/AGENTS.md`` > "Database migrations
(safety)", ``a3rc4de0001``). The indexes are built on the brand-new, empty
table, which is why they are plain (non-``CONCURRENTLY``) builds:
``CONCURRENTLY`` cannot run inside a transaction and would buy nothing on zero
rows.

**Rolling-deploy safety.** No existing column changes type or nullability, so
replicas running the previous version simply never select a table they do not
know about. There is no prior state for them to misinterpret.

**The two partial uniques** are the point of this migration rather than
decoration. Both exclude ``user_id IS NULL`` explicitly. NULLs are distinct in
Postgres so an ownerless row could never collide regardless — writing the
predicate makes that gap visible instead of leaving a later reader to assume
ownerless rows are constrained.

Revision ID: w1wsp3314001
Revises: m4989mcat01
Create Date: 2026-09-04 10:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from app.core.migration_ddl import set_lock_timeout

revision: str = "w1wsp3314001"
# Chained behind whatever main last landed, re-pointed on every merge of main:
# a fork here is invisible until both sides merge.
down_revision: str | Sequence[str] | None = "m4989mcat01"
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


def upgrade() -> None:
    conn = op.get_bind()

    # Idempotency probe: a fresh install runs create_all + stamp head so this
    # body never executes, and the migrations CI job re-runs the revision
    # against an already-upgraded database. Both must be no-ops, not errors.
    if _has_table(conn, "workspaces"):
        return

    # `users` is locked by the FK below, not by the table being created:
    # `CREATE TABLE ... REFERENCES users(id)` installs its triggers on `users`
    # under a SHARE ROW EXCLUSIVE lock, which waits behind any open
    # transaction that WROTE to `users` and queues every later writer behind
    # it. The belt makes a contended attempt fail fast (55P03, retried by the
    # startup runner). No `terminate_lock_holders`: that lock does not
    # conflict with readers, and the pre-flight would kill every session that
    # merely read a user row.
    set_lock_timeout(conn)

    op.create_table(
        "workspaces",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("slug", sa.String(200), nullable=False),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        sa.Column(
            "icon", sa.String(50), nullable=False, server_default="LayoutGrid"
        ),
        sa.Column("color", sa.String(30), nullable=False, server_default=""),
        sa.Column(
            "is_default", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
        sa.Column(
            "is_archived", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
        # Three-tier visibility. Present in full: `user_visible_filter`
        # references shared_user_ids / access_restricted unconditionally on its
        # non-admin branch, so a missing column here is a 500 for every
        # ordinary user and an invisible success for admins.
        sa.Column(
            "is_company_managed",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
        sa.Column("shared_group_ids", postgresql.JSON(), nullable=True),
        sa.Column("shared_group_permissions", postgresql.JSON(), nullable=True),
        sa.Column("shared_user_ids", postgresql.JSON(), nullable=True),
        sa.Column("shared_user_permissions", postgresql.JSON(), nullable=True),
        sa.Column(
            "access_restricted",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
        # NOT NULL with a database default, matching the model's
        # `server_default`: `create_all` and this path must agree, which
        # `test_migrated_schema_matches_create_all_including_indexes` checks.
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )

    op.create_index("ix_workspaces_user_id", "workspaces", ["user_id"])
    op.create_index(
        "ix_workspaces_is_company_managed", "workspaces", ["is_company_managed"]
    )
    op.create_index("ix_workspaces_is_archived", "workspaces", ["is_archived"])

    # At most one default per owner: two concurrent first-use requests on
    # different replicas cannot both insert one. The loser gets an
    # IntegrityError and reads the winner's row.
    op.create_index(
        "uq_workspaces_default_per_user",
        "workspaces",
        ["user_id"],
        unique=True,
        postgresql_where=sa.text("is_default IS TRUE AND user_id IS NOT NULL"),
    )
    # The slug is the human-facing handle and is referenced by nothing
    # persisted, so it is unique per owner rather than globally.
    op.create_index(
        "uq_workspaces_slug_per_user",
        "workspaces",
        ["user_id", "slug"],
        unique=True,
        postgresql_where=sa.text("user_id IS NOT NULL"),
    )


def downgrade() -> None:
    conn = op.get_bind()
    # The indexes fall with the table; no separate drops needed. Dropping it
    # takes an ACCESS EXCLUSIVE lock on `users` to remove the FK triggers.
    if _has_table(conn, "workspaces"):
        set_lock_timeout(conn)
        op.drop_table("workspaces")
