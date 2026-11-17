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

"""The declarative description of a platform's widget editor, read by one generic form in the browser.

Each platform adapter (:mod:`.opencti`, :mod:`.openaev`, :mod:`.opencrq`)
describes how its own widget editor builds and edits a widget; the studio's
form engine (``components/custom-dashboard/platform-form.ts``) knows nothing
of any platform and draws whatever it is given. Product knowledge stays here.

A description (``form``) holds:

* ``typePath``, ``types`` (each with its ``label``, its ``attrs`` - what the
  type takes, read by conditions as ``$attr`` - and the ``new`` widget it
  starts from) and the ``default`` type a new widget is;
* ``carries``: the condition a stored configuration must meet to be shown as
  the form (the engine also requires a known type); anything else opens in
  the source editor, so that nothing is lost;
* ``templates``: named expressions with parameters (a new data selection, a
  table of columns), ``filters``: how the platform writes a filter group,
  ``vars`` and ``sources``: values and schema questions
  (``dashboard_platform_schema`` kinds) the fields read;
* ``fields``: the controls, top to bottom. A value field reads and writes the
  stored configuration in place at its ``path`` (or through a ``bind``: the
  values of one filter of a group, a parameter of a ``compose``), then runs
  its ``onChange`` effects; a key no field writes is kept as it was.

Expressions are JSON: an object whose one key starts with ``$`` is an
operation (``{"$": "path"}`` reads the scope, ``{"$attr": "limit"}`` the
current type's attributes...), anything else is a literal evaluated item by
item. Conditions are objects keyed by their operator (``eq``, ``in``, ``all``...).
Labels are i18n keys of the frontend catalogs (``tests/test_dashboard_platform_forms.py``
checks that each exists in every language); a value shown as it is uses
``text`` instead.

:func:`validate_form` checks a description's structure; ``check_native``
stays the authority on what is saved.
"""

from __future__ import annotations

import re
from functools import cache
from typing import Any

FORM_VERSION = 1

# ── Building descriptions ─────────────────────────────────────────────────────


def drop_none(**items: Any) -> dict:
    return {key: value for key, value in items.items() if value is not None}


def ref(path: str = "") -> dict:
    """A value of the current scope (the configuration, or the list item being edited)."""
    return {"$": path}


def root(path: str = "") -> dict:
    return {"$root": path}


def old(path: str = "") -> dict:
    """The configuration as it was before the change an effect follows."""
    return {"$old": path}


def value(path: str = "") -> dict:
    """The value the field being changed writes."""
    return {"$value": path}


def attr(path: str) -> dict:
    return {"$attr": path}


def old_attr(path: str) -> dict:
    return {"$oldAttr": path}


def param(path: str) -> dict:
    return {"$param": path}


def var(path: str) -> dict:
    return {"$var": path}


def source(name: str) -> dict:
    """A schema source's items, ``null`` until it is answered."""
    return {"$source": name}


def template(name: str, **params: Any) -> dict:
    return {"$template": name, "with": params}


def uuid4() -> dict:
    return {"$uuid": True}


def cond(condition: dict, then: Any, otherwise: Any = None) -> dict:
    return {"$cond": condition, "then": then, "else": otherwise}


def coalesce(*exprs: Any) -> dict:
    return {"$coalesce": list(exprs)}


def either(*exprs: Any) -> dict:
    """The first truthy value (JavaScript ``||``)."""
    return {"$or": list(exprs)}


def concat(*exprs: Any) -> dict:
    return {"$concat": list(exprs)}


def unique(expr: Any) -> dict:
    """Without duplicates, empty strings and nulls."""
    return {"$unique": expr}


def length(expr: Any) -> dict:
    return {"$len": expr}


def get(table: Any, key: Any) -> dict:
    return {"$get": [table, key]}


def map_(over: Any, to: Any = None, *, name: str = "item", where: dict | None = None) -> dict:
    return drop_none(**{"$map": over, "as": name, "where": where, "to": to})


def find(over: Any, where: dict, to: Any = None, *, name: str = "item") -> dict:
    return drop_none(**{"$find": over, "as": name, "where": where, "to": to})


def lower(expr: Any) -> dict:
    return {"$lower": expr}


def filter_values(group: Any, key: Any, *, operator: str | None = None, exact: bool = False) -> dict:
    """The values of the first filter of *group* on *key* (strings only)."""
    return {"$filterValues": {"group": group, "match": match(key, operator=operator, exact=exact)}}


def match(key: Any, *, operator: str | None = None, exact: bool = False) -> dict:
    """Which filter of a group a control is about: its key (``exact``: a list of that key only) and operator."""
    return drop_none(key=key, operator=operator, exact=exact or None)


def include_if(condition: dict, then: Any) -> dict:
    """An array item kept only when *condition* holds."""
    return {"$if": condition, "then": then}


def all_(*conditions: dict) -> dict:
    return {"all": list(conditions)}


def any_(*conditions: dict) -> dict:
    return {"any": list(conditions)}


def not_(condition: dict) -> dict:
    return {"not": condition}


def eq(a: Any, b: Any) -> dict:
    return {"eq": [a, b]}


def ne(a: Any, b: Any) -> dict:
    return {"ne": [a, b]}


def in_(a: Any, b: Any) -> dict:
    return {"in": [a, b]}


def gt(a: Any, b: Any) -> dict:
    return {"gt": [a, b]}


def gte(a: Any, b: Any) -> dict:
    return {"gte": [a, b]}


def truthy(expr: Any) -> dict:
    return {"truthy": expr}


def absent(expr: Any) -> dict:
    return {"absent": expr}


def is_array(expr: Any, *, of: str | None = None, min: int | None = None) -> dict:
    return drop_none(isArray=expr, of=of, min=min)


def ends_with(expr: Any, suffix: str) -> dict:
    return {"endsWith": [expr, suffix]}


def field(kind: str, **items: Any) -> dict:
    """A field: *items* in camelCase as the frontend reads them, ``None`` left out."""
    return {"kind": kind, **drop_none(**items)}


#: A time series' interval, named in the reader's language.
INTERVAL_LABELS = {"hour": "Hour", "day": "Day", "week": "Week", "month": "Month", "quarter": "Quarter", "year": "Year"}


def options(values: list[str] | tuple[str, ...], labels: dict[str, str] | None = None, icons: dict[str, str] | None = None) -> list:
    """Fixed choices: a value with its i18n label (and its Lucide icon, drawn on a tile), or shown as it is."""
    return [
        {"value": v, "label": labels[v], **({"icon": icons[v]} if icons and v in icons else {})} if labels and v in labels else v
        for v in values
    ]


def named(values: Any, names: Any) -> dict:
    """Options for *values*, each named by the ``label`` of the schema item (``names``) whose ``key`` it is."""
    return map_(
        values,
        {"value": var("v"), "text": coalesce(find(names, eq(var("p.key"), var("v")), var("p.label"), name="p"), var("v"))},
        name="v",
    )


# ── Validating descriptions ───────────────────────────────────────────────────

_PATH_RE = re.compile(r"^(?:[A-Za-z0-9_\-]+(?:\.[A-Za-z0-9_\-]+)*)?$")

#: Expression operations: the keys each may carry besides its own.
_EXPRS: dict[str, frozenset[str]] = {
    "$": frozenset(), "$root": frozenset(), "$old": frozenset(), "$value": frozenset(), "$attr": frozenset(),
    "$oldAttr": frozenset(), "$param": frozenset(), "$var": frozenset(), "$source": frozenset(),
    "$template": frozenset({"with"}), "$uuid": frozenset(), "$cond": frozenset({"then", "else"}),
    "$coalesce": frozenset(), "$or": frozenset(), "$concat": frozenset(), "$unique": frozenset(), "$len": frozenset(),
    "$get": frozenset(), "$map": frozenset({"as", "where", "to"}), "$find": frozenset({"as", "where", "to"}),
    "$lower": frozenset(), "$filterValues": frozenset(), "$if": frozenset({"then"}),
}  # fmt: skip
_REFS = ("$", "$root", "$old", "$value", "$attr", "$oldAttr", "$param", "$var")
_CONDS: dict[str, frozenset[str]] = {
    "all": frozenset(), "any": frozenset(), "not": frozenset(), "eq": frozenset(), "ne": frozenset(), "in": frozenset(),
    "gt": frozenset(), "gte": frozenset(), "lt": frozenset(), "truthy": frozenset(), "absent": frozenset(),
    "isArray": frozenset({"of", "min"}), "isString": frozenset(), "endsWith": frozenset(),
}  # fmt: skip

_COMMON = {"kind", "id", "label", "when", "hint"}
#: An option's icon: a Lucide name.
_ICON = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_VALUE = {"path", "bind", "onChange", "default"}
_FIELDS: dict[str, set[str]] = {
    "select": _VALUE | {"options", "sort", "display"},
    "multiselect": _VALUE | {"options", "placeholder", "emptyLabel"},
    "segmented": _VALUE | {"options"},
    "text": _VALUE | {"max", "empty", "ariaLabel"},
    "number": _VALUE | {"min", "max", "empty", "decimal"},
    "switch": _VALUE | {"invert"},
    "day": _VALUE,
    "values": _VALUE | {"filterKey", "single"},
    "filters": {"path", "keys", "hidden", "perspective"},
    "columns": {"path", "items", "valueKey", "labelKey", "available", "names", "reset"},
    "sorts": {"path", "choices", "directions", "new", "max", "fieldKey", "directionKey"},
    "row": {"fields", "columns"},
    "list": {"path", "title", "removeLabel", "add", "max", "min", "count", "footerWhen", "badge", "vars", "sources", "fields"},
    "section": {"path", "title", "vars", "sources", "fields"},
    "compose": {"path", "params", "build", "fields"},
    "note": {"text"},
}
_LABEL_KEYS = ("label", "title", "removeLabel", "count", "placeholder", "emptyLabel", "ariaLabel", "text")
_SOURCE_KINDS = ("entity_types", "filter_keys", "attributes", "saved_filters")
_EFFECTS = {"set", "remove", "truncate", "each", "if", "change", "rebuild"}


class FormError(ValueError):
    """A malformed description (a bug in an adapter, never a person's input)."""


def _path(value: object, where: str) -> None:
    if not isinstance(value, str) or not _PATH_RE.match(value):
        raise FormError(f"{where}: {value!r} is not a dotted path.")


def _expr(value: object, where: str, form: dict) -> None:
    if isinstance(value, list):
        for index, item in enumerate(value):
            _expr(item, f"{where}[{index}]", form)
        return
    if not isinstance(value, dict):
        if value is not None and not isinstance(value, str | int | float | bool):
            raise FormError(f"{where}: {type(value).__name__} is not JSON.")
        return
    ops = [key for key in value if key.startswith("$")]
    if not ops:
        for key, item in value.items():
            _expr(item, f"{where}.{key}", form)
        return
    if len(ops) != 1 or ops[0] not in _EXPRS:
        raise FormError(f"{where}: unknown expression {sorted(value)}.")
    op = ops[0]
    extra = set(value) - {op} - _EXPRS[op]
    if extra:
        raise FormError(f"{where}: {op} does not take {sorted(extra)}.")
    arg = value[op]
    if op in _REFS:
        _path(arg, f"{where}.{op}")
    elif op == "$source":
        if arg not in _all_sources(form):
            raise FormError(f"{where}: unknown source {arg!r}.")
    elif op == "$template":
        if arg not in form.get("templates", {}):
            raise FormError(f"{where}: unknown template {arg!r}.")
        if not isinstance(value.get("with", {}), dict):
            raise FormError(f"{where}.with must be an object.")
        _expr(value.get("with", {}), f"{where}.with", form)
    elif op in ("$cond", "$if"):
        _cond(arg, f"{where}.{op}", form)
        _expr(value.get("then"), f"{where}.then", form)
        _expr(value.get("else"), f"{where}.else", form)
    elif op in ("$map", "$find"):
        _expr(arg, f"{where}.{op}", form)
        if "where" in value:
            _cond(value["where"], f"{where}.where", form)
        _expr(value.get("to"), f"{where}.to", form)
        if op == "$find" and "where" not in value:
            raise FormError(f"{where}: $find needs a where.")
    elif op == "$filterValues":
        if not isinstance(arg, dict) or set(arg) != {"group", "match"}:
            raise FormError(f"{where}: $filterValues takes group and match.")
        _expr(arg["group"], f"{where}.group", form)
        _match(arg["match"], f"{where}.match", form)
    elif op in ("$coalesce", "$or", "$concat", "$get"):
        if not isinstance(arg, list):
            raise FormError(f"{where}: {op} takes a list.")
        _expr(arg, f"{where}.{op}", form)
    else:
        _expr(arg, f"{where}.{op}", form)


def _cond(value: object, where: str, form: dict) -> None:
    if not isinstance(value, dict):
        raise FormError(f"{where}: a condition is an object.")
    ops = [key for key in value if key in _CONDS]
    if len(ops) != 1 or set(value) - {ops[0]} - _CONDS[ops[0]]:
        raise FormError(f"{where}: unknown condition {sorted(value)}.")
    op = ops[0]
    arg = value[op]
    if op in ("all", "any"):
        if not isinstance(arg, list) or not arg:
            raise FormError(f"{where}.{op} takes a list of conditions.")
        for index, item in enumerate(arg):
            _cond(item, f"{where}.{op}[{index}]", form)
    elif op == "not":
        _cond(arg, f"{where}.not", form)
    elif op in ("eq", "ne", "in", "gt", "gte", "lt", "endsWith"):
        if not isinstance(arg, list) or len(arg) != 2:
            raise FormError(f"{where}.{op} takes two operands.")
        _expr(arg, f"{where}.{op}", form)
    else:
        _expr(arg, f"{where}.{op}", form)


def _match(value: object, where: str, form: dict) -> None:
    if not isinstance(value, dict) or "key" not in value or set(value) - {"key", "operator", "exact"}:
        raise FormError(f"{where}: a filter match takes key, operator, exact.")
    _expr(value["key"], f"{where}.key", form)


def _all_sources(form: dict) -> set[str]:
    names = set(form.get("sources", {}))

    def walk(fields: list) -> None:
        for item in fields:
            names.update(item.get("sources", {}))
            walk(item.get("fields", []))

    walk(form.get("fields", []))
    return names


def _sources(value: object, where: str, form: dict) -> None:
    if not isinstance(value, dict):
        raise FormError(f"{where} must be an object.")
    for name, question in value.items():
        at = f"{where}.{name}"
        if not isinstance(question, dict) or question.get("kind") not in _SOURCE_KINDS:
            raise FormError(f"{at}: a source asks one of {', '.join(_SOURCE_KINDS)}.")
        if set(question) - {"kind", "perspective", "types", "when"}:
            raise FormError(f"{at}: unknown keys {sorted(set(question) - {'kind', 'perspective', 'types', 'when'})}.")
        _expr(question.get("perspective"), f"{at}.perspective", form)
        _expr(question.get("types"), f"{at}.types", form)
        if "when" in question:
            _cond(question["when"], f"{at}.when", form)


def _effects(value: object, where: str, form: dict, ids: set[str]) -> None:
    if not isinstance(value, list):
        raise FormError(f"{where} must be a list of effects.")
    for index, effect in enumerate(value):
        at = f"{where}[{index}]"
        kinds = set(effect) & _EFFECTS if isinstance(effect, dict) else set()
        if len(kinds) != 1:
            raise FormError(f"{at}: an effect is one of {', '.join(sorted(_EFFECTS))}.")
        kind = kinds.pop()
        allowed = {
            "set": {"set", "value"}, "remove": {"remove"}, "truncate": {"truncate", "max"},
            "each": {"each", "when", "do"}, "if": {"if", "then", "else"}, "change": {"change", "value"},
            "rebuild": {"rebuild", "carry"},
        }[kind]  # fmt: skip
        if set(effect) - allowed:
            raise FormError(f"{at}: {kind} does not take {sorted(set(effect) - allowed)}.")
        if kind in ("set", "remove", "truncate", "each"):
            _path(effect[kind], f"{at}.{kind}")
        if kind == "change" and effect["change"] not in ids:
            raise FormError(f"{at}: no top-level field has the id {effect['change']!r}.")
        for key in ("value", "max"):
            if key in effect:
                _expr(effect[key], f"{at}.{key}", form)
        if kind == "rebuild":
            if not isinstance(effect["rebuild"], dict):
                raise FormError(f"{at}.rebuild must be the new widget's parameters.")
            _expr(effect["rebuild"], f"{at}.rebuild", form)
            for index_, path in enumerate(effect.get("carry", [])):
                _path(path, f"{at}.carry[{index_}]")
        for key in ("when", "if"):
            if key in effect:
                _cond(effect[key], f"{at}.{key}", form)
        for key in ("do", "then", "else"):
            if key in effect:
                _effects(effect[key], f"{at}.{key}", form, ids)


def _label(value: object, where: str, form: dict) -> None:
    if isinstance(value, str):
        return
    if isinstance(value, dict) and ("$cond" in value or "$get" in value):
        _expr(value, where, form)
        return
    raise FormError(f"{where}: a label is an i18n key, a $cond or a $get of keys.")


def _fields(fields: object, where: str, form: dict, ids: set[str]) -> None:
    if not isinstance(fields, list):
        raise FormError(f"{where} must be a list of fields.")
    for index, item in enumerate(fields):
        at = f"{where}[{index}]"
        if not isinstance(item, dict) or item.get("kind") not in _FIELDS:
            raise FormError(f"{at}: a field's kind is one of {', '.join(_FIELDS)}.")
        kind = item["kind"]
        extra = set(item) - _COMMON - _FIELDS[kind]
        if extra:
            raise FormError(f"{at}: a {kind} field does not take {sorted(extra)}.")
        if "path" in item:
            _path(item["path"], f"{at}.path")
        if kind in ("select", "multiselect", "segmented", "text", "number", "switch", "day", "values") and not (
            "path" in item or "bind" in item
        ):
            raise FormError(f"{at}: a {kind} field writes a path or a bind.")
        if kind in ("filters", "columns", "sorts", "list", "section", "compose") and "path" not in item:
            raise FormError(f"{at}: a {kind} field edits a path.")
        for key in _LABEL_KEYS:
            if key in item:
                _label(item[key], f"{at}.{key}", form)
        hint = item.get("hint")
        if isinstance(hint, dict) and "text" in hint:
            _label(hint["text"], f"{at}.hint.text", form)
            if "when" in hint:
                _cond(hint["when"], f"{at}.hint.when", form)
        elif hint is not None:
            _label(hint, f"{at}.hint", form)
        if "when" in item:
            _cond(item["when"], f"{at}.when", form)
        bind = item.get("bind")
        if bind is not None:
            if not isinstance(bind, dict) or len(bind) != 1 or not set(bind) <= {"filterValues", "param"}:
                raise FormError(f"{at}.bind is a filterValues or a param binding.")
            spec = bind.get("filterValues")
            if spec is not None:
                if not isinstance(spec, dict) or set(spec) - {"group", "match", "new", "single", "clearOthers"} or "match" not in spec:
                    raise FormError(f"{at}.bind.filterValues takes group, match, new, single, clearOthers.")
                _path(spec.get("group"), f"{at}.bind.filterValues.group")
                _match(spec["match"], f"{at}.bind.filterValues.match", form)
                _expr(spec.get("new"), f"{at}.bind.filterValues.new", form)
        for key in ("options", "default", "keys", "available", "names", "reset", "choices", "directions", "new", "min", "max"):
            if key in item:
                _expr(item[key], f"{at}.{key}", form)
        if kind in ("select", "multiselect", "segmented") and isinstance(item.get("options"), list):
            for index_, option in enumerate(item["options"]):
                if isinstance(option, dict) and "label" in option:
                    _label(option["label"], f"{at}.options[{index_}].label", form)
                if isinstance(option, dict) and "icon" in option and not (isinstance(option["icon"], str) and _ICON.match(option["icon"])):
                    raise FormError(f"{at}.options[{index_}].icon is a Lucide icon name.")
        if kind == "select" and item.get("display", "list") not in ("list", "tiles"):
            raise FormError(f"{at}.display is list or tiles.")
        if "onChange" in item:
            _effects(item["onChange"], f"{at}.onChange", form, ids)
        for index_, hidden in enumerate(item.get("hidden", [])):
            _match(hidden, f"{at}.hidden[{index_}]", form)
        for key in ("vars", "params"):
            for name, expr in item.get(key, {}).items():
                _expr(expr, f"{at}.{key}.{name}", form)
        if "sources" in item:
            _sources(item["sources"], f"{at}.sources", form)
        if "build" in item:
            _expr(item["build"], f"{at}.build", form)
        for key in ("footerWhen",):
            if key in item:
                _cond(item[key], f"{at}.{key}", form)
        if "badge" in item:
            _label(item["badge"].get("text"), f"{at}.badge.text", form)
            if "when" in item["badge"]:
                _cond(item["badge"]["when"], f"{at}.badge.when", form)
        for index_, add in enumerate(item.get("add", [])):
            if set(add) - {"label", "item", "when", "disabled"} or "item" not in add:
                raise FormError(f"{at}.add[{index_}] takes label, item, when, disabled.")
            _label(add.get("label"), f"{at}.add[{index_}].label", form)
            _expr(add["item"], f"{at}.add[{index_}].item", form)
            for key in ("when", "disabled"):
                if key in add:
                    _cond(add[key], f"{at}.add[{index_}].{key}", form)
        if "fields" in item:
            _fields(item["fields"], f"{at}.fields", form, ids)


def _top_ids(fields: list) -> set[str]:
    """The ids an effect may ``change``: fields of the configuration's own scope (rows included)."""
    out: set[str] = set()
    for item in fields:
        if item.get("id"):
            out.add(item["id"])
        if item.get("kind") == "row":
            out |= _top_ids(item.get("fields", []))
    return out


def validate_form(form: object) -> dict:
    """*form* if it is a well-formed description, else :class:`FormError`."""
    if not isinstance(form, dict):
        raise FormError("A form description is an object.")
    allowed = {"version", "typePath", "types", "default", "carries", "templates", "filters", "vars", "sources", "fields"}
    if set(form) - allowed:
        raise FormError(f"Unknown keys: {sorted(set(form) - allowed)}.")
    if form.get("version") != FORM_VERSION:
        raise FormError(f"version must be {FORM_VERSION}.")
    _path(form.get("typePath"), "typePath")
    types = form.get("types")
    if not isinstance(types, list) or not types:
        raise FormError("types must list the widget types.")
    names = []
    for index, item in enumerate(types):
        if not isinstance(item, dict) or set(item) - {"value", "label", "attrs", "new"} or not isinstance(item.get("value"), str):
            raise FormError(f"types[{index}] takes value, label, attrs, new.")
        _label(item.get("label", item["value"]), f"types[{index}].label", form)
        if not isinstance(item.get("attrs", {}), dict):
            raise FormError(f"types[{index}].attrs must be an object.")
        _expr(item.get("new"), f"types[{index}].new", form)
        names.append(item["value"])
    if len(set(names)) != len(names):
        raise FormError("types are listed once each.")
    default = form.get("default")
    if not isinstance(default, dict) or default.get("type") not in names or set(default) - {"type", "params"}:
        raise FormError("default names a type (and its params).")
    if "carries" in form:
        _cond(form["carries"], "carries", form)
    for name, body in form.get("templates", {}).items():
        _expr(body, f"templates.{name}", form)
    if "filters" in form:
        _dialect(form["filters"], form)
    for name, expr in form.get("vars", {}).items():
        _expr(expr, f"vars.{name}", form)
    _sources(form.get("sources", {}), "sources", form)
    _fields(form.get("fields"), "fields", form, _top_ids(form.get("fields", [])))
    return form


def _dialect(dialect: object, form: dict) -> None:
    allowed = {"keyForm", "nested", "maxDepth", "ids", "newFilterMode", "groupMode", "operators", "defaultOperator",
               "dateInput", "dateHint", "searched", "pairs"}  # fmt: skip
    if not isinstance(dialect, dict) or set(dialect) - allowed:
        raise FormError(f"filters takes {sorted(allowed)}.")
    if dialect.get("keyForm") not in ("list", "string") or dialect.get("dateInput") not in ("text", "datetime"):
        raise FormError("filters.keyForm is list or string, filters.dateInput text or datetime.")
    for key in ("operators", "defaultOperator"):
        for index, rule in enumerate(dialect.get(key, [])):
            if not isinstance(rule, dict) or set(rule) - {"when", "value"} or "value" not in rule:
                raise FormError(f"filters.{key}[{index}] takes when and value.")
            if "when" in rule:
                _cond(rule["when"], f"filters.{key}[{index}].when", form)
            _expr(rule["value"], f"filters.{key}[{index}].value", form)
    if "searched" in dialect:
        _cond(dialect["searched"], "filters.searched", form)
    if "dateHint" in dialect:
        _label(dialect["dateHint"], "filters.dateHint", form)
    for key, parts in dialect.get("pairs", {}).items():
        for index, part in enumerate(parts):
            if set(part) != {"key", "label", "search"}:
                raise FormError(f"filters.pairs.{key}[{index}] takes key, label, search.")
            _label(part["label"], f"filters.pairs.{key}[{index}].label", form)


def _label_strings(value: object) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict) and "$cond" in value:
        return _label_strings(value.get("then")) + _label_strings(value.get("else"))
    if isinstance(value, dict) and "$get" in value and isinstance(value["$get"][0], dict):
        return [label for item in value["$get"][0].values() for label in _label_strings(item)]
    return []


def form_labels(form: dict) -> set[str]:
    """Every i18n key the description makes the frontend translate."""
    out: set[str] = set()
    for item in form["types"]:
        out.update(_label_strings(item.get("label")))
    dialect = form.get("filters", {})
    out.update(_label_strings(dialect.get("dateHint")))
    for parts in dialect.get("pairs", {}).values():
        out.update(part["label"] for part in parts)

    def walk(fields: list) -> None:
        for item in fields:
            for key in _LABEL_KEYS:
                out.update(_label_strings(item.get(key)))
            hint = item.get("hint")
            out.update(_label_strings(hint.get("text") if isinstance(hint, dict) else hint))
            for key in ("options", "directions"):
                if isinstance(item.get(key), list):
                    out.update(o["label"] for o in item[key] if isinstance(o, dict) and isinstance(o.get("label"), str))
                elif isinstance(item.get(key), dict) and isinstance(item[key].get("to"), dict):
                    # Options mapped from a list, labelled from a table.
                    out.update(_label_strings(item[key]["to"].get("label")))
            for add in item.get("add", []):
                out.update(_label_strings(add.get("label")))
            out.update(_label_strings(item.get("badge", {}).get("text")))
            walk(item.get("fields", []))

    walk(form["fields"])
    return out


@cache
def platform_forms() -> list[dict]:
    """Each platform's description, validated, in :data:`PLATFORM_SOURCES` order."""
    from . import PLATFORM_NAMES, PLATFORM_SOURCES, openaev, opencrq, opencti

    built = {"opencti": opencti.form, "openaev": openaev.form, "opencrq": opencrq.form}
    return [{"source": s, "name": PLATFORM_NAMES[s], "form": validate_form(built[s]())} for s in PLATFORM_SOURCES]


__all__ = ["FORM_VERSION", "FormError", "form_labels", "platform_forms", "validate_form"]
