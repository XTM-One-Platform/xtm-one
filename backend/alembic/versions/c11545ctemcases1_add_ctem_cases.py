"""add ctem_cases, ctem_case_issues, ctem_case_events

CTEM Cases (#11545): open exposures grouped by the remediation that closes
them, with a state, an owner, a due date and a history. Purely additive: three
new tables, no change to any existing column, so old replicas during a rolling
deploy ignore them. Nothing is backfilled: cases are built by the first
recompute. Design: dev-docs/ctem-cases.md section 5.

Revision ID: c11545ctemcases1
Revises: orgctx11533
Create Date: 2026-11-13 10:45:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from app.core.migration_ddl import set_lock_timeout, terminate_lock_holders

revision: str = "c11545ctemcases1"
# NOT a constant: must always be main's CURRENT head, re-pointed whenever main
# gains a migration (backend/AGENTS.md > "Database migrations (safety)", rule 1).
down_revision: str | Sequence[str] | None = "orgctx11533"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_CASES = "ctem_cases"
_ISSUES = "ctem_case_issues"
_EVENTS = "ctem_case_events"


def _has_table(conn, name: str) -> bool:
    return bool(
        conn.execute(
            sa.text(
                "SELECT 1 FROM information_schema.tables WHERE table_schema = current_schema() AND table_name = :n"
            ),
            {"n": name},
        ).scalar()
    )


def _uuid() -> postgresql.UUID:
    return postgresql.UUID(as_uuid=True)


def upgrade() -> None:
    conn = op.get_bind()

    # Idempotency probe: fresh installs run create_all + stamp head (this body
    # never executes), and a crashed replica may retry.
    if _has_table(conn, _EVENTS):
        return

    # The foreign keys to `organizations` and `users` install their triggers
    # there under a SHARE ROW EXCLUSIVE lock: clear the holders first, then the
    # lock_timeout belt makes a racing one fail fast (retried by the runner).
    terminate_lock_holders(conn, "organizations")
    terminate_lock_holders(conn, "users")
    set_lock_timeout(conn)
    op.create_table(
        _CASES,
        sa.Column("id", _uuid(), primary_key=True),
        sa.Column(
            "organization_id",
            _uuid(),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("vertical", sa.String(20), nullable=False, server_default="cyber"),
        sa.Column("title", sa.String(300), nullable=False),
        sa.Column("remediation_kind", sa.String(20), nullable=False),
        sa.Column("product_label", sa.String(200), nullable=True),
        sa.Column("fixed_version", sa.String(100), nullable=True),
        sa.Column("keys", sa.JSON(), nullable=False),
        sa.Column("pinned_issues", sa.JSON(), nullable=False),
        sa.Column("state", sa.String(20), nullable=False, server_default="needs_attention"),
        sa.Column("state_since", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("owner_user_id", _uuid(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("due_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("accepted_reason", sa.String(1000), nullable=True),
        sa.Column("accepted_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("priority", sa.String(10), nullable=False, server_default="low"),
        sa.Column("priority_basis", sa.String(200), nullable=False, server_default="default policy"),
        sa.Column("issue_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("asset_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("crown_jewel_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("kev_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("max_cvss", sa.Float(), nullable=True),
        sa.Column("max_epss", sa.Float(), nullable=True),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True, server_default=sa.text("now()")),
        sa.CheckConstraint(
            "state IN ('needs_attention', 'in_progress', 'resolved', 'accepted_risk')",
            name="ck_ctem_cases_state_valid",
        ),
        sa.CheckConstraint(
            "state <> 'accepted_risk' OR (owner_user_id IS NOT NULL"
            " AND accepted_reason IS NOT NULL AND accepted_until IS NOT NULL)",
            name="ck_ctem_cases_accepted_risk_complete",
        ),
        if_not_exists=True,
    )
    op.create_index(
        "ix_ctem_cases_org_vertical_state",
        _CASES,
        ["organization_id", "vertical", "state"],
        if_not_exists=True,
    )
    op.create_table(
        _ISSUES,
        sa.Column("id", _uuid(), primary_key=True),
        sa.Column("case_id", _uuid(), sa.ForeignKey("ctem_cases.id", ondelete="CASCADE"), nullable=False),
        sa.Column("source", sa.String(10), nullable=False),
        sa.Column("external_id", sa.String(200), nullable=False),
        sa.Column("name", sa.String(300), nullable=False),
        sa.Column("cvss", sa.Float(), nullable=True),
        sa.Column("epss", sa.Float(), nullable=True),
        sa.Column("kev", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("assets", sa.JSON(), nullable=False),
        sa.Column("crown_jewels", sa.JSON(), nullable=False),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("open", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.UniqueConstraint("case_id", "source", "external_id", name="uq_ctem_case_issues_member"),
        if_not_exists=True,
    )
    op.create_index(op.f("ix_ctem_case_issues_case_id"), _ISSUES, ["case_id"], if_not_exists=True)
    op.create_table(
        _EVENTS,
        sa.Column("id", _uuid(), primary_key=True),
        sa.Column("case_id", _uuid(), sa.ForeignKey("ctem_cases.id", ondelete="CASCADE"), nullable=False),
        sa.Column("at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("actor", sa.String(64), nullable=False),
        sa.Column("kind", sa.String(20), nullable=False),
        sa.Column("from_value", sa.String(300), nullable=True),
        sa.Column("to_value", sa.String(300), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("evidence", sa.Text(), nullable=True),
        if_not_exists=True,
    )
    op.create_index(op.f("ix_ctem_case_events_case_id"), _EVENTS, ["case_id"], if_not_exists=True)


def downgrade() -> None:
    conn = op.get_bind()
    # Dropping the tables removes their foreign key triggers from `users` and
    # `organizations` under an ACCESS EXCLUSIVE lock: the belt makes a busy
    # table fail fast.
    set_lock_timeout(conn)
    for table in (_EVENTS, _ISSUES, _CASES):
        if _has_table(conn, table):
            op.drop_table(table, if_exists=True)
