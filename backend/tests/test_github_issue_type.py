"""The GitHub issue tools set the issue Type field in the same call.

``create_github_issue`` and ``update_github_issue`` take ``issue_type`` (a name,
case-insensitive), resolve it against the repository's issue types BEFORE any
write, and send the exact name in the REST ``type`` field. An unknown name, or a
repository without issue types, is refused with nothing written.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import httpx
import pytest

from app.integrations.providers.github import build_tools

_TYPES = {
    "data": {
        "repository": {
            "issueTypes": {
                "nodes": [
                    {"name": "Task", "isEnabled": True},
                    {"name": "Bug", "isEnabled": True},
                    {"name": "Feature", "isEnabled": True},
                    {"name": "Epic", "isEnabled": False},
                ]
            }
        }
    }
}


def _resp(payload, status: int = 200) -> httpx.Response:
    return httpx.Response(status, json=payload)


def _tool(name: str):
    ex = MagicMock()
    ex.register = MagicMock()
    build_tools("fake-token", ex)
    for call in ex.register.call_args_list:
        if call[0][0] == name:
            return call[0][1]
    raise KeyError(name)


def _recorder(types_payload, write_payload):
    calls: list[dict] = []

    async def fake_request(client, method, url, provider, action, **kwargs):
        if url.endswith("/contents/.xtm-one/checks.toml"):
            # No title rule declared: the title check reads nothing and is not counted.
            return None, "Error: GitHub API error (404): Not Found"
        calls.append({"method": method, "url": url, "json": kwargs.get("json")})
        if url.endswith("/graphql"):
            return _resp(types_payload), None
        return _resp(write_payload), None

    return calls, fake_request


@pytest.mark.asyncio
async def test_create_issue_sets_type_in_one_write():
    calls, fake = _recorder(
        _TYPES,
        {"number": 12, "html_url": "https://github.com/octo/repo/issues/12", "type": {"name": "Feature"}},
    )
    fn = _tool("create_github_issue")
    with patch("app.integrations.providers.github._common.safe_request", side_effect=fake):
        result = await fn(repo="octo/repo", title="feat(x): y", issue_type="feature")
    assert result == "Issue #12 created [type Feature]: https://github.com/octo/repo/issues/12"
    assert [c["method"] for c in calls] == ["post", "post"]
    assert calls[0]["url"].endswith("/graphql")
    assert calls[0]["json"]["variables"] == {"o": "octo", "n": "repo"}
    assert calls[1]["url"].endswith("/repos/octo/repo/issues")
    assert calls[1]["json"]["type"] == "Feature"


@pytest.mark.asyncio
async def test_create_issue_without_type_makes_no_lookup():
    calls, fake = _recorder(_TYPES, {"number": 3, "html_url": "https://github.com/octo/repo/issues/3"})
    fn = _tool("create_github_issue")
    with patch("app.integrations.providers.github._common.safe_request", side_effect=fake):
        result = await fn(repo="octo/repo", title="chore(x): y")
    assert result == "Issue #3 created: https://github.com/octo/repo/issues/3"
    assert len(calls) == 1
    assert "type" not in calls[0]["json"]


@pytest.mark.asyncio
async def test_create_issue_unknown_type_writes_nothing():
    calls, fake = _recorder(_TYPES, {"number": 1, "html_url": "x"})
    fn = _tool("create_github_issue")
    with patch("app.integrations.providers.github._common.safe_request", side_effect=fake):
        result = await fn(repo="octo/repo", title="feat(x): y", issue_type="Epic")
    # A disabled type is not offered; the answer names the enabled ones.
    assert "unknown issue type 'Epic'" in result
    assert "Task, Bug, Feature" in result
    assert "Nothing was written" in result
    assert len(calls) == 1 and calls[0]["url"].endswith("/graphql")


@pytest.mark.asyncio
async def test_repository_without_issue_types_is_refused():
    no_types = {"data": {"repository": {"issueTypes": None}}}
    calls, fake = _recorder(no_types, {"number": 1, "html_url": "x"})
    fn = _tool("create_github_issue")
    with patch("app.integrations.providers.github._common.safe_request", side_effect=fake):
        result = await fn(repo="someone/repo", title="fix(x): y", issue_type="Bug")
    assert "has no issue types" in result
    assert "Retry without issue_type" in result
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_graphql_error_is_reported_before_any_write():
    errors = {"errors": [{"message": "Could not resolve to a Repository"}]}
    calls, fake = _recorder(errors, {"number": 1, "html_url": "x"})
    fn = _tool("update_github_issue")
    with patch("app.integrations.providers.github._common.safe_request", side_effect=fake):
        result = await fn(repo="octo/repo", number=4, issue_type="Bug")
    assert "could not read the issue types of octo/repo" in result
    assert "Could not resolve to a Repository" in result
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_graphql_error_with_partial_data_still_refuses():
    partial = {**_TYPES, "errors": [{"message": "Something went wrong"}]}
    calls, fake = _recorder(partial, {"number": 1, "html_url": "x"})
    fn = _tool("create_github_issue")
    with patch("app.integrations.providers.github._common.safe_request", side_effect=fake):
        result = await fn(repo="octo/repo", title="feat(x): y", issue_type="Feature")
    assert "could not read the issue types of octo/repo" in result
    assert "Something went wrong" in result
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_update_issue_type_alone():
    calls, fake = _recorder(
        _TYPES,
        {"number": 9, "state": "open", "html_url": "https://github.com/octo/repo/issues/9", "type": {"name": "Task"}},
    )
    fn = _tool("update_github_issue")
    with patch("app.integrations.providers.github._common.safe_request", side_effect=fake):
        result = await fn(repo="octo/repo", number=9, issue_type="TASK")
    assert result == "Issue #9 updated [open] [type Task]: https://github.com/octo/repo/issues/9"
    assert calls[1]["method"] == "patch"
    assert calls[1]["json"] == {"type": "Task"}


@pytest.mark.asyncio
async def test_update_issue_type_with_other_fields():
    calls, fake = _recorder(
        _TYPES,
        {"number": 9, "state": "closed", "html_url": "https://github.com/octo/repo/issues/9", "type": {"name": "Bug"}},
    )
    fn = _tool("update_github_issue")
    with patch("app.integrations.providers.github._common.safe_request", side_effect=fake):
        await fn(repo="octo/repo", number=9, state="closed", assignees=["octocat"], issue_type="bug")
    assert calls[1]["json"] == {"state": "closed", "assignees": ["octocat"], "type": "Bug"}


@pytest.mark.asyncio
async def test_update_without_fields_still_says_nothing_to_update():
    calls, fake = _recorder(_TYPES, {})
    fn = _tool("update_github_issue")
    with patch("app.integrations.providers.github._common.safe_request", side_effect=fake):
        result = await fn(repo="octo/repo", number=9, issue_type="  ")
    assert result == "No fields to update."
    assert calls == []


def test_issue_type_is_in_both_schemas():
    ex = MagicMock()
    defs = {d.name: d for d in build_tools("fake-token", ex)}
    for name in ("create_github_issue", "update_github_issue"):
        props = defs[name].input_schema["properties"]
        assert props["issue_type"]["type"] == "string"
        assert "issue type" in defs[name].description
