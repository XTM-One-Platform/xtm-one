"""add investigation tables (investigations, investigation_steps)

Purely additive: two new tables, no change to any existing column's type or
nullability, so old replicas during a rolling deploy simply ignore two tables
they do not know about. Nothing is backfilled — there is no prior state that
could be interpreted as an investigation run.

Both `CREATE TABLE`s run under `set_lock_timeout`: their foreign keys lock
the REFERENCED tables (`users`, `conversations`, `assignment_runs`), so a
brand-new table is not exempt (backend/AGENTS.md). The indexes are built
non-concurrently: nothing else knows a table created in this same transaction,
so nothing can queue behind the builds, and `CREATE INDEX CONCURRENTLY` cannot
run inside the transaction that created the table.

The v1 column set lands in ONE migration, including the parts whose behaviour
is not implemented yet (`lead_from_step_id`, `iteration`, `assignment_run_id`),
on the same reasoning as the workset migration: adding a column later is cheap,
but changing what a stored value MEANS once values exist is not. In particular
`confidence` is created NULLABLE WITH NO SERVER DEFAULT and must stay that way
— the requirement is that a confidence which cannot be derived from
findings is absent rather than defaulted, and a `server_default` here would
silently turn "unknown" into a number an analyst could act on.

Revision ID: i1inv3302001
Revises: w4wsp3314004
Create Date: 2026-09-03 10:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from app.core.migration_ddl import set_lock_timeout

revision: str = "i1inv3302001"
# Chained behind the workspace stack, not beside it: one line, one
# head, and main only ever has to be merged into the stack's root.
down_revision: str | Sequence[str] | None = "w4wsp3314004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _has_table(conn, name: str) -> bool:
    return bool(
        conn.execute(
            sa.text(
                "SELECT 1 FROM information_schema.tables WHERE table_schema = current_schema() AND table_name = :n"
            ),
            {"n": name},
        ).scalar()
    )


def upgrade() -> None:
    conn = op.get_bind()

    # Idempotency probe: fresh installs run create_all + stamp head (this body
    # never executes), and the migrations CI job re-runs the revision against
    # an already-upgraded database. Both must be no-ops rather than errors.
    if not _has_table(conn, "investigations"):
        # `CREATE TABLE ... REFERENCES x(id)` takes a SHARE ROW EXCLUSIVE lock
        # on `users`, `conversations` and `assignment_runs`, which waits behind
        # any open transaction that wrote to them: the belt bounds that wait
        # (the startup runner retries a fired one). No pre-flight kill - that
        # lock does not conflict with readers (backend/AGENTS.md).
        set_lock_timeout(conn)

        op.create_table(
            "investigations",
            sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
            sa.Column(
                "user_id",
                postgresql.UUID(as_uuid=True),
                sa.ForeignKey("users.id", ondelete="SET NULL"),
                nullable=True,
            ),
            sa.Column(
                "conversation_id",
                postgresql.UUID(as_uuid=True),
                sa.ForeignKey("conversations.id", ondelete="SET NULL"),
                nullable=True,
            ),
            sa.Column(
                "assignment_run_id",
                postgresql.UUID(as_uuid=True),
                sa.ForeignKey("assignment_runs.id", ondelete="SET NULL"),
                nullable=True,
            ),
            sa.Column("subject_type", sa.String(40), nullable=False),
            sa.Column("subject_value", sa.String(500), nullable=False),
            sa.Column(
                "source_pack_slugs",
                postgresql.JSON(astext_type=sa.Text()),
                nullable=False,
                server_default=sa.text("'[]'::json"),
            ),
            sa.Column(
                "status",
                sa.String(20),
                nullable=False,
                server_default="planning",
            ),
            sa.Column("end_reason", sa.String(200), nullable=True),
            sa.Column(
                "max_iterations",
                sa.Integer(),
                nullable=False,
                server_default=sa.text("10"),
            ),
            sa.Column(
                "iterations_used",
                sa.Integer(),
                nullable=False,
                server_default=sa.text("0"),
            ),
            sa.Column("revision", sa.Integer(), nullable=False, server_default=sa.text("1")),
            sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
            # NOT NULL with a server default, matching what `create_all`
            # emits for the model's non-Optional `Mapped[datetime]` — a
            # nullable column here drifted from the fresh-install schema and
            # from the list endpoints that order by `created_at`. The default
            # also covers any insert that bypasses the ORM's Python default.
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.text("now()"),
            ),
            sa.Column(
                "updated_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.text("now()"),
            ),
        )
        op.create_index("ix_investigations_user_id", "investigations", ["user_id"])
        op.create_index(
            "ix_investigations_conversation_id",
            "investigations",
            ["conversation_id"],
        )
        op.create_index(
            "ix_investigations_assignment_run_id",
            "investigations",
            ["assignment_run_id"],
        )
        op.create_index("ix_investigations_created_at", "investigations", ["created_at"])

    if not _has_table(conn, "investigation_steps"):
        # `investigations` is created above in this same migration, so on a
        # fresh run nothing else can hold it - but this block is reached on
        # its own when a crash landed between the two creates, and then the
        # table is live. Belt it for that path.
        set_lock_timeout(conn)

        op.create_table(
            "investigation_steps",
            sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
            sa.Column(
                "investigation_id",
                postgresql.UUID(as_uuid=True),
                sa.ForeignKey("investigations.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("position", sa.Integer(), nullable=False),
            sa.Column("source_pack_slug", sa.String(100), nullable=True),
            sa.Column("source_name", sa.String(200), nullable=False),
            sa.Column("query", sa.Text(), nullable=True),
            sa.Column("status", sa.String(20), nullable=False, server_default="pending"),
            sa.Column("error_type", sa.String(40), nullable=True),
            sa.Column("status_detail", sa.String(300), nullable=True),
            sa.Column(
                "findings_count",
                sa.Integer(),
                nullable=False,
                server_default=sa.text("0"),
            ),
            # No server_default, nullable: see the module docstring.
            sa.Column("confidence", sa.Float(), nullable=True),
            sa.Column(
                "evidence_urls",
                postgresql.JSON(astext_type=sa.Text()),
                nullable=False,
                server_default=sa.text("'[]'::json"),
            ),
            sa.Column("iteration", sa.Integer(), nullable=True),
            sa.Column(
                "lead_from_step_id",
                postgresql.UUID(as_uuid=True),
                sa.ForeignKey("investigation_steps.id", ondelete="SET NULL"),
                nullable=True,
            ),
            sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
            # NOT NULL with a server default, matching what `create_all`
            # emits for the model's non-Optional `Mapped[datetime]` — a
            # nullable column here drifted from the fresh-install schema and
            # from the list endpoints that order by `created_at`. The default
            # also covers any insert that bypasses the ORM's Python default.
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.text("now()"),
            ),
            sa.Column(
                "updated_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.text("now()"),
            ),
        )
        op.create_index(
            "ix_investigation_steps_run_position",
            "investigation_steps",
            ["investigation_id", "position"],
        )


def downgrade() -> None:
    conn = op.get_bind()

    # Dropping a table with foreign keys takes an ACCESS EXCLUSIVE lock on the
    # referenced tables, so the drops run under the belt too.
    set_lock_timeout(conn)

    # Child first: `investigation_steps.investigation_id` is a CASCADE FK onto
    # `investigations`, so dropping the parent first would fail. Dropping the
    # table drops its indexes, including any the revision once created.
    if _has_table(conn, "investigation_steps"):
        op.drop_table("investigation_steps")

    if _has_table(conn, "investigations"):
        op.drop_index("ix_investigations_created_at", table_name="investigations")
        op.drop_index("ix_investigations_assignment_run_id", table_name="investigations")
        op.drop_index("ix_investigations_conversation_id", table_name="investigations")
        op.drop_index("ix_investigations_user_id", table_name="investigations")
        op.drop_table("investigations")
