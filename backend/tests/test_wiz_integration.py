"""Tests for the Wiz integration provider.

Covers tool introspection, compound-token parsing (the client secret is the
final greedy segment and may itself contain ``||``), API-endpoint
normalisation (tenant-specific, no default, ``/graphql`` suffix stripped),
call-time credential guards, GraphQL request shapes (issues list filters,
issue detail, posture summary, issue update/note mutations, vulnerability
finding filters, Security Graph search, cloud accounts, projects, security
frameworks), GraphQL error-envelope surfacing, and cursor pagination hints.
API calls route through the shared ``safe_request`` helper and the token mint
is monkeypatched, so no network is touched.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.integrations.providers import wiz
from app.integrations.providers.wiz import (
    build_tools,
    resolve_api_endpoint,
    validate_connection,
)
from app.tools.base import CallableToolExecutor, ToolCall

_TOKEN = "https://api.us1.app.wiz.io||client-id-123||s3cret-value"

_EXPECTED_TOOL_COUNT = 10


class _StubResponse:
    def __init__(self, payload: Any):
        self._payload = payload
        self.text = payload if isinstance(payload, str) else ""
        self.status_code = 200

    def json(self) -> Any:
        return self._payload


class _Recorder:
    """Fake ``safe_request`` that records calls and returns queued payloads."""

    def __init__(self, payloads: list[Any]):
        self._payloads = list(payloads)
        self.calls: list[dict[str, Any]] = []

    async def __call__(self, _client, method, url, _provider, action, **kwargs):
        self.calls.append({"method": method, "url": url, "action": action, **kwargs})
        payload = self._payloads.pop(0) if self._payloads else {"data": {}}
        return _StubResponse(payload), None


def _build(
    monkeypatch, payloads: list[Any], token: str = _TOKEN
) -> tuple[CallableToolExecutor, _Recorder]:
    recorder = _Recorder(payloads)
    monkeypatch.setattr(wiz, "safe_request", recorder)

    # The provider's _token closure is defined inside build_tools; patch the
    # underlying mint so the closure returns a token without a network call.
    async def _mint(*_a: Any):
        return "bearer-token", 3000

    monkeypatch.setattr(wiz, "_request_access_token", _mint)
    monkeypatch.setattr(wiz, "get_cached_app_token", lambda _k: None)
    monkeypatch.setattr(wiz, "set_cached_app_token", lambda *_a: None)
    executor = CallableToolExecutor()
    build_tools(token=token, executor=executor)
    return executor, recorder


# ---------------------------------------------------------------------------
# Introspection + token parsing
# ---------------------------------------------------------------------------


def test_build_tools_returns_expected_tools():
    executor = CallableToolExecutor()
    tools = build_tools(token=_TOKEN, executor=executor)
    names = {t.name for t in tools}
    assert len(tools) == _EXPECTED_TOOL_COUNT
    assert "list_wiz_issues" in names
    assert "get_wiz_issue" in names
    assert "get_wiz_posture_summary" in names
    assert "update_wiz_issue" in names
    assert "add_wiz_issue_note" in names
    assert "list_wiz_vulnerability_findings" in names
    assert "search_wiz_cloud_resources" in names
    assert "list_wiz_cloud_accounts" in names
    assert "list_wiz_projects" in names
    assert "list_wiz_security_frameworks" in names
    assert all(t.source == "integration:wiz" for t in tools)
    # Reads sort before writes.
    by_name = {t.name: t for t in tools}
    assert by_name["list_wiz_issues"].priority == 0
    assert by_name["get_wiz_posture_summary"].priority == 0
    assert by_name["search_wiz_cloud_resources"].priority == 0
    assert by_name["update_wiz_issue"].priority == 1
    assert by_name["add_wiz_issue_note"].priority == 1


def test_build_tools_always_builds_full_surface_even_without_creds():
    """Missing endpoint/client_id/secret still builds tools; calls guard at runtime."""
    executor = CallableToolExecutor()
    tools = build_tools(token="||||", executor=executor)
    assert len(tools) == _EXPECTED_TOOL_COUNT
    tools2 = build_tools(token="", executor=CallableToolExecutor())
    assert len(tools2) == _EXPECTED_TOOL_COUNT


@pytest.mark.asyncio
async def test_missing_credentials_surface_call_time_error(monkeypatch):
    minted = False

    async def _fail_mint(*_args: Any):
        nonlocal minted
        minted = True
        return None, 0

    monkeypatch.setattr(wiz, "_request_access_token", _fail_mint)
    executor = CallableToolExecutor()
    build_tools(token="https://api.us1.app.wiz.io||||", executor=executor)
    result = await executor.execute_tool(
        ToolCall(id="c1", name="list_wiz_issues", arguments={})
    )
    assert "credentials are not configured" in str(result.content)
    assert minted is False


@pytest.mark.asyncio
async def test_client_secret_may_contain_delimiter(monkeypatch):
    """The client secret is the final greedy segment - ``||`` must survive."""
    captured: dict[str, Any] = {}

    async def _capture(client_id, client_secret):
        captured.update(client_id=client_id, client_secret=client_secret)
        return None, 0

    monkeypatch.setattr(wiz, "_request_access_token", _capture)
    monkeypatch.setattr(wiz, "get_cached_app_token", lambda _k: None)
    executor = CallableToolExecutor()
    build_tools(
        token="https://api.eu1.app.wiz.io||cid||pa||ss||word",
        executor=executor,
    )
    result = await executor.execute_tool(
        ToolCall(id="c1", name="list_wiz_issues", arguments={})
    )
    assert captured["client_secret"] == "pa||ss||word"
    assert captured["client_id"] == "cid"
    assert "Failed to authenticate" in str(result.content)


def test_resolve_api_endpoint_normalises_and_has_no_default():
    # Tenant-specific: an empty value must NOT fall back to a default host.
    assert resolve_api_endpoint("") == ""
    assert resolve_api_endpoint("   ") == ""
    assert resolve_api_endpoint("https://api.us1.app.wiz.io") == (
        "https://api.us1.app.wiz.io"
    )
    assert resolve_api_endpoint("https://api.us1.app.wiz.io/") == (
        "https://api.us1.app.wiz.io"
    )
    # The /graphql path is appended at request time - strip it if supplied.
    assert resolve_api_endpoint("https://api.us1.app.wiz.io/graphql") == (
        "https://api.us1.app.wiz.io"
    )
    assert resolve_api_endpoint("http://api.eu1.app.wiz.io") == (
        "https://api.eu1.app.wiz.io"
    )
    assert resolve_api_endpoint("api.us2.app.wiz.io") == "https://api.us2.app.wiz.io"


# ---------------------------------------------------------------------------
# Issues
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_issues_builds_filters_and_paginates(monkeypatch):
    executor, recorder = _build(
        monkeypatch,
        [
            {
                "data": {
                    "issues": {
                        "nodes": [
                            {
                                "id": "issue-1",
                                "type": "TOXIC_COMBINATION",
                                "severity": "CRITICAL",
                                "status": "OPEN",
                                "createdAt": "2026-07-01T10:00:00Z",
                                "sourceRule": {"id": "r1", "name": "Public VM with admin"},
                                "entitySnapshot": {
                                    "name": "vm-prod-1",
                                    "type": "VIRTUAL_MACHINE",
                                    "cloudPlatform": "AWS",
                                    "subscriptionExternalId": "123456789012",
                                    "region": "us-east-1",
                                },
                                "projects": [{"id": "p1", "name": "Prod"}],
                            }
                        ],
                        "pageInfo": {"hasNextPage": True, "endCursor": "cursor-abc"},
                    }
                }
            }
        ],
    )
    result = await executor.execute_tool(
        ToolCall(
            id="c1",
            name="list_wiz_issues",
            arguments={
                "severity": "critical, high",
                "status": "open",
                "issue_type": "TOXIC_COMBINATION",
                "search": "public vm",
                "project_id": "p1",
                "related_entity_type": "VIRTUAL_MACHINE",
                "created_after": "2026-01-01T00:00:00Z",
                "limit": 500,
                "after": "prev-cursor",
            },
        )
    )
    call = recorder.calls[0]
    assert call["method"] == "post"
    assert call["url"] == "https://api.us1.app.wiz.io/graphql"
    assert call["headers"]["Authorization"] == "Bearer bearer-token"
    body = call["json"]
    assert "issuesV2" in body["query"]
    variables = body["variables"]
    assert variables["first"] == 100  # clamped
    assert variables["after"] == "prev-cursor"
    filter_by = variables["filterBy"]
    assert filter_by["severity"] == ["CRITICAL", "HIGH"]
    assert filter_by["status"] == ["OPEN"]
    assert filter_by["type"] == ["TOXIC_COMBINATION"]
    assert filter_by["search"] == "public vm"
    assert filter_by["project"] == ["p1"]
    assert filter_by["relatedEntity"] == {"type": ["VIRTUAL_MACHINE"]}
    assert filter_by["createdAt"] == {"after": "2026-01-01T00:00:00Z"}
    content = str(result.content)
    assert "Public VM with admin" in content
    assert "issue-1" in content
    assert 'pass after="cursor-abc"' in content


@pytest.mark.asyncio
async def test_list_issues_rejects_bad_severity(monkeypatch):
    executor, recorder = _build(monkeypatch, [])
    result = await executor.execute_tool(
        ToolCall(id="c1", name="list_wiz_issues", arguments={"severity": "EXTREME"})
    )
    assert "severity must be one of" in str(result.content)
    assert recorder.calls == []


@pytest.mark.asyncio
async def test_get_issue_formats_detail(monkeypatch):
    executor, recorder = _build(
        monkeypatch,
        [
            {
                "data": {
                    "issue": {
                        "id": "issue-1",
                        "type": "CLOUD_CONFIGURATION",
                        "severity": "HIGH",
                        "status": "OPEN",
                        "sourceRule": {
                            "id": "r1",
                            "name": "Bucket publicly readable",
                            "__typename": "CloudConfigurationRule",
                            "cloudConfigurationRuleDescription": "The bucket is public.",
                            "remediationInstructions": "Block public access.",
                        },
                        "entitySnapshot": {
                            "name": "my-bucket",
                            "type": "BUCKET",
                            "cloudPlatform": "AWS",
                            "subscriptionName": "prod",
                            "subscriptionExternalId": "123456789012",
                            "region": "eu-west-1",
                            "cloudProviderURL": "https://console.aws.example/my-bucket",
                            "tags": {"env": "prod"},
                        },
                        "notes": [
                            {
                                "text": "Investigating",
                                "createdAt": "2026-07-02T09:00:00Z",
                                "user": {"name": "Alice", "email": "a@ex.com"},
                                "serviceAccount": None,
                            }
                        ],
                        "serviceTickets": [
                            {"externalId": "JIRA-1", "name": "Fix bucket", "url": "https://jira/1"}
                        ],
                        "projects": [{"id": "p1", "name": "Prod", "slug": "prod"}],
                    }
                }
            }
        ],
    )
    result = await executor.execute_tool(
        ToolCall(id="c1", name="get_wiz_issue", arguments={"issue_id": "issue-1"})
    )
    assert recorder.calls[0]["json"]["variables"] == {"id": "issue-1"}
    content = str(result.content)
    assert "Bucket publicly readable" in content
    assert "Block public access." in content
    assert "my-bucket" in content
    assert "Alice" in content
    assert "JIRA-1" in content


@pytest.mark.asyncio
async def test_get_issue_not_found(monkeypatch):
    executor, _ = _build(monkeypatch, [{"data": {"issue": None}}])
    result = await executor.execute_tool(
        ToolCall(id="c1", name="get_wiz_issue", arguments={"issue_id": "nope"})
    )
    assert "not found" in str(result.content)


@pytest.mark.asyncio
async def test_posture_summary_reads_aliased_counts(monkeypatch):
    executor, recorder = _build(
        monkeypatch,
        [
            {
                "data": {
                    "critical": {"totalCount": 3},
                    "high": {"totalCount": 12},
                    "medium": {"totalCount": 40},
                    "low": {"totalCount": 7},
                    "inProgress": {"totalCount": 5},
                    "toxic": {"totalCount": 2},
                    "threats": {"totalCount": 1},
                    "kev": {"totalCount": 9},
                }
            }
        ],
    )
    result = await executor.execute_tool(
        ToolCall(id="c1", name="get_wiz_posture_summary", arguments={})
    )
    content = str(result.content)
    assert "Critical: 3" in content
    assert "High: 12" in content
    assert "Toxic combinations: 2" in content
    assert "CISA KEV" in content and "9" in content
    assert "issuesV2" in recorder.calls[0]["json"]["query"]


@pytest.mark.asyncio
async def test_non_json_tool_response_surfaces_error(monkeypatch):
    """A 2xx body that is not JSON (proxy/WAF HTML) must return a clear error."""

    class _HtmlResponse:
        status_code = 200
        text = "<html>blocked</html>"

        @staticmethod
        def json():
            raise ValueError("not JSON")

    async def _html_request(_client, _method, _url, _provider, _action, **_kw):
        return _HtmlResponse(), None

    monkeypatch.setattr(wiz, "safe_request", _html_request)

    async def _mint(*_a: Any):
        return "bearer-token", 3000

    monkeypatch.setattr(wiz, "_request_access_token", _mint)
    monkeypatch.setattr(wiz, "get_cached_app_token", lambda _k: None)
    monkeypatch.setattr(wiz, "set_cached_app_token", lambda *_a: None)
    executor = CallableToolExecutor()
    build_tools(token=_TOKEN, executor=executor)
    result = await executor.execute_tool(
        ToolCall(id="c1", name="list_wiz_issues", arguments={})
    )
    content = str(result.content)
    assert "non-JSON" in content
    assert "api.REGION.app.wiz.io" in content


@pytest.mark.asyncio
async def test_missing_data_envelope_surfaces_error(monkeypatch):
    """A JSON body without a GraphQL ``data`` envelope must return an error,
    not silently degrade into an empty result list."""
    executor, _ = _build(monkeypatch, [{"status": "ok"}])
    result = await executor.execute_tool(
        ToolCall(id="c1", name="list_wiz_issues", arguments={})
    )
    content = str(result.content)
    assert "no GraphQL data envelope" in content
    assert "api.REGION.app.wiz.io" in content


@pytest.mark.asyncio
async def test_graphql_errors_are_surfaced(monkeypatch):
    executor, _ = _build(
        monkeypatch,
        [
            {
                "errors": [
                    {"message": "unauthorized", "path": ["issues"]},
                ]
            }
        ],
    )
    result = await executor.execute_tool(
        ToolCall(id="c1", name="list_wiz_issues", arguments={})
    )
    content = str(result.content)
    assert "Error" in content
    assert "unauthorized" in content
    assert "(at issues)" in content


# ---------------------------------------------------------------------------
# Issue mutations
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_update_issue_builds_patch(monkeypatch):
    executor, recorder = _build(
        monkeypatch,
        [
            {
                "data": {
                    "updateIssue": {
                        "issue": {
                            "id": "issue-1",
                            "status": "IN_PROGRESS",
                            "dueAt": "2026-08-01T00:00:00Z",
                        }
                    }
                }
            }
        ],
    )
    result = await executor.execute_tool(
        ToolCall(
            id="c1",
            name="update_wiz_issue",
            arguments={
                "issue_id": "issue-1",
                "status": "in progress",
                "due_at": "2026-08-01T00:00:00Z",
            },
        )
    )
    variables = recorder.calls[0]["json"]["variables"]
    assert variables["issueId"] == "issue-1"
    assert variables["patch"] == {
        "status": "IN_PROGRESS",
        "dueAt": "2026-08-01T00:00:00Z",
    }
    assert "Updated issue issue-1" in str(result.content)


@pytest.mark.asyncio
async def test_update_issue_requires_resolution_reason(monkeypatch):
    executor, recorder = _build(monkeypatch, [])
    result = await executor.execute_tool(
        ToolCall(
            id="c1",
            name="update_wiz_issue",
            arguments={"issue_id": "issue-1", "status": "REJECTED"},
        )
    )
    assert "resolution_reason is required" in str(result.content)
    assert recorder.calls == []


@pytest.mark.asyncio
async def test_update_issue_rejected_uses_note_field(monkeypatch):
    executor, recorder = _build(
        monkeypatch, [{"data": {"updateIssue": {"issue": {"id": "issue-1"}}}}]
    )
    await executor.execute_tool(
        ToolCall(
            id="c1",
            name="update_wiz_issue",
            arguments={
                "issue_id": "issue-1",
                "status": "REJECTED",
                "resolution_reason": "WONT_FIX",
                "resolution_note": "Accepted risk",
            },
        )
    )
    patch = recorder.calls[0]["json"]["variables"]["patch"]
    assert patch["status"] == "REJECTED"
    assert patch["resolutionReason"] == "WONT_FIX"
    # REJECTED carries the note in ``note``; RESOLVED uses ``resolutionNote``.
    assert patch["note"] == "Accepted risk"
    assert "resolutionNote" not in patch


@pytest.mark.asyncio
async def test_update_issue_rejects_bad_reason(monkeypatch):
    executor, recorder = _build(monkeypatch, [])
    result = await executor.execute_tool(
        ToolCall(
            id="c1",
            name="update_wiz_issue",
            arguments={
                "issue_id": "issue-1",
                "status": "RESOLVED",
                "resolution_reason": "BECAUSE",
            },
        )
    )
    assert "resolution_reason must be one of" in str(result.content)
    assert recorder.calls == []


@pytest.mark.asyncio
async def test_update_issue_requires_a_field(monkeypatch):
    executor, recorder = _build(monkeypatch, [])
    result = await executor.execute_tool(
        ToolCall(id="c1", name="update_wiz_issue", arguments={"issue_id": "issue-1"})
    )
    assert "provide at least one field" in str(result.content)
    assert recorder.calls == []


@pytest.mark.asyncio
async def test_add_issue_note_posts_input(monkeypatch):
    executor, recorder = _build(
        monkeypatch,
        [
            {
                "data": {
                    "createIssueNote": {
                        "issueNote": {"id": "n1", "createdAt": "2026-07-10T12:00:00Z"}
                    }
                }
            }
        ],
    )
    result = await executor.execute_tool(
        ToolCall(
            id="c1",
            name="add_wiz_issue_note",
            arguments={"issue_id": "issue-1", "text": "Triaged by agent"},
        )
    )
    variables = recorder.calls[0]["json"]["variables"]
    assert variables["input"] == {"issueId": "issue-1", "text": "Triaged by agent"}
    assert "Note added to issue issue-1" in str(result.content)


@pytest.mark.asyncio
async def test_add_issue_note_requires_text(monkeypatch):
    executor, recorder = _build(monkeypatch, [])
    result = await executor.execute_tool(
        ToolCall(id="c1", name="add_wiz_issue_note", arguments={"issue_id": "issue-1"})
    )
    assert "both issue_id and text are required" in str(result.content)
    assert recorder.calls == []


# ---------------------------------------------------------------------------
# Vulnerability findings
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_vuln_findings_builds_filters(monkeypatch):
    executor, recorder = _build(
        monkeypatch,
        [
            {
                "data": {
                    "vulnerabilityFindings": {
                        "nodes": [
                            {
                                "id": "vf-1",
                                "name": "CVE-2021-44228",
                                "vendorSeverity": "CRITICAL",
                                "status": "OPEN",
                                "score": 10.0,
                                "hasExploit": True,
                                "hasCisaKevExploit": True,
                                "detailedName": "log4j-core",
                                "version": "2.14.0",
                                "fixedVersion": "2.17.0",
                                "vulnerableAsset": {
                                    "id": "asset-1",
                                    "name": "vm-1",
                                    "type": "VIRTUAL_MACHINE",
                                    "cloudPlatform": "AWS",
                                    "subscriptionExternalId": "123456789012",
                                    "region": "us-east-1",
                                },
                            }
                        ],
                        "pageInfo": {"hasNextPage": False, "endCursor": None},
                    }
                }
            }
        ],
    )
    result = await executor.execute_tool(
        ToolCall(
            id="c1",
            name="list_wiz_vulnerability_findings",
            arguments={
                "cve": "cve-2021-44228",
                "vendor_severity": "CRITICAL",
                "has_cisa_kev_exploit": True,
                "has_fix": True,
                "project_id": "p1",
            },
        )
    )
    filter_by = recorder.calls[0]["json"]["variables"]["filterBy"]
    assert filter_by["vulnerabilityExternalId"] == ["CVE-2021-44228"]
    assert filter_by["vendorSeverity"] == ["CRITICAL"]
    assert filter_by["hasCisaKevExploit"] is True
    assert filter_by["hasFix"] is True
    assert filter_by["projectId"] == ["p1"]
    # Tri-state booleans stay absent when not provided.
    assert "hasExploit" not in filter_by
    content = str(result.content)
    assert "CVE-2021-44228" in content
    assert "log4j-core" in content
    assert "fix: 2.17.0" in content
    assert "CISA KEV" in content


# ---------------------------------------------------------------------------
# Inventory
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_graph_search_builds_query_input(monkeypatch):
    executor, recorder = _build(
        monkeypatch,
        [
            {
                "data": {
                    "graphSearch": {
                        "totalCount": 1,
                        "maxCountReached": False,
                        "pageInfo": {"hasNextPage": False, "endCursor": None},
                        "nodes": [
                            {
                                "entities": [
                                    {
                                        "id": "e1",
                                        "name": "prod-db",
                                        "type": "DATABASE",
                                        "properties": {
                                            "cloudPlatform": "GCP",
                                            "subscriptionExternalId": "my-project",
                                            "region": "europe-west1",
                                        },
                                    }
                                ]
                            }
                        ],
                    }
                }
            }
        ],
    )
    result = await executor.execute_tool(
        ToolCall(
            id="c1",
            name="search_wiz_cloud_resources",
            arguments={
                "entity_type": "database",
                "cloud_platform": "GCP",
                "subscription_external_id": "my-project",
                "name_contains": "prod",
            },
        )
    )
    variables = recorder.calls[0]["json"]["variables"]
    assert variables["projectId"] == "*"
    query_input = variables["query"]
    assert query_input["type"] == ["DATABASE"]
    assert query_input["select"] is True
    assert query_input["where"]["cloudPlatform"] == {"EQUALS": ["GCP"]}
    assert query_input["where"]["subscriptionExternalId"] == {"EQUALS": ["my-project"]}
    assert query_input["where"]["name"] == {"CONTAINS": ["prod"]}
    content = str(result.content)
    assert "prod-db" in content
    assert "GCP" in content


@pytest.mark.asyncio
async def test_graph_search_requires_entity_type(monkeypatch):
    executor, recorder = _build(monkeypatch, [])
    result = await executor.execute_tool(
        ToolCall(id="c1", name="search_wiz_cloud_resources", arguments={"entity_type": ""})
    )
    assert "entity_type is required" in str(result.content)
    assert recorder.calls == []


@pytest.mark.asyncio
async def test_list_cloud_accounts_formats(monkeypatch):
    executor, recorder = _build(
        monkeypatch,
        [
            {
                "data": {
                    "cloudAccounts": {
                        "nodes": [
                            {
                                "id": "ca-1",
                                "name": "prod-aws",
                                "externalId": "123456789012",
                                "cloudProvider": "AWS",
                                "status": "CONNECTED",
                                "linkedProjects": [{"id": "p1", "name": "Prod"}],
                                "lastScannedAt": "2026-07-10T00:00:00Z",
                            }
                        ],
                        "pageInfo": {"hasNextPage": False, "endCursor": None},
                    }
                }
            }
        ],
    )
    result = await executor.execute_tool(
        ToolCall(
            id="c1",
            name="list_wiz_cloud_accounts",
            arguments={"cloud_provider": "AWS"},
        )
    )
    filter_by = recorder.calls[0]["json"]["variables"]["filterBy"]
    assert filter_by["cloudProvider"] == ["AWS"]
    content = str(result.content)
    assert "prod-aws" in content
    assert "123456789012" in content


# ---------------------------------------------------------------------------
# Governance
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_projects_formats_risk_profile(monkeypatch):
    executor, recorder = _build(
        monkeypatch,
        [
            {
                "data": {
                    "projects": {
                        "nodes": [
                            {
                                "id": "p1",
                                "name": "Prod",
                                "slug": "prod",
                                "isFolder": False,
                                "archived": False,
                                "businessUnit": "Payments",
                                "riskProfile": {"businessImpact": "HBI"},
                            }
                        ],
                        "pageInfo": {"hasNextPage": False, "endCursor": None},
                    }
                }
            }
        ],
    )
    result = await executor.execute_tool(
        ToolCall(id="c1", name="list_wiz_projects", arguments={"search": "prod"})
    )
    assert recorder.calls[0]["json"]["variables"]["filterBy"] == {"search": "prod"}
    content = str(result.content)
    assert "Prod" in content
    assert "Payments" in content
    assert "HBI" in content


@pytest.mark.asyncio
async def test_list_security_frameworks_counts_controls(monkeypatch):
    executor, recorder = _build(
        monkeypatch,
        [
            {
                "data": {
                    "securityFrameworks": {
                        "nodes": [
                            {
                                "id": "f1",
                                "name": "CIS AWS 1.5",
                                "builtin": True,
                                "enabled": True,
                                "categories": [
                                    {
                                        "id": "c1",
                                        "name": "IAM",
                                        "subCategories": [
                                            {"id": "s1", "title": "MFA"},
                                            {"id": "s2", "title": "Root usage"},
                                        ],
                                    }
                                ],
                            }
                        ],
                        "pageInfo": {"hasNextPage": False, "endCursor": None},
                    }
                }
            }
        ],
    )
    result = await executor.execute_tool(
        ToolCall(id="c1", name="list_wiz_security_frameworks", arguments={})
    )
    assert recorder.calls[0]["json"]["variables"]["filterBy"] == {"enabled": True}
    content = str(result.content)
    assert "CIS AWS 1.5" in content
    assert "(2 controls)" in content
    assert "built-in" in content


@pytest.mark.asyncio
async def test_list_security_frameworks_tolerates_null_categories(monkeypatch):
    """GraphQL list elements are nullable - a null category must not crash."""
    executor, _ = _build(
        monkeypatch,
        [
            {
                "data": {
                    "securityFrameworks": {
                        "nodes": [
                            {
                                "id": "f1",
                                "name": "Custom FW",
                                "builtin": False,
                                "enabled": True,
                                "categories": [
                                    None,
                                    {
                                        "id": "c1",
                                        "name": "IAM",
                                        "subCategories": [
                                            {"id": "s1", "title": "MFA"}
                                        ],
                                    },
                                ],
                            }
                        ],
                        "pageInfo": {"hasNextPage": False, "endCursor": None},
                    }
                }
            }
        ],
    )
    result = await executor.execute_tool(
        ToolCall(id="c1", name="list_wiz_security_frameworks", arguments={})
    )
    content = str(result.content)
    assert "Custom FW" in content
    assert "Categories: 1 (1 controls)" in content


# ---------------------------------------------------------------------------
# Token exchange + connectivity test (validate_connection)
# ---------------------------------------------------------------------------


def _fake_client(responses_by_url: dict[str, Any]):
    """Build an ``httpx.AsyncClient`` stand-in that routes POSTs by URL."""
    posts: list[dict[str, Any]] = []

    class _Client:
        def __init__(self, *args: Any, **kwargs: Any):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args: Any):
            return False

        async def post(self, url: str, **kwargs: Any):
            posts.append({"url": url, **kwargs})
            return responses_by_url[url]

    return _Client, posts


class _FakeResponse:
    def __init__(self, payload: Any, status_code: int = 200):
        self._payload = payload
        self.status_code = status_code
        self.is_success = 200 <= status_code < 300
        self.text = payload if isinstance(payload, str) else ""

    def json(self) -> Any:
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


@pytest.mark.asyncio
async def test_request_access_token_falls_back_to_auth0(monkeypatch):
    """A Cognito failure must fall through to the legacy Auth0 endpoint."""
    client_cls, posts = _fake_client(
        {
            "https://auth.app.wiz.io/oauth/token": _FakeResponse(
                {"error": "access_denied"}, status_code=401
            ),
            "https://auth.wiz.io/oauth/token": _FakeResponse(
                {"access_token": "auth0-token", "expires_in": 86400}
            ),
        }
    )
    monkeypatch.setattr(wiz.httpx, "AsyncClient", client_cls)
    token, ttl = await wiz._request_access_token("cid", "secret")
    assert token == "auth0-token"
    assert ttl == 86400
    assert [p["url"] for p in posts] == [
        "https://auth.app.wiz.io/oauth/token",
        "https://auth.wiz.io/oauth/token",
    ]
    # Each endpoint gets its own audience.
    assert posts[0]["data"]["audience"] == "wiz-api"
    assert posts[1]["data"]["audience"] == "beyond-api"
    assert posts[0]["data"]["grant_type"] == "client_credentials"


@pytest.mark.asyncio
async def test_request_access_token_missing_creds_short_circuits(monkeypatch):
    called = False

    class _Boom:
        def __init__(self, *a: Any, **k: Any):
            nonlocal called
            called = True

    monkeypatch.setattr(wiz.httpx, "AsyncClient", _Boom)
    assert await wiz._request_access_token("", "secret") == (None, 0)
    assert await wiz._request_access_token("cid", "") == (None, 0)
    assert called is False


@pytest.mark.asyncio
async def test_validate_connection_missing_fields():
    ok, message = await validate_connection("https://api.us1.app.wiz.io", "", "s")
    assert ok is False
    assert "client ID" in message
    ok, message = await validate_connection("", "cid", "secret")
    assert ok is False
    assert "API endpoint URL" in message


@pytest.mark.asyncio
async def test_validate_connection_token_failure(monkeypatch):
    async def _fail(*_a: Any):
        return None, 0

    monkeypatch.setattr(wiz, "_request_access_token", _fail)
    ok, message = await validate_connection(
        "https://api.us1.app.wiz.io", "cid", "bad"
    )
    assert ok is False
    assert "Authentication failed" in message


@pytest.mark.asyncio
async def test_validate_connection_probe_success(monkeypatch):
    async def _mint(*_a: Any):
        return "bearer-token", 3000

    monkeypatch.setattr(wiz, "_request_access_token", _mint)
    client_cls, posts = _fake_client(
        {
            "https://api.us1.app.wiz.io/graphql": _FakeResponse(
                {"data": {"__typename": "Query"}}
            )
        }
    )
    monkeypatch.setattr(wiz.httpx, "AsyncClient", client_cls)
    ok, message = await validate_connection(
        "https://api.us1.app.wiz.io/graphql", "cid", "secret"
    )
    assert ok is True
    assert "connected" in message.lower()
    assert posts[0]["headers"]["Authorization"] == "Bearer bearer-token"


@pytest.mark.asyncio
async def test_validate_connection_probe_http_error(monkeypatch):
    async def _mint(*_a: Any):
        return "bearer-token", 3000

    monkeypatch.setattr(wiz, "_request_access_token", _mint)
    client_cls, _ = _fake_client(
        {"https://api.us9.app.wiz.io/graphql": _FakeResponse({}, status_code=404)}
    )
    monkeypatch.setattr(wiz.httpx, "AsyncClient", client_cls)
    ok, message = await validate_connection(
        "https://api.us9.app.wiz.io", "cid", "secret"
    )
    assert ok is False
    assert "HTTP 404" in message


@pytest.mark.asyncio
async def test_validate_connection_probe_non_json_body(monkeypatch):
    """A 2xx HTML body (portal UI / proxy) must NOT report connected."""

    async def _mint(*_a: Any):
        return "bearer-token", 3000

    monkeypatch.setattr(wiz, "_request_access_token", _mint)
    client_cls, _ = _fake_client(
        {
            "https://api.us1.app.wiz.io/graphql": _FakeResponse(
                ValueError("not JSON")
            )
        }
    )
    monkeypatch.setattr(wiz.httpx, "AsyncClient", client_cls)
    ok, message = await validate_connection(
        "https://api.us1.app.wiz.io", "cid", "secret"
    )
    assert ok is False
    assert "non-JSON" in message


@pytest.mark.asyncio
async def test_validate_connection_probe_graphql_errors(monkeypatch):
    """A 200 with a GraphQL errors envelope must NOT report connected."""

    async def _mint(*_a: Any):
        return "bearer-token", 3000

    monkeypatch.setattr(wiz, "_request_access_token", _mint)
    client_cls, _ = _fake_client(
        {
            "https://api.us1.app.wiz.io/graphql": _FakeResponse(
                {"errors": [{"message": "unauthorized"}]}
            )
        }
    )
    monkeypatch.setattr(wiz.httpx, "AsyncClient", client_cls)
    ok, message = await validate_connection(
        "https://api.us1.app.wiz.io", "cid", "secret"
    )
    assert ok is False
    assert "unauthorized" in message


@pytest.mark.asyncio
async def test_validate_connection_probe_missing_data_envelope(monkeypatch):
    """A 200 JSON body without a GraphQL ``data`` object is not a success."""

    async def _mint(*_a: Any):
        return "bearer-token", 3000

    monkeypatch.setattr(wiz, "_request_access_token", _mint)
    client_cls, _ = _fake_client(
        {
            "https://api.us1.app.wiz.io/graphql": _FakeResponse(
                {"status": "ok"}
            )
        }
    )
    monkeypatch.setattr(wiz.httpx, "AsyncClient", client_cls)
    ok, message = await validate_connection(
        "https://api.us1.app.wiz.io", "cid", "secret"
    )
    assert ok is False
    assert "GraphQL" in message
