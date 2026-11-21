"""add ctem_signals

CTEM signals (#826): one intake for every trigger path of the CTEM loop. Purely
additive: one new table, no change to any existing column, so old replicas
during a rolling deploy ignore it. Nothing is backfilled. Design:
dev-docs/ctem-signals.md section 3.

Revision ID: c826ctemsignals1
Revises: h126healthanalysis01
Create Date: 2026-11-21 06:40:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from app.core.migration_ddl import set_lock_timeout, terminate_lock_holders

revision: str = "c826ctemsignals1"
# NOT a constant: must always be main's CURRENT head, re-pointed whenever main
# gains a migration (backend/AGENTS.md > "Database migrations (safety)", rule 1).
down_revision: str | Sequence[str] | None = "h126healthanalysis01"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_SIGNALS = "ctem_signals"

_PATHS = (
    "new_vulnerability",
    "relevant_threat",
    "code_flaw",
    "proven_exploitable",
    "new_exposure",
    "stale_asset",
    "surface_drift",
    "mitigation_regression",
    "control_decay",
    "credential_leak",
    "third_party_event",
    "blind_spot",
)
_LENSES = ("inside", "outside", "world")
_STATES = ("new", "confirmed", "to_validate", "discarded", "watchlist", "case_opened")


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


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
    if _has_table(conn, _SIGNALS):
        return

    # The foreign keys to `organizations` and `ctem_cases` install their
    # triggers there under a SHARE ROW EXCLUSIVE lock: clear the holders first,
    # then the lock_timeout belt makes a racing one fail fast.
    terminate_lock_holders(conn, "organizations")
    terminate_lock_holders(conn, "ctem_cases")
    set_lock_timeout(conn)
    op.create_table(
        _SIGNALS,
        sa.Column("id", _uuid(), primary_key=True),
        sa.Column("organization_id", _uuid(), sa.ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False),
        sa.Column("trigger_path", sa.String(30), nullable=False),
        sa.Column("lens", sa.String(10), nullable=False),
        sa.Column("source_provider", sa.String(40), nullable=False),
        sa.Column("source_external_id", sa.String(300), nullable=True),
        sa.Column("subject_kind", sa.String(20), nullable=False),
        sa.Column("subject_ref", sa.String(300), nullable=False),
        sa.Column("subject_label", sa.String(300), nullable=True),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("seen_count", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("evidence", sa.JSON(), nullable=False),
        sa.Column("dedupe_key", sa.String(200), nullable=False),
        sa.Column("state", sa.String(20), nullable=False, server_default="new"),
        sa.Column("decision", sa.JSON(), nullable=True),
        sa.Column("case_id", _uuid(), sa.ForeignKey("ctem_cases.id", ondelete="SET NULL"), nullable=True),
        sa.Column("recorded_via", sa.String(10), nullable=False),
        sa.Column("recorded_by", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True, server_default=sa.text("now()")),
        sa.UniqueConstraint("organization_id", "recorded_by", "dedupe_key", name="uq_ctem_signals_org_recorder_dedupe"),
        sa.CheckConstraint(_in("trigger_path", _PATHS), name="ck_ctem_signals_path_valid"),
        sa.CheckConstraint(_in("lens", _LENSES), name="ck_ctem_signals_lens_valid"),
        sa.CheckConstraint(_in("state", _STATES), name="ck_ctem_signals_state_valid"),
        sa.CheckConstraint(
            "state <> 'case_opened' OR case_id IS NOT NULL", name="ck_ctem_signals_case_opened_has_case"
        ),
        if_not_exists=True,
    )
    op.create_index("ix_ctem_signals_org_state", _SIGNALS, ["organization_id", "state"], if_not_exists=True)
    op.create_index("ix_ctem_signals_org_path", _SIGNALS, ["organization_id", "trigger_path"], if_not_exists=True)


def downgrade() -> None:
    conn = op.get_bind()
    # Dropping the table removes its foreign key triggers from `organizations`
    # and `ctem_cases` under an ACCESS EXCLUSIVE lock: the belt makes a busy
    # table fail fast.
    set_lock_timeout(conn)
    if _has_table(conn, _SIGNALS):
        op.drop_table(_SIGNALS, if_exists=True)
