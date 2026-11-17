"""In-memory stand-ins for the personal OAuth token gate tests.

``FakeSession`` answers ``select(<Model>)`` by running the statement's real
WHERE clause against in-memory rows with SQL's three-valued logic, so a test
exercises the query a code path builds - its OAuth gate included - instead of a
canned answer that would pass whatever the query said.
"""

from __future__ import annotations

import json
import re
import uuid
from typing import Any

from sqlalchemy import types as sqltypes
from sqlalchemy.sql import elements, functions, operators, selectable, visitors

from app.models.integration import Integration

OWNER_ID = uuid.uuid4()
COLLEAGUE_ID = uuid.uuid4()


def _normalize(value: Any) -> Any:
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, str):
        try:
            return str(uuid.UUID(value))
        except ValueError:
            return value
    return value


class _Correlated:
    """The outer row and the row a correlated subquery is trying, so a column
    of the subquery's table reads the candidate and any other the outer row."""

    def __init__(self, outer: Any, table: Any, candidate: Any):
        self.outer = outer
        self.table = table
        self.candidate = candidate

    def column(self, expr: elements.ColumnClause) -> Any:
        if expr.table is self.table:
            return getattr(self.candidate, expr.key)
        return _column(self.outer, expr)


def _column(row: Any, expr: elements.ColumnClause) -> Any:
    if isinstance(row, _Correlated):
        return row.column(expr)
    return getattr(row, expr.key)


def _table_of(row: Any, rows_by_model: dict[type, list[Any]]) -> Any:
    """The table *row* stands for: its mapped class's, else the model whose
    session rows hold it (a ``SimpleNamespace`` stand-in)."""
    table = getattr(type(row), "__table__", None)
    if table is not None:
        return table
    for model, rows in rows_by_model.items():
        if any(candidate is row for candidate in rows):
            return getattr(model, "__table__", None)
    return None


def _exists(clause: selectable.Exists, row: Any, rows_by_model: dict[type, list[Any]]) -> bool:
    """``EXISTS (SELECT ... WHERE ...)`` correlated with *row*, answered from the
    session's rows of every other table the subquery reads."""
    inner: Any = clause
    while not isinstance(inner, selectable.Select):
        inner = inner.element
    where = inner.whereclause
    tables = {
        col.table
        for col in visitors.iterate(where)
        if isinstance(col, elements.ColumnClause) and col.table is not None
    }
    outer_table = _table_of(row, rows_by_model)
    for model, candidates in rows_by_model.items():
        table = getattr(model, "__table__", None)
        if table is None or table is outer_table or table not in tables:
            continue
        for candidate in candidates:
            if evaluate(where, _Correlated(row, table, candidate), rows_by_model) is True:
                return True
    return False


def _operand(expr: Any, row: Any, rows_by_model: dict[type, list[Any]] | None = None) -> Any:
    if isinstance(expr, elements.BindParameter):
        return expr.effective_value
    if isinstance(expr, elements.True_):
        return True
    if isinstance(expr, elements.False_):
        return False
    if isinstance(expr, elements.Null):
        return None
    if isinstance(expr, elements.Grouping):
        return _operand(expr.element, row, rows_by_model)
    if isinstance(expr, elements.Cast):
        value = _operand(expr.clause, row, rows_by_model)
        # ``CAST(<json column> AS TEXT)``: the stored text, which the parse guard reads.
        if value is not None and isinstance(expr.type, sqltypes.String) and isinstance(expr.clause.type, sqltypes.JSON):
            return json.dumps(value, default=str)
        # ``shared_user_ids::jsonb`` / ``ARRAY[...]::text[]``: the value itself.
        return value
    if isinstance(expr, functions.Function) and expr.name in ("replace", "regexp_replace"):
        args = [_operand(arg, row, rows_by_model) for arg in expr.clauses]
        if any(arg is None for arg in args[:3]):
            return None
        # ``regexp_replace(..., 'g')`` replaces every match, as ``re.sub`` does.
        return args[0].replace(args[1], args[2]) if expr.name == "replace" else re.sub(args[1], args[2], args[0])
    if isinstance(expr, elements.ColumnClause):
        return _column(row, expr)
    return evaluate(expr, row, rows_by_model)


def evaluate(
    clause: Any, row: Any, rows_by_model: dict[type, list[Any]] | None = None
) -> bool | None:
    """SQL three-valued result of *clause* for *row*; ``None`` is NULL.

    *rows_by_model* answers a correlated ``EXISTS`` (the owner-deactivation
    rule); without it no other row exists."""
    rows_by_model = rows_by_model or {}
    if clause is None:
        return True
    if isinstance(clause, elements.Grouping):
        return evaluate(clause.element, row, rows_by_model)
    if isinstance(clause, selectable.Exists):
        return _exists(clause, row, rows_by_model)
    if isinstance(clause, elements.UnaryExpression) and clause.operator is operators.inv:
        result = evaluate(clause.element, row, rows_by_model)
        return None if result is None else not result
    if isinstance(clause, elements.True_):
        return True
    if isinstance(clause, elements.False_):
        return False
    if isinstance(clause, elements.Case):
        # The parse guard's ``CASE`` (``core/json_sql.when_json_parseable``): a branch only when reached.
        for condition, result in clause.whens:
            if evaluate(condition, row, rows_by_model) is True:
                return _operand(result, row, rows_by_model)
        return evaluate(clause.else_, row, rows_by_model) if clause.else_ is not None else None
    if isinstance(clause, elements.AsBoolean):
        value = _operand(clause.element, row, rows_by_model)
        return value is (clause.operator is operators.is_true)
    if isinstance(
        clause,
        (elements.ClauseList, elements.BooleanClauseList, elements.ExpressionClauseList),
    ):
        results = [evaluate(sub, row, rows_by_model) for sub in clause.clauses]
        if clause.operator is operators.and_:
            if any(r is False for r in results):
                return False
            return None if any(r is None for r in results) else True
        if clause.operator is operators.or_:
            if any(r is True for r in results):
                return True
            return None if any(r is None for r in results) else False
        raise NotImplementedError(f"clause list operator {clause.operator!r}")
    if isinstance(clause, elements.BinaryExpression):
        op = clause.operator
        left = _operand(clause.left, row, rows_by_model)
        right = _operand(clause.right, row, rows_by_model)
        if getattr(op, "opstring", None) == "?|":
            # PostgreSQL ``jsonb ?| text[]`` (the visibility filter's grant
            # checks): does the JSON array hold any of the given strings?
            if left is None or right is None:
                return None
            held = {_normalize(v) for v in left} if isinstance(left, (list, tuple)) else set()
            return any(_normalize(key) in held for key in right)
        if op is operators.is_:
            return left is right
        if op is operators.is_not:
            return left is not right
        if op in (operators.in_op, operators.not_in_op):
            if left is None:
                return None
            hit = _normalize(left) in {_normalize(v) for v in right or ()}
            return hit if op is operators.in_op else not hit
        if left is None or right is None:
            return None
        if op is operators.regexp_match_op:
            return re.search(right, left) is not None
        if op is operators.eq:
            return _normalize(left) == _normalize(right)
        if op is operators.ne:
            return _normalize(left) != _normalize(right)
        raise NotImplementedError(f"binary operator {op!r}")
    raise NotImplementedError(f"clause type {type(clause).__name__}")


class FakeResult:
    def __init__(self, rows: list[Any]):
        self._rows = list(rows)

    def scalar_one_or_none(self) -> Any:
        return self._rows[0] if self._rows else None

    def scalars(self) -> FakeResult:
        return self

    def all(self) -> list[Any]:
        return list(self._rows)

    def first(self) -> Any:
        return self._rows[0] if self._rows else None


class FakeSession:
    """AsyncSession stand-in filtering ``select(<Model>)`` rows by their WHERE."""

    def __init__(self, rows_by_model: dict[type, list[Any]] | None = None):
        self.rows_by_model = rows_by_model or {}
        self.statements: list[Any] = []

    async def execute(self, stmt: Any, *_args: Any, **_kwargs: Any) -> FakeResult:
        self.statements.append(stmt)
        entity = stmt.column_descriptions[0]["entity"]
        rows = [
            row
            for row in self.rows_by_model.get(entity, [])
            if evaluate(stmt.whereclause, row, self.rows_by_model) is True
        ]
        limit = getattr(stmt, "_limit", None)
        return FakeResult(rows[:limit] if limit else rows)

    async def get(self, model: type, ident: Any) -> Any:
        wanted = _normalize(ident)
        for row in self.rows_by_model.get(model, []):
            if _normalize(getattr(row, "id", None)) == wanted:
                return row
        return None

    async def commit(self) -> None:
        return None

    async def flush(self) -> None:
        return None

    async def rollback(self) -> None:
        return None

    async def refresh(self, *_args: Any, **_kwargs: Any) -> None:
        return None

    def add(self, _obj: Any) -> None:
        return None


def make_integration(**overrides: Any) -> Integration:
    """A transient ``Integration`` row; OAuth, connected, owned by ``OWNER_ID``."""
    fields: dict[str, Any] = {
        "id": uuid.uuid4(),
        "user_id": OWNER_ID,
        "provider": "slack",
        "auth_type": "oauth",
        "name": "Personal Slack",
        "status": "connected",
        "is_company_managed": False,
        "is_builtin": False,
        "shared_group_ids": None,
        "oauth_share_acknowledged": False,
        "credential_mode": "shared",
        "access_token_enc": "enc-access-token",
        "refresh_token_enc": None,
        "api_key_enc": None,
        "extra_config_enc": None,
        "client_id_enc": None,
        "client_secret_enc": None,
        "token_expires_at": None,
        "token_url": None,
        "scopes": None,
        "description": None,
        "scope_config": None,
        "tags": [],
    }
    fields.update(overrides)
    return Integration(**fields)


#: Who may use an integration's stored token, seen from ``COLLEAGUE_ID``
#: (the acting user): ``{case: (integration overrides, usable)}``.
SHARING_CASES: dict[str, tuple[dict[str, Any], bool]] = {
    "own_oauth": ({"user_id": COLLEAGUE_ID}, True),
    "acknowledged_share": (
        {"is_company_managed": True, "oauth_share_acknowledged": True},
        True,
    ),
    "non_oauth": ({"auth_type": "api_key", "is_company_managed": True}, True),
    "unshared_company_managed": ({"is_company_managed": True}, False),
    "unshared_group_share": ({"shared_group_ids": [str(uuid.uuid4())]}, False),
}
