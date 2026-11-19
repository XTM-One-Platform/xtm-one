# SPDX-License-Identifier: Apache-2.0
"""OpenCTI workflow statuses: both ids listed per type, filters on template ids.

``workflow_id`` filters carry the status template id (the OpenCTI filter
editors list templates): a status name or a per-type Status id written in a
filter becomes its template id. Setting a status (playbook "change status",
``x_opencti_workflow_id``) keeps the per-type Status id.
"""

from __future__ import annotations

import json
from unittest.mock import patch

import httpx
import pytest

from app.integrations.providers.opencti import build_tools
from app.tools.base import CallableToolExecutor, ToolCall

REPORT_STATUS_IDS = {
    "NEW": "11111111-1111-4111-8111-111111111111",
    "QUALIFIED": "22222222-2222-4222-8222-222222222222",
    "PROCESSED": "33333333-3333-4333-8333-333333333333",
    "REJECTED": "44444444-4444-4444-8444-444444444444",
    "PUBLISHED": "55555555-5555-4555-8555-555555555555",
}
CASE_PUBLISHED_ID = "66666666-6666-4666-8666-666666666666"
TEMPLATE_IDS = {
    "NEW": "a1111111-1111-4111-8111-111111111111",
    "QUALIFIED": "a2222222-2222-4222-8222-222222222222",
    "PROCESSED": "a3333333-3333-4333-8333-333333333333",
    "REJECTED": "a4444444-4444-4444-8444-444444444444",
    "PUBLISHED": "a5555555-5555-4555-8555-555555555555",
    "APPROVED": "a7777777-7777-4777-8777-777777777777",
}


def _status(name: str, sid: str, order: int) -> dict:
    return {"id": sid, "order": order, "template": {"id": TEMPLATE_IDS[name], "name": name, "color": "#00b1ff"}}


REPORT = {
    "id": "Report",
    "label": "Report",
    "workflowEnabled": True,
    "statuses": [_status(n, sid, i) for i, (n, sid) in enumerate(REPORT_STATUS_IDS.items(), start=1)],
    "statusesRequestAccess": [],
}
CASE = {
    "id": "Case-Incident",
    "label": "Case-Incident",
    "workflowEnabled": True,
    "statuses": [_status("PUBLISHED", CASE_PUBLISHED_ID, 1)],
    "statusesRequestAccess": [_status("APPROVED", "77777777-7777-4777-8777-777777777777", 1)],
}
MALWARE = {"id": "Malware", "label": "Malware", "workflowEnabled": False, "statuses": [], "statusesRequestAccess": []}
TYPES = {"Report": REPORT, "Case-Incident": CASE, "Malware": MALWARE}
SCHEMA = {
    "data": {
        "filterKeysSchema": [
            {
                "entity_type": "Stix-Core-Object",
                "filters_schema": [
                    {
                        "filterKey": "workflow_id",
                        "filterDefinition": {
                            "filterKey": "workflow_id",
                            "type": "id",
                            "elementsForFilterValuesSearch": ["StatusTemplate"],
                        },
                    },
                    {
                        "filterKey": "entity_type",
                        "filterDefinition": {
                            "filterKey": "entity_type",
                            "type": "string",
                            "elementsForFilterValuesSearch": [],
                        },
                    },
                ],
            },
        ]
    }
}


def _strip_request_access(node: dict) -> dict:
    return {k: v for k, v in node.items() if k != "statusesRequestAccess"}


async def _run(tool: str, arguments: dict, *, request_access_known: bool = True):
    executor = CallableToolExecutor()
    build_tools("https://octi.test||static-token", executor)
    calls: list[dict] = []

    async def _fake(self, method, url, **kwargs):
        body = kwargs.get("json") or {}
        query, variables = body.get("query", ""), body.get("variables") or {}
        calls.append(body)
        request = httpx.Request("POST", url)

        def ok(data: dict) -> httpx.Response:
            return httpx.Response(200, json={"data": data}, request=request)

        if "statusesRequestAccess" in query and not request_access_known:
            return httpx.Response(
                200,
                json={"errors": [{"message": 'Cannot query field "statusesRequestAccess" on type "SubType".'}]},
                request=request,
            )
        if "SubTypeWorkflow(" in query:
            node = TYPES.get(variables.get("id"))
            if node and not request_access_known:
                node = _strip_request_access(node)
            return ok({"subType": node})
        if "SubTypeWorkflows(" in query:
            search = (variables.get("search") or "").lower()
            nodes = [n for n in TYPES.values() if search in n["label"].lower()]
            if not request_access_known:
                nodes = [_strip_request_access(n) for n in nodes]
            return ok({"subTypes": {"edges": [{"node": n} for n in nodes]}})
        if "EntitySetting(" in query:
            return ok(
                {
                    "entitySettingByType": {
                        "id": "es-1",
                        "target_type": variables.get("t"),
                        "platform_hidden_type": False,
                        "enforce_reference": False,
                        "platform_entity_files_ref": False,
                        "attributes_configuration": None,
                        "mandatoryAttributes": [],
                        "availableSettings": ["workflow_configuration", "enforce_reference"],
                    }
                }
            )
        if "FilterKeysSchema" in query:
            return httpx.Response(200, json=SCHEMA, request=request)
        if "query SubTypes(" in query:
            return ok({"subTypes": {"edges": []}})
        if "ListSDOs" in query:
            return ok({"stixDomainObjects": {"edges": [], "pageInfo": {"globalCount": 0}}})
        return ok({})

    with patch.object(httpx.AsyncClient, "request", _fake):
        result = await executor.execute_tool(ToolCall(id="1", name=tool, arguments=arguments))
    return result.content, calls


def _fg(*filters: dict) -> dict:
    return {"mode": "and", "filters": list(filters), "filterGroups": []}


def _workflow(*values: str) -> dict:
    return {"key": ["workflow_id"], "values": list(values), "operator": "eq", "mode": "or"}


def _entity_type(*values: str) -> dict:
    return {"key": ["entity_type"], "values": list(values), "operator": "eq", "mode": "or"}


@pytest.mark.asyncio
async def test_subtypes_list_status_ids_distinct_from_template_ids():
    content, _ = await _run("list_opencti_subtypes", {"search": "Report"})
    for name, sid in REPORT_STATUS_IDS.items():
        assert f"{name} (status id {sid} to set, template {TEMPLATE_IDS[name]} for filters" in content
    assert "order 1" in content and "color #00b1ff" in content


@pytest.mark.asyncio
async def test_subtypes_type_argument_reads_one_type_exactly():
    content, calls = await _run("list_opencti_subtypes", {"type": "Case-Incident"})
    assert any("SubTypeWorkflow(" in c["query"] and c["variables"] == {"id": "Case-Incident"} for c in calls)
    assert "Report" not in content
    assert f"PUBLISHED (status id {CASE_PUBLISHED_ID}" in content
    assert "request-access workflow statuses: APPROVED" in content


@pytest.mark.asyncio
async def test_subtypes_type_without_workflow_lists_no_status():
    content, _ = await _run("list_opencti_subtypes", {"type": "Malware"})
    assert "- Malware (id: Malware)" in content
    assert "[workflow]" not in content
    assert "status id" not in content


@pytest.mark.asyncio
async def test_subtypes_fall_back_when_request_access_is_unknown():
    content, calls = await _run("list_opencti_subtypes", {"type": "Report"}, request_access_known=False)
    assert len([c for c in calls if "SubTypeWorkflow(" in c["query"]]) == 2
    assert f"PUBLISHED (status id {REPORT_STATUS_IDS['PUBLISHED']}" in content
    assert "request-access" not in content


@pytest.mark.asyncio
async def test_entity_setting_get_by_type_shows_the_workflow():
    content, _ = await _run("manage_opencti_entity_setting", {"action": "get_by_type", "target_type": "Report"})
    assert "workflow enabled: True" in content
    assert f"PUBLISHED (status id {REPORT_STATUS_IDS['PUBLISHED']}" in content


@pytest.mark.asyncio
async def test_entity_setting_add_status_writes_through_sub_type_edit_and_reads_back() -> None:
    args = {"action": "add_status", "target_type": "Case-Incident", "template_id": TEMPLATE_IDS["NEW"], "order": 1}
    content, calls = await _run("manage_opencti_entity_setting", args)
    write = next(c for c in calls if "SubTypeStatusAdd(" in c["query"])
    assert "subTypeEdit(id: $id) { statusAdd(" in write["query"]
    assert write["variables"] == {
        "id": "Case-Incident",
        "input": {"template_id": TEMPLATE_IDS["NEW"], "order": 1, "scope": "GLOBAL"},
    }
    assert content.startswith("Added a status to the Case-Incident workflow. Read back:")
    assert f"PUBLISHED (status id {CASE_PUBLISHED_ID}" in content


@pytest.mark.asyncio
async def test_entity_setting_update_and_delete_status_take_the_per_type_status_id() -> None:
    sid = REPORT_STATUS_IDS["QUALIFIED"]
    _, calls = await _run(
        "manage_opencti_entity_setting",
        {"action": "update_status", "target_type": "Report", "status_id": sid, "order": 4},
    )
    patch_call = next(c for c in calls if "SubTypeStatusPatch(" in c["query"])
    assert patch_call["variables"] == {"id": "Report", "statusId": sid, "input": [{"key": "order", "value": [4]}]}
    content, calls = await _run(
        "manage_opencti_entity_setting", {"action": "delete_status", "target_type": "Report", "status_id": sid}
    )
    delete_call = next(c for c in calls if "SubTypeStatusDelete(" in c["query"])
    assert delete_call["variables"] == {"id": "Report", "statusId": sid}
    assert content.startswith("Removed a status from the Report workflow. Read back:")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("arguments", "refusal"),
    [
        ({"action": "add_status", "target_type": "Report", "order": 1}, "template_id and order are required"),
        (
            {"action": "add_status", "target_type": "Report", "template_id": "t", "order": 1, "scope": "x"},
            "scope must be",
        ),
        ({"action": "update_status", "target_type": "Report", "order": 2}, "status_id (the per-type Status id"),
        ({"action": "update_status", "target_type": "Report", "status_id": "s"}, "Provide order and/or template_id"),
        ({"action": "delete_status", "status_id": "s"}, "target_type is required"),
    ],
)
async def test_entity_setting_status_actions_refuse_before_any_write(
    arguments: dict[str, object], refusal: str
) -> None:
    content, calls = await _run("manage_opencti_entity_setting", arguments)
    assert refusal in content
    assert not any("subTypeEdit" in c["query"] for c in calls)


@pytest.mark.asyncio
async def test_validate_nlq_filter_resolves_a_status_name_to_its_template_id():
    fg = _fg(_entity_type("Report"), _workflow("published"))
    result, _ = await _run("validate_nlq_filter", {"filter_str": json.dumps(fg)})
    out = json.loads(result) if isinstance(result, str) else result
    flt = next(f for f in out["filters"]["filters"] if f["key"] == ["workflow_id"])
    assert flt["values"] == [TEMPLATE_IDS["PUBLISHED"]]
    assert out["resolvedStatusNames"] == {"published": [TEMPLATE_IDS["PUBLISHED"]]}
    assert "error" not in out


@pytest.mark.asyncio
async def test_validate_nlq_filter_without_type_carries_one_template_id():
    result, _ = await _run("validate_nlq_filter", {"filter_str": json.dumps(_fg(_workflow("PUBLISHED")))})
    out = json.loads(result) if isinstance(result, str) else result
    flt = out["filters"]["filters"][0]
    assert flt["values"] == [TEMPLATE_IDS["PUBLISHED"]]


@pytest.mark.asyncio
async def test_validate_nlq_filter_unknown_name_lists_the_valid_names():
    fg = _fg(_entity_type("Report"), _workflow("ARCHIVED"))
    result, _ = await _run("validate_nlq_filter", {"filter_str": json.dumps(fg)})
    out = json.loads(result) if isinstance(result, str) else result
    assert "STATUS_NOT_FOUND" in out["error"]
    assert "ARCHIVED" in out["error"]
    for name in REPORT_STATUS_IDS:
        assert name in out["error"]


@pytest.mark.asyncio
async def test_validate_nlq_filter_keeps_a_template_id_untouched():
    tid = TEMPLATE_IDS["NEW"]
    result, _ = await _run("validate_nlq_filter", {"filter_str": json.dumps(_fg(_workflow(tid)))})
    out = json.loads(result) if isinstance(result, str) else result
    assert out["filters"]["filters"][0]["values"] == [tid]
    assert "resolvedStatusNames" not in out


@pytest.mark.asyncio
async def test_validate_nlq_filter_normalises_per_type_status_ids_to_one_template_id():
    fg = _fg(_workflow(REPORT_STATUS_IDS["PUBLISHED"], CASE_PUBLISHED_ID, TEMPLATE_IDS["PUBLISHED"]))
    result, _ = await _run("validate_nlq_filter", {"filter_str": json.dumps(fg)})
    out = json.loads(result) if isinstance(result, str) else result
    assert out["filters"]["filters"][0]["values"] == [TEMPLATE_IDS["PUBLISHED"]]


@pytest.mark.asyncio
async def test_list_entities_resolves_the_name_for_its_types_and_echoes_it():
    fg = _fg(_workflow("Qualified"))
    content, calls = await _run("list_opencti_entities", {"types": ["Report"], "filters": json.dumps(fg)})
    sent = next(c for c in calls if "ListSDOs" in c["query"])
    assert sent["variables"]["filters"]["filters"][0]["values"] == [TEMPLATE_IDS["QUALIFIED"]]
    assert f"Qualified -> {TEMPLATE_IDS['QUALIFIED']}" in content


@pytest.mark.asyncio
async def test_list_entities_replaces_a_status_id_by_its_template_id_with_a_note():
    fg = _fg(_workflow(REPORT_STATUS_IDS["QUALIFIED"]))
    content, calls = await _run("list_opencti_entities", {"types": ["Report"], "filters": json.dumps(fg)})
    sent = next(c for c in calls if "ListSDOs" in c["query"])
    assert sent["variables"]["filters"]["filters"][0]["values"] == [TEMPLATE_IDS["QUALIFIED"]]
    assert "template ids" in content


@pytest.mark.asyncio
async def test_list_entities_unknown_name_is_refused_before_the_query():
    fg = _fg(_workflow("NOPE"))
    content, calls = await _run("list_opencti_entities", {"types": ["Malware"], "filters": json.dumps(fg)})
    assert "Unknown workflow status name(s) for Malware: NOPE" in content
    assert "none (no workflow is enabled)" in content
    assert not any("ListSDOs" in c["query"] for c in calls)


@pytest.mark.asyncio
async def test_trigger_filter_status_id_is_written_as_template_id_with_a_note():
    fg = _fg(_workflow(REPORT_STATUS_IDS["QUALIFIED"]))
    content, calls = await _run(
        "manage_opencti_trigger",
        {"action": "create_live", "name": "t", "filters": json.dumps(fg)},
    )
    written = [c for c in calls if "triggerKnowledgeLiveAdd" in c["query"]]
    assert written, content
    stored = json.loads(written[0]["variables"]["input"]["filters"])
    assert stored["filters"][0]["values"] == [TEMPLATE_IDS["QUALIFIED"]]
    assert "template ids" in content


# Resolver unit tests (no tool).


async def _fake_gql(query: str, variables: dict | None = None):
    if "SubTypeWorkflow(" in query:
        return {"subType": TYPES.get((variables or {}).get("id"))}, None
    return {"subTypes": {"edges": [{"node": n} for n in TYPES.values()]}}, None


@pytest.mark.asyncio
async def test_resolver_maps_request_access_status_id_and_keeps_unknown_ids():
    from app.integrations.providers.opencti.workflow import resolve_status_names

    approved_status = "77777777-7777-4777-8777-777777777777"
    unknown = "99999999-9999-4999-8999-999999999999"
    fg = _fg(_workflow(approved_status, unknown))
    out, resolved, err = await resolve_status_names(fg, _fake_gql)
    assert err is None
    assert resolved == {approved_status: [TEMPLATE_IDS["APPROVED"]]}
    assert out["filters"][0]["values"] == [TEMPLATE_IDS["APPROVED"], unknown]
    assert fg["filters"][0]["values"][0] == approved_status  # input not mutated


@pytest.mark.asyncio
async def test_playbook_configuration_filters_normalised_and_change_status_kept():
    from app.integrations.providers.opencti.workflow import resolve_configuration_filters

    inner = json.dumps(_fg(_workflow(REPORT_STATUS_IDS["QUALIFIED"], "Qualified")))
    config = json.dumps({"filters": inner, "status_id": REPORT_STATUS_IDS["QUALIFIED"]})
    out, note, err = await resolve_configuration_filters(config, _fake_gql)
    assert err is None and note
    parsed = json.loads(out)
    assert json.loads(parsed["filters"])["filters"][0]["values"] == [TEMPLATE_IDS["QUALIFIED"]]
    assert parsed["status_id"] == REPORT_STATUS_IDS["QUALIFIED"]


@pytest.mark.asyncio
async def test_playbook_configuration_without_workflow_filter_is_byte_for_byte():
    from app.integrations.providers.opencti.workflow import resolve_configuration_filters

    config = '{"status_id": "x",  "filters": "{}"}'
    out, note, err = await resolve_configuration_filters(config, _fake_gql)
    assert (out, note, err) == (config, "", None)
