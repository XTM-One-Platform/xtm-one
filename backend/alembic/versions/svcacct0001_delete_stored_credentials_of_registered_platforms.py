"""no-op (formerly: delete the credentials stored on registered module rows)

This revision first cleared the credential columns of every
``platform_managed`` OpenCTI, OpenAEV and OpenCRQ integration. That deletion
was not needed: the unauthorized-read root cause is fixed by the capability
probe alone (``integrations/providers/opencti/capabilities.py``), and a
credential an administrator stored on purpose must never be deleted by an
upgrade, since the deletion cannot be undone.

The revision id is kept so the Alembic history stays linear on a database
that already ran it; ``upgrade()`` now does nothing. Values an earlier run of
this revision cleared are not recoverable and are not restored.

Revision ID: svcacct0001
Revises: c11464dashflt1
Create Date: 2026-11-13 00:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

revision: str = "svcacct0001"
# NOT a constant: must always be main's CURRENT head, re-pointed whenever main
# gains a migration (backend/AGENTS.md > "Database migrations (safety)", rule 1).
down_revision: str | Sequence[str] | None = "c11464dashflt1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """No-op: stored credentials are kept (see the module docstring)."""


def downgrade() -> None:
    pass
