"""rebind the ctem_read and ctem_repair built-in groups to ctem (one CTEM group)

#2443 section A: ``ctem_read`` (#1337) and ``ctem_repair`` (#1778) are merged
into ``ctem`` (#1748). The former ids stay accepted as [DEPRECATED] ids
(``RENAMED_BUILTIN_GROUPS`` in ``app.services.builtin_tool_aliases``), so this
migration only rewrites the stored rows into the canonical form:

- ``agents.builtin_tool_ids`` / ``assignments.builtin_tool_ids`` (when the
  column exists): a former id becomes ``ctem``, deduplicated.
- ``builtin_allowed_tools``: a list keyed by a former id moves under ``ctem``
  (lists merged when several former ids carried one), its functions unchanged.

Reach is unchanged: every ``ctem`` function checks the acting person's CTEM
right at call time (#2441), so a former read-only binding never writes beyond
that person's right. Row by row in Python with ``canonical_builtin_selection``,
the function the readers use. Idempotent. Downgrade is a no-op on the ids (the
merged binding cannot be split back; ``ctem`` grants a superset of both).

Revision ID: s2443ctemgrp01
Revises: s2441ctemseed01
Create Date: 2026-11-26 07:20:00.000000
"""

from __future__ import annotations

import json
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "s2443ctemgrp01"
# NOT a constant: must always be main's CURRENT head, re-pointed whenever main
# gains a migration (backend/AGENTS.md > "Database migrations (safety)", rule 1).
down_revision: str | Sequence[str] | None = "s2441ctemseed01"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLES: tuple[str, ...] = ("agents", "assignments")


def _columns(conn, table: str) -> set[str]:
    rows = conn.execute(
        sa.text(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = current_schema() AND table_name = :table"
        ),
        {"table": table},
    )
    return {row[0] for row in rows}


def _load(value):
    if value is None or isinstance(value, (list, dict)):
        return value
    return json.loads(value)


def upgrade() -> None:
    from app.services.builtin_tool_aliases import canonical_builtin_selection

    conn = op.get_bind()
    for table in _TABLES:
        cols = _columns(conn, table)
        if "builtin_allowed_tools" not in cols:
            continue
        has_ids = "builtin_tool_ids" in cols
        select_ids = "builtin_tool_ids" if has_ids else "NULL"
        rows = conn.execute(
            sa.text(
                f"SELECT id, {select_ids} AS ids, builtin_allowed_tools AS allowed FROM {table} "  # noqa: S608 - table from _TABLES
                f"WHERE CAST({select_ids} AS TEXT) LIKE :marker OR CAST(builtin_allowed_tools AS TEXT) LIKE :marker"
            ),
            {"marker": "%ctem_re%"},
        ).fetchall()
        for row in rows:
            ids, allowed = _load(row.ids) or [], _load(row.allowed)
            new_ids, new_allowed = canonical_builtin_selection(ids, allowed)
            if (new_ids if has_ids else ids) == ids and new_allowed == (allowed or None):
                continue
            sets = ["builtin_allowed_tools = CAST(:allowed AS JSON)"]
            params = {"id": row.id, "allowed": json.dumps(new_allowed) if new_allowed else None}
            if has_ids:
                sets.append("builtin_tool_ids = CAST(:ids AS JSON)")
                params["ids"] = json.dumps(new_ids)
            conn.execute(sa.text(f"UPDATE {table} SET {', '.join(sets)} WHERE id = :id"), params)  # noqa: S608


def downgrade() -> None:
    """No-op: a ``ctem`` binding is kept (it grants a superset of both former groups)."""
