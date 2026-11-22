"""decisions and decision_events: one store for every human gate (#842, S2)

``dev-docs/human-gates.md`` section 8 (G2): a ``decisions`` row per question a
person is asked (a run paused for input, a tool approval, a flow Ask-user park,
a work item waiting for a person) and an append-only ``decision_events``
audit. The engines keep their own columns (dual write); the backfill of the
pauses opened before this revision is S2b.

Lock-light: two new tables. Their foreign keys onto ``users`` install
triggers there under a SHARE ROW EXCLUSIVE lock, so the holders are cleared
first and a lock_timeout makes a racing one fail fast. Re-running after an
interruption is a no-op (the table is probed first). The downgrade drops both
tables; every engine still holds its own pause, so no open question is lost.

Revision ID: c942decisions01
Revises: c916fltcustgates1
Create Date: 2026-11-21 21:30:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from app.core.migration_ddl import set_lock_timeout, terminate_lock_holders

revision: str = "c942decisions01"
# NOT a constant: must always be main's CURRENT head, re-pointed whenever main
# gains a migration (backend/AGENTS.md > "Database migrations (safety)", rule 1).
down_revision: str | Sequence[str] | None = "c916fltcustgates1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_DECISIONS = "decisions"
_EVENTS = "decision_events"
_SOURCE_TYPES = ("run", "flow_run", "work_item")
_KINDS = ("hitl", "tool_approval", "flow_ask", "work_item")
_STATUSES = ("open", "answered", "expired", "cancelled")
_EVENT_NAMES = ("asked", "notified", "answered", "refused", "cancelled", "expired")


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


def _has_table(conn, name: str) -> bool:
    return bool(
        conn.execute(
            sa.text(
                "SELECT 1 FROM information_schema.tables WHERE table_schema = current_schema() AND table_name = :t"
            ),
            {"t": name},
        ).scalar()
    )


def _uuid() -> postgresql.UUID:
    return postgresql.UUID(as_uuid=True)


def upgrade() -> None:
    conn = op.get_bind()
    # Fresh installs run create_all + stamp head (this body never executes),
    # and a crashed replica may retry.
    if _has_table(conn, _EVENTS):
        return
    terminate_lock_holders(conn, "users")
    set_lock_timeout(conn)
    if not _has_table(conn, _DECISIONS):
        op.create_table(
            _DECISIONS,
            sa.Column("id", _uuid(), primary_key=True),
            sa.Column("source_type", sa.String(20), nullable=False),
            sa.Column("source_id", _uuid(), nullable=False),
            sa.Column("kind", sa.String(20), nullable=False),
            sa.Column("ask", sa.JSON(), nullable=False),
            sa.Column("recipient", sa.String(64), nullable=False),
            sa.Column("status", sa.String(20), nullable=False, server_default="open"),
            sa.Column("answer", sa.JSON(), nullable=True),
            sa.Column("answered_by", _uuid(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
            sa.Column("answered_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("surface", sa.String(40), nullable=True),
            sa.Column("deadline", sa.DateTime(timezone=True), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
            sa.CheckConstraint(_in("source_type", _SOURCE_TYPES), name="ck_decisions_source_type"),
            sa.CheckConstraint(_in("kind", _KINDS), name="ck_decisions_kind"),
            sa.CheckConstraint(_in("status", _STATUSES), name="ck_decisions_status"),
        )
        op.create_index(
            "uq_decisions_open_source",
            _DECISIONS,
            ["source_type", "source_id"],
            unique=True,
            postgresql_where=sa.text("status = 'open'"),
        )
        op.create_index("ix_decisions_recipient_status_deadline", _DECISIONS, ["recipient", "status", "deadline"])
    op.create_table(
        _EVENTS,
        sa.Column("id", _uuid(), primary_key=True),
        sa.Column("decision_id", _uuid(), sa.ForeignKey("decisions.id", ondelete="CASCADE"), nullable=False),
        sa.Column("event", sa.String(20), nullable=False),
        sa.Column("actor_id", _uuid(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("surface", sa.String(40), nullable=True),
        sa.Column("detail", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.CheckConstraint(_in("event", _EVENT_NAMES), name="ck_decision_events_event"),
    )
    op.create_index("ix_decision_events_decision_id", _EVENTS, ["decision_id"])


def downgrade() -> None:
    conn = op.get_bind()
    set_lock_timeout(conn)
    if _has_table(conn, _EVENTS):
        op.drop_table(_EVENTS)
    if _has_table(conn, _DECISIONS):
        op.drop_table(_DECISIONS)
