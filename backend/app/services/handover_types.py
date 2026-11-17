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
"""

from __future__ import annotations

from collections.abc import Collection, Iterable, Mapping


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
