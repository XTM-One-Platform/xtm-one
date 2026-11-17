"""switch the seeded CTEM fleet assignments off, awaiting an administrator

The CTEM fleets F1 to F4 (#8975) seeded their 12 assignments enabled on the
dev and test instances that ran main between 5 and 6 Nov 2026. They are opt-in
since #9160 (``app.agents.fleet_opt_in``): the seed creates them disabled and
never touches their switch again, so the rows already enabled would stay on.
This one-time data migration switches them off with the opt-in stamp
(``created_disabled``, awaiting an administrator), exactly as a fresh seed
creates them. The trigger provisioning then removes the OpenCTI and OpenAEV
triggers and notifiers it created at its next tick (it keeps no trigger for a
disabled assignment; the seed keeps its state keys, #9160).

Only rows still enabled AND never resumed by a person are touched
(``paused_reason IS NULL``, no ``schedule_resumed_at``), keyed by the built-in
agent slug and the assignment name. No release carried these assignments, so
no customer instance holds them.

Lock profile: a row UPDATE on ``assignments``, no DDL. The downgrade puts back
the rows this upgrade switched off (still carrying its exact stamp, never
touched since), which is what the previous code's seed would do at its boot.

Revision ID: c9160fleetoptin1
Revises: k5351kbdocsrc01
Create Date: 2026-11-06 11:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "c9160fleetoptin1"
# NOT a constant: must always be main's CURRENT head, re-pointed whenever main
# gains a migration (backend/AGENTS.md > "Database migrations (safety)", rule 1).
down_revision: str | Sequence[str] | None = "k5351kbdocsrc01"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: (built-in agent slug, assignment name), frozen here: a migration never
#: imports the seed definitions, which move on.
_FLEET_ASSIGNMENTS: tuple[tuple[str, str], ...] = (
    ("ctem-threat-response-orchestrator", "New relevant threat"),
    ("ctem-threat-response-orchestrator", "Weekly threat sweep"),
    ("ctem-threat-response-orchestrator", "Re-test due"),
    ("ctem-vuln-prioritization-orchestrator", "New KEV or EPSS jump"),
    ("ctem-vuln-prioritization-orchestrator", "Vulnerability sighted"),
    ("ctem-vuln-prioritization-orchestrator", "Daily backlog pass"),
    ("ctem-vuln-prioritization-orchestrator", "Re-test due"),
    ("ctem-ai-security-orchestrator", "New AI-enabled report"),
    ("ctem-ai-security-orchestrator", "Weekly AI threat watch"),
    ("ctem-ai-security-orchestrator", "AI system registered"),
    ("ctem-posture-orchestrator", "Event refresh"),
    ("ctem-posture-orchestrator", "Monthly board report"),
)

_DETAILS = "Off until an administrator turns it on: choose the account it runs as, then resume it."
_LABEL = "XTM One — startup seeding"


def upgrade() -> None:
    conn = op.get_bind()
    stmt = sa.text(
        "UPDATE assignments AS a SET enabled = false, paused_at = now(), last_paused_at = now(), "
        "paused_reason = 'created_disabled', paused_by_user_id = NULL, "
        "paused_by_label = :label, paused_details = :details "
        "FROM agents AS g "
        "WHERE a.agent_id = g.id AND g.is_builtin = true AND g.slug = :slug AND a.name = :name "
        "AND a.enabled = true AND a.paused_reason IS NULL AND a.schedule_resumed_at IS NULL"
    )
    for slug, name in _FLEET_ASSIGNMENTS:
        conn.execute(stmt, {"slug": slug, "name": name, "label": _LABEL, "details": _DETAILS})


def downgrade() -> None:
    conn = op.get_bind()
    stmt = sa.text(
        "UPDATE assignments AS a SET enabled = true, paused_at = NULL, paused_reason = NULL, "
        "paused_by_user_id = NULL, paused_by_label = NULL, paused_details = NULL "
        "FROM agents AS g "
        "WHERE a.agent_id = g.id AND g.is_builtin = true AND g.slug = :slug AND a.name = :name "
        "AND a.enabled = false AND a.paused_reason = 'created_disabled' "
        "AND a.paused_details = :details AND a.schedule_resumed_at IS NULL"
    )
    for slug, name in _FLEET_ASSIGNMENTS:
        conn.execute(stmt, {"slug": slug, "name": name, "details": _DETAILS})
