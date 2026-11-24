"""The OpenCRQ tools follow the procedure manifest the connected OpenCRQ serves.

``providers/opencrq/procedure_manifest.py`` reads the manifest, ``build_tools`` withholds the tools
it cannot serve and refuses their calls by name. With no registration the tools are all offered and
the first call of one of them reads ``procedureManifest`` once for the build, then refuses what the
instance cannot serve; an instance that does not answer fails open.

XTM One commits no OpenCRQ schema: every manifest here is built in the test, from procedure paths
(XTM One's own, from ``tool_procedures.json``) and neutral input shapes.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from types import MappingProxyType, SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

import app.integrations.providers.opencrq as opencrq
import app.services.integration_tools as integration_tools
from app.core.egress import current_route
from app.integrations.providers.opencrq import admin as opencrq_admin
from app.integrations.providers.opencrq import procedure_manifest as pm
from app.integrations.providers.opencrq.procedure_manifest import (
    PROBE,
    HiddenTool,
    Manifest,
    ManifestProbe,
    availability,
    hidden_refusal,
    load_tool_procedures,
    parse_manifest,
    read_manifest,
    reduce_schema,
)
from app.services.integration_read_only import withhold_write_tools
from app.services.integration_tools import BindingBasis, ToolBindings
from app.tools.base import CallableToolExecutor, ToolCall

TOKEN = "https://demo.opencrq.io||test-token"
FINGERPRINT = "ab" * 32

# ``create_opencrq_group`` sends groups.createGroup, ``delete_opencrq_group`` groups.deleteGroup.
CREATE, DELETE = "create_opencrq_group", "delete_opencrq_group"
CREATE_PROCEDURE, DELETE_PROCEDURE = "groups.createGroup", "groups.deleteGroup"
REFUSAL = (
    "Error: create_opencrq_group is not available: this OpenCRQ instance does not serve the OpenCRQ "
    "procedure(s) groups.createGroup that it needs. Use another tool, or ask the OpenCRQ administrator to upgrade."
)


def _document(procedures=None, **extra) -> dict:
    """What an instance answers: every procedure a tool sends (minus *without*), each with a neutral input."""
    if procedures is None:
        procedures = [
            {"path": path, "type": "query", "input": {"type": "object", "properties": {"id": {"type": "string"}}}}
            for path in sorted({p for sent in load_tool_procedures().values() for p in sent})
        ]
    return {"formatVersion": 1, "procedures": procedures, "fingerprint": FINGERPRINT, **extra}


def _manifest(without: tuple[str, ...] = ()) -> Manifest:
    document = _document()
    document["procedures"] = [entry for entry in document["procedures"] if entry["path"] not in without]
    manifest = parse_manifest(document)
    assert manifest is not None
    return manifest


@pytest.fixture
def old_instance() -> Manifest:
    """An instance serving neither group procedure."""
    return _manifest(without=(CREATE_PROCEDURE, DELETE_PROCEDURE))


async def _call(executor, name):
    return await executor.execute_tool(ToolCall(id="call-1", name=name, arguments={}))


def _tools_sending(*procedures: str) -> set[str]:
    return {tool for tool, sent in load_tool_procedures().items() if set(sent) & set(procedures)}


# --- parse_manifest -------------------------------------------------------------------


def test_a_manifest_keeps_its_fingerprint_and_each_procedure_with_its_type_and_reduced_input():
    manifest = parse_manifest(
        _document(
            [
                {
                    "path": "things.make",
                    "type": "mutation",
                    "input": {"type": "object", "properties": {"n": {"type": "integer", "minimum": 1}}},
                },
                {"path": "things.list", "type": "query", "input": None},
            ]
        )
    )
    assert manifest is not None
    assert manifest.fingerprint == FINGERPRINT
    assert manifest.serves("things.make") and not manifest.serves("things.gone")
    assert manifest.procedures["things.make"] == {
        "type": "mutation",
        "input": {"type": "object", "properties": {"n": {"type": "integer"}}},
    }
    assert manifest.procedures["things.list"] == {"type": "query", "input": None}


@pytest.mark.parametrize("fingerprint", [None, "", "XY", "AB" * 32, "ab" * 31, 12])
def test_a_fingerprint_that_does_not_read_as_sha256_hex_is_none(fingerprint):
    manifest = parse_manifest(_document(fingerprint=fingerprint))
    assert manifest is not None and manifest.fingerprint is None


def test_a_manifest_with_no_fingerprint_key_is_read_all_the_same():
    document = _document()
    del document["fingerprint"]
    manifest = parse_manifest(document)
    assert manifest is not None and manifest.fingerprint is None


@pytest.mark.parametrize(
    "document",
    [
        None,
        [],
        "text",
        {},
        {"formatVersion": 2, "procedures": [{"path": "a.b", "type": "query"}]},
        {"formatVersion": "1", "procedures": [{"path": "a.b", "type": "query"}]},
        {"formatVersion": True, "procedures": [{"path": "a.b", "type": "query"}]},
        {"formatVersion": 1},
        {"formatVersion": 1, "procedures": {}},
        {"formatVersion": 1, "procedures": []},
        {"formatVersion": 1, "procedures": ["a.b"]},
        {"formatVersion": 1, "procedures": [{"type": "query"}]},
        {"formatVersion": 1, "procedures": [{"path": "", "type": "query"}]},
        {"formatVersion": 1, "procedures": [{"path": "a.b", "type": "subscription"}]},
        {"formatVersion": 1, "procedures": [{"path": "a.b", "type": "query", "input": "string"}]},
        {"formatVersion": 1, "procedures": [{"path": "a.b", "type": "query"}, {"path": "a.b", "type": "query"}]},
        {"formatVersion": 1, "procedures": [{"path": "a.b", "type": "query"}, {"path": "a.c"}]},
    ],
)
def test_a_document_that_does_not_read_is_no_manifest(document):
    assert parse_manifest(document) is None


def test_a_document_with_too_many_procedures_is_no_manifest(monkeypatch):
    monkeypatch.setattr(pm, "_MAX_PROCEDURES", 2)
    entries = [{"path": f"a.p{i}", "type": "query"} for i in range(3)]
    assert parse_manifest({"formatVersion": 1, "procedures": entries}) is None
    assert parse_manifest({"formatVersion": 1, "procedures": entries[:2]}) is not None


def test_an_input_nested_past_the_limit_rejects_the_document():
    deep: dict = {"type": "string"}
    for _ in range(pm._MAX_SCHEMA_DEPTH + 2):
        deep = {"type": "array", "items": deep}
    assert parse_manifest({"formatVersion": 1, "procedures": [{"path": "a.b", "type": "query", "input": deep}]}) is None


def test_a_keyword_the_input_check_does_not_read_is_kept_and_logged_once(caplog):
    schema = {
        "type": "object",
        "properties": {"a": {"type": "object", "minProperties": 1}, "b": {"type": "array", "contains": {}}},
        "unevaluatedProperties": False,
    }
    document = {
        "formatVersion": 1,
        "procedures": [
            {"path": "a.b", "type": "query", "input": schema},
            {"path": "a.c", "type": "query", "input": schema},
        ],
    }
    with caplog.at_level(logging.INFO, logger=pm.logger.name):
        manifest = parse_manifest(document)
    assert manifest is not None
    assert manifest.procedures["a.b"]["input"] == schema
    [record] = [r for r in caplog.records if "does not read" in r.getMessage()]
    assert record.getMessage().endswith("contains, minProperties, unevaluatedProperties")


def test_a_manifest_that_uses_only_the_keywords_the_check_reads_logs_nothing(caplog):
    with caplog.at_level(logging.INFO, logger=pm.logger.name):
        assert parse_manifest(_document()) is not None
    assert not caplog.records


# --- reduce_schema --------------------------------------------------------------------


def test_reducing_keeps_the_shape_of_an_input_and_drops_its_value_constraints():
    schema = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "properties": {
            "id": {"type": "string", "format": "uuid", "pattern": "^x$", "minLength": 1, "description": "d"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 5, "default": 2, "exclusiveMinimum": 0},
            "tags": {"type": "array", "items": {"type": "string", "maxLength": 3}, "maxItems": 4, "uniqueItems": True},
            "level": {"anyOf": [{"enum": ["a", "b"], "type": "string", "default": "a"}, {"type": "null"}]},
            "kind": {"oneOf": [{"type": "object", "properties": {"k": {"const": "x"}}, "required": ["k"]}]},
            "extra": {"type": "object", "additionalProperties": {"type": "number", "multipleOf": 2, "title": "t"}},
        },
        "required": ["id"],
        "$defs": {"unit": {"type": "string", "pattern": "^u$", "examples": ["u"]}},
    }

    assert reduce_schema(schema) == {
        "type": "object",
        "properties": {
            "id": {"type": "string"},
            "limit": {"type": "integer"},
            "tags": {"type": "array", "items": {"type": "string"}},
            "level": {"anyOf": [{"enum": ["a", "b"], "type": "string"}, {"type": "null"}]},
            "kind": {"oneOf": [{"type": "object", "properties": {"k": {"const": "x"}}, "required": ["k"]}]},
            "extra": {"type": "object", "additionalProperties": {"type": "number"}},
        },
        "required": ["id"],
        "$defs": {"unit": {"type": "string"}},
    }


def test_a_property_named_like_a_constraint_survives_the_reduction():
    schema = {
        "type": "object",
        "properties": {"pattern": {"type": "string"}, "default": {"type": "string"}, "format": {"type": "string"}},
        "required": ["pattern", "default"],
    }

    assert reduce_schema(schema) == schema


def test_a_keyword_that_is_no_value_constraint_is_never_dropped():
    schema = {"type": "object", "properties": {"a": {}}, "patternProperties": {"^x": {}}, "minProperties": 1}

    assert reduce_schema(schema) == schema


def test_reducing_twice_changes_nothing():
    schema = {"type": "object", "properties": {"n": {"type": "integer", "minimum": 1}}, "default": {}}

    assert reduce_schema(reduce_schema(schema)) == reduce_schema(schema)


def test_a_boolean_schema_and_an_absent_input_pass_through():
    assert reduce_schema(None) is None
    assert reduce_schema({"type": "object", "additionalProperties": False}) == {
        "type": "object",
        "additionalProperties": False,
    }


# --- availability ---------------------------------------------------------------------


def test_availability_names_the_missing_procedures(old_instance):
    hidden = availability(old_instance, load_tool_procedures())
    assert set(hidden) == _tools_sending(CREATE_PROCEDURE, DELETE_PROCEDURE)
    assert hidden[CREATE] == HiddenTool(missing=(CREATE_PROCEDURE,))
    assert hidden[DELETE] == HiddenTool(missing=(DELETE_PROCEDURE,))


def test_a_manifest_that_lists_every_procedure_a_tool_sends_hides_nothing():
    assert availability(_manifest(), load_tool_procedures()) == {}


def test_a_tool_is_hidden_when_any_one_of_its_procedures_is_missing(old_instance):
    tools = {"two_steps": ["groups.getGroups", CREATE_PROCEDURE], "one_step": ["groups.getGroups"]}
    assert availability(old_instance, tools) == {"two_steps": HiddenTool((CREATE_PROCEDURE,))}


def test_the_refusal_names_the_tool_and_the_procedures_and_nothing_the_instance_sent():
    assert hidden_refusal(CREATE, "this OpenCRQ instance", (CREATE_PROCEDURE,)) == REFUSAL
    assert "groups.createGroup, groups.deleteGroup" in hidden_refusal(CREATE, "x", (CREATE_PROCEDURE, DELETE_PROCEDURE))


# --- build_tools ----------------------------------------------------------------------


def test_a_build_without_the_keyword_offers_every_tool():
    names = {tool.name for tool in opencrq.build_tools(TOKEN, CallableToolExecutor())}
    assert names == set(load_tool_procedures())


def test_a_build_with_no_manifest_offers_every_tool():
    names = {tool.name for tool in opencrq.build_tools(TOKEN, CallableToolExecutor(), opencrq_manifest=None)}
    assert names == set(load_tool_procedures())


def test_a_manifest_listing_every_procedure_offers_every_tool():
    names = {tool.name for tool in opencrq.build_tools(TOKEN, CallableToolExecutor(), opencrq_manifest=_manifest())}
    assert names == set(load_tool_procedures())


def test_the_manifest_query_is_never_a_call_of_trpc():
    # The probe sends it with its own request, so the contract suite never resolves it and the pin may lack it.
    assert pm.PROCEDURE == "procedureManifest"
    assert pm.PROCEDURE not in {p for sent in load_tool_procedures().values() for p in sent}


async def test_a_withheld_tool_is_absent_and_refused_by_name(old_instance):
    executor = CallableToolExecutor()
    tools = opencrq.build_tools(TOKEN, executor, opencrq_manifest=old_instance)
    names = {tool.name for tool in tools}
    hidden = _tools_sending(CREATE_PROCEDURE, DELETE_PROCEDURE)
    assert names == set(load_tool_procedures()) - hidden
    for name in hidden:
        assert executor.handler(name) is not None

    result = await _call(executor, CREATE)
    assert result.is_error
    assert result.content == REFUSAL
    assert "groups.deleteGroup" in (await _call(executor, DELETE)).content


async def test_multi_instance_suffixing_leaves_the_refusal_on_the_hidden_base_name(old_instance):
    executor = CallableToolExecutor()
    defs = opencrq.build_tools(TOKEN, executor, opencrq_manifest=old_instance)
    enriched = integration_tools._suffix_instance_tools(
        defs,
        executor,
        provider="opencrq",
        integ_name="Old",
        integ_desc=None,
        integration_id=str(uuid.uuid4()),
        used_slugs={},
    )
    offered = {definition.name for definition in enriched}
    assert not {name for name in offered if name.startswith((CREATE, DELETE))}
    assert {definition.base_name for definition in enriched} == {definition.name for definition in defs}
    # A served tool is reachable under its suffixed name only; the hidden one never gets a real handler.
    assert executor.handler("list_opencrq_groups") is None
    assert any(name.startswith("list_opencrq_groups__") for name in offered)
    result = await _call(executor, CREATE)
    assert result.is_error
    assert result.content == REFUSAL


# --- the manifest a build is given ----------------------------------------------------------


class _Savepoint:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False


class _Session:
    def begin_nested(self):
        return _Savepoint()


async def test_an_integration_no_registration_owns_is_given_the_probe(monkeypatch):
    monkeypatch.setattr(integration_tools, "platform_for_integration", AsyncMock(return_value=None))
    assert await integration_tools._opencrq_manifest(_Session(), "integration-1") is PROBE


async def test_a_registered_integration_is_given_the_manifest_its_registration_holds(monkeypatch):
    stored = _manifest()
    owner = AsyncMock(return_value=SimpleNamespace(platform_identifier="p"))
    held = AsyncMock(return_value=stored)
    monkeypatch.setattr(integration_tools, "platform_for_integration", owner)
    monkeypatch.setattr(integration_tools, "registered_procedure_manifest", held)
    assert await integration_tools._opencrq_manifest(_Session(), "integration-1") is stored
    owner.assert_awaited_once()
    assert held.await_args.args[1] is owner.return_value


async def test_a_registered_integration_whose_manifest_decides_nothing_fails_open(monkeypatch):
    # None stored, or one the platform has moved past (its fingerprint is no longer the reported one).
    monkeypatch.setattr(
        integration_tools, "platform_for_integration", AsyncMock(return_value=SimpleNamespace(platform_identifier="p"))
    )
    monkeypatch.setattr(integration_tools, "registered_procedure_manifest", AsyncMock(return_value=None))
    assert await integration_tools._opencrq_manifest(_Session(), "integration-1") is None


async def test_a_manifest_that_cannot_be_read_leaves_the_probe_to_the_first_call(monkeypatch):
    monkeypatch.setattr(
        integration_tools, "platform_for_integration", AsyncMock(return_value=SimpleNamespace(platform_identifier="p"))
    )
    monkeypatch.setattr(
        integration_tools, "registered_procedure_manifest", AsyncMock(side_effect=RuntimeError("db down"))
    )
    assert await integration_tools._opencrq_manifest(_Session(), "integration-1") is PROBE


async def test_a_registration_that_cannot_be_read_leaves_the_probe_to_the_first_call(monkeypatch):
    monkeypatch.setattr(integration_tools, "platform_for_integration", AsyncMock(side_effect=RuntimeError("db down")))
    assert await integration_tools._opencrq_manifest(_Session(), "integration-1") is PROBE


# --- the probe, at call time ----------------------------------------------------------


@pytest.fixture
def groups_sent(monkeypatch):
    """The groups listing sends no request: it answers an empty tenant, and records the call."""
    sent = AsyncMock(return_value=([], None))
    monkeypatch.setattr(opencrq_admin, "_trpc", sent)
    return sent


@pytest.fixture
def served(monkeypatch):
    """What the probe reads: by default an instance serving neither group procedure."""
    read = AsyncMock(return_value=_manifest(without=(CREATE_PROCEDURE, DELETE_PROCEDURE)))
    monkeypatch.setattr(pm, "read_manifest", read)
    return read


async def _list_groups(executor):
    return await executor.execute_tool(
        ToolCall(id="call-1", name="list_opencrq_groups", arguments={"tenant_id": "tenant-1"})
    )


async def test_a_build_with_no_registration_offers_every_tool_and_sends_no_request(served):
    executor = CallableToolExecutor()
    tools = opencrq.build_tools(TOKEN, executor, opencrq_manifest=PROBE)
    assert {tool.name for tool in tools} == set(load_tool_procedures())
    served.assert_not_awaited()


async def test_a_build_with_no_keyword_never_probes(served, groups_sent):
    executor = CallableToolExecutor()
    opencrq.build_tools(TOKEN, executor)
    assert (await _list_groups(executor)).content == "No groups found in this tenant."
    served.assert_not_awaited()


async def test_a_build_with_a_manifest_in_hand_never_probes(served, groups_sent):
    executor = CallableToolExecutor()
    opencrq.build_tools(TOKEN, executor, opencrq_manifest=_manifest())
    assert (await _list_groups(executor)).content == "No groups found in this tenant."
    served.assert_not_awaited()


async def test_the_first_call_probes_once_for_the_build(served, groups_sent):
    executor = CallableToolExecutor()
    opencrq.build_tools(TOKEN, executor, opencrq_manifest=PROBE)
    await _list_groups(executor)
    await _list_groups(executor)
    await _call(executor, CREATE)
    served.assert_awaited_once()
    base_url, headers, refresh = served.await_args.args
    assert base_url == "https://demo.opencrq.io"
    assert headers["Authorization"] == "Bearer test-token"
    assert callable(refresh)


async def test_the_next_build_probes_again(served, groups_sent):
    for _ in range(2):
        executor = CallableToolExecutor()
        opencrq.build_tools(TOKEN, executor, opencrq_manifest=PROBE)
        await _list_groups(executor)
    assert served.await_count == 2


async def test_the_probe_refreshes_the_token_before_it_reads(monkeypatch, groups_sent):
    order: list[str] = []
    refresh = AsyncMock(side_effect=lambda headers: order.append("refresh"))

    async def read(base_url, headers, refresh_hook):
        await refresh_hook()
        order.append("read")
        return None

    monkeypatch.setattr("app.integrations.providers.opencrq._refresh_if_stale", refresh)
    monkeypatch.setattr(pm, "read_manifest", read)
    executor = CallableToolExecutor()
    opencrq.build_tools(TOKEN, executor, opencrq_manifest=PROBE)
    await _list_groups(executor)
    assert order == ["refresh", "read"]


async def test_concurrent_first_calls_wait_for_one_probe(groups_sent, monkeypatch):
    gate = asyncio.Event()

    async def slow(*_args):
        await gate.wait()
        return _manifest(without=(CREATE_PROCEDURE,))

    probe = AsyncMock(side_effect=slow)
    monkeypatch.setattr(pm, "read_manifest", probe)
    executor = CallableToolExecutor()
    opencrq.build_tools(TOKEN, executor, opencrq_manifest=PROBE)
    calls = asyncio.gather(_list_groups(executor), _list_groups(executor), _call(executor, CREATE))
    await asyncio.sleep(0)
    gate.set()
    first, again, refused = await calls
    assert first.content == again.content == "No groups found in this tenant."
    assert refused.is_error
    probe.assert_awaited_once()


async def test_an_unserved_tool_is_refused_at_call_time_without_running(served, groups_sent, monkeypatch):
    handler = AsyncMock()
    executor = CallableToolExecutor()
    opencrq.build_tools(TOKEN, executor, opencrq_manifest=PROBE)
    monkeypatch.setattr(opencrq_admin, "_trpc", handler)
    result = await _call(executor, CREATE)
    assert result.is_error
    assert result.content == REFUSAL
    handler.assert_not_awaited()


async def test_a_served_tool_runs_its_real_handler(served, groups_sent):
    executor = CallableToolExecutor()
    opencrq.build_tools(TOKEN, executor, opencrq_manifest=PROBE)
    result = await _list_groups(executor)
    assert not result.is_error
    assert result.content == "No groups found in this tenant."
    assert groups_sent.await_args.args[2] == "groups.getGroups"


async def test_an_instance_that_answers_no_manifest_runs_every_tool(served, groups_sent):
    served.return_value = None
    executor = CallableToolExecutor()
    opencrq.build_tools(TOKEN, executor, opencrq_manifest=PROBE)
    result = await _call(executor, CREATE)
    assert not result.content.startswith("Error: create_opencrq_group is not available")
    assert (await _list_groups(executor)).content == "No groups found in this tenant."


async def test_a_probe_that_raises_is_none_and_fails_open(monkeypatch, groups_sent):
    def explode(**_kwargs):
        raise RuntimeError("no network")

    monkeypatch.setattr(pm, "egress_client", explode)
    executor = CallableToolExecutor()
    opencrq.build_tools(TOKEN, executor, opencrq_manifest=PROBE)
    assert (await _list_groups(executor)).content == "No groups found in this tenant."


async def test_the_gate_keeps_what_the_executor_reads_of_a_handler():
    plain, probing = CallableToolExecutor(), CallableToolExecutor()
    opencrq.build_tools(TOKEN, plain)
    opencrq.build_tools(TOKEN, probing, opencrq_manifest=PROBE)
    assert probing._accepted_params == plain._accepted_params
    assert probing._accepts_file_context == plain._accepts_file_context


async def test_the_probe_goes_out_on_the_route_the_call_runs_on(monkeypatch, groups_sent):
    # ``_route_through_relay`` wraps the handlers the builder registered, the gate included.
    route = SimpleNamespace(name="relay")
    seen = []

    async def probe(*_args):
        seen.append(current_route())
        return None

    monkeypatch.setattr(pm, "read_manifest", probe)
    executor = CallableToolExecutor()
    defs = opencrq.build_tools(TOKEN, executor, opencrq_manifest=PROBE)
    integration_tools._route_through_relay(defs, executor, route)
    await _list_groups(executor)
    assert seen == [route]


async def test_multi_instance_suffixing_keeps_the_gate(served):
    executor = CallableToolExecutor()
    defs = opencrq.build_tools(TOKEN, executor, opencrq_manifest=PROBE)
    enriched = integration_tools._suffix_instance_tools(
        defs,
        executor,
        provider="opencrq",
        integ_name="Old",
        integ_desc=None,
        integration_id=str(uuid.uuid4()),
        used_slugs={},
    )
    [create] = [d.name for d in enriched if d.base_name == CREATE]
    assert executor.handler(CREATE) is None
    result = await _call(executor, create)
    assert result.is_error
    assert result.content == REFUSAL


async def test_a_read_only_binding_still_refuses_its_writes_first(served):
    executor = CallableToolExecutor()
    defs = opencrq.build_tools(TOKEN, executor, opencrq_manifest=PROBE)
    kept = withhold_write_tools(defs, executor, integration_label="OpenCRQ")
    assert CREATE not in {d.name for d in kept}
    assert (await _call(executor, CREATE)).content.startswith("Refused: create_opencrq_group")
    served.assert_not_awaited()


async def test_a_probe_with_a_manifest_in_hand_never_asks(served):
    manifest = _manifest()
    probe = ManifestProbe.known(manifest)
    assert probe.loaded
    assert await probe.load() is manifest
    assert probe.assess().manifest is manifest
    served.assert_not_awaited()


# --- read_manifest --------------------------------------------------------------------


def _answering(monkeypatch, handler):
    monkeypatch.setattr(
        pm,
        "egress_client",
        lambda **kwargs: httpx.AsyncClient(transport=httpx.MockTransport(handler), **kwargs),
    )


def _envelope(data) -> httpx.Response:
    return httpx.Response(200, json={"result": {"data": data}})


async def test_the_manifest_is_one_authenticated_get_of_the_query(monkeypatch):
    requests = []

    def handler(request):
        requests.append(request)
        return _envelope(_document())

    _answering(monkeypatch, handler)
    refresh = AsyncMock()
    manifest = await read_manifest("https://demo.opencrq.io/", {"Authorization": "Bearer k"}, refresh)
    assert manifest is not None and manifest.fingerprint == FINGERPRINT
    [request] = requests
    assert (request.method, str(request.url)) == ("GET", "https://demo.opencrq.io/trpc/procedureManifest")
    assert request.headers["Authorization"] == "Bearer k"
    refresh.assert_awaited_once()


@pytest.mark.parametrize(
    "response",
    [
        _envelope({"formatVersion": 2, "procedures": []}),
        _envelope({"formatVersion": 1, "procedures": [{"path": "a.b"}]}),
        _envelope("manifest"),
        _envelope(None),
        httpx.Response(200, json=["not", "an", "envelope"]),
        httpx.Response(200, json={"result": "x"}),
        httpx.Response(200, content=b"<html>not json</html>"),
        httpx.Response(401, json={"error": {"message": "unauthorized"}}),
        httpx.Response(403),
        httpx.Response(404),
        httpx.Response(500),
    ],
    ids=[
        "format",
        "entry",
        "data-not-an-object",
        "null",
        "list",
        "result-not-an-object",
        "html",
        "401",
        "403",
        "404",
        "500",
    ],
)
async def test_an_answer_that_is_no_manifest_is_none(monkeypatch, response):
    _answering(monkeypatch, lambda _request: response)
    assert await read_manifest("https://demo.opencrq.io") is None


@pytest.mark.parametrize("error", [httpx.ConnectError("refused"), httpx.ReadTimeout("slow"), RuntimeError("boom")])
async def test_an_unreachable_instance_is_none(monkeypatch, error):
    def handler(_request):
        raise error

    _answering(monkeypatch, handler)
    assert await read_manifest("https://demo.opencrq.io") is None


async def test_an_answer_over_the_size_limit_is_none(monkeypatch):
    monkeypatch.setattr(pm, "MAX_MANIFEST_BYTES", 100)
    _answering(monkeypatch, lambda _request: _envelope(_document()))
    assert await read_manifest("https://demo.opencrq.io") is None


async def test_a_refresh_that_fails_fails_open(monkeypatch):
    _answering(monkeypatch, lambda _request: _envelope(_document()))
    assert await read_manifest("https://demo.opencrq.io", None, AsyncMock(side_effect=RuntimeError("no jwt"))) is None


# --- the tool build loop --------------------------------------------------------------


async def _built_kwargs(monkeypatch, provider: str, auth_type: str):
    """Run the real tool-build loop for one binding; returns the keywords its builder got."""
    integration_id = str(uuid.uuid4())
    row = SimpleNamespace(id=uuid.UUID(integration_id), name="I", description=None, auth_type=auth_type)

    class _Db:
        async def execute(self, *_args, **_kwargs):
            return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: [row]))

    monkeypatch.setattr(integration_tools, "_parallel_refresh_expiring_tokens", AsyncMock(return_value=({}, set())))
    monkeypatch.setattr(integration_tools, "_get_token", AsyncMock(return_value=TOKEN))
    monkeypatch.setattr(integration_tools, "platforms_reached_internally", AsyncMock(return_value={}))
    manifest = AsyncMock(return_value=PROBE)
    monkeypatch.setattr(integration_tools, "_opencrq_manifest", manifest)
    seen: list[dict] = []

    def _record(_token, _executor, **kwargs):
        seen.append(kwargs)
        return []

    monkeypatch.setitem(integration_tools.PROVIDER_TOOLS, provider, _record)
    await integration_tools.build_integration_tools(_Db(), ToolBindings({provider: [integration_id]}, basis=BindingBasis.CALLER_CHECKED, reason="test"), context={"source": "test"})
    [kwargs] = seen
    return kwargs, manifest, integration_id


@pytest.mark.parametrize("auth_type", ["api_key", "platform_managed"])
async def test_the_tool_build_hands_the_manifest_to_opencrq(monkeypatch, auth_type):
    kwargs, manifest, integration_id = await _built_kwargs(monkeypatch, "opencrq", auth_type)
    assert kwargs["opencrq_manifest"] is PROBE
    assert manifest.await_args.args[1:] == (integration_id,)


async def test_the_tool_build_reads_no_manifest_for_another_provider(monkeypatch):
    kwargs, manifest, _ = await _built_kwargs(monkeypatch, "abuseipdb", "api_key")
    assert "opencrq_manifest" not in kwargs
    manifest.assert_not_awaited()


async def _built(monkeypatch, manifest):
    """The real tool-build loop with the real OpenCRQ builder; returns (definitions, executor)."""
    integration_id = str(uuid.uuid4())
    row = SimpleNamespace(id=uuid.UUID(integration_id), name="I", description=None, auth_type="platform_managed")

    class _Db:
        async def execute(self, *_args, **_kwargs):
            return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: [row]))

    monkeypatch.setattr(integration_tools, "_parallel_refresh_expiring_tokens", AsyncMock(return_value=({}, set())))
    monkeypatch.setattr(integration_tools, "_get_token", AsyncMock(return_value=TOKEN))
    monkeypatch.setattr(integration_tools, "platforms_reached_internally", AsyncMock(return_value={}))
    monkeypatch.setattr(integration_tools, "_opencrq_manifest", AsyncMock(return_value=manifest))
    return await integration_tools.build_integration_tools(
        _Db(), ToolBindings({"opencrq": [integration_id]}, basis=BindingBasis.CALLER_CHECKED, reason="test"), context={"source": "test"}
    )


async def test_an_unregistered_integration_is_built_whole_and_sends_no_request(monkeypatch, served):
    definitions, _ = await _built(monkeypatch, PROBE)
    assert {d.name for d in definitions} == set(load_tool_procedures())
    served.assert_not_awaited()


async def test_a_build_with_a_manifest_lacks_what_it_cannot_serve(monkeypatch, served, old_instance):
    definitions, executor = await _built(monkeypatch, old_instance)
    assert {d.name for d in definitions} == set(load_tool_procedures()) - _tools_sending(
        CREATE_PROCEDURE, DELETE_PROCEDURE
    )
    assert (await _call(executor, CREATE)).is_error
    served.assert_not_awaited()


async def _built_registered(monkeypatch, stored, served):
    """The real tool-build loop, real ``_opencrq_manifest``, for an integration a registration owns."""
    integration_id = str(uuid.uuid4())
    row = SimpleNamespace(id=uuid.UUID(integration_id), name="I", description=None, auth_type="platform_managed")

    class _Db(_Session):
        async def execute(self, *_args, **_kwargs):
            return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: [row]))

    monkeypatch.setattr(integration_tools, "_parallel_refresh_expiring_tokens", AsyncMock(return_value=({}, set())))
    monkeypatch.setattr(integration_tools, "_get_token", AsyncMock(return_value=TOKEN))
    monkeypatch.setattr(integration_tools, "platforms_reached_internally", AsyncMock(return_value={}))
    monkeypatch.setattr(
        integration_tools, "platform_for_integration", AsyncMock(return_value=SimpleNamespace(platform_identifier="p"))
    )
    monkeypatch.setattr(integration_tools, "registered_procedure_manifest", AsyncMock(return_value=stored))
    return await integration_tools.build_integration_tools(
        _Db(), ToolBindings({"opencrq": [integration_id]}, basis=BindingBasis.CALLER_CHECKED, reason="test"), context={"source": "test"}
    )


async def test_a_registered_build_hides_what_the_stored_manifest_cannot_serve_without_a_request(
    monkeypatch, served, old_instance
):
    definitions, executor = await _built_registered(monkeypatch, old_instance, served)
    assert {d.name for d in definitions} == set(load_tool_procedures()) - _tools_sending(
        CREATE_PROCEDURE, DELETE_PROCEDURE
    )
    assert (await _call(executor, CREATE)).is_error
    served.assert_not_awaited()


async def test_a_registered_build_with_a_stale_manifest_offers_everything_and_sends_no_request(
    monkeypatch, served, groups_sent
):
    # The registration reported a fingerprint other than the stored one: nothing hidden, nothing checked.
    definitions, executor = await _built_registered(monkeypatch, None, served)
    assert {d.name for d in definitions} == set(load_tool_procedures())
    served.assert_not_awaited()
    assert not (await _call(executor, CREATE)).content.startswith("Error: create_opencrq_group is not available")


def test_a_manifest_is_immutable_to_its_readers():
    manifest = _manifest()
    assert isinstance(manifest.procedures, MappingProxyType)
    with pytest.raises(TypeError):
        manifest.procedures["x"] = {}  # type: ignore[index]
