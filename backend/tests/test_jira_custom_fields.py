"""Custom-field support for the Jira integration.

Covers the gap reported in production: the integration could not surface custom
fields at all. These tests verify the three layers of the fix:
  1. ``format_issue`` renders populated custom fields (with their human labels).
  2. ``list_jira_fields`` resolves a field NAME to its ``customfield_<n>`` id.
  3. ``get_jira_issue`` renders a custom field value with its label.
  4. ``create_jira_issue`` / ``update_jira_issue`` send ``custom_fields`` in the
     ``fields`` payload.
"""

from unittest.mock import AsyncMock, patch

import httpx
import pytest

from app.integrations.providers.jira import build_tools
from app.integrations.providers.jira._common import format_field_value, format_issue
from app.services.integration_tools import _SafeIntegrationExecutor
from app.tools.base import ToolCall

_ISSUES = "app.integrations.providers.jira.issues.jira_request"
_FIELDS = "app.integrations.providers.jira.fields.jira_request"
_ATTACH = "app.integrations.providers.jira.attachments.jira_request"


def test_format_issue_renders_custom_fields_with_labels():
    issue = {
        "key": "ENG-68",
        "names": {"customfield_11226": "Github url"},
        "fields": {
            "summary": "Dynamic users resolution",
            "status": {"name": "In Development"},
            "issuetype": {"name": "Story"},
            "priority": {"name": "High"},
            "assignee": {"displayName": "Romain"},
            "reporter": {"displayName": "JP"},
            "customfield_11226": "https://github.com/OpenCTI-Platform/opencti/pull/16618",
        },
    }
    out = format_issue(issue, detail=True)
    assert "Github url: https://github.com/OpenCTI-Platform/opencti/pull/16618" in out


def test_format_issue_renders_custom_field_object_value():
    issue = {
        "key": "ENG-1",
        "names": {"customfield_10050": "Team"},
        "fields": {
            "summary": "x",
            "customfield_10050": {"value": "Platform"},
        },
    }
    out = format_issue(issue, detail=True)
    assert "Team: Platform" in out


def test_format_issue_falls_back_to_field_id_without_names():
    issue = {
        "key": "ENG-2",
        "fields": {"summary": "x", "customfield_99999": "value-here"},
    }
    out = format_issue(issue, detail=True)
    assert "customfield_99999: value-here" in out


@pytest.mark.asyncio
async def test_list_jira_fields_resolves_name_to_custom_id():
    executor = _SafeIntegrationExecutor()
    build_tools("cloud-id||token", executor)

    with patch(_FIELDS, new_callable=AsyncMock) as mock_request:
        mock_request.return_value = (
            httpx.Response(
                200,
                json={
                    "total": 1,
                    "values": [
                        {
                            "id": "customfield_11226",
                            "name": "Github url",
                            "custom": True,
                            "schema": {"type": "string"},
                        }
                    ],
                },
            ),
            None,
        )
        result = await executor.execute_tool(
            ToolCall(
                id="f1",
                name="list_jira_fields",
                arguments={"query": "github", "field_type": "custom"},
            )
        )

    assert not result.is_error
    assert "customfield_11226" in result.content
    assert "Github url" in result.content
    # The name filter is forwarded to /field/search.
    assert mock_request.call_args.kwargs["params"]["query"] == "github"
    assert mock_request.call_args.kwargs["params"]["type"] == "custom"


@pytest.mark.asyncio
async def test_get_jira_issue_renders_custom_field_with_label():
    executor = _SafeIntegrationExecutor()
    build_tools("cloud-id||token", executor)

    with patch(_ISSUES, new_callable=AsyncMock) as mock_request:
        mock_request.side_effect = [
            (
                httpx.Response(
                    200,
                    json={
                        "key": "ENG-68",
                        "names": {"customfield_11226": "Github url"},
                        "fields": {
                            "summary": "Dynamic users resolution",
                            "customfield_11226": "https://github.com/x/y/pull/16618",
                        },
                    },
                ),
                None,
            ),
            (httpx.Response(200, json={"comments": []}), None),
        ]
        result = await executor.execute_tool(
            ToolCall(id="i1", name="get_jira_issue", arguments={"issue_key": "ENG-68"})
        )

    assert not result.is_error
    assert "Github url: https://github.com/x/y/pull/16618" in result.content
    # The first call requests expand=names so the labels resolve.
    first_call = mock_request.call_args_list[0]
    assert "names" in first_call.kwargs["params"]["expand"]


@pytest.mark.asyncio
async def test_create_jira_issue_sends_custom_fields():
    executor = _SafeIntegrationExecutor()
    build_tools("cloud-id||token", executor)

    with patch(_ISSUES, new_callable=AsyncMock) as mock_request:
        mock_request.return_value = (
            httpx.Response(200, json={"key": "ENG-100", "id": "100"}),
            None,
        )
        result = await executor.execute_tool(
            ToolCall(
                id="c1",
                name="create_jira_issue",
                arguments={
                    "project_key": "ENG",
                    "summary": "New",
                    "custom_fields": {"customfield_11226": "https://github.com/x/y"},
                },
            )
        )

    assert not result.is_error
    assert "ENG-100" in result.content
    sent_fields = mock_request.call_args.kwargs["json"]["fields"]
    assert sent_fields["customfield_11226"] == "https://github.com/x/y"


@pytest.mark.asyncio
async def test_update_jira_issue_sends_custom_fields():
    executor = _SafeIntegrationExecutor()
    build_tools("cloud-id||token", executor)

    with patch(_ISSUES, new_callable=AsyncMock) as mock_request:
        mock_request.return_value = (httpx.Response(204, json=None), None)
        result = await executor.execute_tool(
            ToolCall(
                id="u1",
                name="update_jira_issue",
                arguments={
                    "issue_key": "ENG-68",
                    "custom_fields": {"customfield_11226": "https://github.com/x/z"},
                },
            )
        )

    assert not result.is_error
    sent_fields = mock_request.call_args.kwargs["json"]["fields"]
    assert sent_fields["customfield_11226"] == "https://github.com/x/z"


# ---------------------------------------------------------------------------
# format_field_value must never silently drop an object-shaped value: an object with only an ``id``, or an unknown shape, still renders.
# ---------------------------------------------------------------------------


def test_format_field_value_renders_id_only_object():
    assert format_field_value({"id": "42", "self": "https://x/42"}) == "42"


def test_format_field_value_renders_unknown_shape_compactly():
    # No known label key -> compact str fallback, never an empty string.
    rendered = format_field_value({"foo": "bar", "baz": 1})
    assert rendered != ""
    assert "foo" in rendered


def test_format_issue_renders_id_only_custom_field():
    issue = {
        "key": "ENG-5",
        "names": {"customfield_10010": "Sprint"},
        "fields": {"summary": "x", "customfield_10010": {"id": "42", "self": "https://x"}},
    }
    out = format_issue(issue, detail=True)
    assert "Sprint: 42" in out


# ---------------------------------------------------------------------------
# search_jira_issues `fields` argument coercion: a comma string
# is split into a list; an invalid type errors out instead of a Jira 400.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_search_fields_accepts_comma_separated_string():
    executor = _SafeIntegrationExecutor()
    build_tools("cloud-id||token", executor)

    with patch(_ISSUES, new_callable=AsyncMock) as mock_request:
        mock_request.return_value = (httpx.Response(200, json={"issues": []}), None)
        result = await executor.execute_tool(
            ToolCall(
                id="s1",
                name="search_jira_issues",
                arguments={
                    "jql": "project = ENG",
                    "fields": "summary, status ,customfield_11226",
                },
            )
        )

    assert not result.is_error
    assert mock_request.call_args.kwargs["json"]["fields"] == [
        "summary",
        "status",
        "customfield_11226",
    ]


@pytest.mark.asyncio
async def test_search_fields_rejects_invalid_type():
    executor = _SafeIntegrationExecutor()
    build_tools("cloud-id||token", executor)

    with patch(_ISSUES, new_callable=AsyncMock) as mock_request:
        result = await executor.execute_tool(
            ToolCall(
                id="s2",
                name="search_jira_issues",
                arguments={"jql": "project = ENG", "fields": {"bad": 1}},
            )
        )

    assert result.is_error
    assert "fields" in result.content.lower()
    mock_request.assert_not_called()


# ---------------------------------------------------------------------------
# download_jira_attachment inline (no-persistence) fallback must include the
# [[FILE:...]] marker instruction too.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_download_attachment_inline_fallback_includes_marker_instruction():
    executor = _SafeIntegrationExecutor()
    # No persist_file_fn -> the inline FileRef.from_bytes fallback is used.
    build_tools("cloud-id||token", executor)

    with patch(_ATTACH, new_callable=AsyncMock) as mock_request:
        mock_request.side_effect = [
            (
                httpx.Response(
                    200,
                    json={"filename": "report.txt", "mimeType": "text/plain", "size": 5},
                ),
                None,
            ),
            (httpx.Response(200, content=b"hello"), None),
        ]
        result = await executor.execute_tool(
            ToolCall(
                id="d1",
                name="download_jira_attachment",
                arguments={"attachment_id": "10001"},
            )
        )

    assert not result.is_error
    assert "[[FILE:" in result.content
    assert "verbatim" in result.content
