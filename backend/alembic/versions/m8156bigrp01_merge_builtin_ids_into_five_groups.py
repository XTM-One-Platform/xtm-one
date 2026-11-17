"""merge twelve built-in ids into the design, web, cti, coordination and files groups

Built-in tools consolidation, stage 2b PR 5. Twelve former ids become five
groups; the former ids stay accepted as [DEPRECATED] aliases
(``app.services.builtin_tool_aliases.BUILTIN_GROUP_ALIASES``), so this
migration only rewrites stored rows into the canonical form:

- ``agents.builtin_tool_ids`` / ``assignments.builtin_tool_ids`` (when the
  column exists): a former id becomes its group, once.
- ``builtin_allowed_tools``: a group that came through former ids keeps
  exactly the functions those ids granted (their own lists applied); a list
  keyed by a former id moves under its group.

General Assistant rows (``general_assistant_config``, ``users.ga_builtin_tool_ids``)
are not touched: the General Assistant accepts both forms and wires a group
from its former ids (``general_chat``); stage 2b PR 7 owns its default set.

The data change is row by row in Python with ``canonical_builtin_selection``,
the function the readers use, so the migration and the runtime cannot drift.
Idempotent (a canonical row is left unchanged). Downgrade expands the five
groups back into the former ids their allow list keeps
(``runtime_builtin_tool_ids``) and moves the group's list back under each
former id it narrows (allowed lists keyed by those ids), the shape the
rows had before.

Revision ID: m8156bigrp01
Revises: o7939obsgrp1
Create Date: 2026-11-03 14:20:00.000000
"""

from __future__ import annotations

import json
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "m8156bigrp01"
# NOT a constant: must always be main's CURRENT head, re-pointed whenever main
# gains a migration (backend/AGENTS.md > "Database migrations (safety)", rule 1).
down_revision: str | Sequence[str] | None = "o7939obsgrp1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_GROUPS: tuple[str, ...] = ("design", "web", "cti", "coordination", "files")
_FORMER: tuple[str, ...] = (
    "template_search",
    "template_authoring",
    "document_vision",
    "web_fetch",
    "browser",
    "stix_bundle_validator",
    "fimi_analytics",
    "work_items",
    "shared_state",
    "workset",
    "generate_file",
    "generate_image",
)
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
        for tool_id in (*_FORMER, *_GROUPS)
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
    """The earlier form of one canonical row: the five groups expanded.

    A group becomes the former ids its allow list keeps; the list moves under
    each of them as the functions of that id it keeps, when that narrows the
    id. Every other group (``observability`` included) and its list are kept
    as they are. Pure, so ``tests/test_builtin_tool_aliases.py`` pins it
    against the upgrade.
    """
    from app.services.builtin_tool_aliases import alias_scoped_functions, runtime_builtin_tool_ids

    allowed = dict(allowed or {})
    out: list[str] = []
    for tool_id in ids:
        if tool_id not in _GROUPS:
            if tool_id not in out:
                out.append(tool_id)
            continue
        keep = allowed.get(tool_id)
        expanded = runtime_builtin_tool_ids([tool_id], {tool_id: keep} if keep is not None else None)
        for former in expanded:
            if former == tool_id or former in out:
                continue
            out.append(former)
            if keep is not None:
                granted = alias_scoped_functions(former)
                kept = [fn for fn in granted if fn in keep]
                if kept != list(granted):
                    allowed[former] = kept
    for group in _GROUPS:
        allowed.pop(group, None)
    return out, (allowed or None)


def downgrade() -> None:
    conn = op.get_bind()
    for table in _TABLES:
        cols = _columns(conn, table)
        if "builtin_allowed_tools" not in cols or "builtin_tool_ids" not in cols:
            continue
        for row in _rows(conn, table, True):
            ids, allowed = _load(row.ids) or [], _load(row.allowed)
            if not any(group in ids for group in _GROUPS):
                continue
            old_ids, old_allowed = downgrade_selection(ids, allowed)
            _write(conn, table, row.id, old_ids, old_allowed, True)
