"""ctem_cases: the loop fields (origin, severity, SLA, tickets, validations, proof)

CTEM cases opened from signals, updated on events, closed on proof (#857).
Adds nullable or defaulted columns to ``ctem_cases`` and widens its state
check with ``mitigated_watching``. Existing cases keep working: they read as
trigger path ``new_vulnerability`` with no origin signal (design:
dev-docs/ctem-cases.md section 9).

Lock-light: ``ADD COLUMN`` with a constant default is a catalog change on
PostgreSQL 11+, no rewrite; the check is only widened, so its validation scan
cannot fail.

Revision ID: c857ctemcaseloop1
Revises: c1crq2vertical1
Create Date: 2026-11-21 09:40:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.core.migration_ddl import set_lock_timeout, terminate_lock_holders

revision: str = "c857ctemcaseloop1"
# NOT a constant: must always be main's CURRENT head, re-pointed whenever main
# gains a migration (backend/AGENTS.md > "Database migrations (safety)", rule 1).
down_revision: str | Sequence[str] | None = "c1crq2vertical1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_CASES = "ctem_cases"
_STATE_CK = "ck_ctem_cases_state_valid"
_STATES_BEFORE = "state IN ('needs_attention', 'in_progress', 'resolved', 'accepted_risk')"
_STATES_AFTER = "state IN ('needs_attention', 'in_progress', 'mitigated_watching', 'resolved', 'accepted_risk')"

_COLUMNS: tuple[tuple[str, sa.types.TypeEngine, bool, str | None], ...] = (
    ("trigger_path", sa.String(30), False, "'new_vulnerability'"),
    ("risk_scenario_ids", sa.JSON(), False, "'[]'"),
    ("severity", sa.String(2), True, None),
    ("sla_due_at", sa.DateTime(timezone=True), True, None),
    ("sla_breached_at", sa.DateTime(timezone=True), True, None),
    ("tickets", sa.JSON(), False, "'[]'"),
    ("validations", sa.JSON(), False, "'[]'"),
    ("validation_plan", sa.Boolean(), False, "false"),
    ("resolution_kind", sa.String(20), True, None),
)


def _columns(conn) -> set[str]:
    rows = conn.execute(
        sa.text(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = current_schema() AND table_name = :t"
        ),
        {"t": _CASES},
    ).scalars()
    return set(rows)


def _check_def(conn) -> str | None:
    return conn.execute(
        sa.text(
            "SELECT pg_get_constraintdef(c.oid) FROM pg_constraint c "
            "JOIN pg_class t ON t.oid = c.conrelid WHERE c.conname = :n AND t.relname = :t"
        ),
        {"n": _STATE_CK, "t": _CASES},
    ).scalar()


def upgrade() -> None:
    conn = op.get_bind()
    present = _columns(conn)
    if not present:  # no ctem_cases table: nothing to extend (fresh installs run create_all)
        return
    missing = [c for c in _COLUMNS if c[0] not in present]
    check = _check_def(conn) or ""
    widen = "mitigated_watching" not in check
    # Idempotency probe: a crashed replica may retry, a fresh install never runs this.
    if not missing and not widen:
        return
    terminate_lock_holders(conn, _CASES)
    set_lock_timeout(conn)
    for name, type_, nullable, default in missing:
        op.add_column(
            _CASES,
            sa.Column(name, type_, nullable=nullable, server_default=sa.text(default) if default else None),
        )
    if widen:
        if check:
            op.drop_constraint(_STATE_CK, _CASES, type_="check")
        op.create_check_constraint(_STATE_CK, _CASES, _STATES_AFTER)


def downgrade() -> None:
    conn = op.get_bind()
    present = _columns(conn)
    if not present:
        return
    terminate_lock_holders(conn, _CASES)
    set_lock_timeout(conn)
    # Narrowing: a watched case reads as resolved under the old states.
    conn.execute(sa.text("UPDATE ctem_cases SET state = 'resolved' WHERE state = 'mitigated_watching'"))
    if _check_def(conn):
        op.drop_constraint(_STATE_CK, _CASES, type_="check")
    op.create_check_constraint(_STATE_CK, _CASES, _STATES_BEFORE)
    for name, *_ in reversed(_COLUMNS):
        if name in present:
            op.drop_column(_CASES, name)
