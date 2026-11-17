"""give every A2A integration without a settings row an inbound one

An A2A integration (``oauth_a2a``, ``entra_a2a``, ``google_a2a``) is not valid
without its ``a2a_integration_settings`` row: the row holds its direction, and
one without it belongs to neither direction, so it can neither mint a partner
credential nor delegate. The REST create always wrote the row; the agent tool
``create_integration`` did not (#11778), so integrations it created since
``a2a3outbound1`` carry none. Every create path now writes it
(``app.a2a.providers.provision_settings``); this revision repairs the rows
left behind, with the default the create paths use: inbound.

Same statement as the backfill of ``a2a3outbound1``, over every A2A provider
key at this revision (spelled as literals: a migration keeps meaning what it
meant on the day it ran). Idempotent by construction (NOT EXISTS: a re-run
inserts nothing). Lock-light: an INSERT of a handful of rows (A2A integrations
are partner relationships, a few per install), no DDL, no rewrite.

Downgrade is a no-op: the rows inserted are indistinguishable from the ones
a create writes, and removing an inbound row would make the integration inert
again, the defect this repairs.

Revision ID: a2ah1dirrow01
Revises: s11696sshyg01
Create Date: 2026-11-14 09:30:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "a2ah1dirrow01"
# NOT a constant: must always be main's CURRENT head, re-pointed whenever main
# gains a migration (backend/AGENTS.md > "Database migrations (safety)", rule 1).
down_revision: str | Sequence[str] | None = "s11696sshyg01"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_SETTINGS = "a2a_integration_settings"
#: Every A2A provider key at this revision (``app.a2a.providers.ALL_A2A_PROVIDERS``).
_A2A_PROVIDERS_AT_THIS_REVISION = ("oauth_a2a", "entra_a2a", "google_a2a")


def upgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    if not (inspector.has_table(_SETTINGS) and inspector.has_table("integrations")):
        return
    op.execute(
        sa.text(
            """
            INSERT INTO a2a_integration_settings
                (id, integration_id, direction, created_at, updated_at)
            SELECT gen_random_uuid(), i.id, 'inbound', NOW(), NOW()
            FROM integrations i
            WHERE i.provider = ANY(:providers)
              AND NOT EXISTS (
                  SELECT 1 FROM a2a_integration_settings s
                  WHERE s.integration_id = i.id
              )
            """
        ).bindparams(providers=list(_A2A_PROVIDERS_AT_THIS_REVISION))
    )


def downgrade() -> None:
    # Nothing to undo: see the module docstring.
    return None
