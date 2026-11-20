"""A JSON split of a text result splits the tool's structured value instead.

A tool that writes text for the model and records for a reader
(``ToolResult.structured``: the OpenCTI listings) is split one workset item
per record; without a structured value the text fallback is unchanged.
"""

from __future__ import annotations

from unittest.mock import patch

import httpx
import pytest

from app.integrations.providers.opencti import build_tools
from app.services.workset_split import SplitSpec, split_result
from app.tools.base import CallableToolExecutor, ToolCall

TEXT = "Found 2 domain objects:\n[Malware] A\n  ID: m-1\n[Malware] B\n  ID: m-2"
RECORDS = [
    {"id": "m-1", "entity_type": "Malware", "name": "A"},
    {"id": "m-2", "entity_type": "Malware", "name": "B"},
]


def test_a_json_split_of_a_text_result_splits_its_structured_list():
    split = split_result(TEXT, SplitSpec.from_argument({"json_path": "", "ref_field": "id"}), RECORDS)
    assert [item.external_ref for item in split.items] == ["m-1", "m-2"]
    assert split.fallback == ""


def test_a_path_inside_a_structured_object_is_followed():
    value = {"id": "r-1", "total_objects": 2, "entities": RECORDS, "relationships": []}
    split = split_result(
        TEXT,
        SplitSpec.from_argument({"json_path": "entities", "ref_field": "id", "title_field": "name"}),
        value,
    )
    assert [item.title for item in split.items] == ["A", "B"]
    assert "total_objects: 2" in split.preamble


def test_without_a_structured_value_the_text_is_stored_whole():
    split = split_result(TEXT, SplitSpec.from_argument({"json_path": ""}))
    assert len(split.items) == 1
    assert "not JSON" in split.fallback


def test_a_json_text_wins_over_the_structured_value():
    split = split_result('[{"id": "x"}]', SplitSpec.from_argument({"json_path": "", "ref_field": "id"}), RECORDS)
    assert [item.external_ref for item in split.items] == ["x"]


async def _run(name: str, arguments: dict, answer: dict):
    executor = CallableToolExecutor()
    build_tools("https://octi.test||static-token", executor)

    async def _fake(self, method, url, **kwargs):
        return httpx.Response(200, json=answer, request=httpx.Request("POST", url))

    with patch.object(httpx.AsyncClient, "request", _fake):
        return await executor.execute_tool(ToolCall(id="1", name=name, arguments=arguments))


@pytest.mark.asyncio
async def test_list_opencti_entities_splits_one_item_per_entity():
    nodes = [{**record, "description": f"about {record['name']}"} for record in RECORDS]
    answer = {"data": {"stixDomainObjects": {"edges": [{"node": n} for n in nodes], "pageInfo": {"globalCount": 2}}}}
    result = await _run("list_opencti_entities", {"types": ["Malware"]}, answer)
    assert [record["description"] for record in result.structured] == ["about A", "about B"]
    split = split_result(
        str(result.content), SplitSpec.from_argument({"json_path": "", "ref_field": "id"}), result.structured
    )
    assert [item.external_ref for item in split.items] == ["m-1", "m-2"]


@pytest.mark.asyncio
async def test_get_opencti_container_full_splits_one_item_per_object():
    container = {
        "id": "report-1",
        "entity_type": "Report",
        "name": "Report 1",
        "objects": {
            "edges": [
                {"node": RECORDS[0]},
                {
                    "node": {
                        "id": "rel-1",
                        "entity_type": "uses",
                        "relationship_type": "uses",
                        "from": {"id": "m-1", "entity_type": "Malware", "name": "A"},
                        "to": {"id": "m-2", "entity_type": "Malware", "name": "B"},
                    }
                },
            ],
            "pageInfo": {"globalCount": 2},
        },
    }
    result = await _run(
        "get_opencti_container_full", {"container_id": "report-1"}, {"data": {"stixDomainObject": container}}
    )
    assert "=== [Report] Report 1 ===" in result.content
    assert result.structured["total_objects"] == 2
    entities = split_result(
        str(result.content), SplitSpec.from_argument({"json_path": "entities", "ref_field": "id"}), result.structured
    )
    relationships = split_result(
        str(result.content),
        SplitSpec.from_argument({"json_path": "relationships", "ref_field": "id"}),
        result.structured,
    )
    assert [item.external_ref for item in entities.items] == ["m-1"]
    assert [item.external_ref for item in relationships.items] == ["rel-1"]
