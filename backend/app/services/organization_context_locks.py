# SPDX-License-Identifier: Apache-2.0

"""Synced rows of the organization profile are read-only by hand (#1367, point 3).

A row a platform sync wrote (``source`` a platform, with its ``external_id``)
belongs to that sync: only the fields the source never sends (the local
enrichment of :data:`organization_context_import.SOURCED_FIELDS`) may be set
by a person. The form, the CTEM Inventory and the API all save through
``PUT /organization-context``; this check runs there, so no surface can
change a synced field, whatever it draws.

Pure: it compares the stored profile with the one being saved, no I/O.
"""

from __future__ import annotations

from typing import Any

from app.services.organization_context import is_platform_source
from app.services.organization_context_import import SOURCED_FIELDS

#: Keys never compared: provenance is checked through the match itself.
_IGNORED: frozenset[str] = frozenset({"source", "external_id"})


def _key(row: dict[str, Any]) -> tuple[str, str] | None:
    """The sync key of a platform row, ``None`` for a row a person or an agent owns."""
    source = str(row.get("source") or "").strip().lower()
    external_id = str(row.get("external_id") or "").strip()
    if not external_id or not is_platform_source(source):
        return None
    return source, external_id


def synced_edit_problems(stored: dict[str, Any] | None, incoming: dict[str, Any]) -> list[str]:
    """Why *incoming* changes a synced row of *stored* by hand; empty when it does not.

    A synced row kept with only its local fields changed passes; a synced row
    absent from *incoming* is not judged here (the sync's exclusion and the
    release path own removals).
    """
    if not stored:
        return []
    problems: list[str] = []
    for section, (attr, name_attr, _cap, local) in SOURCED_FIELDS.items():
        if section == "ai_systems":
            continue  # #1501: the deprecated alias of the AI target assets, judged with the assets
        before = {}
        for row in stored.get(attr) or []:
            if isinstance(row, dict) and (key := _key(row)) is not None:
                before.setdefault(key, row)
        if not before:
            continue
        free = set(local) | _IGNORED
        for row in incoming.get(attr) or []:
            if not isinstance(row, dict) or (key := _key(row)) is None or key not in before:
                continue
            old = before[key]
            changed = sorted(
                field for field in set(old) | set(row) if field not in free and old.get(field) != row.get(field)
            )
            if changed:
                name = str(old.get(name_attr) or key[1])
                problems.append(
                    f"{section}: '{name}' is kept in sync from {key[0]}; {', '.join(changed)} cannot be changed by hand"
                )
    return problems
