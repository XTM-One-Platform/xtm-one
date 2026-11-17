"""Jira comment bodies are read in full up to the body cap.

Comments used to fall back to ``truncate()``'s 500-character custom-field cap
while the issue description got 4,000, so every long comment came back cut
mid-sentence with a bare ``...`` that reads like the end of the text.
"""

from typing import Any

import pytest

from app.integrations.providers.jira import _common as jira_common
from app.integrations.providers.jira import build_tools
from app.services.integration_tools import _SafeIntegrationExecutor
from app.tools.base import ToolCall

LONG = "word " * 300  # 1,500 characters: over the old 500 cap, under the body cap
HUGE = "x" * 6_000  # over the 4,000-character body cap


class _Resp:
    def __init__(self, payload: Any):
        self._payload = payload
        self.status_code = 200

    def json(self) -> Any:
        return self._payload


def _route(monkeypatch, routes: dict[str, Any]) -> list[str]:
    """Answer each Jira call with the payload of the first matching URL suffix."""
    seen: list[str] = []

    async def _fake(client, method, url, provider, action, **kwargs):
        seen.append(url)
        for suffix, payload in routes.items():
            if url.endswith(suffix):
                return _Resp(payload), None
        return None, f"Error: unexpected call {url}"

    monkeypatch.setattr(jira_common, "safe_request", _fake)
    return seen


async def _call(name: str, **arguments: Any) -> str:
    executor = _SafeIntegrationExecutor()
    build_tools("cloud-123||token", executor)
    result = await executor.execute_tool(ToolCall(id="t", name=name, arguments=arguments))
    return result.content


def _comment(cid: str, text: str) -> dict:
    return {
        "id": cid,
        "author": {"displayName": "Analyst"},
        "created": "2026-09-01",
        "body": text,
    }


@pytest.mark.asyncio
async def test_list_comments_returns_a_long_comment_whole(monkeypatch):
    _route(monkeypatch, {"/comment": {"comments": [_comment("10", LONG)], "total": 1}})
    content = await _call("list_jira_comments", issue_key="PROJ-1")
    assert LONG.strip() in content
    assert "truncated" not in content


@pytest.mark.asyncio
async def test_get_issue_returns_a_long_comment_whole_with_its_id(monkeypatch):
    _route(
        monkeypatch,
        {
            "/issue/PROJ-1": {"key": "PROJ-1", "fields": {"summary": "S"}},
            "/comment": {"comments": [_comment("11", LONG)], "total": 1},
        },
    )
    content = await _call("get_jira_issue", issue_key="PROJ-1")
    assert LONG.strip() in content
    assert "[id: 11]" in content


@pytest.mark.asyncio
async def test_service_request_comment_and_description_use_the_body_cap(monkeypatch):
    _route(
        monkeypatch,
        {
            "/request/SD-1": {
                "issueKey": "SD-1",
                "requestFieldValues": [
                    {"fieldId": "description", "label": "Description", "value": LONG},
                    {"fieldId": "summary", "label": "Summary", "value": "y" * 600},
                ],
                "comment": {
                    "values": [
                        {
                            **_comment("12", LONG),
                            # The Service Desk API dates are objects.
                            "created": {"friendly": "Today 10:00"},
                            "public": True,
                        }
                    ]
                },
            }
        },
    )
    content = await _call("get_jira_service_request", issue_key="SD-1")
    assert content.count(LONG.strip()) == 2  # description + comment, both whole
    assert "[id: 12]" in content
    # A short field keeps its field cap.
    assert "y" * 500 + "..." in content
    assert "y" * 501 not in content


@pytest.mark.asyncio
async def test_an_oversized_comment_says_it_was_cut_and_how_to_read_it(monkeypatch):
    _route(monkeypatch, {"/comment": {"comments": [_comment("13", HUGE)], "total": 1}})
    content = await _call("list_jira_comments", issue_key="PROJ-1")
    assert "x" * 4_000 in content
    assert "x" * 4_001 not in content
    assert "[truncated: 4,000 of 6,000 characters" in content
    assert "get_jira_comment(issue_key='PROJ-1', comment_id='13')" in content


@pytest.mark.asyncio
async def test_get_jira_comment_reads_one_comment_in_full(monkeypatch):
    seen = _route(monkeypatch, {"/issue/PROJ-1/comment/13": _comment("13", HUGE)})
    content = await _call("get_jira_comment", issue_key="PROJ-1", comment_id="13")
    assert seen[-1].endswith("/rest/api/3/issue/PROJ-1/comment/13")
    assert HUGE in content
    assert "truncated" not in content


@pytest.mark.asyncio
async def test_get_jira_comment_requires_both_ids(monkeypatch):
    seen = _route(monkeypatch, {})
    content = await _call("get_jira_comment", issue_key="PROJ-1", comment_id="")
    assert content.startswith("Error:")
    assert seen == []


def test_truncate_body_leaves_short_text_alone():
    assert jira_common.truncate_body("abc", 10) == "abc"
    assert jira_common.truncate_body("", 10) == ""
