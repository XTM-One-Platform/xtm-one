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

"""Sandboxed expression evaluator for the flow builder.

The UE "pure function" layer without the LLM tax: `util.expression` evaluates
a single expression over already-rendered ``{{...}}`` values — math, string
ops, JSON access, small collection helpers — using a strict AST whitelist.

Security model: the expression is parsed with :mod:`ast` and only explicitly
allowed node types evaluate. No attribute access, no subscript on arbitrary
objects beyond dict/list/str, no imports, no comprehension over anything but
the expression's own values, no dunder anything. Function calls resolve ONLY
against :data:`ALLOWED_FUNCTIONS`. Evaluation is depth- and size-capped so a
hostile expression cannot burn the worker.
"""

from __future__ import annotations

import ast
import json
import math
from typing import Any

MAX_EXPRESSION_LENGTH = 2000
MAX_DEPTH = 25
# `**` with huge operands is the classic CPU/memory bomb.
MAX_POW_EXPONENT = 512
MAX_REPEAT = 10_000
# Per-operation caps on the *literal* operands are not enough: they launder
# through composition. `(9**512)**512` keeps every visible exponent under
# MAX_POW_EXPONENT while multiplying the base's bit width by 512 each level, and
# `("a" * 10_000) * 10_000` keeps every multiplier under MAX_REPEAT while
# producing 100 MB. So the growing operators bound the *predicted* result before
# computing it, and `_guard_size` re-measures every value the evaluator hands
# back — composition is then bounded level by level, on the way out.
MAX_RESULT_BITS = 8192  # ~2466 decimal digits; no real flow expression is close
MAX_SEQUENCE_LENGTH = 100_000  # characters or items in any intermediate


def _json_parse(text: Any) -> Any:
    return json.loads(text) if isinstance(text, str) else text


ALLOWED_FUNCTIONS: dict[str, Any] = {
    # numbers
    "abs": abs,
    "round": round,
    "min": min,
    "max": max,
    "sum": sum,
    "int": int,
    "float": float,
    "ceil": math.ceil,
    "floor": math.floor,
    # text
    "str": str,
    "len": len,
    "upper": lambda s: str(s).upper(),
    "lower": lambda s: str(s).lower(),
    "strip": lambda s: str(s).strip(),
    "title": lambda s: str(s).title(),
    "replace": lambda s, a, b: str(s).replace(str(a), str(b)),
    "split": lambda s, sep=None: str(s).split(sep),
    "join": lambda sep, items: str(sep).join(str(i) for i in items),
    "startswith": lambda s, p: str(s).startswith(str(p)),
    "endswith": lambda s, p: str(s).endswith(str(p)),
    "contains": lambda haystack, needle: str(needle) in str(haystack),
    "slice": lambda s, start, end=None: s[int(start) : (int(end) if end is not None else None)],
    # collections / JSON
    "json": _json_parse,
    "get": lambda obj, key, default=None: (
        obj.get(str(key), default) if isinstance(obj, dict) else default
    ),
    "at": lambda items, index: (
        items[int(index)] if isinstance(items, (list, str)) and -len(items) <= int(index) < len(items) else None
    ),
    "first": lambda items: items[0] if items else None,
    "last": lambda items: items[-1] if items else None,
    "sort": lambda items: sorted(items),
    "unique": lambda items: list(dict.fromkeys(items)),
    "append": lambda items, value: [*list(items), value],
    "count": lambda items: len(items),
    "keys": lambda obj: list(obj.keys()) if isinstance(obj, dict) else [],
    "values": lambda obj: list(obj.values()) if isinstance(obj, dict) else [],
    "bool": bool,
}

_ALLOWED_NODES = (
    ast.Expression,
    ast.Constant,
    ast.Name,
    ast.Load,
    ast.Call,
    # `ast.walk` visits keyword nodes too, so without this every call using
    # kwargs (`round(x, ndigits=2)`) was refused before the evaluator's own
    # kwargs handling could run.
    ast.keyword,
    ast.BinOp,
    ast.UnaryOp,
    ast.BoolOp,
    ast.Compare,
    ast.IfExp,
    ast.List,
    ast.Tuple,
    ast.Dict,
    ast.Subscript,
    # operators
    ast.Add, ast.Sub, ast.Mult, ast.Div, ast.FloorDiv, ast.Mod, ast.Pow,
    ast.USub, ast.UAdd, ast.Not,
    ast.And, ast.Or,
    ast.Eq, ast.NotEq, ast.Lt, ast.LtE, ast.Gt, ast.GtE, ast.In, ast.NotIn,
)


class ExpressionError(ValueError):
    """A refused or failed expression — the node's error message."""


def _guard_size(value: Any) -> Any:
    """Refuse an intermediate that outgrew the evaluation budget.

    Called on every value an operator or helper produces, so a hostile
    expression cannot compose small legal steps into a huge one: each level is
    measured as it is produced rather than only at the end.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value.bit_length() > MAX_RESULT_BITS:
        raise ExpressionError("The result is too large.")
    if (
        isinstance(value, (str, bytes, list, tuple, dict, set))
        and len(value) > MAX_SEQUENCE_LENGTH
    ):
        raise ExpressionError("The result is too large.")
    return value


def evaluate_expression(text: str) -> Any:
    """Evaluate one sandboxed expression (already ``{{...}}``-rendered)."""
    source = (text or "").strip()
    if not source:
        raise ExpressionError("The expression is empty.")
    if len(source) > MAX_EXPRESSION_LENGTH:
        raise ExpressionError(
            f"The expression exceeds {MAX_EXPRESSION_LENGTH} characters."
        )
    try:
        tree = ast.parse(source, mode="eval")
    except SyntaxError as exc:
        raise ExpressionError(f"Invalid expression: {exc.msg}.") from exc

    for node in ast.walk(tree):
        if not isinstance(node, _ALLOWED_NODES):
            raise ExpressionError(
                f"`{type(node).__name__}` is not allowed in expressions."
            )
        if isinstance(node, ast.Name) and node.id not in ALLOWED_FUNCTIONS and node.id not in ("true", "false", "null", "True", "False", "None"):
            raise ExpressionError(
                f"Unknown name `{node.id}` — pipe values in with "
                "{{node.output}} references, they render before evaluation."
            )
        if isinstance(node, ast.Call) and (
            not isinstance(node.func, ast.Name)
            or node.func.id not in ALLOWED_FUNCTIONS
        ):
            raise ExpressionError("Only the documented helpers are callable.")

    return _eval(tree.body, depth=0)


def _eval(node: ast.AST, depth: int) -> Any:
    if depth > MAX_DEPTH:
        raise ExpressionError("The expression nests too deeply.")
    d = depth + 1

    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.Name):
        lowered = node.id.lower()
        if lowered == "true":
            return True
        if lowered == "false":
            return False
        if lowered in ("null", "none"):
            return None
        raise ExpressionError(f"Unknown name `{node.id}`.")
    if isinstance(node, ast.List):
        return [_eval(item, d) for item in node.elts]
    if isinstance(node, ast.Tuple):
        return tuple(_eval(item, d) for item in node.elts)
    if isinstance(node, ast.Dict):
        return {
            _eval(k, d): _eval(v, d)
            for k, v in zip(node.keys, node.values, strict=True)
            if k is not None
        }
    if isinstance(node, ast.Subscript):
        target = _eval(node.value, d)
        key = _eval(node.slice, d)
        if isinstance(target, dict):
            return target.get(key)
        if isinstance(target, (list, tuple, str)):
            try:
                return target[int(key)]
            except (IndexError, ValueError, TypeError) as exc:
                raise ExpressionError(f"Bad index `{key}`.") from exc
        raise ExpressionError("Only lists, strings and objects are indexable.")
    if isinstance(node, ast.UnaryOp):
        operand = _eval(node.operand, d)
        try:
            if isinstance(node.op, ast.USub):
                return -operand
            if isinstance(node.op, ast.UAdd):
                return +operand
        except TypeError as exc:
            raise ExpressionError(str(exc)) from exc
        return not operand
    if isinstance(node, ast.BoolOp):
        if isinstance(node.op, ast.And):
            result: Any = True
            for value in node.values:
                result = _eval(value, d)
                if not result:
                    return result
            return result
        for value in node.values:
            result = _eval(value, d)
            if result:
                return result
        return result
    if isinstance(node, ast.IfExp):
        return _eval(node.body, d) if _eval(node.test, d) else _eval(node.orelse, d)
    if isinstance(node, ast.Compare):
        left = _eval(node.left, d)
        for op, comparator in zip(node.ops, node.comparators, strict=True):
            right = _eval(comparator, d)
            try:
                ok = (
                    left == right if isinstance(op, ast.Eq)
                    else left != right if isinstance(op, ast.NotEq)
                    else left < right if isinstance(op, ast.Lt)
                    else left <= right if isinstance(op, ast.LtE)
                    else left > right if isinstance(op, ast.Gt)
                    else left >= right if isinstance(op, ast.GtE)
                    else left in right if isinstance(op, ast.In)
                    else left not in right
                )
            except TypeError as exc:
                # `1 in 2`, `"a" < 1` — a raw TypeError here would escape the
                # ExpressionError contract the node's error port relies on.
                raise ExpressionError(str(exc)) from exc
            if not ok:
                return False
            left = right
        return True
    if isinstance(node, ast.BinOp):
        left = _eval(node.left, d)
        right = _eval(node.right, d)
        if isinstance(node.op, ast.Pow):
            if isinstance(right, (int, float)) and abs(right) > MAX_POW_EXPONENT:
                raise ExpressionError("Exponent too large.")
            # Predict the width BEFORE computing. The exponent cap alone admits
            # `(9**512)**512`, whose base is already 1624 bits wide — the
            # multiplication of widths is what has to be refused, and refusing
            # it after the fact would mean having already paid for it.
            if (
                isinstance(left, int)
                and isinstance(right, int)
                and right > 0
                and left.bit_length() * right > MAX_RESULT_BITS
            ):
                raise ExpressionError("The result is too large.")
            try:
                return _guard_size(left ** right)
            except (TypeError, OverflowError, ZeroDivisionError) as exc:
                raise ExpressionError(str(exc)) from exc
        if isinstance(node.op, ast.Mult):
            # "ab" * 10_000_000 is a memory bomb, and so is
            # `("a" * 10_000) * 10_000` whose two multipliers are each legal —
            # the operand's own length has to enter the bound.
            seq, count = (
                (left, right)
                if isinstance(left, (str, list, tuple)) and isinstance(right, int)
                else (right, left)
                if isinstance(right, (str, list, tuple)) and isinstance(left, int)
                else (None, None)
            )
            if seq is not None:
                if count > MAX_REPEAT:
                    raise ExpressionError("Repetition too large.")
                if count > 0 and len(seq) * count > MAX_SEQUENCE_LENGTH:
                    raise ExpressionError("The result is too large.")
                return _guard_size(seq * count)
            if (
                isinstance(left, int)
                and isinstance(right, int)
                and left.bit_length() + right.bit_length() > MAX_RESULT_BITS
            ):
                raise ExpressionError("The result is too large.")
            try:
                return _guard_size(left * right)
            except (TypeError, OverflowError) as exc:
                raise ExpressionError(str(exc)) from exc
        try:
            if isinstance(node.op, ast.Add):
                # Concatenation composes the same way repetition does.
                if (
                    isinstance(left, (str, list, tuple))
                    and isinstance(right, (str, list, tuple))
                    and len(left) + len(right) > MAX_SEQUENCE_LENGTH
                ):
                    raise ExpressionError("The result is too large.")
                return _guard_size(left + right)
            if isinstance(node.op, ast.Sub):
                return _guard_size(left - right)
            if isinstance(node.op, ast.Div):
                return _guard_size(left / right)
            if isinstance(node.op, ast.FloorDiv):
                return _guard_size(left // right)
            return _guard_size(left % right)
        except (TypeError, OverflowError, ZeroDivisionError) as exc:
            raise ExpressionError(str(exc)) from exc
    if isinstance(node, ast.Call):
        func = ALLOWED_FUNCTIONS[node.func.id]  # type: ignore[union-attr]
        args = [_eval(a, d) for a in node.args]
        kwargs = {
            kw.arg: _eval(kw.value, d) for kw in node.keywords if kw.arg
        }
        try:
            return _guard_size(func(*args, **kwargs))
        except ExpressionError:
            raise
        except Exception as exc:
            raise ExpressionError(
                f"`{node.func.id}` failed: {exc}"  # type: ignore[union-attr]
            ) from exc
    raise ExpressionError(f"`{type(node).__name__}` is not allowed.")
