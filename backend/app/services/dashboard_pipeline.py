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

"""A derived data source's steps, applied one after another on the closed shapes.

A ``derived`` data source reads another one (``extends``) and passes its
result through a short list of steps, each a closed operation on the closed
shapes of ``dashboard_live`` (``number`` / ``series`` / ``distribution`` /
``rows`` / ``text``)::

    where   {field, operator, value}         keep the rows / items / points that match
    sort    {field, order}             order them
    head    {count} / tail {count}     keep the first / last ones
    fields  {keep}                     rows: keep these columns
    rename  {field, to, label}         rows: rename a column
    eval    {field, expression, label} rows: a column computed from the others
    top     {field, count}             rows -> distribution: count by value
    stats   {agg, field, by}           rows / distribution / series -> number,
                                       or rows -> distribution when ``by`` is set

No step runs code: an ``eval`` expression is parsed here into arithmetic on
the row's numbers (``+ - * /``, parentheses, ``round``, ``abs``, ``min``,
``max``) and nothing else. Every result is closed again
(``closed_static_data``), so a pipeline can never make a shape larger or
stranger than a static source could.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

MAX_STEPS = 20
MAX_EXPRESSION_CHARS = 200

#: A field a step reads or writes; a name starting with "_" is a row's own (its link, its icon), never a step's.
FieldName = Annotated[str, Field(min_length=1, max_length=60, pattern=r"^[^_]")]


class PipelineError(ValueError):
    """A step that does not apply to the data it receives; the message says which and why."""


class _Step(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class WhereStep(_Step):
    op: Literal["where"]
    field: FieldName
    operator: Literal["eq", "ne", "gt", "gte", "lt", "lte", "contains", "in"] = "eq"
    value: Annotated[str | float | int | bool | list[str | float | int] | None, Field(default=None)]


class SortStep(_Step):
    op: Literal["sort"]
    field: FieldName
    order: Literal["asc", "desc"] = "asc"


class HeadStep(_Step):
    op: Literal["head", "tail"]
    count: Annotated[int, Field(ge=1, le=1000)]


class FieldsStep(_Step):
    op: Literal["fields"]
    keep: Annotated[list[FieldName], Field(min_length=1, max_length=20)]


class RenameStep(_Step):
    op: Literal["rename"]
    field: FieldName
    to: FieldName
    label: Annotated[str | None, Field(default=None, max_length=60)]


class EvalStep(_Step):
    op: Literal["eval"]
    field: FieldName
    expression: Annotated[str, Field(min_length=1, max_length=MAX_EXPRESSION_CHARS)]
    label: Annotated[str | None, Field(default=None, max_length=60)]


class TopStep(_Step):
    op: Literal["top"]
    field: FieldName
    count: Annotated[int, Field(default=10, ge=1, le=50)]


class StatsStep(_Step):
    op: Literal["stats"]
    agg: Literal["count", "sum", "avg", "min", "max", "last"]
    field: FieldName | None = None
    by: FieldName | None = None


Step = Annotated[
    WhereStep | SortStep | HeadStep | FieldsStep | RenameStep | EvalStep | TopStep | StatsStep,
    Field(discriminator="op"),
]


# ── eval: arithmetic on a row's numbers, parsed here ─────────────────────────

_TOKEN = re.compile(r"\s*(?:(\d+(?:\.\d+)?)|([A-Za-z_][A-Za-z0-9_]*)|(.))")
_FUNCTIONS = {"round": (1, 2), "abs": (1, 1), "min": (2, 2), "max": (2, 2)}


def parse_expression(source: str) -> Any:
    """``source`` as a tree of ``("num", x)``, ``("field", name)``, ``("op", op, a, b)``, ``("call", f, args)``."""
    tokens: list[tuple[str, str]] = []
    for number, name, other in _TOKEN.findall(source):
        if number:
            tokens.append(("num", number))
        elif name:
            tokens.append(("name", name))
        elif other.strip():
            if other not in "+-*/(),":
                raise PipelineError(f"eval: '{other}' is not allowed (numbers, fields, + - * / ( ) and round, abs, min, max)")
            tokens.append(("sym", other))
    pos = 0

    def peek() -> tuple[str, str] | None:
        return tokens[pos] if pos < len(tokens) else None

    def take() -> tuple[str, str]:
        nonlocal pos
        if pos >= len(tokens):
            raise PipelineError("eval: the expression ends too early")
        pos += 1
        return tokens[pos - 1]

    def expr() -> Any:
        node = term()
        while (tok := peek()) and tok[1] in "+-" and tok[0] == "sym":
            take()
            node = ("op", tok[1], node, term())
        return node

    def term() -> Any:
        node = unary()
        while (tok := peek()) and tok[1] in "*/" and tok[0] == "sym":
            take()
            node = ("op", tok[1], node, unary())
        return node

    def unary() -> Any:
        tok = peek()
        if tok == ("sym", "-"):
            take()
            return ("op", "-", ("num", 0.0), unary())
        return atom()

    def atom() -> Any:
        kind, value = take()
        if kind == "num":
            return ("num", float(value))
        if kind == "name":
            if peek() == ("sym", "("):
                if value not in _FUNCTIONS:
                    raise PipelineError(f"eval: unknown function '{value}'")
                take()
                args = [expr()]
                while peek() == ("sym", ","):
                    take()
                    args.append(expr())
                if take() != ("sym", ")"):
                    raise PipelineError("eval: a ')' is missing")
                low, high = _FUNCTIONS[value]
                if not low <= len(args) <= high:
                    raise PipelineError(f"eval: {value} takes {low}-{high} arguments")
                return ("call", value, args)
            return ("field", value)
        if (kind, value) == ("sym", "("):
            node = expr()
            if take() != ("sym", ")"):
                raise PipelineError("eval: a ')' is missing")
            return node
        raise PipelineError(f"eval: unexpected '{value}'")

    tree = expr()
    if pos != len(tokens):
        raise PipelineError(f"eval: unexpected '{tokens[pos][1]}'")
    return tree


def _evaluate(node: Any, row: dict) -> float | None:
    kind = node[0]
    if kind == "num":
        return node[1]
    if kind == "field":
        return _number(row.get(node[1]))
    if kind == "call":
        args = [_evaluate(arg, row) for arg in node[2]]
        if any(arg is None for arg in args):
            return None
        if node[1] == "round":
            return round(args[0], int(args[1]) if len(args) > 1 else 0)
        if node[1] == "abs":
            return abs(args[0])
        return min(args) if node[1] == "min" else max(args)
    left, right = _evaluate(node[2], row), _evaluate(node[3], row)
    if left is None or right is None:
        return None
    if node[1] == "+":
        return left + right
    if node[1] == "-":
        return left - right
    if node[1] == "*":
        return left * right
    return left / right if right else None


# ── Steps on the shapes ──────────────────────────────────────────────────────


def _number(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return float(value) if math.isfinite(value) else None
    if isinstance(value, str):
        try:
            parsed = float(value.strip())
        except ValueError:
            return None
        return parsed if math.isfinite(parsed) else None
    return None


def _show(value: float) -> str:
    return str(int(value)) if value.is_integer() else f"{value:.4g}"


def _matches(actual: object, step: WhereStep) -> bool:
    wanted = step.value
    if step.operator == "in":
        options = wanted if isinstance(wanted, list) else [wanted]
        return any(_equal(actual, option) for option in options)
    if step.operator in ("eq", "ne"):
        return _equal(actual, wanted) == (step.operator == "eq")
    if step.operator == "contains":
        return str(wanted).lower() in str(actual if actual is not None else "").lower()
    left, right = _number(actual), _number(wanted)
    if left is None or right is None:
        return False
    return {"gt": left > right, "gte": left >= right, "lt": left < right, "lte": left <= right}[step.operator]


def _equal(actual: object, wanted: object) -> bool:
    left, right = _number(actual), _number(wanted)
    if left is not None and right is not None:
        return left == right
    return str(actual if actual is not None else "").strip().lower() == str(wanted if wanted is not None else "").strip().lower()


def _sort_key(value: object) -> tuple[int, float, str]:
    parsed = _number(value)
    return (0, parsed, "") if parsed is not None else (1, 0.0, str(value or "").lower())


def _items_of(shape: str, data: dict, where: str) -> list[dict]:
    if shape == "rows":
        return [dict(row) for row in data.get("rows") or []]
    if shape == "distribution":
        return [dict(item) for item in data.get("items") or []]
    raise PipelineError(f"{where} applies to rows or a distribution, not to {shape}")


def _with_items(shape: str, data: dict, items: list[dict]) -> dict:
    return {**data, "rows": items} if shape == "rows" else {**data, "items": items}


def _apply(step: Any, shape: str, data: dict, where: str) -> tuple[str, dict]:
    if isinstance(step, WhereStep | SortStep | HeadStep) and shape == "series":
        series = []
        for one in data.get("series") or []:
            _, points = _apply(step, "distribution", {"items": [{"label": p["x"], "value": p["y"]} for p in one.get("points") or []]}, where)
            series.append({**one, "points": [{"x": i["label"], "y": i["value"]} for i in points["items"]]})
        return shape, {"series": series}
    if isinstance(step, WhereStep):
        field = _alias(shape, step.field)
        items = [item for item in _items_of(shape, data, where) if _matches(item.get(field), step)]
        return shape, _with_items(shape, data, items)
    if isinstance(step, SortStep):
        field = _alias(shape, step.field)
        items = sorted(_items_of(shape, data, where), key=lambda item: _sort_key(item.get(field)), reverse=step.order == "desc")
        return shape, _with_items(shape, data, items)
    if isinstance(step, HeadStep):
        items = _items_of(shape, data, where)
        return shape, _with_items(shape, data, items[: step.count] if step.op == "head" else items[-step.count :])
    if isinstance(step, StatsStep):
        return _stats(step, shape, data, where)
    if shape != "rows":
        raise PipelineError(f"{where} ({step.op}) applies to rows, not to {shape}")
    columns = list(data.get("columns") or [])
    rows = [dict(row) for row in data.get("rows") or []]
    if isinstance(step, FieldsStep):
        keep = set(step.keep)
        return shape, {**data, "columns": [c for c in columns if c["key"] in keep], "rows": [{k: v for k, v in r.items() if k in keep or k.startswith("_")} for r in rows]}
    if isinstance(step, RenameStep):
        columns = [{"key": step.to, "label": step.label or c["label"]} if c["key"] == step.field else c for c in columns]
        rows = [{(step.to if k == step.field else k): v for k, v in r.items()} for r in rows]
        return shape, {**data, "columns": columns, "rows": rows}
    if isinstance(step, EvalStep):
        tree = parse_expression(step.expression)
        for row in rows:
            value = _evaluate(tree, row)
            row[step.field] = "" if value is None else _show(value)
        if not any(c["key"] == step.field for c in columns):
            columns.append({"key": step.field, "label": step.label or step.field})
        return shape, {**data, "columns": columns, "rows": rows}
    if isinstance(step, TopStep):
        counts = Counter(str(row.get(step.field) or "-") for row in rows)
        return "distribution", {"items": [{"label": label, "value": n} for label, n in counts.most_common(step.count)]}
    raise PipelineError(f"{where}: unknown step")  # pragma: no cover - the union is closed


def _alias(shape: str, field: str) -> str:
    """A distribution's ``x``/``y`` are its ``label``/``value`` (a series' points are read as one)."""
    if shape in ("distribution", "series"):
        return {"x": "label", "y": "value", "name": "label"}.get(field, field)
    return field


def _aggregate(agg: str, values: list[float]) -> float | None:
    if agg == "count":
        return float(len(values))
    if not values:
        return None
    if agg == "sum":
        return sum(values)
    if agg == "avg":
        return sum(values) / len(values)
    if agg == "min":
        return min(values)
    if agg == "max":
        return max(values)
    return values[-1]


def _stats(step: StatsStep, shape: str, data: dict, where: str) -> tuple[str, dict]:
    if shape == "series":
        first = (data.get("series") or [{}])[0]
        values = [y for y in (_number(p.get("y")) for p in first.get("points") or []) if y is not None]
        result = _aggregate(step.agg, values)
        return "number", {"value": result if result is not None else 0.0}
    if shape == "distribution":
        values = [v for v in (_number(i.get("value")) for i in data.get("items") or []) if v is not None]
        result = _aggregate(step.agg, values)
        return "number", {"value": result if result is not None else 0.0}
    if shape != "rows":
        raise PipelineError(f"{where} (stats) applies to rows, a distribution or a series, not to {shape}")
    if step.agg != "count" and not step.field:
        raise PipelineError(f"{where}: stats {step.agg} needs a field")
    rows = data.get("rows") or []

    def values_of(group: list[dict]) -> list[float]:
        if step.agg == "count":
            return [1.0] * len(group)
        return [v for v in (_number(r.get(step.field)) for r in group) if v is not None]

    if step.by:
        groups: dict[str, list[dict]] = {}
        for row in rows:
            groups.setdefault(str(row.get(step.by) or "-"), []).append(row)
        items = [{"label": label, "value": _aggregate(step.agg, values_of(group)) or 0.0} for label, group in groups.items()]
        return "distribution", {"items": items}
    result = _aggregate(step.agg, values_of(list(rows)))
    return "number", {"value": result if result is not None else 0.0}


def run_pipeline(steps: list[Any], shape: str, data: dict) -> tuple[str, dict]:
    """The result of *steps* on ``(shape, data)``, closed again; :class:`PipelineError` when a step does not apply."""
    from app.services.dashboard_live import closed_static_data

    if shape == "text" and steps:
        raise PipelineError("steps.0: a text result has no steps")
    for index, step in enumerate(steps):
        shape, data = _apply(step, shape, data, f"steps.{index}")
    # The data came out of a builder (a static source, an answer or a platform's): its links are kept.
    return shape, closed_static_data(shape, data, reclosing=True)
