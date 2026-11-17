"""merge agent_builder, fleets and flow_builder into the platform group

Built-in tools consolidation, stage 2b PR 6. Three former ids become the
``platform`` group; they stay accepted as [DEPRECATED] aliases
(``app.services.builtin_tool_aliases.BUILTIN_GROUP_ALIASES``), so this
migration only rewrites stored rows into the canonical form, as ``m8156bigrp01`` did for
five groups:

- ``agents.builtin_tool_ids`` / ``assignments.builtin_tool_ids`` (when the
  column exists): a former id becomes ``platform``, once.
- ``builtin_allowed_tools``: the group keeps exactly the functions its former
  ids granted (their own lists applied); a list keyed by a former id moves
  under ``platform``. A row that carried ``flow_builder`` keeps an explicit
  list, so the FLOW_BUILDER flag never takes away a tool it named.

General Assistant rows are not touched: the General Assistant accepts both
forms (``general_chat``); stage 2b PR 7 owns its default set.

Row by row in Python with ``canonical_builtin_selection``, the function the
readers use. Idempotent. Downgrade expands ``platform`` back into the former
ids its allow list keeps and moves the list back under each former id it
narrows.

Revision ID: p8270platgrp
Revises: e8297flimp01
Create Date: 2026-11-03 22:20:00.000000
"""

from __future__ import annotations

import json
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "p8270platgrp"
# NOT a constant: must always be main's CURRENT head, re-pointed whenever main
# gains a migration (backend/AGENTS.md > "Database migrations (safety)", rule 1).
down_revision: str | Sequence[str] | None = "e8297flimp01"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_GROUP = "platform"
_FORMER: tuple[str, ...] = ("agent_builder", "fleets", "flow_builder")
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
    # A coarse text filter; each row is decided in Python. The literals are
    # this module's constants (ids are [a-z_] only), never input.
    likes = " OR ".join(
        f"CAST({column} AS TEXT) LIKE '%\"{tool_id}\"%'"
        for column in (select_ids, "builtin_allowed_tools")
        for tool_id in (*_FORMER, _GROUP)
    )
    return conn.execute(
        sa.text(
            f"SELECT id, {select_ids} AS ids, builtin_allowed_tools AS allowed FROM {table} WHERE {likes}"  # noqa: S608 - no input: table from _TABLES, ids from module constants
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
                _write(conn, table, row.id, new_ids if has_ids else ids, new_allowed, has_ids)


def downgrade_selection(ids: list[str], allowed: dict | None) -> tuple[list[str], dict | None]:
    """The earlier form of one canonical row: ``platform`` expanded.

    The group becomes the former ids its allow list keeps; the list moves
    under each of them as the functions of that id it keeps, when that
    narrows the id. Every other id and its list are kept as they are. Pure,
    so ``tests/test_platform_group_migration.py`` pins it against the upgrade.
    """
    from app.services.builtin_tool_aliases import alias_scoped_functions

    allowed = dict(allowed or {})
    out: list[str] = []
    for tool_id in ids:
        if tool_id != _GROUP:
            if tool_id not in out:
                out.append(tool_id)
            continue
        keep = allowed.get(_GROUP)
        for former in _FORMER:
            granted = alias_scoped_functions(former)
            if keep is not None and not any(fn in keep for fn in granted):
                continue
            if former in out:
                continue
            out.append(former)
            if keep is not None:
                kept = [fn for fn in granted if fn in keep]
                if kept != list(granted):
                    allowed[former] = kept
    allowed.pop(_GROUP, None)
    return out, (allowed or None)


def downgrade() -> None:
    conn = op.get_bind()
    for table in _TABLES:
        cols = _columns(conn, table)
        if "builtin_allowed_tools" not in cols or "builtin_tool_ids" not in cols:
            continue
        for row in _rows(conn, table, True):
            ids, allowed = _load(row.ids) or [], _load(row.allowed)
            if _GROUP not in ids:
                continue
            old_ids, old_allowed = downgrade_selection(ids, allowed)
            _write(conn, table, row.id, old_ids, old_allowed, True)
