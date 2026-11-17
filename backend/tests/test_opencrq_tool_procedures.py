"""Each OpenCRQ tool is mapped to the tRPC procedures and the input keys it sends.

``providers/opencrq/tool_procedures.json`` is generated from the provider's source
(``scripts/dump_opencrq_tool_procedures.py``), so the tools an OpenCRQ instance cannot serve can be
named without reading the code at runtime, and ``tool_input_keys.json`` beside it lists the keys each
tool sends. These tests hold both files to the code, to the tool list and to the pin of OpenCRQ's
procedures (``data/opencrq_trpc_procedures.json``: paths and types, never an input schema).
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from app.integrations.providers.opencrq import build_tools
from app.integrations.providers.opencrq.procedure_manifest import PROCEDURE
from app.tools.base import CallableToolExecutor
from scripts.dump_opencrq_tool_procedures import KEYS_SNAPSHOT, SNAPSHOT, render, render_keys
from tests import app_sources, opencrq_trpc_resolution
from tests.opencrq_trpc_resolution import (
    PROVIDER,
    _definitions,
    _provider_imports,
    _provider_modules,
    called_procedures,
    tool_input_keys,
    tool_procedures,
)

_PINNED = Path(__file__).with_name("data") / "opencrq_trpc_procedures.json"


def _manifest() -> dict:
    return json.loads(_PINNED.read_text(encoding="utf-8"))


def _built_tool_names() -> set[str]:
    return {tool.name for tool in build_tools("https://x||t", CallableToolExecutor())}


def test_the_committed_map_matches_the_source():
    assert SNAPSHOT.read_text(encoding="utf-8") == render(), (
        "tool_procedures.json is stale. Run `python -m scripts.dump_opencrq_tool_procedures` from backend/ "
        "and read the diff: each procedure added to a tool is one more an OpenCRQ version must serve."
    )


def test_the_map_names_exactly_the_tools_build_tools_offers():
    assert set(json.loads(SNAPSHOT.read_text(encoding="utf-8"))) == _built_tool_names()


def test_every_tool_sends_a_closed_set_of_procedures():
    unresolved = sorted(tool for tool, procedures in tool_procedures().items() if procedures is None)
    assert not unresolved, f"pass these tools' procedures as literals, or names bound only to literals: {unresolved}"


def test_every_procedure_a_tool_sends_is_one_the_pin_serves():
    served = {procedure["path"] for procedure in _manifest()["procedures"]}
    sent = {procedure for procedures in tool_procedures().values() for procedure in procedures or []}
    assert sorted(sent - served) == []


def test_the_manifest_query_is_a_query_the_pin_lists():
    # The probe sends it without ``_trpc``, so no contract test resolves it.
    pinned = {procedure["path"]: procedure["type"] for procedure in _manifest()["procedures"]}
    assert pinned.get(PROCEDURE) == "query"


def test_the_committed_keys_match_the_source():
    assert KEYS_SNAPSHOT.read_text(encoding="utf-8") == render_keys(), (
        "tool_input_keys.json is stale. Run `python -m scripts.dump_opencrq_tool_procedures` from backend/ "
        "and read the diff: each key added to a tool is one more an OpenCRQ instance must accept."
    )


def test_the_keys_name_only_tools_and_procedures_the_map_knows():
    sent = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    keys = json.loads(KEYS_SNAPSHOT.read_text(encoding="utf-8"))
    assert set(keys) <= set(sent)
    for tool, calls in keys.items():
        for call in calls:
            assert set(call["procedures"]) <= set(sent[tool]), tool
            assert call["fields"] and set(call["conditional"]) <= set(call["fields"]), tool


def test_a_call_that_can_go_to_several_procedures_sends_each_field_to_all_of_them():
    # OpenCRQ's contract test holds a conditional field to the required fields of every procedure of the call:
    # it cannot tell which of them receives a field added under a condition.
    keys = json.loads(KEYS_SNAPSHOT.read_text(encoding="utf-8"))
    mixed = sorted(
        tool for tool, calls in keys.items() for call in calls if len(call["procedures"]) > 1 and call["conditional"]
    )
    assert not mixed, f"give each procedure its own call where only some of them receive a conditional field: {mixed}"


def test_every_procedure_the_provider_calls_belongs_to_a_tool():
    sent = {procedure for procedures in tool_procedures().values() for procedure in procedures or []}
    assert sorted(called_procedures() - sent) == [], "a call no tool reaches: the map would never name it"


def _senders(module: ast.Module) -> set[str]:
    """The functions of ``module`` with a ``_trpc`` call anywhere in their body."""
    return {
        name
        for name, functions in _definitions(module).items()
        if any(
            isinstance(node, ast.Call) and getattr(node.func, "id", None) == "_trpc"
            for f in functions
            for node in ast.walk(f)
        )
    }


def test_a_function_that_sends_a_procedure_is_only_ever_called_by_name():
    """The map follows direct calls by name: a sender passed as a callback or reached as an attribute
    would send its procedures without the map naming them, and its tool would stay offered to an
    OpenCRQ version that cannot serve it."""
    senders = {stem: _senders(module) for stem, module in _provider_modules().items()}
    every_sender = set().union(*senders.values())
    escaped = []
    for path, module in app_sources.modules(PROVIDER):
        reachable = senders[Path(path).stem] | {
            local for local, (source, original) in _provider_imports(module).items() if original in senders[source]
        }
        parents = app_sources.parents(module)
        for node in app_sources.walk(module):
            parent = parents.get(node)
            if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load) and node.id in reachable:
                called = isinstance(parent, ast.Call) and parent.func is node
                registered = isinstance(parent, ast.Call) and getattr(parent.func, "attr", None) == "register"
                if not (called or registered):
                    escaped.append(f"{path}:{node.lineno} {node.id}")
            elif (
                isinstance(node, ast.Attribute)
                and node.attr in every_sender
                and getattr(node.value, "id", None) != "executor"
            ):
                escaped.append(f"{path}:{node.lineno} .{node.attr}")
    assert escaped == [], f"call these directly by name, so the map follows them: {escaped}"


# --- The mapping itself, on code shaped like the provider's ---------------------------

_COMMON = (
    "async def _trpc(base_url, headers, procedure, action): ...\n"
    "async def _resolve_tenant(base_url, headers, tenant_id):\n"
    '    return await _trpc(base_url, headers, "tenants.getTenants", "list tenants")\n'
)


def _mapped(monkeypatch: pytest.MonkeyPatch, tools: str) -> dict[str, list[str] | None]:
    source = "from ._common import _resolve_tenant, _trpc\n\ndef register(b, h, executor):\n" + "".join(
        f"    {line}\n" for line in tools.splitlines()
    )
    modules = {"_common": ast.parse(_COMMON), "tools": ast.parse(source)}
    monkeypatch.setattr(opencrq_trpc_resolution, "_provider_modules", lambda: modules)
    return tool_procedures()


def test_a_helper_shared_by_two_tools_names_for_each_the_procedure_it_is_given(monkeypatch):
    mapped = _mapped(
        monkeypatch,
        "async def fetch(procedure):\n"
        '    return await _trpc(b, h, procedure, "x")\n'
        "async def first():\n"
        '    await fetch("a.getA")\n'
        "async def second():\n"
        '    await fetch(procedure="b.getB")\n'
        'executor.register("first", first)\n'
        'executor.register("second", second)\n',
    )
    assert mapped == {"first": ["a.getA"], "second": ["b.getB"]}


def test_resolving_the_tenant_adds_the_procedure_that_lists_tenants(monkeypatch):
    mapped = _mapped(
        monkeypatch,
        "async def tool(tenant_id):\n"
        "    await _resolve_tenant(b, h, tenant_id)\n"
        '    await _trpc(b, h, "a.getA", "x")\n'
        'executor.register("tool", tool)\n',
    )
    assert mapped == {"tool": ["a.getA", "tenants.getTenants"]}


def test_a_tool_calling_another_sends_what_both_send(monkeypatch):
    mapped = _mapped(
        monkeypatch,
        "async def get():\n"
        '    await _trpc(b, h, "a.getA", "x")\n'
        "async def manage():\n"
        "    await get()\n"
        '    await _trpc(b, h, "a.setA", "x", mutation=True)\n'
        'executor.register("manage", manage)\n',
    )
    assert mapped == {"manage": ["a.getA", "a.setA"]}


def test_a_tool_that_sends_nothing_is_mapped_to_no_procedure(monkeypatch):
    assert _mapped(monkeypatch, 'async def tool():\n    return "done"\nexecutor.register("tool", tool)\n') == {
        "tool": []
    }


@pytest.mark.parametrize(
    "tool",
    [
        'async def tool(action):\n    p = pick(action)\n    await _trpc(b, h, p, "x")\n',
        'async def fetch(procedure):\n    await _trpc(b, h, procedure, "x")\nasync def tool(action):\n    await fetch(action)\n',
        'async def fetch(procedure):\n    procedure += "s"\n    await _trpc(b, h, procedure, "x")\nasync def tool():\n    await fetch("a.getA")\n',
    ],
    ids=["runtime name", "runtime argument to a helper", "parameter the helper reassigns"],
)
def test_a_tool_sending_a_procedure_decided_at_runtime_is_mapped_to_none(monkeypatch, tool):
    assert _mapped(monkeypatch, tool + 'executor.register("tool", tool)\n') == {"tool": None}


def test_a_tool_registered_under_a_runtime_name_is_refused(monkeypatch):
    with pytest.raises(ValueError, match="not a literal"):
        _mapped(monkeypatch, "async def tool():\n    ...\nexecutor.register(name, tool)\n")


# --- The input keys each tool sends -----------------------------------------------------


def _keys_of(monkeypatch: pytest.MonkeyPatch, tools: str) -> dict[str, list[dict[str, object]]]:
    source = "from ._common import _trpc\n\ndef register(b, h, executor):\n" + "".join(
        f"    {line}\n" for line in tools.splitlines()
    )
    monkeypatch.setattr(opencrq_trpc_resolution, "_provider_modules", lambda: {"tools": ast.parse(source)})
    return tool_input_keys()


def test_a_tool_lists_the_keys_its_literal_sends_and_the_ones_added_under_a_condition(monkeypatch):
    keys = _keys_of(
        monkeypatch,
        "async def tool(flag):\n"
        '    payload = {"id": 1, "nested": {"k": 1}, "rows": [{"r": 1}]}\n'
        "    if flag:\n"
        '        payload["extra"] = 2\n'
        '    await _trpc(b, h, "a.setA", "x", input=payload, mutation=True)\n'
        'executor.register("tool", tool)\n',
    )
    assert keys == {
        "tool": [
            {
                "procedures": ["a.setA"],
                "fields": ["extra", "id", "nested", "nested.k", "rows", "rows[].r"],
                "conditional": ["extra"],
                "closed": True,
            }
        ]
    }


def test_a_key_a_shared_helper_names_by_parameter_belongs_to_the_tool_that_passed_it(monkeypatch):
    keys = _keys_of(
        monkeypatch,
        "async def fetch(procedure, field):\n"
        '    payload = {"id": 1}\n'
        "    payload[field] = 2\n"
        '    return await _trpc(b, h, procedure, "x", input=payload)\n'
        "async def first():\n"
        '    await fetch("a.getA", "alpha")\n'
        "async def second():\n"
        '    await fetch("b.getB", "beta")\n'
        'executor.register("first", first)\n'
        'executor.register("second", second)\n',
    )
    assert keys == {
        "first": [{"procedures": ["a.getA"], "fields": ["alpha", "id"], "conditional": [], "closed": True}],
        "second": [{"procedures": ["b.getB"], "fields": ["beta", "id"], "conditional": [], "closed": True}],
    }


def test_a_call_without_a_literal_input_lists_nothing(monkeypatch):
    keys = _keys_of(
        monkeypatch,
        "async def tool(a):\n"
        '    await _trpc(b, h, "a.getA", "x")\n'
        '    await _trpc(b, h, "a.getB", "x", input=build(a))\n'
        'executor.register("tool", tool)\n',
    )
    assert keys == {}


def test_an_input_with_a_key_the_source_does_not_spell_out_is_not_closed(monkeypatch):
    keys = _keys_of(
        monkeypatch,
        "async def tool(extra):\n"
        '    payload = {"id": 1}\n'
        "    payload.update(extra)\n"
        '    await _trpc(b, h, "a.setA", "x", input=payload, mutation=True)\n'
        'executor.register("tool", tool)\n',
    )
    assert keys == {"tool": [{"procedures": ["a.setA"], "fields": ["id"], "conditional": [], "closed": False}]}
