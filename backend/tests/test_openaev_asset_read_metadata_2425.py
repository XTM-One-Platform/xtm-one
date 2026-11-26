"""The OpenAEV asset read tools show what a collector wrote (#2425).

``get_openaev_asset`` and ``search_openaev_assets`` format through ``_fmt_asset``.
It used to drop ``asset_metadata`` (the map collectors write, e.g. the GitHub
collector's ``github_state`` / ``github_end_date``), omit a null
``asset_internet_facing`` and print tags as ids. These tests pin:

* the metadata block (sorted keys, bounded values and key count), absent when empty;
* ``Internet-facing: unset`` for a null flag, ``False`` kept as a value;
* tag names resolved from ONE ``/api/tags`` read per tool call, an unknown id
  printed alone, the ids kept when the tag read fails;
* no tag read when no asset carries a tag.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any
from unittest.mock import patch

import httpx
import pytest

from app.integrations.providers.openaev._common import _fmt_asset, _headers
from app.integrations.providers.openaev.core import register_core
from app.tools.base import CallableToolExecutor, ToolCall

BASE = "https://demo.openaev.io"
TAG_A = "11111111-aaaa-4aaa-8aaa-111111111111"
TAG_B = "22222222-bbbb-4bbb-8bbb-222222222222"

ENDPOINT: dict[str, Any] = {
    "asset_id": "a1",
    "asset_name": "repo-1",
    "asset_internet_facing": None,
    "asset_tags": [TAG_A, TAG_B],
    "asset_metadata": {"github_state": "deleted", "github_end_date": "2026-11-25T23:45:00Z"},
}

TAGS = [
    {"tag_id": TAG_A, "tag_name": "github-state:deleted"},
    {"tag_id": TAG_B, "tag_name": "source:github"},
]


def test_fmt_asset_prints_metadata_unset_flag_and_tag_names() -> None:
    out = _fmt_asset(ENDPOINT, tag_names={TAG_A: "github-state:deleted"})

    assert "  Metadata:" in out
    assert "    github_end_date: 2026-11-25T23:45:00Z" in out
    assert "    github_state: deleted" in out
    assert out.index("github_end_date") < out.index("github_state")
    assert "Internet-facing: unset" in out
    assert f"Tags: github-state:deleted ({TAG_A}), {TAG_B}" in out


def test_fmt_asset_without_metadata_or_flag_prints_neither() -> None:
    out = _fmt_asset({"asset_id": "a1", "asset_name": "n", "asset_metadata": {}, "asset_tags": [TAG_A]})

    assert "Metadata" not in out
    assert "Internet-facing" not in out
    assert f"Tags: {TAG_A}" in out


def test_fmt_asset_keeps_a_false_flag_as_a_value() -> None:
    out = _fmt_asset({"asset_id": "a1", "asset_internet_facing": False})

    assert "Internet-facing: False" in out
    assert "unset" not in out


def test_fmt_asset_bounds_the_metadata_block() -> None:
    metadata: dict[str, Any] = {f"k{i:02d}": "x" * 500 for i in range(35)}
    metadata["nested"] = {"a": 1}
    out = _fmt_asset({"asset_id": "a1", "asset_metadata": metadata})

    assert "    +6 more" in out
    assert "x" * 201 not in out
    assert ("x" * 200 + "...") in out


def _server(sent: list[str], *, tags_status: int = 200) -> Callable[..., Awaitable[httpx.Response]]:
    async def _fake_request(self: httpx.AsyncClient, method: str, url: Any, **kwargs: Any) -> httpx.Response:
        request = httpx.Request(method, str(url))
        path = httpx.URL(str(url)).path
        sent.append(path)
        if path == "/api/tags":
            if tags_status != 200:
                return httpx.Response(tags_status, request=request, json={"message": "nope"})
            return httpx.Response(200, request=request, json=TAGS)
        if path == "/api/endpoints/search":
            return httpx.Response(200, request=request, json={"content": [ENDPOINT], "totalElements": 1})
        if path == "/api/endpoints/a1":
            return httpx.Response(200, request=request, json=ENDPOINT)
        if path == "/api/endpoints/a2":
            return httpx.Response(200, request=request, json={"asset_id": "a2", "asset_name": "bare"})
        return httpx.Response(404, request=request, json={})

    return _fake_request


async def _call(name: str, arguments: dict[str, Any], sent: list[str], **server: Any) -> str:
    executor = CallableToolExecutor()
    register_core(BASE, _headers("token"), executor)
    with patch.object(httpx.AsyncClient, "request", _server(sent, **server)):
        result = await executor.execute_tool(ToolCall(id="1", name=name, arguments=arguments))
    assert not result.is_error, result.content
    return result.content


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tool", "arguments"),
    [("get_openaev_asset", {"asset_id": "a1"}), ("search_openaev_assets", {"search": "repo"})],
)
async def test_both_read_tools_show_metadata_unset_flag_and_tag_names(tool: str, arguments: dict[str, Any]) -> None:
    sent: list[str] = []
    content = await _call(tool, arguments, sent)

    assert "github_state: deleted" in content
    assert "github_end_date: 2026-11-25T23:45:00Z" in content
    assert "Internet-facing: unset" in content
    assert f"github-state:deleted ({TAG_A})" in content
    assert f"source:github ({TAG_B})" in content
    assert sent.count("/api/tags") == 1


@pytest.mark.asyncio
async def test_a_failed_tag_read_keeps_the_ids() -> None:
    sent: list[str] = []
    content = await _call("get_openaev_asset", {"asset_id": "a1"}, sent, tags_status=403)

    assert f"Tags: {TAG_A}, {TAG_B}" in content
    assert "github_state: deleted" in content


@pytest.mark.asyncio
async def test_no_tag_read_when_the_asset_has_no_tag() -> None:
    sent: list[str] = []
    content = await _call("get_openaev_asset", {"asset_id": "a2"}, sent)

    assert "bare" in content
    assert "/api/tags" not in sent
