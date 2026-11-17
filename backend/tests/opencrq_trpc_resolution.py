"""Which OpenCRQ procedures the provider's code sends, resolved statically.

The ``_trpc`` call sites of ``app/integrations/providers/opencrq`` name their procedure as a
``"router.procedure"`` literal, or through a name bound only to literals. This module reads that off
the source, for ``test_opencrq_trpc_contract.py`` (every call against OpenCRQ's procedure list) and
for the tool to procedures mapping ``scripts/dump_opencrq_tool_procedures.py`` commits, and reads the
keys a call's input literal carries (:func:`literal_input_keys`, :func:`tool_input_keys`: XTM One's
own keys, committed as ``tool_input_keys.json``). It stays out of the runtime: the mappings the
runtime reads are the generated files.
"""

from __future__ import annotations

import ast
import functools
import json
from collections.abc import Mapping
from pathlib import Path
from typing import NamedTuple

from tests import app_sources

PROVIDER = app_sources.APP_DIR / "integrations/providers/opencrq"
# Deep enough for a parameter passed down a helper or two; a cycle stops here instead of recursing forever.
_MAX_DEPTH = 5


def _trpc_calls(module: ast.Module) -> list[ast.Call]:
    return [
        node
        for node in app_sources.walk(module)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "_trpc"
    ]


def _procedure_argument(call: ast.Call) -> ast.expr | None:
    if len(call.args) > 2:
        return call.args[2]
    return next((keyword.value for keyword in call.keywords if keyword.arg == "procedure"), None)


def _scope_of(module: ast.Module, node: ast.AST) -> ast.FunctionDef | ast.AsyncFunctionDef | ast.Module:
    parents = app_sources.parents(module)
    scope = node
    while not isinstance(scope, ast.FunctionDef | ast.AsyncFunctionDef | ast.Module):
        scope = parents[scope]
    return scope


def _module_table(module: ast.Module, name: str) -> ast.expr | None:
    """The literal a module-level ``name = {...}`` / ``[...]`` / ``(...)`` binds, if any."""
    for statement in module.body:
        if isinstance(statement, ast.Assign) and [ast.unparse(target) for target in statement.targets] == [name]:
            return statement.value
        if isinstance(statement, ast.AnnAssign) and ast.unparse(statement.target) == name:
            return statement.value
    return None


def _all_or_none(resolved: list[list[str] | None]) -> list[str] | None:
    if not resolved or any(candidates is None for candidates in resolved):
        return None
    return [procedure for candidates in resolved for procedure in candidates or []]


def _resolve(node: ast.AST | None, module: ast.Module, context: ast.AST, depth: int = 0) -> list[str] | None:
    """The procedures ``node`` can evaluate to, or ``None`` when that set is not provably closed.

    Closed means a string literal, a conditional between closed expressions, a lookup in a
    module-level table of literals, or a name every binding of which is closed: an assignment in
    the same function, a loop over a tuple of literals, or a parameter every caller passes.
    """
    if node is None or depth > _MAX_DEPTH:
        return None
    if isinstance(node, ast.Constant):
        return [node.value] if isinstance(node.value, str) else None
    if isinstance(node, ast.IfExp):
        return _all_or_none([_resolve(branch, module, context, depth + 1) for branch in (node.body, node.orelse)])
    if isinstance(node, ast.Subscript) and isinstance(node.value, ast.Name):
        table = _module_table(module, node.value.id)
        if isinstance(table, ast.Dict):
            return _all_or_none([_resolve(value, module, table, depth + 1) for value in table.values])
        if isinstance(table, ast.List | ast.Tuple):
            return _all_or_none([_resolve(element, module, table, depth + 1) for element in table.elts])
        return None
    if isinstance(node, ast.Name):
        return _resolve_name(node.id, module, context, depth)
    return None


def _binds(node: ast.AST, name: str, scope: ast.AST) -> bool:
    if isinstance(node, ast.Name):
        return node.id == name and not isinstance(node.ctx, ast.Load)
    if isinstance(node, ast.ExceptHandler | ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
        return node.name == name and node is not scope
    if isinstance(node, ast.alias):
        return (node.asname or node.name.split(".")[0]) == name
    if isinstance(node, ast.MatchAs | ast.MatchStar):
        return node.name == name
    if isinstance(node, ast.MatchMapping):
        return node.rest == name
    return isinstance(node, ast.Global | ast.Nonlocal) and name in node.names


def _bindings(scope: ast.AST, name: str) -> list[ast.AST]:
    """Every node of ``scope`` that binds ``name``, by whatever statement."""
    return [node for node in ast.walk(scope) if _binds(node, name, scope)]


def _understood_bindings(scope: ast.AST, name: str) -> list[ast.AST]:
    """The bindings of ``name`` ``_resolve_name`` reads: a plain or flat-unpacked assignment, a ``for`` target."""
    understood: list[ast.AST] = []
    for node in ast.walk(scope):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == name:
                    understood.append(target)
                elif isinstance(target, ast.Tuple | ast.List):
                    understood += [e for e in target.elts if isinstance(e, ast.Name) and e.id == name]
        elif isinstance(node, ast.For | ast.AsyncFor):
            if isinstance(node.target, ast.Name) and node.target.id == name:
                understood.append(node.target)
            elif isinstance(node.target, ast.Tuple):
                understood += [e for e in node.target.elts if isinstance(e, ast.Name) and e.id == name]
    return understood


def _resolve_name(name: str, module: ast.Module, context: ast.AST, depth: int) -> list[str] | None:
    scope = _scope_of(module, context)
    # A name resolves only if every statement binding it is one this resolver reads: an augmented
    # assignment, a walrus, a ``with ... as``, an annotated assignment or a reassigned parameter would
    # otherwise change the value without the resolver seeing it.
    understood = {id(node) for node in _understood_bindings(scope, name)}
    if any(id(node) not in understood for node in _bindings(scope, name)):
        return None
    if not isinstance(scope, ast.Module):
        positional = [arg.arg for arg in (*scope.args.posonlyargs, *scope.args.args)]
        if name in positional or name in {arg.arg for arg in scope.args.kwonlyargs}:
            # What the callers pass says nothing of a parameter the body reassigns.
            if _bindings(scope, name):
                return None
            index = positional.index(name) if name in positional else None
            passed = []
            for caller in app_sources.walk(module):
                if isinstance(caller, ast.Call) and isinstance(caller.func, ast.Name) and caller.func.id == scope.name:
                    by_keyword = next((keyword.value for keyword in caller.keywords if keyword.arg == name), None)
                    by_position = caller.args[index] if index is not None and len(caller.args) > index else None
                    passed.append(_resolve(by_keyword or by_position, module, caller, depth + 1))
            return _all_or_none(passed)

    resolved: list[list[str] | None] = []
    for node in ast.walk(scope):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if ast.unparse(target) == name:
                    resolved.append(_resolve(node.value, module, node, depth + 1))
                elif (index := _position_in(target, name)) is not None:
                    resolved.append(_resolve_unpacked(node.value, index, len(target.elts), module, node, depth))
        elif isinstance(node, ast.For | ast.AsyncFor) and name in {
            n.id for n in ast.walk(node.target) if isinstance(n, ast.Name)
        }:
            resolved.append(_resolve_loop(name, node, module, depth))
    return _all_or_none(resolved)


def _position_in(target: ast.AST, name: str) -> int | None:
    """Where ``name`` sits in a flat ``a, b = ...`` target."""
    if not isinstance(target, ast.Tuple | ast.List):
        return None
    return next(
        (i for i, element in enumerate(target.elts) if isinstance(element, ast.Name) and element.id == name), None
    )


def _resolve_unpacked(
    value: ast.AST, index: int, width: int, module: ast.Module, context: ast.AST, depth: int
) -> list[str] | None:
    """``procedure, action = ("a.b", "A")``, through conditionals between such tuples."""
    if depth > _MAX_DEPTH:
        return None
    if isinstance(value, ast.Tuple | ast.List) and len(value.elts) == width:
        return _resolve(value.elts[index], module, context, depth + 1)
    if isinstance(value, ast.IfExp):
        return _all_or_none(
            [
                _resolve_unpacked(branch, index, width, module, context, depth + 1)
                for branch in (value.body, value.orelse)
            ]
        )
    return None


def _resolve_loop(name: str, loop: ast.For | ast.AsyncFor, module: ast.Module, depth: int) -> list[str] | None:
    """``for name in ("a", "b")`` or ``for name, label in (("a", "A"), ("b", "B"))``."""
    if not isinstance(loop.iter, ast.Tuple | ast.List):
        return None
    if isinstance(loop.target, ast.Name):
        return _all_or_none([_resolve(element, module, loop, depth + 1) for element in loop.iter.elts])
    if not isinstance(loop.target, ast.Tuple):
        return None
    index = next(
        (i for i, target in enumerate(loop.target.elts) if isinstance(target, ast.Name) and target.id == name), None
    )
    if index is None:
        return None
    return _all_or_none(
        [
            _resolve(element.elts[index], module, loop, depth + 1)
            if isinstance(element, ast.Tuple | ast.List) and len(element.elts) == len(loop.target.elts)
            else None
            for element in loop.iter.elts
        ]
    )


def _sent_as(call: ast.Call) -> str | None:
    """``_trpc`` sends a query as a GET and a mutation as a POST; ``None`` when that is decided at runtime."""
    mutation = next((keyword.value for keyword in call.keywords if keyword.arg == "mutation"), None)
    if mutation is None:
        return "query"
    if isinstance(mutation, ast.Constant) and isinstance(mutation.value, bool):
        return "mutation" if mutation.value else "query"
    return None


def resolved_calls(module: ast.Module) -> list[tuple[int, list[str] | None]]:
    """``(line, the procedures it can send or None)`` for every ``_trpc`` call of ``module``."""
    return [(call.lineno, _resolve(_procedure_argument(call), module, call)) for call in _trpc_calls(module)]


def called_procedures() -> frozenset[str]:
    return frozenset(
        procedure
        for _, module in app_sources.modules(PROVIDER)
        for _, candidates in resolved_calls(module)
        for procedure in candidates or []
    )


def missing_procedures(called: frozenset[str], served: dict[str, str]) -> list[str]:
    return sorted(called - served.keys())


# --- Which tool sends which procedures -------------------------------------------------


@functools.cache
def _provider_modules() -> dict[str, ast.Module]:
    """``{module name: tree}`` for every module of the provider, ``_common`` included."""
    return {Path(path).stem: module for path, module in app_sources.modules(PROVIDER)}


@functools.cache
def _definitions(module: ast.Module) -> dict[str, list[ast.FunctionDef | ast.AsyncFunctionDef]]:
    definitions: dict[str, list[ast.FunctionDef | ast.AsyncFunctionDef]] = {}
    for node in app_sources.walk(module):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            definitions.setdefault(node.name, []).append(node)
    return definitions


@functools.cache
def _provider_imports(module: ast.Module) -> dict[str, tuple[str, str]]:
    """``{local name: (provider module, name there)}`` for each ``from ._common import name``."""
    imports: dict[str, tuple[str, str]] = {}
    for node in app_sources.walk(module):
        if isinstance(node, ast.ImportFrom) and node.level == 1 and node.module in _provider_modules():
            imports.update({alias.asname or alias.name: (node.module, alias.name) for alias in node.names})
    return imports


def _functions_named(name: str, module: ast.Module) -> list[tuple[ast.Module, ast.FunctionDef | ast.AsyncFunctionDef]]:
    """The provider functions a call to ``name`` from ``module`` can reach: its own, or one it imports."""
    if name in _definitions(module):
        return [(module, function) for function in _definitions(module)[name]]
    if name in _provider_imports(module):
        source, original = _provider_imports(module)[name]
        target = _provider_modules()[source]
        return [(target, function) for function in _definitions(target).get(original, [])]
    return []


def _own_nodes(node: ast.AST):
    """The nodes under ``node`` that run when it does: a nested function runs only when called."""
    for child in ast.iter_child_nodes(node):
        if not isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef):
            yield child
            yield from _own_nodes(child)


def _resolve_bound(
    node: ast.AST | None, module: ast.Module, context: ast.AST, bound: dict[str, list[str] | None]
) -> list[str] | None:
    """``_resolve``, except that a parameter of the function being followed is what its call site passed."""
    if isinstance(node, ast.Name) and node.id in bound:
        return bound[node.id]
    return _resolve(node, module, context)


def _bind_arguments(
    function: ast.FunctionDef | ast.AsyncFunctionDef,
    call: ast.Call,
    module: ast.Module,
    bound: dict[str, list[str] | None],
) -> dict[str, list[str] | None]:
    """What ``call`` passes to each parameter of ``function``, as the procedures it can stand for."""
    positional = [arg.arg for arg in (*function.args.posonlyargs, *function.args.args)]
    passed = dict(zip(positional, call.args, strict=False))
    passed.update({keyword.arg: keyword.value for keyword in call.keywords if keyword.arg})
    return {
        name: None if _bindings(function, name) else _resolve_bound(argument, module, call, bound)
        for name, argument in passed.items()
        if name in positional or name in {arg.arg for arg in function.args.kwonlyargs}
    }


def _reach(
    function: ast.FunctionDef | ast.AsyncFunctionDef,
    module: ast.Module,
    bound: dict[str, list[str] | None],
    depth: int,
) -> list[str] | None:
    """The procedures ``function`` sends, itself or through the provider functions it calls."""
    if depth > _MAX_DEPTH:
        return None
    reached: list[list[str] | None] = []
    for node in _own_nodes(function):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
            continue
        if node.func.id == "_trpc":
            reached.append(_resolve_bound(_procedure_argument(node), module, node, bound))
            continue
        targets = _functions_named(node.func.id, module)
        if len(targets) > 1:
            reached.append(None)
        for target_module, target in targets[:1]:
            reached.append(_reach(target, target_module, _bind_arguments(target, node, module, bound), depth + 1))
    return _all_or_none(reached) if reached else []


def tool_procedures() -> dict[str, list[str] | None]:
    """``{tool name: the procedures its handler can send, sorted}``, ``None`` when that set is not closed.

    A tool is each ``executor.register("<name>", handler)``. Its procedures are the ``_trpc`` calls of
    the handler and of the provider functions it calls, followed down: a parameter takes what that
    call site passes, not what every caller of the helper does, so a helper shared by two tools
    names, for each, only its own procedure.
    """
    tools: dict[str, list[str] | None] = {}
    for module in _provider_modules().values():
        for call in app_sources.walk(module):
            if not (
                isinstance(call, ast.Call)
                and isinstance(call.func, ast.Attribute)
                and call.func.attr == "register"
                and isinstance(call.func.value, ast.Name)
                and call.func.value.id == "executor"
            ):
                continue
            name, handler = call.args[0], call.args[1]
            if not (isinstance(name, ast.Constant) and isinstance(name.value, str)):
                raise ValueError(f"line {call.lineno}: a tool is registered under a name that is not a literal")
            if name.value in tools:
                raise ValueError(f"tool {name.value!r} is registered twice")
            functions = _functions_named(handler.id, module) if isinstance(handler, ast.Name) else []
            reached = _reach(functions[0][1], functions[0][0], {}, 0) if len(functions) == 1 else None
            tools[name.value] = sorted(set(reached)) if reached is not None else None
    return dict(sorted(tools.items()))


# --- Which keys a call's input carries ---------------------------------------------------

# Stands for a value the source does not give: any non-None scalar, which the field check never reads.
RUNTIME_VALUE = "?"
# What a name's value is read through without adding a key to it.
_READING_METHODS = frozenset({"get", "items", "keys", "values", "copy"})
_BRANCHING = (
    ast.If,
    ast.IfExp,
    ast.For,
    ast.AsyncFor,
    ast.While,
    ast.Try,
    ast.Match,
    ast.ListComp,
    ast.DictComp,
    ast.SetComp,
    ast.GeneratorExp,
)


class LiteralInput(NamedTuple):
    """What a ``_trpc`` call's input literal says it sends."""

    #: The JSON the literal builds, a placeholder for each value only known at runtime.
    shape: object
    #: Whether every top-level key is listed (nothing is spread in, added by a call or by a key the source does not spell out).
    closed: bool
    #: The top-level keys the call adds under a condition.
    conditional: frozenset[str]


def _value_shape(node: ast.AST, module: ast.Module, context: ast.AST) -> object:
    """A literal as the JSON it builds; ``RUNTIME_VALUE`` for what is not known until it runs."""
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.Dict | ast.List):
        literal = _literal(node, module, context)
        return RUNTIME_VALUE if literal is None else literal.shape
    return RUNTIME_VALUE


def _literal(node: ast.AST, module: ast.Module, context: ast.AST) -> LiteralInput | None:
    """A dict or list literal as what it sends, ``None`` for anything else."""
    if isinstance(node, ast.List):
        return LiteralInput([_value_shape(element, module, context) for element in node.elts], False, frozenset())
    if not isinstance(node, ast.Dict):
        return None
    shape: dict[str, object] = {}
    conditional: set[str] = set()
    closed = True
    for key, value in zip(node.keys, node.values, strict=True):
        if isinstance(key, ast.Constant) and isinstance(key.value, str):
            shape[key.value] = _value_shape(value, module, context)
            continue
        closed = False
        if key is not None:
            continue
        # ``**x``: a literal spread in adds its keys, a conditional between literals adds them under a condition.
        branches = (value.body, value.orelse) if isinstance(value, ast.IfExp) else (value,)
        for branch in branches:
            spread = _literal(branch, module, context)
            if spread is not None and isinstance(spread.shape, dict):
                shape.update(spread.shape)
                if isinstance(value, ast.IfExp):
                    conditional.update(spread.shape)
    return LiteralInput(shape, closed, frozenset(conditional))


def _is_a_read_of(use: ast.Name, parent: ast.AST) -> bool:
    """A use of the name that can neither add a key to it nor hand it to code that might."""
    if isinstance(parent, ast.keyword):
        return parent.arg == "input"
    if isinstance(parent, ast.Subscript):
        return parent.value is use
    if isinstance(parent, ast.Attribute):
        return parent.attr in _READING_METHODS
    if isinstance(parent, ast.Call):
        return isinstance(parent.func, ast.Name) and parent.func.id == "len" and use in parent.args
    return isinstance(parent, ast.UnaryOp | ast.BoolOp | ast.If | ast.While | ast.IfExp | ast.Compare)


def _is_conditional(node: ast.AST, scope: ast.AST, parents: Mapping[ast.AST, ast.AST]) -> bool:
    """Whether a branch or a loop of ``scope`` stands between it and ``node``."""
    while node is not scope:
        node = parents[node]
        if isinstance(node, _BRANCHING):
            return True
    return False


def _named_dict(
    name: str, module: ast.Module, call: ast.Call, bound: dict[str, list[str] | None] | None = None
) -> LiteralInput | None:
    """A name bound once to a dict literal and completed by ``name["key"] = ...``, read as that literal.

    ``None`` unless that is the only way the name is ever bound. It stops being ``closed`` as soon as it is
    handed to anything that could add a key (``update``, a call, a key the source does not spell out).
    A key a parameter names is what *bound* says the call site passed, when the call is followed from a tool.
    """
    scope = _scope_of(module, call)
    parents = app_sources.parents(module)
    bindings = _bindings(scope, name)
    if len(bindings) != 1:
        return None
    holder = parents[bindings[0]]
    if not (isinstance(holder, ast.Assign | ast.AnnAssign) and isinstance(holder.value, ast.Dict)):
        return None
    literal = _literal(holder.value, module, holder)
    if literal is None or not isinstance(literal.shape, dict):
        return None
    shape, closed, conditional = dict(literal.shape), literal.closed, set(literal.conditional)
    for use in ast.walk(scope):
        if not (isinstance(use, ast.Name) and use.id == name and isinstance(use.ctx, ast.Load)):
            continue
        parent = parents[use]
        if isinstance(parent, ast.Subscript) and parent.value is use and isinstance(parent.ctx, ast.Store | ast.Del):
            spelled = _resolve_bound(parent.slice, module, parent, bound or {})
            if spelled is None:
                closed = False
            for key in spelled or []:
                shape.setdefault(key, RUNTIME_VALUE)
                if _is_conditional(parent, scope, parents):
                    conditional.add(key)
        elif not _is_a_read_of(use, parent):
            closed = False
    return LiteralInput(shape, closed, frozenset(conditional))


def literal_input_keys(
    call: ast.Call, module: ast.Module, bound: dict[str, list[str] | None] | None = None
) -> LiteralInput | None:
    """The input a ``_trpc`` call sends, read from the source, ``None`` when it is not a literal.

    A dict or list literal, or a name bound once to a dict literal and completed by ``name["key"] = ...``;
    anything else (no input, one built by a call, a name bound twice) is ``None``, which the key tests
    skip. A key added under a condition counts as sent but is listed as conditional: the check refuses an
    unknown key, and only a *closed* input is held to ``required``, so reading generously never refuses
    a payload the code builds correctly.
    """
    given = next((keyword.value for keyword in call.keywords if keyword.arg == "input"), None)
    if isinstance(given, ast.Name):
        return _named_dict(given.id, module, call, bound)
    return _literal(given, module, call) if given is not None else None


def _shape_paths(shape: object, prefix: str = "") -> set[str]:
    """Every key path of a literal's shape: ``a``, ``a.b``, ``rows[].k`` (the paths ``known_paths`` lists)."""
    paths: set[str] = set()
    if isinstance(shape, dict):
        for key, value in shape.items():
            path = f"{prefix}.{key}" if prefix else key
            paths.add(path)
            paths |= _shape_paths(value, path)
    elif isinstance(shape, list):
        for element in shape:
            paths |= _shape_paths(element, f"{prefix}[]")
    return paths


def _reached_calls(
    function: ast.FunctionDef | ast.AsyncFunctionDef,
    module: ast.Module,
    bound: dict[str, list[str] | None],
    depth: int,
) -> list[tuple[ast.Call, ast.Module, dict[str, list[str] | None]]]:
    """The ``_trpc`` calls ``function`` sends, itself or through the provider functions it calls, as ``_reach`` follows them.

    Each comes with the module it sits in and what the call sites on the way passed to the parameters.
    """
    if depth > _MAX_DEPTH:
        return []
    reached: list[tuple[ast.Call, ast.Module, dict[str, list[str] | None]]] = []
    for node in _own_nodes(function):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
            continue
        if node.func.id == "_trpc":
            reached.append((node, module, bound))
            continue
        for target_module, target in _functions_named(node.func.id, module)[:1]:
            reached += _reached_calls(target, target_module, _bind_arguments(target, node, module, bound), depth + 1)
    return reached


def tool_input_keys() -> dict[str, list[dict[str, object]]]:
    """``{tool name: [{"procedures", "fields", "conditional", "closed"}]}``: the input keys each tool's handler sends.

    One entry per ``_trpc`` call the handler reaches (see :func:`tool_procedures`) whose input is a
    literal (:func:`literal_input_keys`), the call sites on the way giving the parameters their own
    values, so a helper shared by two tools lists for each only the keys that tool's call site names.
    ``fields`` is every key path the literal spells out, ``conditional`` those added under a condition;
    a call that sends several procedures lists the keys of the one input they all take. ``closed`` says
    every top-level key is spelled out, so a required field of the instance's schema that is not in
    ``fields`` is one the tool never sends.
    """
    keys: dict[str, list[dict[str, object]]] = {}
    for module in _provider_modules().values():
        for call in app_sources.walk(module):
            if not (
                isinstance(call, ast.Call)
                and isinstance(call.func, ast.Attribute)
                and call.func.attr == "register"
                and isinstance(call.func.value, ast.Name)
                and call.func.value.id == "executor"
            ):
                continue
            name, handler = call.args[0], call.args[1]
            functions = _functions_named(handler.id, module) if isinstance(handler, ast.Name) else []
            if len(functions) != 1:
                continue
            entries: dict[str, dict[str, object]] = {}
            for trpc, trpc_module, bound in _reached_calls(functions[0][1], functions[0][0], {}, 0):
                literal = literal_input_keys(trpc, trpc_module, bound)
                procedures = _resolve_bound(_procedure_argument(trpc), trpc_module, trpc, bound)
                fields = _shape_paths(literal.shape) if literal is not None else set()
                if not (fields and procedures):
                    continue
                conditional = {
                    path
                    for path in fields
                    for key in literal.conditional
                    if path == key or path.startswith((f"{key}.", f"{key}["))
                }
                entry = {
                    "procedures": sorted(set(procedures)),
                    "fields": sorted(fields),
                    "conditional": sorted(conditional),
                    "closed": literal.closed,
                }
                entries[json.dumps(entry, sort_keys=True)] = entry
            if entries:
                keys[name.value] = sorted(entries.values(), key=lambda e: (e["procedures"], e["fields"]))
    return dict(sorted(keys.items()))
