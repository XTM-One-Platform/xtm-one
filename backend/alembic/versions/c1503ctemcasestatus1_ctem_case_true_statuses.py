"""ctem_cases: true case statuses moved by the loop (#1503)

The first case model had two open states, ``needs_attention`` and
``in_progress``; a case paused on a person read "in progress", so the Cases
page landed on an empty "Needs attention". The open lifecycle is now ``new``,
``analyzing``, ``waiting_decision``, ``remediating``, ``validating`` (closed:
``mitigated_watching``, ``resolved``, ``accepted_risk`` as before).

Existing rows are split by what the case already records, newest fact first:
- its newest ``gate`` event is ``waited`` -> ``waiting_decision``;
- its newest validation is ``pending`` -> ``validating``;
- ``in_progress`` -> ``remediating``;
- ``needs_attention`` with a triage write (exploitation, attack path, threat
  package, gate) -> ``analyzing``, else ``new``.
History events keep the old names: they are history (the frontend labels them).

The check is widened to both vocabularies, the rows moved, then narrowed; the
downgrade maps back (``new``/``analyzing`` -> ``needs_attention``, the other
open statuses -> ``in_progress``). Lock-light: one UPDATE over the cases table,
small by design (one row per remediation).

Revision ID: c1503ctemcasestatus1
Revises: g1556grpmembers1
Create Date: 2026-11-23 12:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.core.migration_ddl import set_lock_timeout, terminate_lock_holders

revision: str = "c1503ctemcasestatus1"
# NOT a constant: must always be main's CURRENT head, re-pointed whenever main
# gains a migration (backend/AGENTS.md > "Database migrations (safety)", rule 1).
down_revision: str | Sequence[str] | None = "g1556grpmembers1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_CASES = "ctem_cases"
_STATE_CK = "ck_ctem_cases_state_valid"
_OLD = ("needs_attention", "in_progress", "mitigated_watching", "resolved", "accepted_risk")
_NEW = (
    "new",
    "analyzing",
    "waiting_decision",
    "remediating",
    "validating",
    "mitigated_watching",
    "resolved",
    "accepted_risk",
)


def _check(states: Sequence[str]) -> str:
    return "state IN (" + ", ".join(f"'{s}'" for s in dict.fromkeys(states)) + ")"


def _has_cases(conn: sa.Connection) -> bool:
    return bool(sa.inspect(conn).has_table(_CASES))


def _check_def(conn: sa.Connection) -> str | None:
    found = conn.execute(
        sa.text(
            "SELECT pg_get_constraintdef(c.oid) FROM pg_constraint c "
            "JOIN pg_class t ON t.oid = c.conrelid WHERE c.conname = :n AND t.relname = :t"
        ),
        {"n": _STATE_CK, "t": _CASES},
    ).scalar()
    return str(found) if found else None


def _replace_check(conn: sa.Connection, states: Sequence[str]) -> None:
    if _check_def(conn):
        op.drop_constraint(_STATE_CK, _CASES, type_="check")
    op.create_check_constraint(_STATE_CK, _CASES, _check(states))


#: The open cases whose newest gate event says they wait on a person.
_TO_WAITING = sa.text(
    "UPDATE ctem_cases SET state = 'waiting_decision' "
    "WHERE state IN ('needs_attention', 'in_progress') AND "
    "(SELECT e.to_value FROM ctem_case_events e WHERE e.case_id = ctem_cases.id AND e.kind = 'gate' "
    "ORDER BY e.at DESC LIMIT 1) = 'waited'"
)
#: The open cases whose newest validation reference is pending.
_TO_VALIDATING = sa.text(
    "UPDATE ctem_cases SET state = 'validating' "
    "WHERE state IN ('needs_attention', 'in_progress') AND "
    "json_typeof(validations) = 'array' AND json_array_length(validations) > 0 "
    "AND (validations -> (json_array_length(validations) - 1) ->> 'result') = 'pending'"
)
#: The cases a person or a ticket had already taken on.
_TO_REMEDIATING = sa.text("UPDATE ctem_cases SET state = 'remediating' WHERE state = 'in_progress'")
#: The cases a triage step already wrote on.
_TO_ANALYZING = sa.text(
    "UPDATE ctem_cases SET state = 'analyzing' WHERE state = 'needs_attention' AND "
    "EXISTS (SELECT 1 FROM ctem_case_events e WHERE e.case_id = ctem_cases.id "
    "AND e.kind IN ('exploitation', 'attack_path', 'threat_package', 'gate'))"
)
_TO_NEW = sa.text("UPDATE ctem_cases SET state = 'new' WHERE state = 'needs_attention'")


def upgrade() -> None:
    conn = op.get_bind()
    if not _has_cases(conn):  # fresh installs run create_all with the new check
        return
    check = _check_def(conn) or ""
    if "waiting_decision" in check and "needs_attention" not in check:
        return  # a crashed replica retried after the narrow step
    terminate_lock_holders(conn, _CASES)
    set_lock_timeout(conn)
    _replace_check(conn, (*_OLD, *_NEW))
    for statement in (_TO_WAITING, _TO_VALIDATING, _TO_REMEDIATING, _TO_ANALYZING, _TO_NEW):
        conn.execute(statement)
    op.alter_column(_CASES, "state", server_default="new")
    _replace_check(conn, _NEW)


def downgrade() -> None:
    conn = op.get_bind()
    if not _has_cases(conn):
        return
    terminate_lock_holders(conn, _CASES)
    set_lock_timeout(conn)
    _replace_check(conn, (*_OLD, *_NEW))
    conn.execute(sa.text("UPDATE ctem_cases SET state = 'needs_attention' WHERE state IN ('new', 'analyzing')"))
    conn.execute(
        sa.text(
            "UPDATE ctem_cases SET state = 'in_progress' WHERE state IN ('waiting_decision', 'remediating', 'validating')"
        )
    )
    op.alter_column(_CASES, "state", server_default="needs_attention")
    _replace_check(conn, _OLD)
