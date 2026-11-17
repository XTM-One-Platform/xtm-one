"""integrations.scopes: drop the Jira Insight scope Atlassian never grants (#11033)

The Jira catalog requested ``read:servicemanagement-insight-objects``. No Jira
tool uses it, and Atlassian grants Insight/Assets scopes only where the site
has JSM Assets: elsewhere it drops them silently from the consent and from the
token answer. Since #9776 a requested scope absent from the grant reads
"Re-authorization required", so every such Jira row stayed amber however often
it was re-authorized.

The catalog no longer requests it; a row's stored ``scopes`` (the catalog value
at its last consent, which the integration answer prefers over the catalog)
still does until the next consent. This data migration removes it from the
stored scopes of the Jira rows, so they read connected at deploy without a new
consent. Row update only, no DDL, on the few Jira rows of ``integrations``;
idempotent (a re-run matches nothing). Fresh installs run ``create_all`` +
``stamp head``: the body never executes there.

Revision ID: x11033jirascope1
Revises: w10838vidfb1
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "x11033jirascope1"
# NOT a constant: must always be main's CURRENT head, re-pointed on every rebase
# (backend/AGENTS.md > "Database migrations (safety)", rule 1).
down_revision: str | None = "w10838vidfb1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_SCOPE = "read:servicemanagement-insight-objects"


def upgrade() -> None:
    conn = op.get_bind()
    rows = conn.execute(
        sa.text("SELECT id, scopes FROM integrations WHERE provider = 'jira' AND scopes LIKE :pattern"),
        {"pattern": f"%{_SCOPE}%"},
    ).fetchall()
    for row in rows:
        kept = [scope for scope in (row.scopes or "").split() if scope != _SCOPE]
        conn.execute(
            sa.text("UPDATE integrations SET scopes = :scopes WHERE id = :id"),
            {"scopes": " ".join(kept) or None, "id": row.id},
        )


def downgrade() -> None:
    # Nothing to restore: the scope was never granted, and the previous release
    # writes the catalog scopes back on the next consent.
    pass
