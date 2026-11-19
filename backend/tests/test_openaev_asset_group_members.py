# SPDX-License-Identifier: Apache-2.0
"""get_openaev_asset_group names the dynamic filter and the members (xtm-one #301).

Gap openaev-asset-group-tool-members: the read returned the group's name and id
only, so an agent could not check which servers a dynamic group held before
scanning them. The read now gives the filter as one line, the member id counts
the group carries, and one page of members (static and dynamic) from OpenAEV's
member search, falling back to the ids when that search is refused.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.integrations.providers.openaev import build_tools
from app.integrations.providers.openaev._common import (
    _fmt_asset_group,
    asset_group_member_ids_lines,
    describe_filter_group,
    fmt_asset_group_members,
)
from app.tools.base import CallableToolExecutor, ToolCall

TOKEN = "https://demo.openaev.io||test-token"
GROUP_ID = "de799619-00cb-409c-8f29-8013e50fdb08"
FILTER = {
    "mode": "and",
    "filters": [
        {"key": "asset_name", "operator": "contains", "values": ["ovh", "srv"]},
        {
            "key": "nested",
            "filterGroup": {
                "mode": "or",
                "filters": [
                    {"key": "endpoint_platform", "operator": "eq", "values": ["Linux"]},
                    {"key": "asset_tags", "operator": "not_empty", "values": []},
                ],
            },
        },
    ],
}
GROUP = {
    "asset_group_id": GROUP_ID,
    "asset_group_name": "OVHCloud Servers",
    "asset_group_dynamic_filter": FILTER,
    "asset_group_assets": ["a1"],
    "asset_group_dynamic_assets": ["a2", "a3"],
    "asset_group_tags": [],
}
PAGE = {
    "content": [
        {
            "asset_id": "a2",
            "asset_name": "srv-1",
            "asset_type": "Endpoint",
            "asset_hostname": "srv-1.ovh",
            "endpoint_platform": "Linux",
            "is_static": False,
        },
        {"asset_id": "a1", "asset_name": "srv-2", "asset_type": "Endpoint", "is_static": True},
    ],
    "totalElements": 3,
}


def test_filter_group_reads_as_one_line_with_nested_groups() -> None:
    assert describe_filter_group(FILTER) == (
        "asset_name contains [ovh, srv] AND (endpoint_platform eq [Linux] OR asset_tags not_empty)"
    )
    assert describe_filter_group({"mode": "and", "filters": []}) == ""
    assert describe_filter_group(None) == ""


def test_group_read_names_filter_and_member_id_counts() -> None:
    out = _fmt_asset_group(GROUP)
    assert "Dynamic filter: asset_name contains [ovh, srv] AND (" in out
    assert "Member ids on the group: 1 static, 2 dynamic" in out


def test_static_group_says_it_has_no_filter() -> None:
    out = _fmt_asset_group({**GROUP, "asset_group_dynamic_filter": {"mode": "and", "filters": []}})
    assert "Dynamic filter: none (static members only)" in out


def test_search_rows_without_the_fields_keep_their_short_form() -> None:
    out = _fmt_asset_group({"asset_group_id": "g1", "asset_group_name": "G", "asset_group_assets_number": 4})
    assert "Dynamic filter" not in out
    assert "Member ids" not in out
    assert "Assets count: 4" in out


def test_member_page_lists_rows_and_points_at_the_next_page() -> None:
    lines = fmt_asset_group_members(PAGE, 0, 2)
    assert lines[0] == "  Members: 3 in all (static and dynamic), showing 1-2:"
    assert "    - srv-1 (id: a2, type: Endpoint, hostname: srv-1.ovh, platform: Linux) [dynamic]" in lines
    assert "    - srv-2 (id: a1, type: Endpoint) [static]" in lines
    assert lines[-1] == "    ... 1 more: call again with members_page=1."


def test_empty_and_past_the_end_pages_say_so() -> None:
    assert fmt_asset_group_members({"content": [], "totalElements": 0}, 0, 20) == [
        "  Members: none (static and dynamic)."
    ]
    past = fmt_asset_group_members({"content": [], "totalElements": 3}, 5, 20, "srv")
    assert past == ["  Members matching 'srv': 3 in all; page 5 is past the last one."]


def test_refused_member_search_falls_back_to_the_ids() -> None:
    lines = asset_group_member_ids_lines(GROUP, "404 Not Found")
    assert lines[0] == "  Members: not listed by name (404 Not Found)."
    assert "    Static member ids: a1" in lines
    assert "    Dynamic member ids: a2, a3" in lines


class _Resp:
    def __init__(self, data: Any) -> None:
        self._data = data

    def json(self) -> Any:
        return self._data


async def _run(arguments: dict[str, Any]) -> str:
    executor = CallableToolExecutor()
    build_tools(TOKEN, executor)
    result = await executor.execute_tool(ToolCall(id="c1", name="get_openaev_asset_group", arguments=arguments))
    assert not result.is_error, result.content
    return result.content


def _fake(calls: list[dict[str, Any]], members: tuple[Any, Any]) -> Any:
    async def fake_safe_request(client: Any, method: str, url: str, provider: str, action: str, **kw: Any) -> Any:
        calls.append({"method": method, "url": url, "json": kw.get("json")})
        if url.endswith("/assets/search"):
            return members
        return _Resp(GROUP), None

    return fake_safe_request


async def test_tool_reads_the_group_then_one_member_page(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.integrations.providers.openaev import core

    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(core, "scoped_safe_request", _fake(calls, (_Resp(PAGE), None)))
    out = await _run({"group_id": GROUP_ID, "members_limit": 2, "member_search": " srv "})
    assert calls[1]["url"].endswith(f"/api/asset_groups/{GROUP_ID}/assets/search")
    assert calls[1]["json"] == {"page": 0, "size": 2, "textSearch": "srv"}
    assert "[AssetGroup] OVHCloud Servers" in out
    assert "Members matching 'srv': 3 in all" in out


async def test_tool_caps_the_page_and_skips_members_on_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.integrations.providers.openaev import core

    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(core, "scoped_safe_request", _fake(calls, (_Resp(PAGE), None)))
    await _run({"group_id": GROUP_ID, "members_limit": 500, "members_page": -3})
    assert calls[1]["json"] == {"page": 0, "size": 100}
    calls.clear()
    out = await _run({"group_id": GROUP_ID, "members_limit": 0})
    assert len(calls) == 1
    assert "Members" not in out


async def test_tool_falls_back_when_the_member_search_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.integrations.providers.openaev import core

    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(core, "scoped_safe_request", _fake(calls, (None, "OpenAEV error 404")))
    out = await _run({"group_id": GROUP_ID})
    assert "Members: not listed by name (OpenAEV error 404)." in out
    assert "Dynamic member ids: a2, a3" in out
