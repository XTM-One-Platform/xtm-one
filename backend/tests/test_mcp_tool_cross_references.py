"""A tool text names another tool only where that tool is served.

``get_opencti_container_full`` points at ``download_opencti_file`` to read an
attached file. The platform MCP server (``/mcp/opencti``) does not serve that
tool, and an agent's tool scope may drop it, so the pointer appears - in the
description and in the result - only on a surface that offers the tool under
that exact name: ``fit_descriptions_to_surface`` / ``offered_tools()``, which
the agent loop and the MCP servers both bind.
"""

from __future__ import annotations

import re
import uuid
from types import SimpleNamespace

import httpx
import pytest

from app.api.v1 import mcp_platform
from app.api.v1.mcp_platform import MCP_TOOL_ALLOW_LISTS
from app.integrations.providers import openaev, opencrq, opencti
from app.services.integration_tools import _suffix_instance_tools
from app.services.mcp_tool_metadata import tools_to_mcp
from app.tools.base import (
    CallableToolExecutor,
    ScopedToolExecutor,
    ToolCall,
    ToolDefinition,
    fit_descriptions_to_surface,
    offering_tools,
)
from tests.integration_file_fakes import fake_api

_BASE = "https://octi.example.com"
_CONTAINER = "get_opencti_container_full"
_DOWNLOAD = "download_opencti_file"
_POINTER = f"Use {_DOWNLOAD} with the file ID"
_FILE_ID = "import/Report/r1/findings.pdf"


def _opencti(**kwargs) -> tuple[list[ToolDefinition], CallableToolExecutor]:
    executor = CallableToolExecutor()
    defs = opencti.build_tools(f"{_BASE}||octi-token", executor, **kwargs)
    return defs, executor


def _mcp_surface(defs: list[ToolDefinition]) -> list[ToolDefinition]:
    """What ``/mcp/opencti`` serves out of *defs*: its allow-list."""
    return [d for d in defs if d.name in MCP_TOOL_ALLOW_LISTS["opencti"]]


def _description(defs: list[ToolDefinition], name: str = _CONTAINER) -> str:
    return next(d.description for d in defs if d.name == name)


# ── Description ──────────────────────────────────────────────────────────────


def test_the_mcp_surface_does_not_serve_the_file_tool():
    defs, _ = _opencti(direct_mutations=True)
    served = {d.name for d in _mcp_surface(defs)}
    assert _CONTAINER in served
    assert _DOWNLOAD not in served


def test_the_mcp_listed_description_does_not_name_the_file_tool():
    defs, _ = _opencti(direct_mutations=True)
    listed = {t["name"]: t["description"] for t in tools_to_mcp(_mcp_surface(defs))}
    assert _DOWNLOAD not in listed[_CONTAINER]
    assert "attached files (with IDs and download links)" in listed[_CONTAINER]


def test_a_surface_that_offers_the_file_tool_names_it():
    defs, _ = _opencti()
    assert _DOWNLOAD in {d.name for d in defs}
    fitted = fit_descriptions_to_surface(defs)
    assert f"attached files (with IDs for download via {_DOWNLOAD})" in _description(fitted)


def test_the_unresolved_description_names_no_other_tool():
    """What a catalog or a trace reads, with no surface to vouch for the file tool."""
    defs, _ = _opencti()
    assert _DOWNLOAD not in _description(defs)


def test_a_surface_offering_the_file_tool_under_another_name_is_not_pointed_at_it():
    defs, _ = _opencti()
    container = next(d for d in defs if d.name == _CONTAINER)
    renamed = ToolDefinition(name=f"{_DOWNLOAD}__prod", description="Download.")
    (fitted,) = [
        d for d in fit_descriptions_to_surface([container, renamed]) if d.name == _CONTAINER
    ]
    assert _DOWNLOAD not in fitted.description


def test_an_instance_suffixed_tool_keeps_its_label_on_its_surface_description():
    defs, executor = _opencti()
    suffixed = _suffix_instance_tools(
        defs,
        executor,
        provider="opencti",
        integ_name="Prod",
        integ_desc=None,
        integration_id=str(uuid.uuid4()),
        used_slugs={},
    )
    container = next(d for d in suffixed if d.base_name == _CONTAINER)
    assert container.description.startswith("[Prod] ")
    (fitted,) = [
        d for d in fit_descriptions_to_surface(suffixed) if d.base_name == _CONTAINER
    ]
    assert fitted.description.startswith("[Prod] ")
    # The surface offers ``download_opencti_file__<slug>``, never the bare name.
    assert _DOWNLOAD not in fitted.description


@pytest.mark.parametrize(
    ("provider", "module", "kwargs"),
    [
        ("opencti", opencti, {"direct_mutations": True}),
        ("openaev", openaev, {}),
        ("opencrq", opencrq, {}),
    ],
)
def test_no_mcp_listed_description_names_a_tool_the_server_does_not_serve(provider, module, kwargs):
    """Every platform MCP server: a listed description names only listed tools."""
    executor = CallableToolExecutor()
    defs = module.build_tools("http://platform.local||token", executor, **kwargs)
    allow = MCP_TOOL_ALLOW_LISTS[provider]
    hidden = {d.name for d in defs} - allow
    served = [d for d in defs if d.name in allow]
    named = {
        tool["name"]: sorted(
            name for name in hidden if re.search(rf"\b{re.escape(name)}\b", tool["description"])
        )
        for tool in tools_to_mcp(served)
    }
    assert {name: found for name, found in named.items() if found} == {}


# ── Result ───────────────────────────────────────────────────────────────────


def _report_with_a_file(request: httpx.Request) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "data": {
                "stixDomainObject": {
                    "id": "r1",
                    "entity_type": "Report",
                    "name": "Weekly",
                    "importFiles": {
                        "edges": [
                            {
                                "node": {
                                    "id": _FILE_ID,
                                    "name": "findings.pdf",
                                    "uploadStatus": "complete",
                                    "metaData": {"mimetype": "application/pdf"},
                                }
                            }
                        ]
                    },
                    "objects": {"edges": [], "pageInfo": {"globalCount": 0}},
                }
            }
        },
    )


async def _container_text(executor: CallableToolExecutor) -> str:
    with fake_api(_report_with_a_file):
        result = await executor.execute_tool(
            ToolCall(id="c1", name=_CONTAINER, arguments={"container_id": "r1"})
        )
    assert not result.is_error, result.content
    return result.content


@pytest.mark.asyncio
async def test_the_result_points_at_the_file_tool_where_it_is_offered():
    defs, executor = _opencti()
    with offering_tools(defs):
        text = await _container_text(executor)
    assert _POINTER in text
    assert f"findings.pdf (ID: {_FILE_ID}" in text


@pytest.mark.asyncio
async def test_the_result_names_no_tool_outside_a_surface():
    _, executor = _opencti()
    text = await _container_text(executor)
    assert _DOWNLOAD not in text
    # The file is still listed, with its ID and its download link.
    assert f"findings.pdf (ID: {_FILE_ID}" in text
    assert f"{_BASE}/storage/get/import%2FReport%2Fr1%2Ffindings.pdf" in text


@pytest.mark.asyncio
async def test_the_mcp_tools_call_result_does_not_name_the_file_tool(monkeypatch):
    async def _no_tracking(_registration):
        return None

    monkeypatch.setattr(mcp_platform, "_track_platform_tool_call", _no_tracking)
    defs, executor = _opencti(direct_mutations=True)
    served = _mcp_surface(defs)
    with fake_api(_report_with_a_file):
        response = await mcp_platform._handle_tools_call(
            1,
            {"name": _CONTAINER, "arguments": {"container_id": "r1"}},
            served,
            ScopedToolExecutor(executor, served),
            SimpleNamespace(integration_id=uuid.uuid4()),
        )
    assert response["result"]["isError"] is False
    text = response["result"]["content"][0]["text"]
    assert f"findings.pdf (ID: {_FILE_ID}" in text
    assert _DOWNLOAD not in text


@pytest.mark.asyncio
async def test_the_mcp_tools_call_binds_the_served_tools_as_the_surface(monkeypatch):
    """A tool run by ``tools/call`` reads the server's tools as its surface."""
    from app.tools.base import offered_tools

    async def _no_tracking(_registration):
        return None

    monkeypatch.setattr(mcp_platform, "_track_platform_tool_call", _no_tracking)
    seen: list[tuple[str, ...] | None] = []
    executor = CallableToolExecutor()

    async def _probe() -> str:
        surface = offered_tools()
        seen.append(None if surface is None else tuple(t.name for t in surface))
        return "ok"

    executor.register("search_opencti", _probe)
    served = [
        ToolDefinition(name="search_opencti", description="Search."),
        ToolDefinition(name="get_opencti_entity", description="Get."),
    ]
    await mcp_platform._handle_tools_call(
        2,
        {"name": "search_opencti", "arguments": {}},
        served,
        ScopedToolExecutor(executor, served),
        SimpleNamespace(integration_id=uuid.uuid4()),
    )
    assert seen == [("search_opencti", "get_opencti_entity")]
