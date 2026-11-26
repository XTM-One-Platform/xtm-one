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

"""The handover type map of an agent and of the General Assistant.

The backend half of ``frontend/src/lib/handover-types.ts``: both read and write
the map by the same rules, pinned against one table,
``frontend/tests_e2e/fixtures/handover-type-cases.json``.

An EMPTY map is the legacy mode of a configuration saved before any target had
a type: every handover tool (``spawn_background_task``, ``consult_agent``,
``transfer_to_agent``) is registered for every target. A map that holds a type,
even one of a target no longer listed, is explicit: a target without an entry
runs as ``delegate``. A write never changes how a stored target runs unless it
types that target:

- a target added next to stored targets that run every type joins them, since
  recording a type for it would narrow them all to ``delegate``; anywhere else a
  target added without a type is recorded as ``delegate``;
- removing targets never empties a map that holds a type, which would give the
  targets left every type.

An entry holds one type (``consult``), several joined by ``|``
(``consult|delegate``), or ``all`` (every tool type), so one target can keep
running every type in a map where another runs ``consult`` only
(``handover_type_set`` reads an entry, ``merged_handover_types`` writes a
partial update, #2207).
"""

from __future__ import annotations

from collections.abc import Collection, Iterable, Mapping

#: The types a handover tool implements, and what ``all`` stands for.
TOOL_TYPES: tuple[str, ...] = ("delegate", "consult", "transfer")
#: Every type an entry may hold; ``mandatory_transfer`` only alone.
VALID_TYPES: tuple[str, ...] = (*TOOL_TYPES, "mandatory_transfer")
#: The entry of a target that runs every tool type in an explicit map.
ALL_TYPES = "all"


def handover_type_set(value: object) -> frozenset[str]:
    """The types one stored entry runs: ``all``, one type, or several joined by ``|``.

    An entry with no type a tool implements runs as ``delegate``, as an absent
    one does; ``mandatory_transfer`` is never mixed with another type.
    """
    raw = str(value or "").strip().lower()
    if raw == ALL_TYPES:
        return frozenset(TOOL_TYPES)
    if raw == "mandatory_transfer":
        return frozenset({"mandatory_transfer"})
    types = frozenset(part.strip() for part in raw.split("|")) & frozenset(TOOL_TYPES)
    return types or frozenset({"delegate"})


def parse_handover_type_entry(text: str) -> str | None:
    """The canonical stored form of a written entry, ``None`` when it is invalid.

    ``consult``, ``consult|delegate`` (sorted, deduplicated; every tool type is
    written ``all``), ``all`` or ``mandatory_transfer`` alone.
    """
    parts = {part.strip().lower() for part in text.split("|") if part.strip()}
    if not parts:
        return None
    if parts == {ALL_TYPES} or parts == {"mandatory_transfer"}:
        return next(iter(parts))
    if not parts <= set(TOOL_TYPES):
        return None
    if parts == set(TOOL_TYPES):
        return ALL_TYPES
    return "|".join(t for t in TOOL_TYPES if t in parts)


def merged_handover_types(
    target_ids: Iterable[object],
    stored: Mapping[str, str] | None,
    written: Mapping[str, str],
) -> dict[str, str]:
    """The map once *written* types the targets it names, the others left as they ran.

    On an explicit map the targets not named keep their entry. On a legacy
    (empty) map they each ran every type, so they are written ``all``: typing
    one target never narrows the others to ``delegate``. An entry for an agent
    that is not a target is dropped, so a write that types no target leaves the
    map as it was (a legacy map stays legacy).
    """
    targets = [str(agent_id) for agent_id in target_ids]
    written = {aid: entry for aid, entry in written.items() if str(aid) in targets}
    if not written:
        return dict(stored or {})
    base = dict.fromkeys(targets, ALL_TYPES) if is_legacy_handover_map(stored) else dict(stored or {})
    return {**base, **written}


def is_legacy_handover_map(type_map: Mapping[str, str] | None) -> bool:
    """No target has a type: each one runs every handover type."""
    return not type_map


def runs_every_handover_type(
    target_ids: Collection[object], type_map: Mapping[str, str] | None
) -> bool:
    """Targets saved without a type, each running every handover type."""
    return bool(target_ids) and is_legacy_handover_map(type_map)


def with_added_handover_targets(
    kept_ids: Collection[object],
    type_map: Mapping[str, str] | None,
    added_ids: Iterable[object],
) -> dict[str, str]:
    """The type map once *added_ids* join the *kept_ids* already stored.

    Kept targets that run every type keep doing so, the added ones with them.
    Otherwise an added target without an entry in *type_map* is recorded as
    ``delegate``, how it runs in a map that holds a type.
    """
    if runs_every_handover_type(kept_ids, type_map):
        return {}
    return {**dict.fromkeys((str(agent_id) for agent_id in added_ids), "delegate"), **(type_map or {})}


def handover_types_after_removal(
    target_ids: Iterable[object],
    type_map: Mapping[str, str] | None,
    kept: dict[str, str],
) -> dict[str, str]:
    """*kept*, the entries of *type_map* for the *target_ids* left.

    When dropping the other entries empties a map that held a type, the targets
    left are written out as ``delegate`` instead, how each of them ran.
    """
    remaining = [str(agent_id) for agent_id in target_ids]
    if kept or is_legacy_handover_map(type_map) or not remaining:
        return kept
    return dict.fromkeys(remaining, "delegate")
