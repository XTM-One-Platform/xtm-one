# Copyright (c) 2025-2026 Filigran SAS
#
# This file is part of the XTM One Enterprise Edition ("EE") and is
# licensed under the XTM One Enterprise Edition License (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://github.com/XTM-One-Platform/xtm-one/blob/main/LICENSE
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.

"""Merge Platform Health's specialist tool groups into ``health_specialists`` (#1943).

The diagnosis, experiments, sandbox, evaluation, Flow Lab, memory steward,
modules and improvement proposal groups (and their former ``concierge_*`` ids)
are deleted: every stored row naming one now names the one internal group.
No alias is kept. Rewritten:

- ``agents.builtin_tool_ids`` and ``general_assistant_config.builtin_tool_ids``
  and ``users.ga_builtin_tool_ids``: each retired id becomes
  ``health_specialists``, once, in the place of the first one.
- ``agents.builtin_allowed_tools`` and ``assignments.builtin_allowed_tools``:
  the lists keyed by a retired id are merged under ``health_specialists``
  (the union of their functions; a key mapped to ``null`` restricts nothing and
  makes the merged key ``null``).

Revision ID: h1943healthgrp01
Revises: s1495swfield01
Create Date: 2026-11-24
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa

from alembic import op

revision: str = "h1943healthgrp01"
# Main's current head: re-pointed whenever main gains a migration (backend/AGENTS.md rule 1).
down_revision: str | Sequence[str] | None = "s1495swfield01"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

NEW = "health_specialists"
_RETIRED: frozenset[str] = frozenset(
    {
        "health_sandbox",
        "health_evaluation",
        "improvement_proposals",
        "memory_steward",
        "health_diagnosis",
        "health_experiments",
        "flow_lab",
        "health_modules",
        "concierge_sandbox",
        "concierge_evaluation",
        "concierge_diagnosis",
        "concierge_experiments",
    }
)

#: (table, list column) pairs holding built-in group ids.
_ID_COLUMNS: tuple[tuple[str, str], ...] = (
    ("agents", "builtin_tool_ids"),
    ("general_assistant_config", "builtin_tool_ids"),
    ("users", "ga_builtin_tool_ids"),
)
#: (table, map column) pairs holding ``builtin_allowed_tools``.
_MAP_COLUMNS: tuple[tuple[str, str], ...] = (
    ("agents", "builtin_allowed_tools"),
    ("assignments", "builtin_allowed_tools"),
)


def merged_ids(ids: list[Any] | None) -> list[Any] | None:
    """*ids* with every retired id replaced by ``health_specialists``, once, where the first stood."""
    if not ids or not any(i in _RETIRED for i in ids):
        return ids
    out: list[Any] = []
    for tool_id in ids:
        target = NEW if tool_id in _RETIRED else tool_id
        if target not in out:
            out.append(target)
    return out


def merged_map(allowed: dict[str, Any] | None) -> dict[str, Any] | None:
    """*allowed* with the lists keyed by a retired id merged under ``health_specialists``."""
    if not allowed or not any(k in _RETIRED for k in allowed):
        return allowed
    out: dict[str, Any] = {}
    merged: list[str] | None = list(allowed.get(NEW) or []) if NEW in allowed else []
    unrestricted = NEW in allowed and allowed[NEW] is None
    for key, functions in allowed.items():
        if key == NEW:
            continue
        if key in _RETIRED:
            if functions is None:
                unrestricted = True
            else:
                for fn in functions:
                    if merged is not None and fn not in merged:
                        merged.append(fn)
            continue
        out[key] = functions
    out[NEW] = None if unrestricted else merged
    return out


def _table_exists(bind: sa.engine.Connection, table: str) -> bool:
    return sa.inspect(bind).has_table(table)


def upgrade() -> None:
    bind = op.get_bind()
    for table, column in _ID_COLUMNS:
        if not _table_exists(bind, table):
            continue
        rows = bind.execute(sa.text(f"SELECT id, {column} FROM {table} WHERE {column} IS NOT NULL")).fetchall()  # noqa: S608
        for row_id, value in rows:
            ids = value if isinstance(value, list) else json.loads(value)
            new = merged_ids(ids)
            if new != ids:
                bind.execute(
                    sa.text(f"UPDATE {table} SET {column} = CAST(:v AS JSON) WHERE id = :id"),  # noqa: S608
                    {"v": json.dumps(new), "id": row_id},
                )
    for table, column in _MAP_COLUMNS:
        if not _table_exists(bind, table):
            continue
        rows = bind.execute(sa.text(f"SELECT id, {column} FROM {table} WHERE {column} IS NOT NULL")).fetchall()  # noqa: S608
        for row_id, value in rows:
            allowed = value if isinstance(value, dict) else json.loads(value)
            new = merged_map(allowed)
            if new != allowed:
                bind.execute(
                    sa.text(f"UPDATE {table} SET {column} = CAST(:v AS JSON) WHERE id = :id"),  # noqa: S608
                    {"v": json.dumps(new), "id": row_id},
                )


def downgrade() -> None:
    # Building mode, no alias (#1943): the merge is not split back. The seed
    # rewrites the Platform Health fleet's own rows at the next boot.
    pass
