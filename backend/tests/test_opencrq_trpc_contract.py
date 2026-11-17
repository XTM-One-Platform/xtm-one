"""The OpenCRQ provider calls only procedures OpenCRQ serves.

The provider names every OpenCRQ tRPC procedure as a ``"router.procedure"``
string, and OpenCRQ renames its routers without knowing who calls them: a
renamed procedure answers ``NOT_FOUND`` and the tool fails for the user, with
no test noticing. ``data/opencrq_trpc_procedures.json`` is OpenCRQ's own list,
generated from its router and pinned at a commit; these tests hold the
provider's calls against it.

Each ``_trpc`` call must resolve statically to the procedures it can send, so
that none escapes the check: a name built at runtime is refused. The resolution
is ``opencrq_trpc_resolution``, which also reads the keys a call's input literal carries.

The pin holds paths and types only, never an input schema: what an OpenCRQ instance accepts is
read from the instance itself at runtime (``providers/opencrq/procedure_manifest.py``).

Refreshing the copy (``scripts/refresh_opencrq_trpc_procedures.py``) is the
explicit step that surfaces OpenCRQ's renames and removals: this suite then
names each call they break.
"""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path

import pytest

from tests import app_sources
from tests.opencrq_trpc_resolution import (
    PROVIDER,
    LiteralInput,
    _procedure_argument,
    _resolve,
    _sent_as,
    _trpc_calls,
    called_procedures,
    literal_input_keys,
    missing_procedures,
    resolved_calls,
)

_PINNED = Path(__file__).with_name("data") / "opencrq_trpc_procedures.json"
# The layout of the pinned file this suite reads; OpenCRQ bumps it on a change of shape.
_MANIFEST_FORMAT = 1


def _manifest() -> dict:
    return json.loads(_PINNED.read_text(encoding="utf-8"))


def _served(manifest: dict) -> dict[str, str]:
    """``{procedure path: "query" | "mutation" | "subscription"}``."""
    return {procedure["path"]: procedure["type"] for procedure in manifest["procedures"]}


def test_the_pinned_copy_names_the_commit_it_was_taken_at():
    manifest = _manifest()
    assert manifest["formatVersion"] == _MANIFEST_FORMAT, "the pinned file changed shape: update this suite"
    assert re.fullmatch(r"[0-9a-f]{40}", manifest["pinnedFrom"]["commit"])
    assert manifest["procedures"], "an empty pinned copy would let every call through"


def test_every_trpc_call_resolves_to_the_procedures_it_can_send():
    calls = [
        (path, line, candidates)
        for path, module in app_sources.modules(PROVIDER)
        for line, candidates in resolved_calls(module)
    ]
    assert calls, "no _trpc call found: the provider moved, and this suite tests nothing"

    unresolved = [f"{path}:{line}" for path, line, candidates in calls if candidates is None]
    assert not unresolved, (
        "pass the procedure to _trpc as a 'router.procedure' literal, or through a name bound only to "
        f"literals (a conditional, a loop over a tuple, a module-level table, a parameter every caller passes one): {unresolved}"
    )


def test_the_provider_calls_only_procedures_opencrq_serves():
    missing = missing_procedures(called_procedures(), _served(_manifest()))
    assert not missing, (
        f"OpenCRQ does not serve {len(missing)} procedure(s) the provider calls: {missing}. "
        "Remap each tool to the procedure that replaced it, or drop the tool when none did."
    )


def test_each_call_uses_the_http_method_of_its_procedures():
    served = _served(_manifest())
    mismatched = []
    for path, module in app_sources.modules(PROVIDER):
        for call in _trpc_calls(module):
            sent = _sent_as(call)
            for procedure in _resolve(_procedure_argument(call), module, call) or []:
                if procedure in served and served[procedure] != sent:
                    mismatched.append(
                        f"{path}:{call.lineno} {procedure} sent as a {sent}, served as a {served[procedure]}"
                    )
    assert not mismatched, f"pass mutation=True for a mutation and leave it out for a query: {mismatched}"


def test_a_procedure_renamed_in_the_pinned_copy_fails_the_guard():
    called = called_procedures()
    served = _served(_manifest())
    renamed = next((procedure for procedure in sorted(called) if procedure in served), None)
    if renamed is None:
        pytest.fail("the provider calls no procedure OpenCRQ serves, so the guard cannot be shown to work")

    served[f"{renamed}Renamed"] = served.pop(renamed)

    assert missing_procedures(called, served) == sorted({renamed} | (called - _served(_manifest()).keys()))


# --- The resolution itself, on code shaped like the provider's ------------------------


_CLOSED = {
    "literal": ('await _trpc(b, h, "jobs.getJob", "get job")', ["jobs.getJob"]),
    "conditional": (
        'p = "jobs.cancelJob" if a == "cancel" else "jobs.retryFromDlq"\nawait _trpc(b, h, p, "job")',
        ["jobs.cancelJob", "jobs.retryFromDlq"],
    ),
    "loop over pairs": (
        'for p, label in (("a.getA", "A"), ("b.getB", "B")):\n    await _trpc(b, h, p, label)',
        ["a.getA", "b.getB"],
    ),
    "module table": ('p = _TABLE[action]\nawait _trpc(b, h, p, "x")', ["t.renameT", "t.publishT"]),
    "unpacked conditional": (
        'p, label = ("a.getA", "A") if a else ("a.getAuto", "Auto")\nawait _trpc(b, h, p, label)',
        ["a.getA", "a.getAuto"],
    ),
    "unpacked in branches": (
        'if a:\n    p, params = "a.getA", {"id": a}\nelse:\n    p, params = "a.getB", {}\nawait _trpc(b, h, p, "x", input=params)',
        ["a.getA", "a.getB"],
    ),
}

_OPEN = {
    "f-string": 'await _trpc(b, h, f"jobs.{action}Job", "x")',
    "format": 'p = "jobs.{}Job".format(action)\nawait _trpc(b, h, p, "x")',
    "join": 'p = ".".join(("jobs", action))\nawait _trpc(b, h, p, "x")',
    "call": 'p = build_procedure(action)\nawait _trpc(b, h, p, "x")',
    "local table": 'table = {k: f"t.{k}" for k in ks}\nawait _trpc(b, h, table[action], "x")',
    "loop over a runtime iterable": 'for p in procedures:\n    await _trpc(b, h, p, "x")',
    "one runtime branch": 'p = "jobs.getJob" if a else name\nawait _trpc(b, h, p, "x")',
    "unpacked from a call": "p, label = pick(action)\nawait _trpc(b, h, p, label)",
    "augmented": 'p = "jobs.getJob"\np += action\nawait _trpc(b, h, p, "x")',
    "walrus": 'p = "jobs.getJob"\nif (p := pick(action)):\n    await _trpc(b, h, p, "x")',
    "annotated": 'p: str = "jobs.getJob"\nawait _trpc(b, h, p, "x")',
    "with as": 'p = "jobs.getJob"\nwith ctx() as p:\n    await _trpc(b, h, p, "x")',
    "except as": 'p = "jobs.getJob"\ntry:\n    pass\nexcept Exception as p:\n    await _trpc(b, h, p, "x")',
    "rebound in a later loop": 'p = "jobs.getJob"\nfor p in runtime():\n    pass\nawait _trpc(b, h, p, "x")',
    "nested loop target": 'for (p, (q, r)) in (("a.getA", ("x", "y")),):\n    await _trpc(b, h, q, "x")',
    "reassigned parameter": 'action = pick(action)\nawait _trpc(b, h, action, "x")',
}


def _tool(body: str) -> ast.Module:
    source = '_TABLE = {"rename": "t.renameT", "publish": "t.publishT"}\n\nasync def tool(b, h, a, action):\n'
    return ast.parse(source + "".join(f"    {line}\n" for line in body.splitlines()))


@pytest.mark.parametrize("body, expected", list(_CLOSED.values()), ids=list(_CLOSED))
def test_a_procedure_bound_to_literals_resolves_to_them(body, expected):
    assert [candidates for _, candidates in resolved_calls(_tool(body))] == [expected]


@pytest.mark.parametrize("body", list(_OPEN.values()), ids=list(_OPEN))
def test_a_procedure_built_at_runtime_does_not_resolve(body):
    assert [candidates for _, candidates in resolved_calls(_tool(body))] == [None]


def test_a_parameter_resolves_to_what_every_caller_passes():
    module = ast.parse(
        "async def fetch(b, h, procedure):\n"
        '    return await _trpc(b, h, procedure, "x")\n'
        "async def tool(b, h, name):\n"
        '    await fetch(b, h, "a.getA")\n'
        '    await fetch(b, h, procedure="b.getB")\n'
    )
    assert [candidates for _, candidates in resolved_calls(module)] == [["a.getA", "b.getB"]]


def test_a_parameter_one_caller_passes_at_runtime_does_not_resolve():
    module = ast.parse(
        "async def fetch(b, h, procedure):\n"
        '    return await _trpc(b, h, procedure, "x")\n'
        "async def tool(b, h, name):\n"
        '    await fetch(b, h, "a.getA")\n'
        "    await fetch(b, h, name)\n"
    )
    assert [candidates for _, candidates in resolved_calls(module)] == [None]


@pytest.mark.parametrize("rebinding", ["procedure += suffix", 'procedure = "a.getB"'])
def test_a_parameter_the_function_reassigns_does_not_resolve(rebinding):
    module = ast.parse(
        "async def fetch(b, h, procedure):\n"
        f"    {rebinding}\n"
        '    return await _trpc(b, h, procedure, "x")\n'
        "async def tool(b, h):\n"
        '    await fetch(b, h, "a.getA")\n'
    )
    assert [candidates for _, candidates in resolved_calls(module)] == [None]


def _trpc_references_outside_a_direct_call(module: ast.Module) -> list[int]:
    """Lines where ``_trpc`` is named other than as the callee of ``_trpc(...)`` or in an import."""
    callees = {id(node.func) for node in app_sources.walk(module) if isinstance(node, ast.Call)}
    return sorted(
        node.lineno
        for node in app_sources.walk(module)
        if (isinstance(node, ast.Name) and node.id == "_trpc" and id(node) not in callees)
        or (isinstance(node, ast.Attribute) and node.attr == "_trpc")
        or (isinstance(node, ast.alias) and node.asname == "_trpc" and node.name != "_trpc")
        or (isinstance(node, ast.alias) and node.name == "_trpc" and node.asname not in (None, "_trpc"))
    )


def test_the_provider_refers_to_trpc_only_as_the_callee_of_a_direct_call():
    aliased = [
        f"{path}:{line}"
        for path, module in app_sources.modules(PROVIDER)
        for line in _trpc_references_outside_a_direct_call(module)
    ]
    assert not aliased, (
        f"call _trpc(...) directly so the contract test sees the call: an alias, an attribute access or a partial hides it: {aliased}"
    )


@pytest.mark.parametrize(
    "source",
    [
        "x = _common._trpc(b, h, 'a.getA', 'x')",
        "f = _trpc",
        "g = partial(_trpc, b, h)",
        "from ._common import _trpc as call",
        "run(_trpc)",
        "table = {'a': _trpc}",
    ],
)
def test_an_alias_of_trpc_is_found(source):
    assert _trpc_references_outside_a_direct_call(ast.parse(source))


def test_a_direct_call_and_the_import_of_trpc_are_no_alias():
    module = ast.parse("from ._common import _trpc\nasync def t():\n    await _trpc(b, h, 'a.getA', 'x')\n")
    assert _trpc_references_outside_a_direct_call(module) == []


def test_a_mutation_flag_decided_at_runtime_is_no_method():
    call = ast.parse('_trpc(b, h, "a.setA", "x", mutation=writes)').body[0].value
    assert _sent_as(call) is None


# --- The keys a call's input carries, read from code shaped like the provider's -----------


def _input_of(body: str) -> LiteralInput | None:
    module = ast.parse("async def tool(b, h, a, action):\n" + "".join(f"    {line}\n" for line in body.splitlines()))
    (call,) = _trpc_calls(module)
    return literal_input_keys(call, module)


def test_a_dict_literal_is_an_input_that_lists_every_key():
    literal = _input_of(
        'await _trpc(b, h, "a.setA", "x", input={"id": a, "kind": "cve", "gone": None, "nested": {"k": 1}})'
    )
    assert literal == LiteralInput({"id": "?", "kind": "cve", "gone": None, "nested": {"k": 1}}, True, frozenset())


def test_a_spread_or_a_key_built_at_runtime_leaves_the_input_open():
    spread = _input_of('await _trpc(b, h, "a.setA", "x", input={"id": a, **extra})')
    computed = _input_of('await _trpc(b, h, "a.setA", "x", input={"id": a, action: 1})')
    assert spread == LiteralInput({"id": "?"}, False, frozenset())
    assert computed == LiteralInput({"id": "?"}, False, frozenset())


def test_a_literal_spread_in_under_a_condition_adds_conditional_keys():
    literal = _input_of('await _trpc(b, h, "a.setA", "x", input={"id": a, **({"ownerId": a} if a else {})})')
    assert literal == LiteralInput({"id": "?", "ownerId": "?"}, False, frozenset({"ownerId"}))


def test_a_name_completed_by_key_assignments_is_read_as_one_input():
    literal = _input_of(
        'payload = {"id": a}\n'
        'payload["always"] = 1\n'
        "if a:\n"
        '    payload["sometimes"] = 2\n'
        'await _trpc(b, h, "a.setA", "x", input=payload)'
    )
    assert literal == LiteralInput({"id": "?", "always": "?", "sometimes": "?"}, True, frozenset({"sometimes"}))


def test_a_key_assigned_from_a_loop_over_literals_is_read_as_each_of_them():
    literal = _input_of(
        "payload = {}\n"
        'for key, value in (("first", a), ("second", a)):\n'
        "    payload[key] = value\n"
        'await _trpc(b, h, "a.setA", "x", input=payload)'
    )
    assert literal == LiteralInput({"first": "?", "second": "?"}, True, frozenset({"first", "second"}))


@pytest.mark.parametrize(
    "handling",
    ["payload.update(extra)", "payload[action] = 1", "fill(payload)", "other = payload"],
    ids=["update", "runtime key", "passed to a call", "aliased"],
)
def test_a_name_handed_to_code_that_could_add_a_key_is_no_longer_closed(handling):
    literal = _input_of(f'payload = {{"id": a}}\n{handling}\nawait _trpc(b, h, "a.setA", "x", input=payload)')
    assert literal is not None and literal.closed is False


def test_reading_a_name_does_not_open_it():
    literal = _input_of(
        'payload = {"id": a}\n'
        "if len(payload) == 1 or not payload or payload.get('id'):\n"
        "    pass\n"
        'await _trpc(b, h, "a.setA", "x", input=payload)'
    )
    assert literal is not None and literal.closed is True


def test_a_list_literal_is_read_through_its_elements():
    literal = _input_of('await _trpc(b, h, "a.setA", "x", input=[{"id": a}])')
    assert literal is not None and literal.shape == [{"id": "?"}]


@pytest.mark.parametrize(
    "body",
    [
        'await _trpc(b, h, "a.getA", "x")',
        'await _trpc(b, h, "a.setA", "x", input=build(a))',
        'ids = clean(a)\nawait _trpc(b, h, "a.setA", "x", input=ids)',
        'p = {"id": a}\nif a:\n    p = {"id": a, "x": 1}\nawait _trpc(b, h, "a.setA", "x", input=p)',
        'p = {"id": a}\np |= {"x": 1}\nawait _trpc(b, h, "a.setA", "x", input=p)',
    ],
    ids=["no input", "built by a call", "bound to a call", "bound twice", "augmented"],
)
def test_an_input_that_is_no_literal_is_not_read(body):
    assert _input_of(body) is None
