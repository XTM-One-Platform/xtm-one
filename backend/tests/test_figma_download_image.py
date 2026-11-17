"""Tests for the Figma ``download_figma_image`` tool.

Figma's export API only returns a short-lived signed URL, not bytes. This tool
fetches the rendered bytes server-side and registers them as a forwardable
file_id (a download card) - the same hop as Notion's ``download_notion_file`` -
so a diagram can be embedded / attached as evidence. The HTTP layer is mocked at
``figma.safe_request`` (used for both the export call and the byte fetch).
"""

from unittest.mock import AsyncMock, patch

import httpx
import pytest

from app.integrations.providers.figma import build_tools
from app.services.integration_tools import _SafeIntegrationExecutor
from app.tools.base import ToolCall

# A Figma signed export URL lives on a Figma/AWS host (download allow-list).
_SIGNED = "https://figma-alpha-api.s3.us-west-2.amazonaws.com/images/abc.png"


def _ok(payload: dict) -> tuple[httpx.Response, None]:
    return (httpx.Response(200, json=payload), None)


def _bytes_resp(data: bytes, content_type: str) -> tuple[httpx.Response, None]:
    return (httpx.Response(200, content=data, headers={"content-type": content_type}), None)


def _build() -> _SafeIntegrationExecutor:
    executor = _SafeIntegrationExecutor()
    build_tools("dummy-token", executor)
    return executor


def test_download_figma_image_in_tool_surface():
    executor = _SafeIntegrationExecutor()
    defs = build_tools("dummy-token", executor)
    tool = next(t for t in defs if t.name == "download_figma_image")
    assert tool.input_schema["required"] == []
    for key in ("file_key", "node_id", "image_url", "format", "scale", "filename"):
        assert key in tool.input_schema["properties"]
    assert tool.source == "integration:figma"


@pytest.mark.asyncio
async def test_download_figma_image_via_node_returns_file_id():
    executor = _build()
    with patch(
        "app.integrations.providers.figma.safe_request", new_callable=AsyncMock
    ) as mock_request:
        mock_request.side_effect = [
            _ok({"err": None, "images": {"1:2": _SIGNED}}),
            _bytes_resp(b"\x89PNG diagram bytes", "image/png"),
        ]
        result = await executor.execute_tool(
            ToolCall(
                id="f1",
                name="download_figma_image",
                arguments={"file_key": "FILEKEY", "node_id": "1:2"},
            )
        )
    assert not result.is_error, result.content
    assert "Downloaded Figma render" in result.content
    assert "[[FILE:" in result.content
    assert result.files and len(result.files) == 1
    assert result.files[0].content_type == "image/png"
    # export hit the images endpoint at scale 2 (default), then the signed URL
    export_call = mock_request.call_args_list[0]
    assert export_call.args[2].endswith("/v1/images/FILEKEY")
    assert export_call.kwargs["params"]["scale"] == 2.0
    assert mock_request.call_args_list[1].args[2] == _SIGNED


@pytest.mark.asyncio
async def test_download_figma_image_via_image_url():
    executor = _build()
    with patch(
        "app.integrations.providers.figma.safe_request", new_callable=AsyncMock
    ) as mock_request:
        mock_request.return_value = _bytes_resp(b"%PDF-1.4 diagram", "application/pdf")
        result = await executor.execute_tool(
            ToolCall(
                id="f2",
                name="download_figma_image",
                arguments={"image_url": _SIGNED, "filename": "architecture.pdf"},
            )
        )
    assert not result.is_error, result.content
    assert "architecture.pdf" in result.content
    # no export call - went straight to the signed URL fetch
    assert mock_request.call_count == 1
    assert mock_request.call_args_list[0].args[2] == _SIGNED


@pytest.mark.asyncio
async def test_download_figma_image_rejects_foreign_host():
    executor = _build()
    with patch(
        "app.integrations.providers.figma.safe_request", new_callable=AsyncMock
    ) as mock_request:
        result = await executor.execute_tool(
            ToolCall(
                id="f3",
                name="download_figma_image",
                arguments={"image_url": "https://169.254.169.254/latest/meta-data/"},
            )
        )
    assert result.is_error
    assert "refusing to download" in result.content
    mock_request.assert_not_called()


@pytest.mark.asyncio
async def test_download_figma_image_node_render_empty():
    executor = _build()
    with patch(
        "app.integrations.providers.figma.safe_request", new_callable=AsyncMock
    ) as mock_request:
        mock_request.return_value = _ok({"err": None, "images": {"1:2": None}})
        result = await executor.execute_tool(
            ToolCall(
                id="f4",
                name="download_figma_image",
                arguments={"file_key": "FILEKEY", "node_id": "1:2"},
            )
        )
    assert result.is_error
    assert "rendered no image" in result.content


@pytest.mark.asyncio
async def test_download_figma_image_requires_a_source():
    executor = _build()
    result = await executor.execute_tool(
        ToolCall(id="f5", name="download_figma_image", arguments={})
    )
    assert result.is_error
    assert "file_key" in result.content and "image_url" in result.content


@pytest.mark.asyncio
async def test_download_figma_image_rejects_both_sources():
    executor = _build()
    result = await executor.execute_tool(
        ToolCall(
            id="f6",
            name="download_figma_image",
            arguments={"file_key": "K", "node_id": "1:2", "image_url": _SIGNED},
        )
    )
    assert result.is_error
    assert "not both" in result.content


@pytest.mark.asyncio
async def test_download_figma_image_rejects_bad_format():
    executor = _build()
    result = await executor.execute_tool(
        ToolCall(
            id="f7",
            name="download_figma_image",
            arguments={"file_key": "K", "node_id": "1:2", "format": "gif"},
        )
    )
    assert result.is_error
    assert "format must be one of" in result.content
