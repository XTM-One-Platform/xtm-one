"""merge the two trace built-in ids into the observability group

Built-in tools consolidation, stage 2b PR 4. ``inspect_my_traces`` and
``inspect_agent_traces`` become the ``observability`` group; the former ids
stay accepted as [DEPRECATED] aliases (``app.services.builtin_tool_aliases``),
so this migration only rewrites stored rows into the canonical form:

- ``agents.builtin_tool_ids`` / ``assignments.builtin_tool_ids`` (when the
  column exists): a former id becomes ``observability``.
- ``builtin_allowed_tools``: when a row carried only one former id, the group
  is scoped to the functions that id granted; a list keyed by a former id
  moves under ``observability``.

The data change is row by row in Python with ``canonical_builtin_selection``,
the function the readers use, so the migration and the runtime cannot drift.
Idempotent (a canonical row is left unchanged). Downgrade restores the former
ids from the group and its scoping.

Revision ID: o7939obsgrp1
Revises: t8043trace01
Create Date: 2026-11-03 09:10:00.000000
"""

from __future__ import annotations

import json
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "o7939obsgrp1"
# NOT a constant: must always be main's CURRENT head, re-pointed whenever main
# gains a migration (backend/AGENTS.md > "Database migrations (safety)", rule 1).
down_revision: str | Sequence[str] | None = "t8043trace01"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_GROUP = "observability"
_FORMER = ("inspect_my_traces", "inspect_agent_traces")
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


def _rows(conn, table: str, has_ids: bool):
    select_ids = "builtin_tool_ids" if has_ids else "NULL"
    return conn.execute(
        sa.text(
            f"SELECT id, {select_ids} AS ids, builtin_allowed_tools AS allowed FROM {table} "  # noqa: S608 - no input: table from _TABLES, column a literal
            f"WHERE CAST({select_ids} AS TEXT) LIKE '%inspect_%' "
            f"OR CAST(builtin_allowed_tools AS TEXT) LIKE '%inspect_%' "
            f"OR CAST({select_ids} AS TEXT) LIKE '%{_GROUP}%'"
        )
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
        for row in _rows(conn, table, has_ids):
            ids, allowed = _load(row.ids) or [], _load(row.allowed)
            new_ids, new_allowed = canonical_builtin_selection(ids, allowed)
            if (new_ids if has_ids else ids) != ids or new_allowed != (allowed or None):
                _write(conn, table, row.id, new_ids, new_allowed, has_ids)


def downgrade() -> None:
    from app.services.builtin_tool_aliases import runtime_builtin_tool_ids

    conn = op.get_bind()
    for table in _TABLES:
        cols = _columns(conn, table)
        if "builtin_allowed_tools" not in cols or "builtin_tool_ids" not in cols:
            continue
        for row in _rows(conn, table, True):
            ids, allowed = _load(row.ids) or [], dict(_load(row.allowed) or {})
            if _GROUP not in ids:
                continue
            expanded = runtime_builtin_tool_ids(ids, allowed)
            old_ids = [t for t in expanded if t != _GROUP]
            allowed.pop(_GROUP, None)
            _write(conn, table, row.id, old_ids, allowed or None, True)
