"""ctem cases store no source content: shared workflow, details read per viewer

CTEM Cases (#11545) stored the issue names, CVEs, scores, KEV flags, assets,
crown jewels, priority and counts read with one administrator's credentials,
and every user read them (#11613). A case now stores its fix and its shared
workflow only; the member issues keep their source id and open flag; the
details are read at display time with the viewer's own credentials.

The rows written before this revision were built from one user's view: they
are deleted (the cases shipped on 13 Nov 2026; the next recompute rebuilds
them). ``DROP COLUMN`` and a nullable ``ADD COLUMN`` with no default are
metadata only.

Revision ID: ctemscope11613
Revises: orgnotice11586
Create Date: 2026-11-13 13:55:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.core.migration_ddl import set_lock_timeout, terminate_lock_holders

# revision identifiers, used by Alembic.
revision: str = "ctemscope11613"
# NOT a constant: must always be main's CURRENT head, re-pointed on every rebase
# (backend/AGENTS.md > "Database migrations (safety)", rule 1).
down_revision: str | None = "orgnotice11586"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_CASES = "ctem_cases"
_ISSUES = "ctem_case_issues"
_EVENTS = "ctem_case_events"

#: The source content a case no longer stores, with the DDL that restores it.
_CASE_CONTENT = {
    "priority": "VARCHAR(10) NOT NULL DEFAULT 'low'",
    "priority_basis": "VARCHAR(200) NOT NULL DEFAULT 'default policy'",
    "issue_count": "INTEGER NOT NULL DEFAULT 0",
    "asset_count": "INTEGER NOT NULL DEFAULT 0",
    "crown_jewel_count": "INTEGER NOT NULL DEFAULT 0",
    "kev_count": "INTEGER NOT NULL DEFAULT 0",
    "max_cvss": "DOUBLE PRECISION",
    "max_epss": "DOUBLE PRECISION",
    "first_seen_at": "TIMESTAMP WITH TIME ZONE",
}
_ISSUE_CONTENT = {
    "name": "VARCHAR(300) NOT NULL DEFAULT ''",
    "cvss": "DOUBLE PRECISION",
    "epss": "DOUBLE PRECISION",
    "kev": "BOOLEAN NOT NULL DEFAULT false",
    "assets": "JSON NOT NULL DEFAULT '[]'",
    "crown_jewels": "JSON NOT NULL DEFAULT '[]'",
    "first_seen_at": "TIMESTAMP WITH TIME ZONE",
    "last_seen_at": "TIMESTAMP WITH TIME ZONE",
}


def _columns(conn, table: str) -> set[str]:
    return {
        row[0]
        for row in conn.execute(
            sa.text(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema = current_schema() AND table_name = :t"
            ),
            {"t": table},
        )
    }


def upgrade() -> None:
    conn = op.get_bind()
    cases, issues, events = _columns(conn, _CASES), _columns(conn, _ISSUES), _columns(conn, _EVENTS)
    # Fresh installs run create_all + stamp head (no table yet, or already the
    # new shape): nothing to do.
    if not cases or ("issue" in events and "name" not in issues and "priority" not in cases):
        return
    for table in (_CASES, _ISSUES, _EVENTS):
        terminate_lock_holders(conn, table)
    set_lock_timeout(conn)
    # Built from one user's view and readable by all: dropped, rebuilt by the
    # next recompute (issues and events go by ON DELETE CASCADE).
    op.execute(f"DELETE FROM {_CASES}")  # noqa: S608 - module constant, no input
    for column in _CASE_CONTENT:
        op.execute(f"ALTER TABLE {_CASES} DROP COLUMN IF EXISTS {column}")
    for column in _ISSUE_CONTENT:
        op.execute(f"ALTER TABLE {_ISSUES} DROP COLUMN IF EXISTS {column}")
    op.execute(f"ALTER TABLE {_EVENTS} ADD COLUMN IF NOT EXISTS issue VARCHAR(220)")


def downgrade() -> None:
    conn = op.get_bind()
    if not _columns(conn, _CASES):
        return
    for table in (_CASES, _ISSUES, _EVENTS):
        terminate_lock_holders(conn, table)
    set_lock_timeout(conn)
    # The shared rows carry no content to put back: dropped, rebuilt by the
    # previous code's recompute.
    op.execute(f"DELETE FROM {_CASES}")  # noqa: S608 - module constant, no input
    for column, ddl in _CASE_CONTENT.items():
        op.execute(f"ALTER TABLE {_CASES} ADD COLUMN IF NOT EXISTS {column} {ddl}")
    for column, ddl in _ISSUE_CONTENT.items():
        op.execute(f"ALTER TABLE {_ISSUES} ADD COLUMN IF NOT EXISTS {column} {ddl}")
    op.execute(f"ALTER TABLE {_EVENTS} DROP COLUMN IF EXISTS issue")
