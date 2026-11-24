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

"""Validation of ``builtin_allowed_tools``: per-function scoping of built-in groups.

``Agent.builtin_allowed_tools`` and ``Assignment.builtin_allowed_tools`` map a
built-in group id (an entry of ``builtin_tool_ids``) to the function names it
keeps, with the semantics of ``mcp_allowed_tools``: a group absent or mapped to
``null`` keeps every function, a list keeps only the functions it names. The
runtime filter is ``filtering.filter_builtin_tools``.

Every writer (the agents and assignments APIs, the agent-builder tools) goes
through :func:`normalize_builtin_allowed_tools`, so a typo is refused with the
group's valid functions instead of silently emptying the group.

A list the platform wrote, not a person (#39), is named in the agent's
``config`` under :data:`IMPLICIT_SCOPE_CONFIG_KEY`: a former id folded into its
group (``fold_builtin_selection``) or a group a fleet role granted scoped
(``fleets.grant_group_functions``). A function a release adds to such a group
reaches the agent by itself (``builtin_tool_release_notice``); the group
leaves the marker the moment a person writes a different list for it.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

#: ``Agent.config`` key of the built-in groups whose allow list the platform wrote, not a person (#39).
IMPLICIT_SCOPE_CONFIG_KEY = "builtin_scope_implicit"


class BuiltinAllowedToolsError(ValueError):
    """A ``builtin_allowed_tools`` value names an unknown group or function."""


def builtin_group_functions(group_id: str) -> tuple[str, ...]:
    """The function names built-in group *group_id* grants (empty for an unknown id)."""
    from app.tools.builtin_suites import builtin_suite_tools

    return builtin_suite_tools(group_id)


def builtin_functions_outside_allowlist(value: Any) -> dict[str, list[str]]:
    """Per scoped group, the functions it ships that *value* does not grant (#12192).

    *value* is a stored ``builtin_allowed_tools``; a group mapped to ``None``
    or absent grants everything and is not listed, nor is a group with nothing
    left out. Never raises on a stale value: a read must not fail on it.
    """
    if not isinstance(value, dict) or not value:
        return {}
    from app.services.builtin_tool_aliases import canonical_builtin_selection, current_function_name

    try:
        folded = canonical_builtin_selection((), value)[1] or {}
    except Exception:  # a stale stored value never fails a read
        return {}
    outside: dict[str, list[str]] = {}
    for group, names in sorted(folded.items()):
        if names is None:
            continue
        granted = {current_function_name(str(name)) for name in names}
        left_out = [fn for fn in builtin_group_functions(group) if fn not in granted]
        if left_out:
            outside[group] = left_out
    return outside


def allowlisted_groups_not_enabled(tool_ids: Iterable[str] | None, value: Any) -> list[str]:
    """The groups *value* scopes that *tool_ids* does not enable (#1428), in stored order. Pure.

    Both sides are made canonical first (a former id such as ``work_items``
    counts as its group, ``coordination``), so an allow list keyed by a former
    id of an enabled group is fine. Such an entry grants nothing: the group is
    not wired, so the functions it names never reach the agent. Never raises on
    a stale value: a read must not fail on it.
    """
    if not isinstance(value, Mapping) or not value:
        return []
    from app.services.builtin_tool_aliases import canonical_builtin_selection

    try:
        ids, allowed = canonical_builtin_selection(list(tool_ids or []), value)
    except Exception:  # a stale stored value never fails a read
        return []
    enabled = set(ids)
    return [str(group) for group, names in (allowed or {}).items() if names is not None and group not in enabled]


def not_enabled_refusal(groups: Iterable[str]) -> str:
    """The refusal of a write whose allow list scopes *groups* the agent does not enable (#1428)."""
    names = ", ".join(f"'{group}'" for group in groups)
    return (
        f"builtin_allowed_tools scopes built-in group(s) {names} that the agent does not enable: "
        "their functions would never be granted. Add the group(s) to builtin_tool_ids, or drop the entry."
    )


def without_groups_not_enabled(tool_ids: Iterable[str] | None, value: Any) -> dict[str, list[str]] | None:
    """*value* without the entries of groups *tool_ids* does not enable (#1428). Pure.

    Used when a write changes ``builtin_tool_ids`` alone: a group switched off
    takes its allow list with it instead of leaving an entry that grants nothing.
    """
    stale = set(allowlisted_groups_not_enabled(tool_ids, value))
    if not stale or not isinstance(value, Mapping):
        return value if isinstance(value, dict) and value else None
    from app.services.builtin_tool_aliases import alias_group, current_group_id

    kept = {
        str(key): names
        for key, names in value.items()
        if (alias_group(current_group_id(str(key))) or current_group_id(str(key))) not in stale
    }
    return kept or None


def not_enabled_written_or_pruned(agent: Any, before_allowed: Any) -> str | None:
    """Settle the allow-list entries of groups *agent* does not enable after a write (#1428).

    *agent* carries the canonical ``builtin_tool_ids`` / ``builtin_allowed_tools``
    the write leaves; *before_allowed* is the canonical list before it. An
    entry the write added or changed for a group not enabled is the caller's
    mistake: the refusal is answered and nothing is changed. An entry left as
    it was (its group switched off by this write, or stored so before the
    fix) is dropped, so no entry ever claims a grant that does not exist.
    ``None`` when the write may go on.
    """
    ids = list(getattr(agent, "builtin_tool_ids", None) or [])
    after = getattr(agent, "builtin_allowed_tools", None)
    stale = allowlisted_groups_not_enabled(ids, after)
    if not stale:
        return None
    written = [group for group in stale if group in groups_a_person_changed(before_allowed, after)]
    if written:
        return not_enabled_refusal(written)
    agent.builtin_allowed_tools = without_groups_not_enabled(ids, after)
    return None


def implicit_scope_groups(config: Any) -> list[str]:
    """The groups of *config* whose allow list the platform wrote, in stored order. Pure."""
    raw = config.get(IMPLICIT_SCOPE_CONFIG_KEY) if isinstance(config, Mapping) else None
    if not isinstance(raw, list):
        return []
    return list(dict.fromkeys(group for group in raw if isinstance(group, str)))


def with_implicit_scope(config: Any, add: Iterable[str] = (), remove: Iterable[str] = ()) -> dict[str, Any]:
    """A copy of *config* with *add* marked platform-written and *remove* unmarked. Pure.

    The key is dropped when no group is left, so an agent nobody scoped keeps
    a config without it.
    """
    dropped = set(remove)
    groups = [g for g in implicit_scope_groups(config) if g not in dropped]
    groups += [g for g in dict.fromkeys(add) if g not in groups and g not in dropped]
    out = dict(config) if isinstance(config, Mapping) else {}
    if groups:
        out[IMPLICIT_SCOPE_CONFIG_KEY] = groups
    else:
        out.pop(IMPLICIT_SCOPE_CONFIG_KEY, None)
    return out


def groups_a_person_changed(before: Any, after: Any) -> list[str]:
    """The groups whose list differs between two stored ``builtin_allowed_tools`` values. Pure.

    A group present on one side only counts as changed; list order does not.
    """
    left = before if isinstance(before, Mapping) else {}
    right = after if isinstance(after, Mapping) else {}
    changed: list[str] = []
    for group in dict.fromkeys([*left, *right]):
        a, b = left.get(group), right.get(group)
        if (a is None) != (b is None) or (a is not None and sorted(map(str, a)) != sorted(map(str, b or []))):
            changed.append(str(group))
    return changed


def record_person_scope(agent: Any, before: Any) -> None:
    """Unmark the groups a person's write changed on *agent* (``builtin_allowed_tools`` against *before*)."""
    marked = implicit_scope_groups(getattr(agent, "config", None))
    if not marked:
        return
    changed = set(groups_a_person_changed(before, getattr(agent, "builtin_allowed_tools", None)))
    gone = [g for g in marked if g in changed]
    if gone:
        agent.config = with_implicit_scope(agent.config, remove=gone)


def normalize_builtin_allowed_tools(value: Any) -> dict[str, list[str]] | None:
    """Validate and normalise a ``builtin_allowed_tools`` value.

    ``None`` or an empty map is ``None`` (no scoping). A group mapped to
    ``None`` is dropped (every function, same as absent). Function lists are
    de-duplicated in their given order. Raises :class:`BuiltinAllowedToolsError`
    naming the valid group ids or the group's valid functions.
    """
    if value is None:
        return None
    if not isinstance(value, dict):
        raise BuiltinAllowedToolsError(
            "builtin_allowed_tools must be an object mapping a built-in group id to a list of function names"
        )
    from app.services.builtin_tool_aliases import (
        alias_group,
        alias_scoped_functions,
        current_function_name,
        current_group_id,
        fold_allowed_map,
    )

    cleaned: dict[str, list[str]] = {}
    for group_id, functions in value.items():
        # A [DEPRECATED] renamed group id keys its current id, nothing scoped.
        group = current_group_id(str(group_id))
        if functions is None:
            continue
        # A [DEPRECATED] id keys its group, within what it granted.
        is_alias = alias_group(group) is not None
        valid = alias_scoped_functions(group) if is_alias else builtin_group_functions(group)
        if not valid:
            raise BuiltinAllowedToolsError(f"builtin_allowed_tools: unknown built-in group '{group}'")
        if not isinstance(functions, list):
            raise BuiltinAllowedToolsError(
                f"builtin_allowed_tools['{group}'] must be a list of function names or null"
            )
        # A [DEPRECATED] function name (``read_file``) is stored renamed.
        names = list(dict.fromkeys(current_function_name(str(name)) for name in functions))
        unknown = [name for name in names if name not in valid]
        if unknown:
            raise BuiltinAllowedToolsError(
                f"builtin_allowed_tools['{group}']: unknown function(s) {', '.join(sorted(unknown))}. "
                f"Valid functions: {', '.join(valid)}"
            )
        cleaned[group] = names
    # Former-id keys land on their group without widening or narrowing what
    # the other former ids of the group grant (``fold_allowed_map``).
    return fold_allowed_map(cleaned) or None
