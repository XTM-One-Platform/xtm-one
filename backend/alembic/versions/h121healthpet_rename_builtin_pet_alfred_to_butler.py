"""rename the built-in companion pet ``alfred`` to ``butler`` in stored preferences

Platform Health (#101, dev-docs/platform-health.md section 2.3): no "Alfred" name is
left in the product. The built-in pet ships as ``butler``; ``alfred`` stays a
read alias (``RENAMED_BUILTIN_PETS`` in ``app.services.health.pets``). This
migration rewrites ``users.concierge_companion`` so a stored preference names the
current slug: ``{"pet": "alfred"}`` becomes ``{"pet": "butler"}``, every other
key kept. Idempotent; a fresh install has no such row. The downgrade writes the
former slug back.

Revision ID: h121healthpet
Revises: h120healthidx
Create Date: 2026-11-19 06:05:00.000000
"""

from __future__ import annotations

import json
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "h121healthpet"
# NOT a constant: must always be main's CURRENT head, re-pointed whenever main
# gains a migration (backend/AGENTS.md > "Database migrations (safety)", rule 1).
down_revision: str | Sequence[str] | None = "h120healthidx"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: Former slug -> current slug, frozen here: the migration never follows a later edit of the map.
_RENAMED: dict[str, str] = {"alfred": "butler"}


def _load(value):
    if value is None or isinstance(value, dict):
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return None


def rewrite_preference(stored, renamed: dict[str, str]):
    """*stored* with its ``pet`` renamed by *renamed*; ``None`` when nothing changes. Pure."""
    if not isinstance(stored, dict):
        return None
    pet = stored.get("pet")
    if not isinstance(pet, str) or pet not in renamed:
        return None
    return {**stored, "pet": renamed[pet]}


def _rename(renamed: dict[str, str]) -> None:
    conn = op.get_bind()
    columns = {
        row[0]
        for row in conn.execute(
            sa.text(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema = current_schema() AND table_name = 'users'"
            )
        )
    }
    if "concierge_companion" not in columns:
        return
    for former in renamed:
        rows = conn.execute(
            sa.text("SELECT id, concierge_companion FROM users WHERE CAST(concierge_companion AS TEXT) LIKE :marker"),
            {"marker": f'%"{former}"%'},
        ).fetchall()
        for row in rows:
            new = rewrite_preference(_load(row.concierge_companion), renamed)
            if new is not None:
                conn.execute(
                    sa.text("UPDATE users SET concierge_companion = CAST(:value AS JSON) WHERE id = :id"),
                    {"id": row.id, "value": json.dumps(new)},
                )


def upgrade() -> None:
    _rename(_RENAMED)


def downgrade() -> None:
    _rename({new: old for old, new in _RENAMED.items()})
