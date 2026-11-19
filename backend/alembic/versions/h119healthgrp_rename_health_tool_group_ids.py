"""rename the Platform Health built-in group ids from concierge_* to health_*

Platform Health (#101, dev-docs/platform-health.md section 2.3): the five
built-in groups of the former Concierge are named after the suite:
``concierge_diagnosis``, ``concierge_experiments``, ``concierge_sandbox``,
``concierge_orchestrator`` and ``concierge_evaluation`` become
``health_diagnosis``, ``health_experiments``, ``health_sandbox``,
``health_orchestrator`` and ``health_evaluation``. The former ids stay
accepted as [DEPRECATED] aliases (``RENAMED_BUILTIN_GROUPS`` in
``app.services.builtin_tool_aliases``), so this migration only rewrites the
stored rows into the canonical form:

- ``agents.builtin_tool_ids`` / ``assignments.builtin_tool_ids`` (when the
  column exists): a former id becomes its current id.
- ``builtin_allowed_tools``: a list keyed by a former id moves under the
  current id, its functions unchanged.

Row by row in Python with ``canonical_builtin_selection``, the function the
readers use, so the migration and the runtime cannot drift. Idempotent (a
canonical row is left unchanged). Downgrade writes the former ids back.

Revision ID: h119healthgrp
Revises: h118health01
Create Date: 2026-11-19 04:00:00.000000
"""

from __future__ import annotations

import json
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "h119healthgrp"
# NOT a constant: must always be main's CURRENT head, re-pointed whenever main
# gains a migration (backend/AGENTS.md > "Database migrations (safety)", rule 1).
down_revision: str | Sequence[str] | None = "h118health01"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: Former id -> current id, frozen here: the migration never follows a later edit of the map.
_RENAMED: dict[str, str] = {
    "concierge_diagnosis": "health_diagnosis",
    "concierge_experiments": "health_experiments",
    "concierge_sandbox": "health_sandbox",
    "concierge_orchestrator": "health_orchestrator",
    "concierge_evaluation": "health_evaluation",
}
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


def _rows(conn, table: str, has_ids: bool, marker: str):
    select_ids = "builtin_tool_ids" if has_ids else "NULL"
    return conn.execute(
        sa.text(
            f"SELECT id, {select_ids} AS ids, builtin_allowed_tools AS allowed FROM {table} "  # noqa: S608 - no input: table from _TABLES, column a literal
            f"WHERE CAST({select_ids} AS TEXT) LIKE :marker "
            f"OR CAST(builtin_allowed_tools AS TEXT) LIKE :marker"
        ),
        {"marker": f"%{marker}%"},
    ).fetchall()


def _write(conn, table: str, row_id, ids, allowed, has_ids: bool) -> None:
    sets = ["builtin_allowed_tools = CAST(:allowed AS JSON)"]
    params = {"id": row_id, "allowed": json.dumps(allowed) if allowed else None}
    if has_ids:
        sets.append("builtin_tool_ids = CAST(:ids AS JSON)")
        params["ids"] = json.dumps(ids)
    # No input: table from _TABLES, SET clauses literals; values are bound.
    conn.execute(sa.text(f"UPDATE {table} SET {', '.join(sets)} WHERE id = :id"), params)  # noqa: S608


def upgrade() -> None:
    from app.services.builtin_tool_aliases import canonical_builtin_selection

    conn = op.get_bind()
    for table in _TABLES:
        cols = _columns(conn, table)
        if "builtin_allowed_tools" not in cols:
            continue
        has_ids = "builtin_tool_ids" in cols
        for row in _rows(conn, table, has_ids, "concierge_"):
            ids, allowed = _load(row.ids) or [], _load(row.allowed)
            new_ids, new_allowed = canonical_builtin_selection(ids, allowed)
            if (new_ids if has_ids else ids) != ids or new_allowed != (allowed or None):
                _write(conn, table, row.id, new_ids if has_ids else ids, new_allowed, has_ids)


def downgrade_selection(ids: list[str], allowed: dict | None) -> tuple[list[str], dict | None]:
    """*ids* and *allowed* with every current id written back as its former id. Pure."""
    back = {new: old for old, new in _RENAMED.items()}
    return [back.get(str(t), str(t)) for t in ids], (
        {back.get(str(k), str(k)): v for k, v in allowed.items()} if allowed else None
    )


def downgrade() -> None:
    conn = op.get_bind()
    for table in _TABLES:
        cols = _columns(conn, table)
        if "builtin_allowed_tools" not in cols:
            continue
        has_ids = "builtin_tool_ids" in cols
        for row in _rows(conn, table, has_ids, "health_"):
            ids, allowed = _load(row.ids) or [], _load(row.allowed)
            old_ids, old_allowed = downgrade_selection(ids, allowed)
            if old_ids != ids or old_allowed != (allowed or None):
                _write(conn, table, row.id, old_ids, old_allowed, has_ids)
