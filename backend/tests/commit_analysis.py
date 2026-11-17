"""Static analysis behind ``test_write_routes_commit_before_response``.

``get_db`` commits on dependency teardown, which FastAPI runs AFTER the response
is sent, so a route that returns without committing hands its writes to that
late commit and a client acting on the response can read the old state. This
module finds, for any route, the writes it may still hold when it answers.

The analysis walks the endpoint, the dependencies that reach ``get_db`` and
every function of the application they hand the request session (or an object
loaded from it) to. It is path-sensitive: the state along a path is the set of
writes made through the request session and not committed yet. A write adds to
it, a commit or a rollback empties it, branches merge by union, and a function
is summarised once per way it is called. A route leaves writes to the teardown
when one of its return points can still hold pending writes.

What counts as a write: ``add`` / ``add_all`` / ``delete`` / ``merge`` /
``flush`` on the session, an ``execute`` / ``scalar(s)`` of an INSERT, UPDATE,
DELETE (or of a statement it cannot classify), and changing a mapped attribute
of an object loaded from the session - assigning it, mutating it in place
(``agent.members.append(user)``) or calling a model method that assigns it
(``agent.prune_orphaned_integration_scopes()``). A model column or
relationship only: a transient attribute such as ``user.auth_credential_kind``
marks nothing dirty.

Deliberate limits, each of which errs towards reporting:

- Exceptions: a handler for an application exception class only receives the
  states at matching explicit ``raise`` sites (propagated from callees); a
  handler for SQLAlchemy exceptions starts empty, because a database failure
  kills the transaction and nothing it held can land; any other handler starts
  from every state a statement of the ``try`` body could have raised in.
- Values are not tracked, except two that decide a branch for sure: a
  parameter a caller leaves at a falsy constant default (``if source_id:`` on
  a call that never passes one), and the ``rowcount`` of an INSERT / UPDATE /
  DELETE, zero when the statement matched no row and so wrote nothing.
- The session escaping into an object the analysis cannot follow (a class it is
  handed to, a builder that captures it in a closure) is recorded as an escape
  site; the test pins the reviewed ones so a new one is looked at.
- The body of a generator runs when the response streams, on a session that
  outlives the return: calling one applies nothing.
"""

from __future__ import annotations

import ast
import builtins
import importlib
import inspect
import sys
import textwrap
import types
from collections.abc import Iterable
from dataclasses import dataclass, field

from fastapi.routing import APIRoute

from app.core.database import get_db

_SESSION_WRITES = frozenset({"add", "add_all", "delete", "merge", "flush"})
_SESSION_STATEMENTS = frozenset({"execute", "scalar", "scalars", "stream", "stream_scalars"})
_SESSION_LOADS = frozenset({"get", "get_one", "refresh"})
_SESSION_ENDS = frozenset({"commit", "rollback"})
# ``agent.members.append(user)`` writes through a mapped collection; the same
# call on an attribute no model maps changes nothing in the database.
_COLLECTION_MUTATORS = frozenset(
    {"append", "extend", "remove", "insert", "pop", "clear", "add", "discard", "update", "setdefault"}
)
# The argument of these runs later, not before the response.
_DEFERRING = frozenset({"create_task", "ensure_future", "spawn_channel_task", "add_task"})
_MAX_DESCRIPTIONS = 64
_MAX_DEPTH = 12

EMPTY: frozenset[str] = frozenset()
# Stands for the caller's pending writes while a function is summarised: it
# survives to an exit exactly when that path commits nothing.
_ENTRY_MARK = "\x00entry"
_ENTRY = frozenset({_ENTRY_MARK})

# Read helpers that create a missing singleton row on first use, inside a
# SAVEPOINT: that insert is not the route's write, but what they return is an
# object of the request session like any other.
LAZY_INIT_WRITERS = frozenset({"app.api.v1.platform_settings_helpers.get_platform_settings"})

# Reviewed helpers analysed as doing nothing, neither their writes nor their
# commit: each commits what it writes on its success path and leaves a write
# pending only on a failure path it swallows itself.
REVIEWED_HELPERS: dict[str, str] = {
    "app.services.integration_tools._do_inline_refresh": (
        "commits the refreshed OAuth tokens; they stay pending only when a local "
        "step fails after the provider already rotated them, and landing them "
        "later is then the point"
    ),
}


def _mapped_attribute_names() -> frozenset[str]:
    import app.models  # noqa: F401 - registers every model
    from app.core.database import Base

    names: set[str] = set()
    for mapper in Base.registry.mappers:
        names.update(mapper.attrs.keys())
    return frozenset(names)


def _mutating_model_methods() -> frozenset[str]:
    """Names of the model methods that assign a mapped attribute of ``self``."""
    import app.models  # noqa: F401 - registers every model
    from app.core.database import Base

    names: set[str] = set()
    for mapper in Base.registry.mappers:
        mapped = set(mapper.attrs.keys())
        for klass in mapper.class_.__mro__:
            if not (getattr(klass, "__module__", "") or "").startswith("app."):
                continue
            for name, member in vars(klass).items():
                # Dunders are left out: Python 3.14's generated __annotate_func__
                # reports the whole class body as its source.
                if name in names or name.startswith("__") or not isinstance(member, types.FunctionType):
                    continue
                try:
                    tree = ast.parse(textwrap.dedent(inspect.getsource(member)))
                except (OSError, TypeError, SyntaxError):
                    continue
                if any(
                    isinstance(target, ast.Attribute)
                    and isinstance(target.value, ast.Name)
                    and target.value.id == "self"
                    and target.attr in mapped
                    for node in ast.walk(tree)
                    if isinstance(node, (ast.Assign, ast.AugAssign, ast.AnnAssign))
                    for target in (node.targets if isinstance(node, ast.Assign) else [node.target])
                ):
                    names.add(name)
    return frozenset(names)


def _list_elements(node: ast.AST, own: dict[str, list[ast.AST]]) -> dict[str, list[ast.AST]]:
    """The elements of each local list that is only built and iterated.

    ``queries = []``, ``queries.append(select(...))``, ``for query in queries:``
    - the loop variable is one of the appended statements. A list bound to
    anything but a list literal, or used any other way (handed to a call,
    returned, indexed), keeps its elements unknown.
    """
    stores: dict[str, int] = {}
    for sub in ast.walk(node):
        if isinstance(sub, ast.Name) and isinstance(sub.ctx, ast.Store):
            stores[sub.id] = stores.get(sub.id, 0) + 1
    elements = {
        name: [element for value in values for element in value.elts]
        for name, values in own.items()
        if len(values) == stores.get(name) and all(isinstance(v, ast.List) for v in values)
    }
    parents = {child: parent for parent in ast.walk(node) for child in ast.iter_child_nodes(parent)}
    for sub in ast.walk(node):
        if not (isinstance(sub, ast.Name) and isinstance(sub.ctx, ast.Load) and sub.id in elements):
            continue
        parent = parents.get(sub)
        if isinstance(parent, (ast.For, ast.AsyncFor)) and parent.iter is sub:
            continue
        call = parents.get(parent)
        if (
            isinstance(parent, ast.Attribute)
            and parent.attr in ("append", "extend")
            and isinstance(call, ast.Call)
            and call.func is parent
            and len(call.args) == 1
            and not call.keywords
        ):
            if parent.attr == "append":
                elements[sub.id].append(call.args[0])
                continue
            if isinstance(call.args[0], (ast.List, ast.Tuple)):
                elements[sub.id].extend(call.args[0].elts)
                continue
        del elements[sub.id]
    return elements


def _sql_constructors() -> dict[int, str]:
    import sqlalchemy
    from sqlalchemy.dialects import postgresql

    kinds = {
        sqlalchemy.select: "read",
        sqlalchemy.exists: "read",
        sqlalchemy.union: "read",
        sqlalchemy.union_all: "read",
        sqlalchemy.insert: "write",
        sqlalchemy.update: "write",
        sqlalchemy.delete: "write",
        postgresql.insert: "write",
        sqlalchemy.text: "text",
    }
    return {id(k): v for k, v in kinds.items()}


MAPPED_ATTRIBUTES = _mapped_attribute_names()
MUTATING_MODEL_METHODS = _mutating_model_methods()
_SQL_CONSTRUCTORS = _sql_constructors()


@dataclass(frozen=True)
class Summary:
    """What calling a function does to the caller's pending writes."""

    pending: frozenset[str]
    keeps_entry: bool
    # Exceptions escaping from an explicit ``raise``: (class or None, writes
    # pending there, whether the caller's own pending writes survive).
    raises: tuple = ()

    def apply(self, state: frozenset[str]) -> frozenset[str]:
        return _cap(self.pending | state) if self.keeps_entry else self.pending


_IDENTITY = Summary(EMPTY, True)


@dataclass
class RouteReport:
    pending: frozenset[str]
    escapes: list[str] = field(default_factory=list)


def _cap(state: frozenset[str]) -> frozenset[str]:
    if len(state) <= _MAX_DESCRIPTIONS:
        return state
    return frozenset(sorted(state)[:_MAX_DESCRIPTIONS])


def _union(states: Iterable[frozenset[str] | None]) -> frozenset[str] | None:
    live = [s for s in states if s is not None]
    if not live:
        return None
    out: frozenset[str] = frozenset()
    for s in live:
        out |= s
    return _cap(out)


def _exception_kind(cls: object) -> str:
    """``specific`` (an application exception), ``db`` (SQLAlchemy) or ``generic``."""
    if not isinstance(cls, type) or not issubclass(cls, BaseException):
        return "generic"
    module = cls.__module__ or ""
    if module.startswith("sqlalchemy"):
        return "db"
    if module.startswith("app.") and cls not in (Exception, BaseException):
        return "specific"
    return "generic"


def _sql_text_kind(call: ast.Call) -> str:
    if not call.args:
        return "unknown"
    arg = call.args[0]
    text = None
    if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
        text = arg.value
    elif isinstance(arg, ast.JoinedStr):
        text = "".join(v.value for v in arg.values if isinstance(v, ast.Constant) and isinstance(v.value, str))
    words = (text or "").split(None, 1)
    if not words:
        return "unknown"
    word = words[0].upper()
    # SET / RESET change the transaction's settings, not its data.
    if word in {"SELECT", "WITH", "SHOW", "SET", "RESET"}:
        return "read"
    return "write" if word in {"INSERT", "UPDATE", "DELETE", "MERGE", "TRUNCATE"} else "unknown"


def _chain_root(expr: ast.AST) -> str | None:
    """``query.where(...).order_by(...)`` -> ``query`` (both arms of a ternary)."""
    if isinstance(expr, ast.IfExp):
        body, orelse = _chain_root(expr.body), _chain_root(expr.orelse)
        return body if body == orelse else None
    while isinstance(expr, ast.Call) and isinstance(expr.func, ast.Attribute):
        expr = expr.func.value
    return expr.id if isinstance(expr, ast.Name) else None


def _is_generator(fdef: ast.AST) -> bool:
    """Whether *fdef* yields in its own body (not in a nested function)."""
    stack = list(fdef.body)
    while stack:
        node = stack.pop()
        if isinstance(node, (ast.Yield, ast.YieldFrom)):
            return True
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)):
            continue
        stack.extend(ast.iter_child_nodes(node))
    return False


def _always_true(test: ast.AST) -> bool:
    return isinstance(test, ast.Constant) and bool(test.value) is True


def _import_or_none(name: str):
    """The module, or None for an optional import the code guards itself."""
    try:
        return importlib.import_module(name)
    except ImportError:
        return None


class _Function:
    """One function body, walked from a given entry state."""

    def __init__(self, engine: Engine, fn, node, line_offset: int, sessions, orms, depth: int, *, scope=None, falsy=()):
        self.engine = engine
        self.fn = fn
        self.node = node
        self.line_offset = line_offset
        self.sessions: set[str] = set(sessions)
        self.orms: set[str] = set(orms)
        self.depth = depth
        self.name = f"{fn.__module__}.{fn.__qualname__}" if fn is not None else scope.name
        self.globals = fn.__globals__ if fn is not None else scope.globals
        self.local_imports: dict[str, object] = dict(scope.local_imports) if scope else {}
        self.nested: dict[str, ast.AST] = dict(scope.nested) if scope else {}
        self.returns: list[frozenset[str]] = []
        self.raise_sites: list[tuple[object, frozenset[str]]] = []
        self.escapes: list[str] = []
        self.loop_stack: list[tuple[list, list]] = []
        # (states before a statement that may raise, states after one that calls out)
        self.exc_stack: list[tuple[list, list]] = []
        self.iterated_calls: set[int] = set()
        # name -> the write of the session DML statement whose result it holds
        self.dml_results: dict[str, str] = {}
        own: dict[str, list[ast.AST]] = {}
        rebound: set[str] = set()
        for sub in ast.walk(node):
            if isinstance(sub, ast.Name) and isinstance(sub.ctx, ast.Store):
                rebound.add(sub.id)
            if isinstance(sub, ast.Assign):
                for target in sub.targets:
                    if isinstance(target, ast.Name):
                        own.setdefault(target.id, []).append(sub.value)
            elif isinstance(sub, (ast.AnnAssign, ast.NamedExpr)) and isinstance(sub.target, ast.Name) and sub.value is not None:
                own.setdefault(sub.target.id, []).append(sub.value)
            elif isinstance(sub, (ast.For, ast.AsyncFor)) and isinstance(sub.target, ast.Name) and isinstance(sub.iter, (ast.List, ast.Tuple)):
                own.setdefault(sub.target.id, []).extend(sub.iter.elts)
        lists = _list_elements(node, own)
        for sub in ast.walk(node):
            if (
                isinstance(sub, (ast.For, ast.AsyncFor))
                and isinstance(sub.target, ast.Name)
                and isinstance(sub.iter, ast.Name)
                and sub.iter.id in lists
            ):
                own.setdefault(sub.target.id, []).extend(lists[sub.iter.id])
        # A nested function reads the enclosing function's names it does not
        # bind itself (a statement built outside, executed inside).
        self.assignments: dict[str, list[ast.AST]] = {**(scope.assignments if scope else {}), **own}
        # A parameter left at a falsy default, unless the body reassigns it.
        self.falsy = {name for name, _ in falsy} - rebound
        self.falsy_none = {name for name, is_none in falsy if is_none} - rebound

    # -- bookkeeping ---------------------------------------------------------

    def where(self, node: ast.AST) -> str:
        return f"{self.name}:{getattr(node, 'lineno', 0) + self.line_offset}"

    def write(self, state: frozenset[str], node: ast.AST, what: str) -> frozenset[str]:
        return _cap(state | {f"{what} @ {self.where(node)}"})

    def escape(self, node: ast.AST, what: str) -> None:
        self.escapes.append(f"{what} in {self.name}")

    def summary(self) -> Summary:
        end = self.block(self.node.body, _ENTRY)
        exits = [*self.returns, end] if end is not None else list(self.returns)
        keeps = any(_ENTRY_MARK in s for s in exits)
        pending = (_union(exits) or EMPTY) - _ENTRY
        raises = tuple((cls, frozenset(s - _ENTRY), _ENTRY_MARK in s) for cls, s in self.raise_sites)
        return Summary(frozenset(pending), keeps, raises)

    # -- name resolution -----------------------------------------------------

    def resolve(self, func: ast.AST):
        if isinstance(func, ast.Name):
            if func.id in self.local_imports:
                return self.local_imports[func.id]
            if func.id in self.globals:
                return self.globals[func.id]
            code = getattr(self.fn, "__code__", None)
            closure = getattr(self.fn, "__closure__", None)
            if code is not None and closure and func.id in code.co_freevars:
                try:
                    return closure[code.co_freevars.index(func.id)].cell_contents
                except ValueError:
                    return None
            return None
        if isinstance(func, ast.Attribute):
            base = self.resolve(func.value)
            if isinstance(base, (types.ModuleType, type)):
                return getattr(base, func.attr, None)
        return None

    # -- statement classification --------------------------------------------

    def statement_kind(self, node: ast.AST) -> str:
        return self._kind(node, set()) or "unknown"

    @staticmethod
    def _combine(kinds: Iterable[str | None]) -> str | None:
        kinds = {k for k in kinds if k is not None}
        if not kinds:
            return None
        if "write" in kinds:
            return "write"
        return "read" if kinds == {"read"} else "unknown"

    def _kind(self, node: ast.AST, seen: set[str]) -> str | None:
        """``read`` / ``write`` / ``unknown``, or None for a cycle (neutral)."""
        if isinstance(node, ast.Await):
            return self._kind(node.value, seen)
        if isinstance(node, ast.Name):
            if node.id in seen:
                return None
            seen.add(node.id)
            values = self.assignments.get(node.id)
            return self._combine(self._kind(v, seen) for v in values) if values else "unknown"
        if isinstance(node, ast.IfExp):
            return self._combine([self._kind(node.body, seen), self._kind(node.orelse, seen)])
        if not isinstance(node, ast.Call):
            return "unknown"
        base = node
        while isinstance(base, ast.Call) and isinstance(base.func, ast.Attribute):
            base = base.func.value
        if isinstance(base, ast.Name) and base.id in self.assignments:
            return self._kind(base, seen)  # a generative chain keeps its kind
        if isinstance(node.func, ast.Name) and node.func.id in self.nested:
            fdef = self.nested[node.func.id]
            return self._combine(
                self._kind(r.value, seen) for r in ast.walk(fdef) if isinstance(r, ast.Return) and r.value is not None
            )
        root = node
        while isinstance(root.func, ast.Attribute) and isinstance(root.func.value, ast.Call):
            root = root.func.value
        constructor = self.resolve(root.func)
        kind = _SQL_CONSTRUCTORS.get(id(constructor)) if constructor is not None else None
        if kind == "text":
            return _sql_text_kind(root)
        if kind is not None:
            return kind
        if isinstance(root.func, ast.Attribute) and root.func.attr in {"insert", "update", "delete"}:
            return "write"  # ``Model.__table__.delete()``
        if constructor is not None:
            # A helper handing back (a chain on) one of its arguments - a filter
            # applied to a query - is that argument.
            param = self.engine.returned_param(constructor)
            argument = self._argument_for(constructor, root, param) if param else None
            if argument is not None:
                return self._kind(argument, seen)
            return self.engine.returned_statement_kind(constructor)
        return "unknown"

    def _argument_for(self, target, call: ast.Call, param: str) -> ast.AST | None:
        params = self.engine.positional_params(inspect.unwrap(target))
        if params is None:
            return None
        for keyword in call.keywords:
            if keyword.arg == param:
                return keyword.value
        if param in params and params.index(param) < len(call.args):
            return call.args[params.index(param)]
        return None

    # -- statements ----------------------------------------------------------

    def block(self, stmts: list[ast.stmt], state: frozenset[str] | None) -> frozenset[str] | None:
        for stmt in stmts:
            if state is None:
                return None
            raises, calls_out = self._may_raise(stmt)
            if self.exc_stack and raises:
                self.exc_stack[-1][0].append(state)
            state = self.stmt(stmt, state)
            if self.exc_stack and calls_out and state is not None:
                self.exc_stack[-1][1].append(state)
        return state

    def _may_raise(self, stmt: ast.stmt) -> tuple[bool, bool]:
        """(may raise a non-database exception, calls another function).

        A database failure contributes nothing: it kills the transaction, so the
        writes it held never land. An explicit ``raise`` is a raise site of its
        own, with its class.
        """
        if isinstance(stmt, ast.Raise):
            return False, False
        if isinstance(stmt, ast.Assert):
            return True, False
        if isinstance(stmt, (ast.If, ast.While)):
            headers = [stmt.test]
        elif isinstance(stmt, (ast.For, ast.AsyncFor)):
            headers = [stmt.iter]
        elif isinstance(stmt, (ast.With, ast.AsyncWith)):
            headers = [item.context_expr for item in stmt.items]
        elif isinstance(stmt, ast.Match):
            headers = [stmt.subject]
        elif isinstance(stmt, (ast.Try, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) or type(stmt).__name__ == "TryStar":
            headers = []
        else:
            headers = [stmt]
        for expr in headers:
            for sub in ast.walk(expr):
                if isinstance(sub, ast.Call) and not (
                    isinstance(sub.func, ast.Attribute) and self.is_session(sub.func.value)
                ):
                    return True, True
        return False, False

    def _dml_write(self, value: ast.AST) -> str | None:
        """The write a ``db.execute(<INSERT / UPDATE / DELETE>)`` expression adds."""
        call = value.value if isinstance(value, ast.Await) else value
        if not (
            isinstance(call, ast.Call)
            and isinstance(call.func, ast.Attribute)
            and self.is_session(call.func.value)
            and call.func.attr == "execute"
            and call.args
            and self.statement_kind(call.args[0]) == "write"
        ):
            return None
        return f"db.execute(<DML>) @ {self.where(call)}"

    def _rowcount_guard(self, test: ast.AST) -> tuple[str, bool] | None:
        """(write, whether *test* holds when it matched rows) for a ``rowcount`` test.

        A statement whose ``rowcount`` is zero matched no row and wrote nothing,
        so the branch taken on a zero count does not carry its write.
        """
        polarity = True
        if isinstance(test, ast.UnaryOp) and isinstance(test.op, ast.Not):
            test, polarity = test.operand, False
        if isinstance(test, ast.Compare) and len(test.ops) == 1 and isinstance(test.comparators[0], ast.Constant):
            op, bound = test.ops[0], test.comparators[0].value
            holds_on_rows = {
                (ast.Gt, 0): True,
                (ast.GtE, 1): True,
                (ast.NotEq, 0): True,
                (ast.Eq, 0): False,
                (ast.Lt, 1): False,
                (ast.LtE, 0): False,
            }.get((type(op), bound))
            if holds_on_rows is None:
                return None
            test, polarity = test.left, polarity == holds_on_rows
        if (
            isinstance(test, ast.Attribute)
            and test.attr == "rowcount"
            and isinstance(test.value, ast.Name)
            and test.value.id in self.dml_results
        ):
            return self.dml_results[test.value.id], polarity
        return None

    def _known_truth(self, test: ast.AST) -> bool | None:
        """True / False when *test* reads only a parameter left at a falsy default."""
        if isinstance(test, ast.Name) and test.id in self.falsy:
            return False
        if isinstance(test, ast.UnaryOp) and isinstance(test.op, ast.Not):
            inner = self._known_truth(test.operand)
            return None if inner is None else not inner
        if (
            isinstance(test, ast.Compare)
            and isinstance(test.left, ast.Name)
            and test.left.id in self.falsy_none
            and len(test.ops) == 1
            and isinstance(test.comparators[0], ast.Constant)
            and test.comparators[0].value is None
        ):
            if isinstance(test.ops[0], ast.Is):
                return True
            if isinstance(test.ops[0], ast.IsNot):
                return False
        return None

    def stmt(self, node: ast.stmt, state: frozenset[str]) -> frozenset[str] | None:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            self.nested[node.name] = node
            return state
        if isinstance(node, ast.ClassDef):
            return state
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            self._record_import(node)
            return state
        if isinstance(node, ast.Assign):
            kind, state = self.expr(node.value, state)
            for target in node.targets:
                state = self.bind(target, kind, node.value, state, node)
            write = self._dml_write(node.value)
            if write is not None and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
                self.dml_results[node.targets[0].id] = write
            return state
        if isinstance(node, ast.AnnAssign):
            if node.value is None:
                return state
            kind, state = self.expr(node.value, state)
            return self.bind(node.target, kind, node.value, state, node)
        if isinstance(node, ast.AugAssign):
            _, state = self.expr(node.value, state)
            return self.mutate(node.target, state, node)
        if isinstance(node, ast.Delete):
            for target in node.targets:
                state = self.mutate(target, state, node)
            return state
        if isinstance(node, ast.Expr):
            _, state = self.expr(node.value, state)
            return state
        if isinstance(node, ast.Return):
            if node.value is not None:
                _, state = self.expr(node.value, state)
            self.returns.append(state)
            return None
        if isinstance(node, ast.Raise):
            exc = node.exc.func if isinstance(node.exc, ast.Call) else node.exc
            cls = self.resolve(exc) if exc is not None else None
            if node.exc is not None:
                _, state = self.expr(node.exc, state)
            self.raise_sites.append((cls if isinstance(cls, type) else None, state))
            return None
        if isinstance(node, ast.If):
            _, state = self.expr(node.test, state)
            known = self._known_truth(node.test)
            if known is True:
                return self.block(node.body, state)
            if known is False:
                return self.block(node.orelse, state)
            body_state = else_state = state
            guard = self._rowcount_guard(node.test)
            if guard is not None:
                write, holds_on_rows = guard
                if holds_on_rows:
                    else_state = state - {write}
                else:
                    body_state = state - {write}
            return _union([self.block(node.body, body_state), self.block(node.orelse, else_state)])
        if isinstance(node, (ast.For, ast.AsyncFor, ast.While)):
            return self.loop(node, state)
        if isinstance(node, (ast.Break, ast.Continue)):
            if self.loop_stack:
                breaks, continues = self.loop_stack[-1]
                (breaks if isinstance(node, ast.Break) else continues).append(state)
            return None
        if isinstance(node, (ast.With, ast.AsyncWith)):
            return self.with_(node, state)
        if isinstance(node, ast.Try) or type(node).__name__ == "TryStar":
            return self.try_(node, state)
        if isinstance(node, ast.Match):
            _, state = self.expr(node.subject, state)
            return _union([*(self.block(case.body, state) for case in node.cases), state])
        if isinstance(node, ast.Assert):
            _, state = self.expr(node.test, state)
        return state

    def _record_import(self, node: ast.Import | ast.ImportFrom) -> None:
        # Routes import lazily inside their body; resolve like the globals.
        if isinstance(node, ast.Import):
            for alias in node.names:
                module = _import_or_none(alias.name)
                if module is None:
                    continue
                if alias.asname:
                    self.local_imports[alias.asname] = module
                else:
                    head = alias.name.split(".")[0]
                    self.local_imports[head] = sys.modules[head]
            return
        if node.level or not node.module:
            return
        module = _import_or_none(node.module)
        if module is None:
            return
        for alias in node.names:
            obj = getattr(module, alias.name, None)
            if obj is None:
                obj = _import_or_none(f"{node.module}.{alias.name}")
            if obj is not None:
                self.local_imports[alias.asname or alias.name] = obj

    def loop(self, node: ast.For | ast.AsyncFor | ast.While, state: frozenset[str]) -> frozenset[str] | None:
        forever = isinstance(node, ast.While) and _always_true(node.test)
        if isinstance(node, ast.While):
            _, state = self.expr(node.test, state)
        else:
            if isinstance(node.iter, ast.Call):
                self.iterated_calls.add(id(node.iter))
            kind, state = self.expr(node.iter, state)
            self.bind(node.target, "orm" if kind in ("orm", "session") else None, None, state, node)
        exits: list[frozenset[str] | None] = []
        current = state
        for _ in range(3):  # the state only grows: a few rounds reach the fixpoint
            breaks: list = []
            continues: list = []
            self.loop_stack.append((breaks, continues))
            try:
                end = self.block(node.body, current)
            finally:
                self.loop_stack.pop()
            exits.extend(breaks)
            following = _union([current, end, *continues])
            if following == current:
                break
            current = following
        if not forever:
            exits.append(current)
        after = _union(exits)
        if after is not None and node.orelse:
            after = self.block(node.orelse, after)
        return after

    def with_(self, node: ast.With | ast.AsyncWith, state: frozenset[str]) -> frozenset[str] | None:
        begins = False
        for item in node.items:
            ctx = item.context_expr
            if (
                isinstance(ctx, ast.Call)
                and isinstance(ctx.func, ast.Attribute)
                and self.is_session(ctx.func.value)
                and ctx.func.attr == "begin"
            ):
                begins = True  # ``async with db.begin():`` commits when the block ends
                continue
            kind, state = self.expr(ctx, state)
            if item.optional_vars is not None:
                state = self.bind(item.optional_vars, kind, ctx, state, node)
        state = self.block(node.body, state)
        if state is not None and begins:
            return EMPTY
        return state

    def _handler_classes(self, handler: ast.ExceptHandler) -> list | None:
        if handler.type is None:
            return None
        nodes = handler.type.elts if isinstance(handler.type, ast.Tuple) else [handler.type]
        classes = [self.resolve(n) for n in nodes]
        return classes if all(isinstance(c, type) for c in classes) else None

    def try_(self, node: ast.Try, state: frozenset[str]) -> frozenset[str] | None:
        self.exc_stack.append(([], []))
        start = len(self.raise_sites)
        try:
            current = self.block(node.body, state)
        finally:
            befores, afters = self.exc_stack.pop()
        body_raises = self.raise_sites[start:]
        del self.raise_sites[start:]
        generic_entry = _union([*befores, *afters, *(s for _, s in body_raises)]) or EMPTY
        caught: set[int] = set()
        outs: list[frozenset[str] | None] = []
        if current is not None:
            outs.append(self.block(node.orelse, current) if node.orelse else current)
        for handler in node.handlers:
            classes = self._handler_classes(handler)
            kinds = {_exception_kind(c) for c in classes} if classes else {"generic"}
            if kinds == {"db"}:
                entry = EMPTY
            elif kinds == {"specific"}:
                matching = [
                    i
                    for i, (cls, _) in enumerate(body_raises)
                    if cls is not None and issubclass(cls, tuple(classes)) and i not in caught
                ]
                caught.update(matching)
                entry = _union([*befores, *(body_raises[i][1] for i in matching)]) or EMPTY
            else:
                caught.update(range(len(body_raises)))
                entry = generic_entry
            outs.append(self.block(handler.body, entry))
        self.raise_sites.extend(r for i, r in enumerate(body_raises) if i not in caught)
        after = _union(outs)
        if node.finalbody:
            if after is None:
                self.block(node.finalbody, generic_entry)
                return None
            after = self.block(node.finalbody, after)
        return after

    # -- binding / mutation --------------------------------------------------

    def is_session(self, node: ast.AST) -> bool:
        return isinstance(node, ast.Name) and node.id in self.sessions

    def kind_of(self, node: ast.AST) -> str | None:
        if isinstance(node, ast.Name):
            if node.id in self.sessions:
                return "session"
            return "orm" if node.id in self.orms else None
        if isinstance(node, (ast.Attribute, ast.Subscript)):
            return "orm" if self.kind_of(node.value) == "orm" else None
        return None

    def bind(self, target: ast.AST, kind: str | None, value, state: frozenset[str], node: ast.AST) -> frozenset[str]:
        if isinstance(target, ast.Name):
            self.dml_results.pop(target.id, None)
            if kind == "session":
                self.sessions.add(target.id)
            elif kind == "orm":
                self.orms.add(target.id)
            return state
        if isinstance(target, (ast.Tuple, ast.List)):
            if isinstance(value, (ast.Tuple, ast.List)) and len(value.elts) == len(target.elts):
                for t, v in zip(target.elts, value.elts, strict=True):
                    state = self.bind(t, self.kind_of(v), v, state, node)
            else:
                for t in target.elts:
                    state = self.bind(t, "orm" if kind in ("orm", "session") else None, None, state, node)
            return state
        if isinstance(target, ast.Starred):
            return self.bind(target.value, kind, None, state, node)
        return self.mutate(target, state, node)

    def mutate(self, target: ast.AST, state: frozenset[str], node: ast.AST) -> frozenset[str]:
        if isinstance(target, ast.Attribute):
            if target.attr in MAPPED_ATTRIBUTES and self.kind_of(target.value) == "orm":
                return self.write(state, node, f"sets {ast.unparse(target)}")
        elif isinstance(target, ast.Subscript):
            if isinstance(target.value, ast.Attribute) and self.kind_of(target.value.value) == "orm":
                return self.write(state, node, f"sets {ast.unparse(target)}")
        elif isinstance(target, (ast.Tuple, ast.List)):
            for t in target.elts:
                state = self.mutate(t, state, node)
        return state

    # -- expressions ---------------------------------------------------------

    def expr(self, node: ast.AST | None, state: frozenset[str]) -> tuple[str | None, frozenset[str]]:
        if node is None:
            return None, state
        if isinstance(node, ast.Await):
            return self.expr(node.value, state)
        if isinstance(node, ast.Name):
            return self.kind_of(node), state
        if isinstance(node, ast.Attribute):
            kind, state = self.expr(node.value, state)
            return ("orm" if kind == "orm" else None), state
        if isinstance(node, ast.Subscript):
            kind, state = self.expr(node.value, state)
            _, state = self.expr(node.slice, state)
            return ("orm" if kind == "orm" else None), state
        if isinstance(node, ast.Call):
            return self.call(node, state)
        if isinstance(node, ast.NamedExpr):
            kind, state = self.expr(node.value, state)
            return kind, self.bind(node.target, kind, node.value, state, node)
        if isinstance(node, (ast.ListComp, ast.SetComp, ast.GeneratorExp, ast.DictComp)):
            for generator in node.generators:
                kind, state = self.expr(generator.iter, state)
                self.bind(generator.target, "orm" if kind in ("orm", "session") else None, None, state, node)
                for condition in generator.ifs:
                    _, state = self.expr(condition, state)
            if isinstance(node, ast.DictComp):
                _, state = self.expr(node.key, state)
                kind, state = self.expr(node.value, state)
            else:
                kind, state = self.expr(node.elt, state)
            return ("orm" if kind == "orm" else None), state
        if isinstance(node, ast.Lambda):
            return None, state
        if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
            kinds = []
            for element in node.elts:
                k, state = self.expr(element, state)
                kinds.append(k)
            return ("orm" if "orm" in kinds else None), state
        if isinstance(node, ast.IfExp):
            _, state = self.expr(node.test, state)
            a, state_a = self.expr(node.body, state)
            b, state_b = self.expr(node.orelse, state)
            return ("orm" if "orm" in (a, b) else None), _union([state_a, state_b]) or EMPTY
        kind = None
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.expr):
                k, state = self.expr(child, state)
                if isinstance(node, ast.Starred):
                    kind = k
        return kind, state

    def call(self, node: ast.Call, state: frozenset[str]) -> tuple[str | None, frozenset[str]]:
        func = node.func
        if isinstance(func, ast.Attribute) and self.is_session(func.value):
            for argument in [*node.args, *(k.value for k in node.keywords)]:
                _, state = self.expr(argument, state)
            method = func.attr
            if method in _SESSION_WRITES:
                if method in ("add", "merge", "add_all") and node.args and isinstance(node.args[0], ast.Name):
                    self.orms.add(node.args[0].id)
                return None, self.write(state, node, f"db.{method}()")
            if method in _SESSION_STATEMENTS:
                kind = self.statement_kind(node.args[0]) if node.args else "unknown"
                if kind != "read":
                    label = "db.execute(<DML>)" if kind == "write" else f"db.{method}(<unclassified statement>)"
                    state = self.write(state, node, label)
                return "orm", state
            if method in _SESSION_LOADS:
                return "orm", state
            if method in _SESSION_ENDS:
                return None, EMPTY
            if method == "run_sync":
                self.escape(node, "db.run_sync()")
            return None, state
        name = func.attr if isinstance(func, ast.Attribute) else func.id if isinstance(func, ast.Name) else None
        if name in _DEFERRING:
            for argument in node.args:
                self.expr(argument, EMPTY)  # runs later: evaluated, never applied
            return None, state
        if isinstance(func, ast.Attribute):
            receiver, state = self.expr(func.value, state)
            if receiver == "orm":
                for argument in [*node.args, *(k.value for k in node.keywords)]:
                    _, state = self.expr(argument, state)
                if (
                    func.attr in _COLLECTION_MUTATORS
                    and isinstance(func.value, ast.Attribute)
                    and func.value.attr in MAPPED_ATTRIBUTES
                ) or func.attr in MUTATING_MODEL_METHODS:
                    state = self.write(state, node, f"{ast.unparse(func)}()")
                return "orm", state
            return self.invoke(self.resolve(func), node, state)
        if isinstance(func, ast.Name):
            if func.id in ("setattr", "delattr") and node.args and self.kind_of(node.args[0]) == "orm":
                for argument in node.args:
                    _, state = self.expr(argument, state)
                return None, self.write(state, node, f"{func.id}({ast.unparse(node.args[0])}, ...)")
            if func.id == "flag_modified" and node.args and self.kind_of(node.args[0]) == "orm":
                return None, self.write(state, node, f"flag_modified({ast.unparse(node.args[0])})")
            if func.id in self.nested:
                return self.inline(self.nested[func.id], node, state)
            return self.invoke(self.resolve(func), node, state)
        _, state = self.expr(func, state)
        for argument in [*node.args, *(k.value for k in node.keywords)]:
            _, state = self.expr(argument, state)
        return None, state

    def _arguments(self, node: ast.Call, state: frozenset[str]):
        kinds, keywords = [], {}
        for argument in node.args:
            k, state = self.expr(argument, state)
            kinds.append(k)
        for keyword in node.keywords:
            k, state = self.expr(keyword.value, state)
            keywords[keyword.arg] = k
        return kinds, keywords, state

    def invoke(self, target, node: ast.Call, state: frozenset[str]) -> tuple[str | None, frozenset[str]]:
        kinds, keywords, state = self._arguments(node, state)
        passes_session = "session" in kinds or "session" in keywords.values()
        if not (passes_session or "orm" in kinds or "orm" in keywords.values()):
            return None, state
        summary = self._summary_for(target, kinds, keywords, node, passes_session)
        result = "orm" if passes_session else None
        if summary is None:
            return result, state
        fn = inspect.unwrap(target)
        if (inspect.isgeneratorfunction(fn) or inspect.isasyncgenfunction(fn)) and id(node) not in self.iterated_calls:
            return result, state  # its body runs when the response streams
        self._propagate_raises(summary, state)
        return result, summary.apply(state)

    def _propagate_raises(self, summary: Summary, state: frozenset[str]) -> None:
        for cls, pending, keeps in summary.raises:
            self.raise_sites.append((cls, _cap(pending | state) if keeps else pending))

    def _summary_for(self, target, kinds, keywords, node: ast.Call, passes_session: bool) -> Summary | None:
        if target is None:
            if isinstance(node.func, ast.Name) and hasattr(builtins, node.func.id):
                return None
            if passes_session:
                self.escape(node, f"passes the session to {ast.unparse(node.func)}()")
            return None
        if isinstance(target, type):
            if passes_session and self.engine.is_own(target):
                self.escape(node, f"session captured by {target.__qualname__}()")
            return None
        if isinstance(target, types.BuiltinFunctionType):
            return None
        fn = inspect.unwrap(target) if callable(target) else None
        shift = 0
        if inspect.ismethod(fn):
            shift, fn = 1, fn.__func__
        if not isinstance(fn, types.FunctionType) or not self.engine.is_own(fn):
            return None
        qualified = f"{fn.__module__}.{fn.__qualname__}"
        if qualified in LAZY_INIT_WRITERS or qualified in REVIEWED_HELPERS:
            return None
        params = self.engine.positional_params(fn)
        if params is None:
            if passes_session:
                self.escape(node, f"no source for {fn.__qualname__}")
            return None
        params = params[shift:]
        sessions: set[str] = set()
        orms: set[str] = set()
        for param, kind in [*zip(params, kinds, strict=False), *keywords.items()]:
            if param is None:
                continue
            if kind == "session":
                sessions.add(param)
            elif kind == "orm":
                orms.add(param)
        if self.depth >= _MAX_DEPTH:
            if sessions:
                self.escape(node, f"depth limit at {fn.__qualname__}")
            return None
        falsy = self.engine.falsy_params(fn, node, shift)
        summary = self.engine.summary(fn, frozenset(sessions), frozenset(orms), self.depth + 1, falsy)
        self.escapes.extend(self.engine.escapes.get((fn, frozenset(sessions), frozenset(orms), falsy), ()))
        return summary

    def inline(self, fdef: ast.AST, node: ast.Call, state: frozenset[str]) -> tuple[str | None, frozenset[str]]:
        kinds, keywords, state = self._arguments(node, state)
        child = _Function(self.engine, None, fdef, self.line_offset, self.sessions, self.orms, self.depth + 1, scope=self)
        child.name = f"{self.name}.<{fdef.name}>"
        params = [a.arg for a in fdef.args.posonlyargs + fdef.args.args]
        for param, kind in [*zip(params, kinds, strict=False), *keywords.items()]:
            if param:
                child.bind(ast.Name(id=param), kind, None, EMPTY, node)
        child.nested.pop(fdef.name, None)
        summary = child.summary()
        self.escapes.extend(child.escapes)
        if _is_generator(fdef) and id(node) not in self.iterated_calls:
            return None, state  # its body runs when the response streams
        self._propagate_raises(summary, state)
        return None, summary.apply(state)


class Engine:
    """Summaries shared across routes (a function is summarised once per call shape)."""

    def __init__(self, own_prefixes: tuple[str, ...] = ("app.",)):
        self.own_prefixes = own_prefixes
        self.escapes: dict = {}
        self._summaries: dict = {}
        self._in_progress: set = set()
        self._sources: dict = {}
        self._returns: dict = {}

    def is_own(self, obj) -> bool:
        return (getattr(obj, "__module__", "") or "").startswith(self.own_prefixes)

    def source(self, fn):
        if fn in self._sources:
            return self._sources[fn]
        parsed = None
        try:
            lines, start = inspect.getsourcelines(fn)
            tree = ast.parse(textwrap.dedent("".join(lines)))
            parsed = next(
                ((n, start - 1) for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))),
                None,
            )
        except (OSError, TypeError, SyntaxError):
            parsed = None
        self._sources[fn] = parsed
        return parsed

    def positional_params(self, fn) -> list[str] | None:
        parsed = self.source(fn) if isinstance(fn, types.FunctionType) else None
        if parsed is None:
            return None
        args = parsed[0].args
        return [a.arg for a in args.posonlyargs + args.args]

    def falsy_params(self, fn, call: ast.Call, shift: int) -> frozenset:
        """(name, default is None) of the parameters *call* leaves at a falsy constant."""
        parsed = self.source(fn)
        if parsed is None or any(k.arg is None for k in call.keywords):
            return frozenset()
        if any(isinstance(a, ast.Starred) for a in call.args):
            return frozenset()
        args = parsed[0].args
        names = [a.arg for a in args.posonlyargs + args.args]
        defaults = dict(zip(names[len(names) - len(args.defaults) :], args.defaults, strict=True))
        defaults.update((a.arg, d) for a, d in zip(args.kwonlyargs, args.kw_defaults, strict=True) if d is not None)
        supplied = set(names[shift : shift + len(call.args)])
        explicit = {k.arg: k.value for k in call.keywords}
        out = set()
        for name, default in defaults.items():
            if name in supplied:
                continue
            value = explicit.get(name, default)
            if isinstance(value, ast.Constant) and not value.value:
                out.add((name, value.value is None))
        return frozenset(out)

    def returned_statement_kind(self, target) -> str:
        fn = inspect.unwrap(target) if callable(target) else None
        if not isinstance(fn, types.FunctionType) or not self.is_own(fn):
            return "unknown"
        if fn in self._returns:
            return self._returns[fn]
        self._returns[fn] = "unknown"
        parsed = self.source(fn)
        if parsed is None:
            return "unknown"
        walker = _Function(self, fn, parsed[0], parsed[1], (), (), _MAX_DEPTH)
        kinds = {
            walker.statement_kind(r.value)
            for r in ast.walk(parsed[0])
            if isinstance(r, ast.Return)
            and r.value is not None
            and not (isinstance(r.value, ast.Constant) and r.value.value is None)
        }
        result = "write" if "write" in kinds else "read" if kinds == {"read"} else "unknown"
        self._returns[fn] = result
        return result

    def returned_param(self, target) -> str | None:
        """The parameter every ``return`` of *target* hands back or chains on."""
        fn = inspect.unwrap(target) if callable(target) else None
        if not isinstance(fn, types.FunctionType) or not self.is_own(fn):
            return None
        parsed = self.source(fn)
        if parsed is None:
            return None
        node = parsed[0]
        params = {a.arg for a in node.args.posonlyargs + node.args.args + node.args.kwonlyargs}
        returned = {_chain_root(r.value) for r in ast.walk(node) if isinstance(r, ast.Return) and r.value is not None}
        if len(returned) != 1 or None in returned:
            return None
        (name,) = returned
        # Every value given to the returned name has to chain on the parameter
        # (``query = query.where(...)``); a fresh statement means it is not it.
        stores = [s for s in ast.walk(node) if isinstance(s, ast.Name) and isinstance(s.ctx, ast.Store) and s.id == name]
        values = [
            s.value
            for s in ast.walk(node)
            if isinstance(s, ast.Assign) and any(isinstance(t, ast.Name) and t.id == name for t in s.targets)
        ]
        if len(values) != len(stores):
            return None
        roots = {_chain_root(v) for v in values}
        if name in params:
            return name if roots <= {name} else None
        root = next(iter(roots)) if len(roots) == 1 else None
        return root if root in params else None

    def summary(self, fn, sessions: frozenset, orms: frozenset, depth: int, falsy: frozenset = frozenset()) -> Summary:
        key = (fn, sessions, orms, falsy)
        if key in self._summaries:
            return self._summaries[key]
        if key in self._in_progress:
            return _IDENTITY
        parsed = self.source(fn)
        if parsed is None:
            return _IDENTITY
        self._in_progress.add(key)
        try:
            walker = _Function(self, fn, parsed[0], parsed[1], sessions, orms, depth, falsy=falsy)
            result = walker.summary()
            self.escapes[key] = walker.escapes
        finally:
            self._in_progress.discard(key)
        self._summaries[key] = result
        return result


def _reaches_get_db(dependant) -> bool:
    return any(dep.call is get_db or _reaches_get_db(dep) for dep in dependant.dependencies)


def _bound_names(dependant) -> tuple[frozenset[str], frozenset[str]]:
    """The parameters holding the request session, and the ones holding objects loaded from it."""
    sessions = frozenset(d.name for d in dependant.dependencies if d.call is get_db and d.name)
    orms = frozenset(d.name for d in dependant.dependencies if d.call is not get_db and d.name and _reaches_get_db(d))
    return sessions, orms


def analyze_route(engine: Engine, route: APIRoute) -> RouteReport:
    """The request-session writes *route* may still hold when it answers.

    Its dependencies run first and share its session (FastAPI caches ``get_db``
    per request), so their summaries are applied before the endpoint's.
    """
    state = EMPTY
    escapes: list[str] = []
    seen: set = set()

    def dependencies(dependant) -> None:
        nonlocal state
        for dep in dependant.dependencies:
            if dep.call is get_db or dep.call is None or dep.call in seen:
                continue
            seen.add(dep.call)
            dependencies(dep)
            sessions, orms = _bound_names(dep)
            fn = inspect.unwrap(dep.call)
            if (sessions or orms) and isinstance(fn, types.FunctionType) and engine.is_own(fn):
                state = engine.summary(fn, sessions, orms, 0).apply(state)
                escapes.extend(engine.escapes.get((fn, sessions, orms, frozenset()), ()))

    dependencies(route.dependant)
    sessions, orms = _bound_names(route.dependant)
    fn = inspect.unwrap(route.endpoint)
    pending = engine.summary(fn, sessions, orms, 0).apply(state)
    escapes.extend(engine.escapes.get((fn, sessions, orms, frozenset()), ()))
    return RouteReport(pending=pending, escapes=escapes)


def writer_of(description: str) -> str:
    """``db.add() @ app.services.audit.log_audit:92`` -> ``app.services.audit.log_audit``."""
    return description.rsplit(" @ ", 1)[-1].rsplit(":", 1)[0]
