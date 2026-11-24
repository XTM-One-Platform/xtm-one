# SPDX-License-Identifier: Apache-2.0
"""manage_opencti_entity_setting: the "Entity status sync" setting (sync_workflow_status_by_name).

OpenCTI exposes it as an entity setting a stream synchronization reads to
name-match incoming workflow statuses. The tool patches it like the other
switches (written as the string OpenCTI's own screen sends), shows it in
``get_by_type``, refuses it on a type that does not offer it, and still reads
an OpenCTI that predates the field.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import httpx
import pytest

from app.integrations.providers.opencti import build_tools
from app.tools.base import CallableToolExecutor, ToolCall

_UNKNOWN_FIELD = 'Cannot query field "sync_workflow_status_by_name" on type "EntitySetting".'


async def _run(
    arguments: dict[str, Any], *, available: list[str], field_known: bool = True
) -> tuple[Any, list[dict[str, Any]]]:
    executor = CallableToolExecutor()
    build_tools("https://octi.test||static-token", executor)
    calls: list[dict[str, Any]] = []

    async def _fake(self: httpx.AsyncClient, method: str, url: str, **kwargs: Any) -> httpx.Response:
        body = kwargs.get("json") or {}
        query = body.get("query", "")
        calls.append(body)
        request = httpx.Request("POST", url)
        if "sync_workflow_status_by_name" in query and not field_known:
            return httpx.Response(200, json={"errors": [{"message": _UNKNOWN_FIELD}]}, request=request)
        if "EsPatch(" in query:
            return httpx.Response(
                200,
                json={"data": {"entitySettingsFieldPatch": [{"id": "es-1", "target_type": "Report"}]}},
                request=request,
            )
        if "EntitySetting(" in query:
            node: dict[str, Any] = {
                "id": "es-1",
                "target_type": "Report",
                "platform_hidden_type": False,
                "enforce_reference": False,
                "platform_entity_files_ref": False,
                "attributes_configuration": None,
                "mandatoryAttributes": [],
                "availableSettings": available,
            }
            if field_known:
                node["sync_workflow_status_by_name"] = True
            return httpx.Response(200, json={"data": {"entitySettingByType": node}}, request=request)
        return httpx.Response(200, json={"data": {}}, request=request)

    with patch.object(httpx.AsyncClient, "request", _fake):
        result = await executor.execute_tool(
            ToolCall(id="1", name="manage_opencti_entity_setting", arguments=arguments)
        )
    return result.content, calls


@pytest.mark.asyncio
@pytest.mark.parametrize(("flag", "written"), [(True, "true"), (False, "false")])
async def test_patch_writes_the_status_sync_setting_as_opencti_stores_it(flag: bool, written: str) -> None:
    content, calls = await _run(
        {"action": "patch", "target_type": "Report", "sync_workflow_status_by_name": flag},
        available=["workflow_configuration", "sync_workflow_status_by_name"],
    )
    write = next(c for c in calls if "EsPatch(" in c["query"])
    assert write["variables"] == {
        "ids": ["es-1"],
        "input": [{"key": "sync_workflow_status_by_name", "value": [written]}],
    }
    assert content == "Updated entity setting for 'Report'."


@pytest.mark.asyncio
async def test_patch_refuses_the_setting_where_the_type_does_not_offer_it() -> None:
    content, calls = await _run(
        {"action": "patch", "target_type": "Report", "sync_workflow_status_by_name": True},
        available=["enforce_reference"],
    )
    assert "sync_workflow_status_by_name is not available for 'Report'" in content
    assert "Nothing was written." in content
    assert not any("EsPatch(" in c["query"] for c in calls)


@pytest.mark.asyncio
async def test_get_by_type_shows_the_status_sync_setting() -> None:
    content, _ = await _run({"action": "get_by_type", "target_type": "Report"}, available=["enforce_reference"])
    assert "  sync_workflow_status_by_name: True" in content


@pytest.mark.asyncio
async def test_get_by_type_reads_an_opencti_without_the_field() -> None:
    content, calls = await _run(
        {"action": "get_by_type", "target_type": "Report"}, available=["enforce_reference"], field_known=False
    )
    reads = [c for c in calls if "EntitySetting(" in c["query"]]
    assert len(reads) == 2
    assert "sync_workflow_status_by_name" not in reads[1]["query"]
    assert content.startswith("Entity setting for Report (id: es-1)")
    assert "sync_workflow_status_by_name" not in content


@pytest.mark.asyncio
async def test_the_setting_is_named_as_ignored_on_an_action_that_does_not_apply_it() -> None:
    content, _calls = await _run(
        {"action": "list", "sync_workflow_status_by_name": True}, available=["enforce_reference"]
    )
    assert "does not apply sync_workflow_status_by_name" in content
    assert "ignored and the rest ran" in content
