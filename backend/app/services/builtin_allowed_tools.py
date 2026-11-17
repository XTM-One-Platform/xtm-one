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
"""

from __future__ import annotations

from typing import Any


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
        fold_allowed_map,
    )

    cleaned: dict[str, list[str]] = {}
    for group_id, functions in value.items():
        group = str(group_id)
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
