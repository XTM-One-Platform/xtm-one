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

"""FilterGroup checks shared by the OpenCTI and OpenAEV native configurations.

Both platforms describe what a widget counts with a FilterGroup: a ``mode``
(``and`` / ``or``) and a list of filters ``{key, operator, values, mode}``.
OpenCTI adds nested ``filterGroups`` and filter values that are themselves
small objects (``regardingOf``: ``{key: "id", values: [...]}``, ``dynamic``: a
FilterGroup). The check is structural and bounded - depth, number of filters,
key syntax, operator names, value types and lengths - and returns the group in
the platform's canonical form. It never decides what a key means: the platform
does, with the viewer's access, when the widget is drawn.
"""

from __future__ import annotations

import re
from typing import Literal

from . import MAX_FILTER_DEPTH, MAX_FILTERS, NativeError

#: A filter key or attribute name: the platforms' attribute names
#: (``entity_type``, ``objectLabel``, ``rel_object-marking.internal_id``).
KEY_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.\-]{0,99}$")

MAX_VALUES = 100
MAX_VALUE_CHARS = 500

Style = Literal["opencti", "openaev"]


def empty_group(style: Style) -> dict:
    return {"mode": "and", "filters": [], "filterGroups": []} if style == "opencti" else {"mode": "and", "filters": []}


class _Budget:
    def __init__(self) -> None:
        self.filters = 0


def check_key(value: object, where: str) -> str:
    if not isinstance(value, str) or not KEY_RE.match(value):
        raise NativeError(f"{where} must be an attribute name (letters, digits, '_', '.', '-'), not {value!r:.60}.")
    return value


def _check_scalar(value: object, where: str) -> object:
    if value is None or isinstance(value, bool | int | float):
        if isinstance(value, float) and value != value:  # NaN
            raise NativeError(f"{where} is not a number.")
        return value
    if isinstance(value, str):
        if len(value) > MAX_VALUE_CHARS or "\x00" in value:
            raise NativeError(f"{where} is longer than {MAX_VALUE_CHARS} characters or holds a NUL character.")
        return value
    raise NativeError(f"{where} must be a string, a number, a boolean or null.")


def _check_value(value: object, where: str, *, style: Style, operators: frozenset[str], depth: int, budget: _Budget):
    if style == "opencti" and isinstance(value, dict):
        # A nested FilterGroup (``dynamic``) or a ``{key, values}`` pair
        # (``regardingOf``, ``dynamicRegardingOf``).
        if "filters" in value or "filterGroups" in value:
            return check_filter_group(
                value, style=style, operators=operators, where=where, _depth=depth + 1, _budget=budget
            )
        unknown = set(value) - {"key", "values", "operator", "mode"}
        if unknown:
            raise NativeError(f"{where} has unknown keys: {', '.join(sorted(unknown))}.")
        inner = value.get("values", [])
        if not isinstance(inner, list) or len(inner) > MAX_VALUES:
            raise NativeError(f"{where}.values must be a list of at most {MAX_VALUES} values.")
        out: dict = {
            "key": check_key(value.get("key"), f"{where}.key"),
            "values": [
                _check_value(v, f"{where}.values[{i}]", style=style, operators=operators, depth=depth + 1, budget=budget)
                for i, v in enumerate(inner)
            ],
        }
        for extra in ("operator", "mode"):
            if extra in value:
                out[extra] = _check_scalar(value[extra], f"{where}.{extra}")
        return out
    return _check_scalar(value, where)


def check_filter_group(
    group: object,
    *,
    style: Style,
    operators: frozenset[str],
    where: str = "filters",
    _depth: int = 1,
    _budget: _Budget | None = None,
) -> dict:
    """The FilterGroup *group* in canonical form, or :class:`NativeError`.

    ``None`` and ``{}`` are the empty group. ``style`` picks the platform's
    form: OpenCTI keys are lists and groups nest under ``filterGroups``;
    OpenAEV keys are strings and groups do not nest.
    """
    budget = _budget or _Budget()
    if group is None or group == {}:
        return empty_group(style)
    if not isinstance(group, dict):
        raise NativeError(f"{where} must be a FilterGroup object with 'mode' and 'filters'.")
    if _depth > MAX_FILTER_DEPTH:
        raise NativeError(f"{where} nests filter groups more than {MAX_FILTER_DEPTH} levels deep.")
    allowed = {"mode", "filters", "filterGroups"} if style == "opencti" else {"mode", "filters"}
    unknown = set(group) - allowed
    if unknown:
        raise NativeError(f"{where} has unknown keys: {', '.join(sorted(unknown))}.")
    mode = group.get("mode", "and")
    if mode not in ("and", "or"):
        raise NativeError(f"{where}.mode must be 'and' or 'or'.")
    filters = group.get("filters", [])
    if not isinstance(filters, list):
        raise NativeError(f"{where}.filters must be a list.")
    out_filters: list[dict] = []
    for index, item in enumerate(filters):
        at = f"{where}.filters[{index}]"
        budget.filters += 1
        if budget.filters > MAX_FILTERS:
            raise NativeError(f"A widget may use at most {MAX_FILTERS} filters.")
        if not isinstance(item, dict):
            raise NativeError(f"{at} must be an object with 'key', 'operator' and 'values'.")
        # Both platforms may give a filter an ``id`` (the OpenCTI front does in nested groups).
        known = {"key", "values", "operator", "mode", "id"}
        extra = set(item) - known
        if extra:
            raise NativeError(f"{at} has unknown keys: {', '.join(sorted(extra))}.")
        raw_key = item.get("key")
        if style == "opencti":
            keys = raw_key if isinstance(raw_key, list) else [raw_key]
            if not keys or len(keys) > 10:
                raise NativeError(f"{at}.key must name 1 to 10 attributes.")
            key: object = [check_key(k, f"{at}.key") for k in keys]
        else:
            key = check_key(raw_key, f"{at}.key")
        operator = item.get("operator", "eq")
        if operator not in operators:
            raise NativeError(f"{at}.operator '{operator!s:.40}' is not allowed; use one of: {', '.join(sorted(operators))}.")
        filter_mode = item.get("mode", "or")
        if filter_mode not in ("and", "or"):
            raise NativeError(f"{at}.mode must be 'and' or 'or'.")
        values = item.get("values", [])
        if not isinstance(values, list) or len(values) > MAX_VALUES:
            raise NativeError(f"{at}.values must be a list of at most {MAX_VALUES} values.")
        checked = {
            "key": key,
            "values": [
                _check_value(v, f"{at}.values[{i}]", style=style, operators=operators, depth=_depth, budget=budget)
                for i, v in enumerate(values)
            ],
            "operator": operator,
            "mode": filter_mode,
        }
        if isinstance(item.get("id"), str):
            checked["id"] = _check_scalar(item["id"], f"{at}.id")
        out_filters.append(checked)
    out: dict = {"mode": mode, "filters": out_filters}
    if style == "opencti":
        groups = group.get("filterGroups", [])
        if not isinstance(groups, list):
            raise NativeError(f"{where}.filterGroups must be a list.")
        out["filterGroups"] = [
            check_filter_group(
                g, style=style, operators=operators, where=f"{where}.filterGroups[{i}]", _depth=_depth + 1, _budget=budget
            )
            for i, g in enumerate(groups)
        ]
    return out
