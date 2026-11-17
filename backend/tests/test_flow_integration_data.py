"""An Integration node's operation answers data a flow can use without an agent.

The case: one OpenCTI RFI from several reports, its type set, named after
them. The reports are listed (``items``, with their names), the RFI is
created with its information types and comes back by id (``fields``).
"""

from __future__ import annotations

from unittest.mock import patch

import httpx
import pytest

from app.integrations.providers.opencti import build_tools
from app.services.flow_graph import validate_graph
from app.services.flow_runner import _lookup_path, flow_integration_context, operation_data
from app.tools.base import CallableToolExecutor, ToolCall, ToolResult

RFI = {
    "id": "rfi-1",
    "entity_type": "Case-Rfi",
    "standard_id": "case-rfi--1",
    "name": "RFI on A, B",
    "information_types": ["strategic"],
}
REPORTS = [
    {"id": "r-1", "entity_type": "Report", "name": "A"},
    {"id": "r-2", "entity_type": "Report", "name": "B"},
]


async def _call(name: str, arguments: dict, answer: dict) -> tuple[ToolResult, list[dict]]:
    executor = CallableToolExecutor()
    # The handler itself; the draft gate in front of it has its own tests.
    build_tools("https://octi.test||static-token", executor, direct_mutations=True)
    sent: list[dict] = []

    async def _fake(self, method, url, **kwargs):
        sent.append(kwargs.get("json") or {})
        return httpx.Response(200, json=answer, request=httpx.Request("POST", url))

    with patch.object(httpx.AsyncClient, "request", _fake):
        result = await executor.execute_tool(ToolCall(id="1", name=name, arguments=arguments))
    return result, sent


@pytest.mark.asyncio
async def test_an_rfi_is_created_with_its_type_and_comes_back_by_id():
    result, sent = await _call(
        "create_opencti_container",
        {
            "container_type": "Case-Rfi",
            "name": "RFI on A, B",
            "object_ids": ["r-1", "r-2"],
            "case_types": ["strategic"],
        },
        {"data": {"caseRfiAdd": RFI}},
    )

    variables = sent[-1]["variables"]["input"]
    assert variables["information_types"] == ["strategic"]
    assert variables["objects"] == ["r-1", "r-2"]
    assert "information_types" in sent[-1]["query"]
    assert not result.is_error
    assert "Types: strategic" in result.content
    assert operation_data(result) == {
        "fields": {"id": "rfi-1", "standard_id": "case-rfi--1", "entity_type": "Case-Rfi", "name": "RFI on A, B"}
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("container_type", "mutation", "field"),
    [("Case-Rft", "caseRftAdd", "takedown_types"), ("Case-Incident", "caseIncidentAdd", "response_types")],
)
async def test_each_case_type_sends_its_own_vocabulary(container_type, mutation, field):
    _, sent = await _call(
        "create_opencti_container",
        {"container_type": container_type, "name": "Case", "case_types": ["x"]},
        {"data": {mutation: {"id": "c-1", "entity_type": container_type, "name": "Case"}}},
    )

    assert sent[-1]["variables"]["input"][field] == ["x"]


@pytest.mark.asyncio
async def test_a_listing_comes_back_as_items_with_their_names():
    edges = [{"node": report} for report in REPORTS]
    result, _ = await _call(
        "list_opencti_entities",
        {"types": ["Report"]},
        {"data": {"stixDomainObjects": {"edges": edges, "pageInfo": {"globalCount": 2}}}},
    )

    assert operation_data(result) == {"items": REPORTS}


def test_operation_data_reads_json_text_and_ignores_prose():
    assert operation_data(ToolResult(tool_call_id="", content='{"a": 1}')) == {"fields": {"a": 1}}
    assert operation_data(ToolResult(tool_call_id="", content="[1, 2]")) == {"items": [1, 2]}
    assert operation_data(ToolResult(tool_call_id="", content="Found 2 reports.")) == {}


def test_a_key_on_a_list_of_objects_lists_that_key():
    outputs = {"items": REPORTS}

    assert _lookup_path(outputs, ["items", "name"]) == ["A", "B"]
    assert _lookup_path(outputs, ["items", "1", "name"]) == "B"
    assert _lookup_path(outputs, ["items", "5"]) is None
    assert _lookup_path({"items": ["a", "b"]}, ["items", "name"]) is None


def test_a_reference_to_the_items_of_an_integration_node_is_valid():
    def node(node_id: str, kind: str, **config) -> dict:
        return {"id": node_id, "kind": kind, "position": {"x": 0, "y": 0}, "config": config}

    graph = {
        "nodes": [
            node("t", "trigger.manual"),
            node("list", "integration", integration_id="i", operation="list_opencti_entities"),
            node("names", "util.expression", expression='join(", ", {{list.items.name}})'),
        ],
        "edges": [
            {"id": "e1", "source": "t", "source_handle": "out", "target": "list", "target_handle": "in", "kind": "data"},
            {"id": "e2", "source": "list", "source_handle": "out", "target": "names", "target_handle": "in", "kind": "data"},
        ],
    }

    assert not [i for i in validate_graph(graph).issues if i.severity == "error"]


@pytest.mark.asyncio
async def test_a_flow_step_writes_through_a_draft_it_provisions_instead_of_asking():
    """A flow step asks nobody: OpenCTI's draft gate provisions the draft."""
    executor = CallableToolExecutor()
    build_tools("https://octi.test||static-token", executor, autonomous=flow_integration_context("u")["autonomous"])
    sent: list[dict] = []

    async def _fake(self, method, url, **kwargs):
        body = kwargs.get("json") or {}
        sent.append(body)
        if "draftWorkspaceAdd" in body.get("query", ""):
            data = {"draftWorkspaceAdd": {"id": "draft-1", "name": "d"}}
        else:
            data = {"caseRfiAdd": RFI}
        return httpx.Response(200, json={"data": data}, request=httpx.Request("POST", url))

    with patch.object(httpx.AsyncClient, "request", _fake):
        result = await executor.execute_tool(
            ToolCall(id="1", name="create_opencti_container", arguments={"container_type": "Case-Rfi", "name": "RFI"})
        )

    assert any("caseRfiAdd" in body.get("query", "") for body in sent)
    assert operation_data(result)["fields"]["id"] == "rfi-1"


def test_the_names_of_the_listed_reports_join_into_one_title():
    from app.services.flow_expression import evaluate_expression
    from app.services.flow_runner import _to_text

    names = _to_text(_lookup_path({"items": [*REPORTS, {"id": "r-3", "name": 'The "Cozy" one'}]}, ["items", "name"]))

    assert evaluate_expression(f'join(", ", {names})') == 'A, B, The "Cozy" one'
