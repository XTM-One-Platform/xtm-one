"""Tests for the GitHub integration package (multi-file split + new domains).

Covers: the build_tools surface (backward-compatible names + new tools), repo
path-injection validation, the Projects v2 GraphQL tools (the product-backlog
gap), sub-issue id resolution, Actions job introspection, security alert
dismissal payloads, and the raw GraphQL escape hatch.

All tools route HTTP through ``_common.safe_request`` and GraphQL through
``_common.gh_graphql``, so patching those two on ``_common`` covers everything.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from app.integrations.providers.github import build_tools
from app.integrations.providers.github._common import _PLACEHOLDER_LOGINS


def _mock_response(payload, status: int = 200, headers: dict[str, str] | None = None) -> httpx.Response:
    return httpx.Response(status, json=payload, headers=headers)


def _get_fn(executor: MagicMock, name: str):
    build_tools("fake-token", executor)
    for call in executor.register.call_args_list:
        if call[0][0] == name:
            return call[0][1]
    raise KeyError(f"Tool {name!r} not registered")


def _executor() -> MagicMock:
    ex = MagicMock()
    ex.register = MagicMock()
    return ex


# ---------------------------------------------------------------------------
# Surface
# ---------------------------------------------------------------------------


def test_build_tools_surface_is_complete_and_unique():
    ex = _executor()
    defs = build_tools("fake-token", ex)
    names = [d.name for d in defs]
    registered = [c[0][0] for c in ex.register.call_args_list]
    # Every definition is registered, and every registration has a definition.
    assert set(names) == set(registered)
    # No duplicate tool names.
    assert len(names) == len(set(names))
    # Backward-compatible: a few original names must still be present.
    for original in (
        "list_github_repos",
        "get_github_file",
        "create_github_issue",
        "get_github_pr_diff",
        "github_graphql_query",
    ):
        assert original in names
    # New first-class Projects v2 + gap-filling tools are present.
    for new_tool in (
        "list_github_projects",
        "list_github_project_items",
        "set_github_project_field",
        "list_github_sub_issues",
        "manage_github_sub_issue",
        "get_github_job_logs",
        "list_github_secret_scanning_alerts",
        "list_github_repository_security_advisories",
        "get_github_repository_security_advisory",
        "manage_github_release",
        # Org/enterprise membership & role governance tools.
        "get_github_org_membership",
        "get_github_enterprise",
        "list_github_enterprise_organizations",
        "list_github_enterprise_members",
        "list_github_enterprise_owners",
        # Rulesets (modern branch governance / change-control).
        "get_github_branch_rules",
        "list_github_rulesets",
        "get_github_ruleset",
    ):
        assert new_tool in names


def test_repository_security_advisory_tool_schema_matches_github_contract():
    definitions = {definition.name: definition for definition in build_tools("fake-token", _executor())}
    properties = definitions["list_github_repository_security_advisories"].input_schema["properties"]
    assert properties["state"]["enum"] == ["triage", "draft", "published", "closed"]
    assert properties["sort"]["enum"] == ["created", "updated", "published"]
    assert properties["direction"]["enum"] == ["asc", "desc"]
    assert "after" in properties


def test_github_catalog_distinguishes_repository_and_global_advisories():
    from app.api.v1.integration_catalog import PROVIDER_CATALOG

    github = PROVIDER_CATALOG["github"]
    events = {event["event"]: event for event in github["available_events"]}
    assert events["repository_advisory"]["label"] == "Repository security advisory"
    assert "published or privately reported" in events["repository_advisory"]["description"]
    assert events["security_advisory"]["label"] == "Global security advisory"
    permissions = github["setup_guide"]["permissions"]
    assert any(permission["scope"] == "Repository — Repository security advisories: Read" for permission in permissions)


# ---------------------------------------------------------------------------
# Repository validation (path-injection safety)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_repo_validation_rejects_bad_repo():
    fn = _get_fn(_executor(), "get_github_repository")
    # No HTTP call should be made for an invalid repo - patch to prove it.
    with patch("app.integrations.providers.github._common.safe_request", new_callable=AsyncMock) as req:
        for bad in ("not-a-repo", "owner/name/extra", "../../etc", "owner /name"):
            result = await fn(repo=bad)
            assert "invalid repository" in result.lower() or "owner/name" in result.lower()
        req.assert_not_called()


# ---------------------------------------------------------------------------
# Placeholder-username guard (add_collaborator / remove_collaborator)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_manage_repository_rejects_placeholder_username():
    """manage_github_repository must reject known placeholder usernames without
    making an HTTP call, so the agent can self-correct instead of hitting the
    GitHub API with a bogus name."""
    fn = _get_fn(_executor(), "manage_github_repository")
    # Iterate the real deny-list (plus case variants) so the test can never
    # drift from the implementation when new placeholder logins are added.
    placeholder_usernames = [
        *sorted(_PLACEHOLDER_LOGINS),
        "Placeholder",  # case-insensitive
        "PLACEHOLDER",
    ]
    with patch("app.integrations.providers.github._common.safe_request", new_callable=AsyncMock) as req:
        for bad_username in placeholder_usernames:
            for action in ("add_collaborator", "remove_collaborator"):
                result = await fn(repo="owner/repo", action=action, username=bad_username)
                assert "placeholder" in result.lower(), (
                    f"Expected placeholder error for username={bad_username!r} action={action}, got: {result!r}"
                )
        req.assert_not_called()


@pytest.mark.asyncio
async def test_manage_repository_rejects_template_token_username():
    """manage_github_repository must also reject template-style tokens like
    {{username}}, <username>, or [username] that were not substituted."""
    fn = _get_fn(_executor(), "manage_github_repository")
    template_tokens = [
        "{{username}}",
        "{{collaborator}}",
        "<username>",
        "<collaborator>",
        "[username]",
        "[collaborator]",
        "&lt;username&gt;",  # HTML-escaped angle-bracket form
        "&amp;lt;username&amp;gt;",  # doubly HTML-escaped form
        "&amp;amp;lt;username&amp;amp;gt;",  # triply HTML-escaped form (unescape-loop bound)
        "&#60;username&#62;",  # numeric-entity-escaped form
    ]
    with patch("app.integrations.providers.github._common.safe_request", new_callable=AsyncMock) as req:
        for token in template_tokens:
            result = await fn(repo="owner/repo", action="add_collaborator", username=token)
            assert "placeholder" in result.lower() or "unsubstituted" in result.lower(), (
                f"Expected placeholder/unsubstituted error for username={token!r}, got: {result!r}"
            )
        req.assert_not_called()


@pytest.mark.asyncio
async def test_manage_repository_placeholder_hint_is_field_aware():
    """Placeholder rejections on non-username fields (e.g. new_owner) must not
    point the agent at the PR trigger payload's 'user.login' field - that hint
    only applies to person-login fields."""
    fn = _get_fn(_executor(), "manage_github_repository")
    with patch("app.integrations.providers.github._common.safe_request", new_callable=AsyncMock) as req:
        result = await fn(repo="owner/repo", action="transfer", new_owner="placeholder")
        assert "placeholder" in result.lower()
        assert "'new_owner'" in result
        assert "user.login" not in result
        # The username field keeps the PR-author-specific guidance.
        result = await fn(repo="owner/repo", action="add_collaborator", username="placeholder")
        assert "user.login" in result
        req.assert_not_called()


@pytest.mark.asyncio
async def test_manage_repository_accepts_real_username():
    """manage_github_repository must NOT block real GitHub usernames."""
    fn = _get_fn(_executor(), "manage_github_repository")
    with patch("app.integrations.providers.github._common.safe_request", new_callable=AsyncMock) as req:
        req.return_value = (_mock_response({}), None)
        result = await fn(repo="owner/repo", action="add_collaborator", username="octocat")
    # Should reach the API (not rejected by the placeholder guard).
    req.assert_called_once()
    assert "placeholder" not in result.lower()


# ---------------------------------------------------------------------------
# Projects v2 (GraphQL)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_github_project_items_formats_fields():
    fn = _get_fn(_executor(), "list_github_project_items")
    items_payload = {
        "node": {
            "items": {
                "pageInfo": {"hasNextPage": False, "endCursor": None},
                "nodes": [
                    {
                        "id": "PVTI_item1",
                        "isArchived": False,
                        "fieldValues": {
                            "nodes": [
                                {"name": "In Progress", "field": {"name": "Status"}},
                                {"name": "P1", "field": {"name": "Priority"}},
                            ]
                        },
                        "content": {
                            "number": 42,
                            "title": "Fix the thing",
                            "state": "OPEN",
                            "repository": {"nameWithOwner": "octo/repo"},
                        },
                    }
                ],
            }
        }
    }
    with patch("app.integrations.providers.github._common.gh_graphql", new_callable=AsyncMock) as gql:
        gql.return_value = (items_payload, None)
        result = await fn(project_id="PVT_123")

    assert "[octo/repo#42] Fix the thing (OPEN)" in result
    assert "Status: In Progress" in result
    assert "Priority: P1" in result
    assert "item_id: PVTI_item1" in result


@pytest.mark.asyncio
async def test_list_github_projects_falls_back_from_org_to_user():
    """A combined org+user query errors on the wrong owner type; the tool must
    tolerate the org NOT_FOUND error and fall back to the user-owned projects."""
    fn = _get_fn(_executor(), "list_github_projects")

    async def fake_gql(client, headers, query, variables=None, *, action="", extra_headers=None):
        if "organization(login" in query:
            return (None, "GitHub GraphQL error: Could not resolve to an Organization with the login of 'someuser'.")
        return (
            {
                "user": {
                    "projectsV2": {
                        "nodes": [
                            {
                                "id": "PVT_user1",
                                "number": 3,
                                "title": "Roadmap",
                                "url": "https://github.com/users/someuser/projects/3",
                                "closed": False,
                                "shortDescription": "",
                                "items": {"totalCount": 5},
                            }
                        ]
                    }
                }
            },
            None,
        )

    with patch("app.integrations.providers.github._common.gh_graphql", side_effect=fake_gql):
        result = await fn(login="someuser")

    assert "Roadmap" in result
    assert "#3" in result
    assert "5 items" in result


@pytest.mark.asyncio
async def test_list_github_projects_login_queries_are_brace_balanced():
    """Regression: the login-path Projects v2 query had one extra '}', so GitHub
    rejected it with 'Expected one of SCHEMA, SCALAR, TYPE, ... actual: RCURLY'.
    Capture the org AND user query variants and assert each is brace-balanced."""
    fn = _get_fn(_executor(), "list_github_projects")
    captured: list[str] = []

    async def fake_gql(client, headers, query, variables=None, *, action="", extra_headers=None):
        captured.append(query)
        if "organization(login" in query:
            # Force the org attempt to "miss" so the user variant is built too.
            return (None, "GitHub GraphQL error: Could not resolve to an Organization with the login of 'x'.")
        return ({"user": {"projectsV2": {"nodes": []}}}, None)

    with patch("app.integrations.providers.github._common.gh_graphql", side_effect=fake_gql):
        await fn(login="some-login")

    assert captured, "no GraphQL query was sent"
    for q in captured:
        assert q.count("{") == q.count("}"), f"unbalanced braces in query: {q!r}"
        assert q.lstrip().startswith("query")
    # Both owner-type variants must have been exercised (org first, then user).
    assert any("organization(login" in q for q in captured)
    assert any("user(login" in q for q in captured)


@pytest.mark.asyncio
async def test_set_github_project_field_resolves_single_select_option():
    fn = _get_fn(_executor(), "set_github_project_field")
    captured: dict = {}

    async def fake_gql(client, headers, query, variables=None, *, action="", extra_headers=None):
        if action == "fetch project fields":
            return (
                {
                    "node": {
                        "fields": {
                            "nodes": [
                                {
                                    "id": "PVTSSF_status",
                                    "name": "Status",
                                    "dataType": "SINGLE_SELECT",
                                    "options": [
                                        {"id": "opt_todo", "name": "Todo"},
                                        {"id": "opt_ip", "name": "In Progress"},
                                    ],
                                }
                            ]
                        }
                    }
                },
                None,
            )
        captured["vars"] = variables
        return ({"updateProjectV2ItemFieldValue": {"projectV2Item": {"id": "PVTI_x"}}}, None)

    with patch("app.integrations.providers.github._common.gh_graphql", side_effect=fake_gql):
        # Case-insensitive option match -> resolved to the option id.
        result = await fn(item_id="PVTI_x", field_name="Status", value="in progress", project_id="PVT_1")

    assert "Set 'Status'" in result
    assert captured["vars"]["v"] == {"singleSelectOptionId": "opt_ip"}
    assert captured["vars"]["f"] == "PVTSSF_status"


@pytest.mark.asyncio
async def test_set_github_project_field_rejects_unknown_option():
    fn = _get_fn(_executor(), "set_github_project_field")

    async def fake_gql(client, headers, query, variables=None, *, action="", extra_headers=None):
        return (
            {
                "node": {
                    "fields": {
                        "nodes": [
                            {
                                "id": "PVTSSF_status",
                                "name": "Status",
                                "dataType": "SINGLE_SELECT",
                                "options": [{"id": "opt_todo", "name": "Todo"}],
                            }
                        ]
                    }
                }
            },
            None,
        )

    with patch("app.integrations.providers.github._common.gh_graphql", side_effect=fake_gql):
        result = await fn(item_id="PVTI_x", field_name="Status", value="Nonexistent", project_id="PVT_1")

    assert "not a valid option" in result
    assert "Todo" in result


# ---------------------------------------------------------------------------
# Sub-issues (REST) - issue NUMBER is resolved to the internal id
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_manage_github_sub_issue_add_resolves_internal_id():
    fn = _get_fn(_executor(), "manage_github_sub_issue")
    calls: list = []

    async def fake_request(client, method, url, provider, action, **kwargs):
        calls.append((method, url, kwargs.get("json")))
        if method == "get":
            # Resolving the sub-issue number -> internal id.
            return (_mock_response({"id": 99999, "number": 7}), None)
        return (_mock_response({"id": 1}), None)

    with patch("app.integrations.providers.github._common.safe_request", side_effect=fake_request):
        result = await fn(repo="octo/repo", parent_number=1, action="add", sub_issue_number=7)

    # The POST must target the plural .../sub_issues and carry the internal id.
    post = next(c for c in calls if c[0] == "post")
    assert post[1].endswith("/issues/1/sub_issues")
    assert post[2] == {"sub_issue_id": 99999}
    assert "sub-issue" in result.lower()


@pytest.mark.asyncio
async def test_manage_github_sub_issue_remove_uses_singular_endpoint():
    fn = _get_fn(_executor(), "manage_github_sub_issue")
    calls: list = []

    async def fake_request(client, method, url, provider, action, **kwargs):
        calls.append((method, url, kwargs.get("json")))
        if method == "get":
            return (_mock_response({"id": 222, "number": 9}), None)
        return (_mock_response({}), None)

    with patch("app.integrations.providers.github._common.safe_request", side_effect=fake_request):
        await fn(repo="octo/repo", parent_number=3, action="remove", sub_issue_number=9)

    delete = next(c for c in calls if c[0] == "delete")
    # GitHub's remove endpoint is the SINGULAR /sub_issue.
    assert delete[1].endswith("/issues/3/sub_issue")
    assert delete[2] == {"sub_issue_id": 222}


# ---------------------------------------------------------------------------
# Actions - job introspection surfaces the failed step
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_github_workflow_jobs_surfaces_failed_step():
    fn = _get_fn(_executor(), "list_github_workflow_jobs")
    payload = {
        "jobs": [
            {
                "id": 555,
                "name": "build",
                "conclusion": "failure",
                "status": "completed",
                "steps": [
                    {"name": "checkout", "conclusion": "success"},
                    {"name": "pytest", "conclusion": "failure"},
                ],
            }
        ]
    }
    with patch("app.integrations.providers.github._common.safe_request", new_callable=AsyncMock) as req:
        req.return_value = (_mock_response(payload), None)
        result = await fn(repo="octo/repo", run_id=123)

    assert "build" in result
    assert "job_id 555" in result
    assert "failed step(s): pytest" in result


# Recorded shapes of GitHub's jobs endpoints for a run whose attempt 1 failed
# and whose automatic attempt 2 is still running (trimmed to the fields read).
_ATTEMPT_1_JOBS = {
    "total_count": 2,
    "jobs": [
        {
            "id": 901,
            "run_attempt": 1,
            "name": "API Tests (elk8-7)",
            "status": "completed",
            "conclusion": "failure",
            "steps": [{"name": "Run tests", "conclusion": "failure"}],
        },
        {"id": 902, "run_attempt": 1, "name": "Lint", "status": "completed", "conclusion": "success", "steps": []},
    ],
}
_ATTEMPT_2_JOBS = {
    "total_count": 2,
    "jobs": [
        {
            "id": 911,
            "run_attempt": 2,
            "name": "API Tests (elk8-7)",
            "status": "in_progress",
            "conclusion": None,
            "steps": [],
        },
        {"id": 912, "run_attempt": 2, "name": "Lint", "status": "completed", "conclusion": "success", "steps": []},
    ],
}


def _jobs_router(urls: list[str]):
    async def fake_request(client, method, url, provider, action, **kwargs):
        urls.append(url)
        if url.endswith("/attempts/1/jobs"):
            return _mock_response(_ATTEMPT_1_JOBS), None
        if url.endswith("/runs/77/jobs") or url.endswith("/attempts/2/jobs"):
            return _mock_response(_ATTEMPT_2_JOBS), None
        return None, f"Error: unexpected {url}"

    return fake_request


@pytest.mark.asyncio
async def test_list_github_workflow_jobs_reads_the_attempt_asked():
    fn = _get_fn(_executor(), "list_github_workflow_jobs")
    urls: list[str] = []
    with patch("app.integrations.providers.github._common.safe_request", side_effect=_jobs_router(urls)):
        result = await fn(repo="octo/repo", run_id=77, attempt=1)

    assert urls == ["https://api.github.com/repos/octo/repo/actions/runs/77/attempts/1/jobs"]
    assert "Jobs for run 77 attempt 1 in octo/repo (2):" in result
    assert "API Tests (elk8-7) [failure] (job_id 901)" in result
    assert "failed step(s): Run tests" in result
    assert "still running" not in result


@pytest.mark.asyncio
async def test_list_github_workflow_jobs_names_the_failed_previous_attempt_while_the_retry_runs():
    fn = _get_fn(_executor(), "list_github_workflow_jobs")
    urls: list[str] = []
    with patch("app.integrations.providers.github._common.safe_request", side_effect=_jobs_router(urls)):
        result = await fn(repo="octo/repo", run_id=77)

    assert urls[0].endswith("/actions/runs/77/jobs")
    assert urls[1].endswith("/actions/runs/77/attempts/1/jobs")
    assert "Jobs for run 77 attempt 2 (latest) in octo/repo (2):" in result
    assert "API Tests (elk8-7) [in_progress] (job_id 911)" in result
    assert "Attempt 1 (the latest is still running) ended with: API Tests (elk8-7) [failure] (job_id 901)." in result
    assert "Lint [success] (job_id 902)" not in result
    assert "attempt=1" in result


@pytest.mark.asyncio
async def test_list_github_workflow_jobs_first_attempt_makes_one_call():
    fn = _get_fn(_executor(), "list_github_workflow_jobs")
    urls: list[str] = []
    with patch("app.integrations.providers.github._common.safe_request", side_effect=_jobs_router(urls)):
        result = await fn(repo="octo/repo", run_id=77, attempt=2)

    assert len(urls) == 1
    assert "Jobs for run 77 attempt 2 in octo/repo (2):" in result
    assert "Attempt 1" not in result


@pytest.mark.asyncio
async def test_list_github_workflow_jobs_previous_attempt_error_keeps_the_listing():
    fn = _get_fn(_executor(), "list_github_workflow_jobs")

    async def fake_request(client, method, url, provider, action, **kwargs):
        if url.endswith("/attempts/1/jobs"):
            return None, "Error: GitHub 503"
        return _mock_response(_ATTEMPT_2_JOBS), None

    with patch("app.integrations.providers.github._common.safe_request", side_effect=fake_request):
        result = await fn(repo="octo/repo", run_id=77)

    assert not result.startswith("Error")
    assert "attempt 2 (latest)" in result
    assert "Attempt 1" not in result


def _bad_gateway(url: str) -> httpx.Response:
    return httpx.Response(502, json={"message": "Server Error"}, request=httpx.Request("GET", url))


_RUN_77 = {"id": 77, "run_attempt": 2, "check_suite_node_id": "CS_kwDO77"}


@pytest.mark.asyncio
async def test_list_github_workflow_jobs_falls_back_to_the_attempt_endpoint_on_a_502():
    fn = _get_fn(_executor(), "list_github_workflow_jobs")
    urls: list[str] = []

    async def fake_request(client, method, url, provider, action, **kwargs):
        urls.append(url)
        if url.endswith("/runs/77/jobs"):
            return _bad_gateway(url), "Error: GitHub server error (502) while trying to list jobs."
        if url.endswith("/runs/77"):
            return _mock_response(_RUN_77), None
        if url.endswith("/attempts/2/jobs"):
            return _mock_response(_ATTEMPT_1_JOBS), None
        return None, f"Error: unexpected {url}"

    with patch("app.integrations.providers.github._common.safe_request", side_effect=fake_request):
        result = await fn(repo="octo/repo", run_id=77)

    called = [u.rsplit("/actions/", 1)[1] for u in urls]
    assert called == ["runs/77/jobs", "runs/77/jobs", "runs/77", "runs/77/attempts/2/jobs"]
    assert not result.startswith("Error")
    assert "Jobs for run 77 attempt 2 in octo/repo (2):" in result
    assert "Source: /attempts/2/jobs (the run's current attempt)" in result
    assert "API Tests (elk8-7) [failure] (job_id 901)" in result


@pytest.mark.asyncio
async def test_list_github_workflow_jobs_falls_back_to_the_previous_attempt_then_the_check_suite():
    fn = _get_fn(_executor(), "list_github_workflow_jobs")
    urls: list[str] = []
    gql: list[dict] = []

    async def fake_request(client, method, url, provider, action, **kwargs):
        urls.append(url)
        if url.endswith("/runs/77"):
            return _mock_response(_RUN_77), None
        if url.endswith("/graphql"):
            gql.append(kwargs["json"])
            nodes = [{"databaseId": 901, "name": "API Tests (elk8-7)", "status": "COMPLETED", "conclusion": "FAILURE"}]
            return _mock_response({"data": {"node": {"checkRuns": {"nodes": nodes}}}}), None
        return _bad_gateway(url), "Error: GitHub server error (502)."

    with patch("app.integrations.providers.github._common.safe_request", side_effect=fake_request):
        result = await fn(repo="octo/repo", run_id=77)

    assert any(u.endswith("/attempts/2/jobs") for u in urls)
    assert any(u.endswith("/attempts/1/jobs") for u in urls)
    assert gql[0]["variables"] == {"id": "CS_kwDO77"}
    assert "checkType: ALL" in gql[0]["query"]
    assert "Source: the check suite's failed check runs" in result
    assert "API Tests (elk8-7) [failure] (job_id 901)" in result


@pytest.mark.asyncio
async def test_list_github_workflow_jobs_reports_when_every_fallback_fails():
    fn = _get_fn(_executor(), "list_github_workflow_jobs")

    async def fake_request(client, method, url, provider, action, **kwargs):
        return _bad_gateway(url), "Error: GitHub server error (502)."

    with patch("app.integrations.providers.github._common.safe_request", side_effect=fake_request):
        result = await fn(repo="octo/repo", run_id=77)

    assert result.startswith("Error: GitHub server error (502).")
    assert "Retried once" in result
    assert "attempt=N" in result


# ---------------------------------------------------------------------------
# Repository security advisories - read-only list/detail with validation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_github_repository_security_advisories_uses_documented_filters_and_clamps_count():
    fn = _get_fn(_executor(), "list_github_repository_security_advisories")
    captured: dict = {}

    async def fake_request(client, method, url, provider, action, **kwargs):
        captured["method"] = method
        captured["url"] = url
        captured["params"] = kwargs.get("params") or {}
        return (
            _mock_response(
                [
                    {
                        "ghsa_id": "GHSA-1234-ABCD-EF12",
                        "summary": "Critical issue",
                        "severity": "high",
                        "state": "published",
                        "cve_id": "CVE-2025-1234",
                        "html_url": "https://github.com/advisories/GHSA-1234-ABCD-EF12",
                        "published_at": "2025-01-02T00:00:00Z",
                        "updated_at": "2025-01-03T00:00:00Z",
                        "vulnerabilities": [
                            {
                                "package": {"ecosystem": "pip", "name": "demo"},
                                "vulnerable_version_range": ">=1.0.0 <2.0.0",
                            }
                        ],
                    }
                ],
                headers={
                    "Link": '<https://api.github.com/repositories/1/security-advisories?after=cursor-2>; rel="next"'
                },
            ),
            None,
        )

    with patch("app.integrations.providers.github._common.safe_request", side_effect=fake_request):
        result = await fn(
            repo="octo/repo",
            state="closed",
            sort="updated",
            direction="asc",
            after="next-cursor",
            count=0,
        )

    assert captured["method"] == "get"
    assert "/repos/octo/repo/security-advisories" in captured["url"]
    assert captured["params"] == {
        "state": "closed",
        "sort": "updated",
        "direction": "asc",
        "after": "next-cursor",
        "per_page": 1,
    }
    assert "GHSA-1234-ABCD-EF12" in result
    assert "Critical issue" in result
    assert "demo / pip: >=1.0.0 <2.0.0" in result
    assert "https://github.com/advisories/GHSA-1234-ABCD-EF12" in result
    assert "after=cursor-2" in result


_ADVISORY = {"ghsa_id": "GHSA-1111-2222-3333", "summary": "s", "severity": "low", "vulnerabilities": []}
_NOTICE = "the pagination cursor could not be safely rendered"


async def _advisories_with_link(link: str | None) -> str:
    fn = _get_fn(_executor(), "list_github_repository_security_advisories")

    async def fake_request(client, method, url, provider, action, **kwargs):
        headers = {"Link": link} if link is not None else {}
        return _mock_response([_ADVISORY], headers=headers), None

    with patch("app.integrations.providers.github._common.safe_request", side_effect=fake_request):
        return await fn(repo="octo/repo", state="published")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "link",
    [
        # Fails the allowlist only AFTER parse_qs decodes %2B / %2F.
        '<https://api.github.com/repositories/1/security-advisories?after=Y3Vy%2Bc29y%2Fdg%3D%3D>; rel="next"',
        # A raw '+' decodes to a space.
        '<https://api.github.com/repositories/1/security-advisories?after=abc+def>; rel="next"',
        # Newline / over-long values are never interpolated.
        '<https://api.github.com/repositories/1/security-advisories?after=a%0Ab>; rel="next"',
        f'<https://api.github.com/repositories/1/security-advisories?after={"a" * 250}>; rel="next"',
        # A next link without any cursor.
        '<https://api.github.com/repositories/1/security-advisories?page=2>; rel="next"',
    ],
)
async def test_advisories_say_so_when_a_next_page_cursor_cannot_be_rendered(link):
    result = await _advisories_with_link(link)
    assert _NOTICE in result
    assert "call this tool again with after=" not in result
    assert "Y3Vy" not in result and "abc" not in result and "a" * 200 not in result


@pytest.mark.asyncio
async def test_advisories_last_page_has_no_pagination_text():
    for link in (None, '<https://api.github.com/repositories/1/security-advisories?after=x>; rel="prev"'):
        result = await _advisories_with_link(link)
        assert _NOTICE not in result
        assert "More results are available" not in result


@pytest.mark.asyncio
async def test_list_github_repository_security_advisories_handles_empty_or_null_payloads_gracefully():
    fn = _get_fn(_executor(), "list_github_repository_security_advisories")

    async def fake_request_empty(client, method, url, provider, action, **kwargs):
        return (_mock_response([]), None)

    with patch("app.integrations.providers.github._common.safe_request", side_effect=fake_request_empty):
        result = await fn(repo="octo/repo", state="draft")
        assert "No draft repository security advisories in octo/repo." in result

    async def fake_request_nullish(client, method, url, provider, action, **kwargs):
        return (
            _mock_response(
                [
                    {
                        "ghsa_id": None,
                        "summary": None,
                        "severity": None,
                        "state": None,
                        "cve_id": None,
                        "html_url": None,
                        "published_at": None,
                        "updated_at": None,
                        "vulnerabilities": [],
                    }
                ]
            ),
            None,
        )

    with patch("app.integrations.providers.github._common.safe_request", side_effect=fake_request_nullish):
        result = await fn(repo="octo/repo", state="published")
        assert "no CVE" in result
        assert "none listed" in result
        assert "unknown" in result


@pytest.mark.asyncio
async def test_list_github_repository_security_advisories_queries_every_state_by_default():
    fn = _get_fn(_executor(), "list_github_repository_security_advisories")
    requested: list[str] = []
    pages = {
        "triage": [{"ghsa_id": "GHSA-tttt-tttt-tttt", "state": "triage", "updated_at": "2025-03-01T00:00:00Z"}],
        "draft": [],
        "published": [
            {"ghsa_id": "GHSA-pppp-pppp-pppp", "state": "published", "updated_at": "2025-01-01T00:00:00Z"},
            {"ghsa_id": "GHSA-qqqq-qqqq-qqqq", "state": "published", "updated_at": "2025-04-01T00:00:00Z"},
        ],
        "closed": [{"ghsa_id": "GHSA-TTTT-TTTT-TTTT", "state": "closed", "updated_at": "2025-02-01T00:00:00Z"}],
    }

    async def fake_request(client, method, url, provider, action, **kwargs):
        state = kwargs["params"]["state"]
        requested.append(state)
        return (_mock_response(pages[state]), None)

    with patch("app.integrations.providers.github._common.safe_request", side_effect=fake_request):
        result = await fn(repo="octo/repo", count=2)

    assert sorted(requested) == sorted(["triage", "draft", "published", "closed"])
    assert "2 across every state" in result
    # Newest first across states; the closed duplicate of the triage advisory is dropped.
    assert result.index("GHSA-qqqq-qqqq-qqqq") < result.index("GHSA-tttt-tttt-tttt")
    assert "GHSA-pppp-pppp-pppp" not in result
    assert "GHSA-TTTT-TTTT-TTTT" not in result
    assert "More published advisories exist; call this tool again with state=published" in result
    assert "Only published advisories were returned" not in result


@pytest.mark.asyncio
async def test_list_github_repository_security_advisories_carries_each_state_cursor_by_default():
    fn = _get_fn(_executor(), "list_github_repository_security_advisories")
    next_link = '<https://api.github.com/repositories/1/security-advisories?after={cursor}>; rel="next"'
    pages = {
        "triage": ([{"ghsa_id": "GHSA-tttt-tttt-tttt", "state": "triage"}], "triage-2"),
        "draft": ([{"ghsa_id": "GHSA-dddd-dddd-dddd", "state": "draft"}], "bad cursor!"),
        "published": ([], None),
        "closed": ([], None),
    }

    async def fake_request(client, method, url, provider, action, **kwargs):
        payload, cursor = pages[kwargs["params"]["state"]]
        headers = {"Link": next_link.format(cursor=cursor)} if cursor else {}
        return (_mock_response(payload, headers=headers), None)

    with patch("app.integrations.providers.github._common.safe_request", side_effect=fake_request):
        result = await fn(repo="octo/repo")

    assert "state=triage, after=triage-2 and the same sort, direction and count" in result
    assert "More draft advisories exist, but the pagination cursor could not be safely rendered" in result
    assert "state=published" not in result


@pytest.mark.asyncio
async def test_list_github_repository_security_advisories_explains_when_only_published_come_back():
    fn = _get_fn(_executor(), "list_github_repository_security_advisories")

    async def fake_request(client, method, url, provider, action, **kwargs):
        if kwargs["params"]["state"] == "published":
            return (_mock_response([{"ghsa_id": "GHSA-aaaa-bbbb-cccc", "state": "published"}]), None)
        if kwargs["params"]["state"] == "draft":
            return (None, "GitHub API error (403): Resource not accessible by integration")
        return (_mock_response([]), None)

    with patch("app.integrations.providers.github._common.safe_request", side_effect=fake_request):
        result = await fn(repo="octo/repo")

    assert "GHSA-aaaa-bbbb-cccc" in result
    assert "Could not list draft advisories" in result
    assert "Only published advisories were returned" in result
    assert "Repository security advisories: Read" in result
    assert "security manager" in result


@pytest.mark.asyncio
async def test_list_github_repository_security_advisories_requires_state_with_a_cursor():
    fn = _get_fn(_executor(), "list_github_repository_security_advisories")
    with patch("app.integrations.providers.github._common.safe_request", new_callable=AsyncMock) as request:
        result = await fn(repo="octo/repo", after="cursor-2")
    assert "pass the 'state'" in result
    request.assert_not_called()


@pytest.mark.asyncio
async def test_list_github_repository_security_advisories_rejects_invalid_filters_without_request():
    fn = _get_fn(_executor(), "list_github_repository_security_advisories")
    with patch("app.integrations.providers.github._common.safe_request", new_callable=AsyncMock) as request:
        assert "invalid state" in (await fn(repo="octo/repo", state="withdrawn")).lower()
        assert "invalid sort" in (await fn(repo="octo/repo", sort="severity")).lower()
        assert "invalid direction" in (await fn(repo="octo/repo", direction="sideways")).lower()
        request.assert_not_called()


@pytest.mark.asyncio
async def test_get_github_repository_security_advisory_rejects_invalid_ghsa_id_without_request():
    fn = _get_fn(_executor(), "get_github_repository_security_advisory")
    with patch("app.integrations.providers.github._common.safe_request", new_callable=AsyncMock) as req:
        for invalid in ("ghsa-1234", "ABC", "GHSA-1234-ABCD", "{{ghsa_id}}"):
            result = await fn(repo="octo/repo", ghsa_id=invalid)
            assert "ghsa" in result.lower() or "invalid" in result.lower()
        req.assert_not_called()


@pytest.mark.asyncio
async def test_get_github_repository_security_advisory_uses_detail_endpoint_and_bounds_output():
    fn = _get_fn(_executor(), "get_github_repository_security_advisory")
    captured: dict = {}

    async def fake_request(client, method, url, provider, action, **kwargs):
        captured["url"] = url
        return (
            _mock_response(
                {
                    "ghsa_id": "GHSA-ABCD-EFGH-IJKL",
                    "summary": "Long summary",
                    "description": "A" * 9000,
                    "severity": "critical",
                    "state": "published",
                    "cve_id": "CVE-2025-4321",
                    "html_url": "https://github.com/advisories/GHSA-ABCD-EFGH-IJKL",
                    "identifiers": [
                        {"type": "GHSA", "value": "GHSA-ABCD-EFGH-IJKL"},
                        {"type": "CVE", "value": "CVE-2025-4321"},
                    ],
                    "references": [{"url": "https://example.com/a"}, {"url": "https://example.com/b"}],
                    "published_at": "2025-01-01T00:00:00Z",
                    "updated_at": "2025-01-02T00:00:00Z",
                    "withdrawn_at": None,
                    "cvss_severities": {
                        "cvss_v4": {"score": 9.3, "vector_string": "CVSS:4.0/AV:N"},
                    },
                    "cwes": [{"cwe_id": "CWE-79", "name": "Cross-site Scripting"}],
                    "vulnerabilities": [
                        {
                            "package": {"ecosystem": "npm", "name": "demo"},
                            "vulnerable_version_range": ">=1.0.0 <2.0.0",
                            "first_patched_version": {"identifier": "2.0.1"},
                        }
                    ],
                    "credits": [{"login": "alice"}, {"login": "bob"}],
                }
            ),
            None,
        )

    with patch("app.integrations.providers.github._common.safe_request", side_effect=fake_request):
        result = await fn(repo="octo/repo", ghsa_id="ghsa-abcd-efgh-ijkl")

    assert "/repos/octo/repo/security-advisories/ghsa-abcd-efgh-ijkl" in captured["url"]
    assert "ghsa-abcd-efgh-ijkl" in result.lower()
    assert "critical" in result.lower()
    assert "demo / npm: >=1.0.0 <2.0.0" in result
    assert "CWE-79" in result
    assert "CVSS v4: 9.3" in result
    assert "Credits: alice, bob" in result
    assert "Updated: 2025-01-02T00:00:00Z" in result
    assert "[truncated]" in result
    assert len(result) <= 12_000 + len("\n... [truncated]")


@pytest.mark.asyncio
async def test_repository_security_advisory_errors_preserve_404_and_hint_permissions():
    fn = _get_fn(_executor(), "get_github_repository_security_advisory")
    not_found = "GitHub API error (404): Not Found"
    forbidden = "GitHub API error (403): Resource not accessible by integration"

    with patch(
        "app.integrations.providers.github._common.safe_request",
        new=AsyncMock(return_value=(_mock_response({}, 404), not_found)),
    ):
        assert await fn(repo="octo/repo", ghsa_id="GHSA-ABCD-EFGH-IJKL") == not_found

    with patch(
        "app.integrations.providers.github._common.safe_request",
        new=AsyncMock(return_value=(_mock_response({}, 403), forbidden)),
    ):
        result = await fn(repo="octo/repo", ghsa_id="GHSA-ABCD-EFGH-IJKL")
        assert forbidden in result
        assert "repository_advisories:read" in result


@pytest.mark.asyncio
async def test_manage_github_alert_secret_scanning_payload():
    fn = _get_fn(_executor(), "manage_github_alert")
    captured: dict = {}

    async def fake_request(client, method, url, provider, action, **kwargs):
        captured["url"] = url
        captured["json"] = kwargs.get("json")
        return (_mock_response({"state": "resolved"}), None)

    with patch("app.integrations.providers.github._common.safe_request", side_effect=fake_request):
        result = await fn(
            repo="octo/repo",
            alert_type="secret_scanning",
            alert_number=4,
            state="dismiss",
            reason="false_positive",
        )

    assert "/secret-scanning/alerts/4" in captured["url"]
    assert captured["json"]["state"] == "resolved"
    assert captured["json"]["resolution"] == "false_positive"
    assert "secret scanning" in result.lower()


# ---------------------------------------------------------------------------
# GraphQL escape hatch
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_github_graphql_query_returns_json():
    fn = _get_fn(_executor(), "github_graphql_query")
    with patch("app.integrations.providers.github._common.gh_graphql", new_callable=AsyncMock) as gql:
        gql.return_value = ({"viewer": {"login": "octocat"}}, None)
        result = await fn(query="query{viewer{login}}")

    assert json.loads(result) == {"viewer": {"login": "octocat"}}


@pytest.mark.asyncio
async def test_github_graphql_query_rejects_bad_variables():
    fn = _get_fn(_executor(), "github_graphql_query")
    result = await fn(query="query{viewer{login}}", variables="{not json}")
    assert "valid JSON" in result


@pytest.mark.asyncio
async def test_github_graphql_query_still_sends_a_mutation():
    """The write tool is unchanged: a mutation goes through where the binding allows it."""
    fn = _get_fn(_executor(), "github_graphql_query")
    with patch("app.integrations.providers.github._common.gh_graphql", new_callable=AsyncMock) as gql:
        gql.return_value = ({"addStar": {"clientMutationId": None}}, None)
        await fn(query='mutation { addStar(input: {starrableId: "x"}) { clientMutationId } }')
    gql.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "document",
    [
        "query { viewer { login } }",
        "{ viewer { login } }",
        'query Q($n: Int = 1) { repository(owner: "o", name: "r") { mutation: issues(first: $n) { totalCount } } }',
        '# mutation in a comment\nquery { search(query: "mutation { x }", type: ISSUE, first: 1) { issueCount } }',
        'query A { ...F } fragment F on Query { viewer { login } } query B { viewer { bio(arg: """subscription""") } }',
    ],
)
async def test_query_github_graphql_sends_a_query_document(document):
    fn = _get_fn(_executor(), "query_github_graphql")
    with patch("app.integrations.providers.github._common.gh_graphql", new_callable=AsyncMock) as gql:
        gql.return_value = ({"viewer": {"login": "octocat"}}, None)
        result = await fn(query=document)
    assert json.loads(result) == {"viewer": {"login": "octocat"}}
    assert gql.await_args.args[2] == document


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("document", "found"),
    [
        ("mutation { addStar(input: {starrableId: \"x\"}) { clientMutationId } }", "'mutation'"),
        ("subscription { viewer { login } }", "'subscription'"),
        ("query A { viewer { login } } mutation B { deleteIssue(input: {issueId: \"x\"}) { clientMutationId } }", "'mutation'"),
        ("{ viewer { login } } mutation { x }", "'mutation'"),
        ("query { viewer { login }", "unfinished"),
        ("", "no query operation"),
        ("fragment F on Query { viewer { login } }", "no query operation"),
    ],
)
async def test_query_github_graphql_refuses_anything_but_queries_unsent(document, found):
    fn = _get_fn(_executor(), "query_github_graphql")
    with patch("app.integrations.providers.github._common.gh_graphql", new_callable=AsyncMock) as gql:
        result = await fn(query=document)
    gql.assert_not_awaited()
    assert result.startswith("Error:")
    assert found in result
    assert "nothing was sent to GitHub" in result
    assert "read-only GitHub binding" in result


def test_a_read_only_github_binding_keeps_the_query_tool_and_withholds_the_raw_one():
    from app.services.integration_read_only import integration_tool_access, withhold_write_tools

    executor = _executor()
    defs = build_tools("fake-token", executor)
    kept = {d.name for d in withhold_write_tools(defs, executor, integration_label="GitHub")}
    assert integration_tool_access("query_github_graphql") == "read"
    assert integration_tool_access("github_graphql_query") == "write"
    assert "query_github_graphql" in kept
    assert "github_graphql_query" not in kept


def test_a_read_only_github_binding_keeps_the_declared_compare_read():
    """``compare`` is no verb the name classifier reads: the declaration keeps it (#9580)."""
    from app.services.integration_read_only import integration_tool_access, withhold_write_tools

    executor = _executor()
    defs = build_tools("fake-token", executor)
    kept = {d.name for d in withhold_write_tools(defs, executor, integration_label="GitHub")}
    assert integration_tool_access("compare_github_commits") == "write"
    assert "compare_github_commits" in kept
    # The writes around it stay withheld.
    assert {"revert_github_commit", "create_github_commit", "merge_github_branch"}.isdisjoint(kept)


@pytest.mark.asyncio
async def test_gh_graphql_handles_null_body_without_crashing():
    """Regression: a JSON `null` / empty 200 body made `body.get(...)` raise
    'NoneType' object has no attribute 'get'. It must instead return a clear,
    retryable error string."""
    from app.integrations.providers.github import _common

    async def fake_request(client, method, url, provider, action, **kwargs):
        return (_mock_response(None), None)  # resp.json() -> None

    with patch("app.integrations.providers.github._common.safe_request", side_effect=fake_request):
        data, err = await _common.gh_graphql(None, {}, "query{viewer{login}}")

    assert data is None
    assert err is not None
    assert "NoneType" not in err
    assert "unexpected" in err.lower() or "empty" in err.lower()


@pytest.mark.asyncio
async def test_gh_graphql_handles_non_json_body_without_crashing():
    """A non-JSON 200 body (e.g. an HTML 502 page) must not crash either."""
    from app.integrations.providers.github import _common

    async def fake_request(client, method, url, provider, action, **kwargs):
        return (httpx.Response(200, content=b"<html>502 Bad Gateway</html>"), None)

    with patch("app.integrations.providers.github._common.safe_request", side_effect=fake_request):
        data, err = await _common.gh_graphql(None, {}, "query{viewer{login}}")

    assert data is None
    assert err is not None
    assert "NoneType" not in err


_THREE_ALIASES = (
    'mutation M($t: String!) { a1: updateIssue(input: {id: "I1", title: $t}) { issue { number } } '
    'a2: updateIssue(input: {id: "I2", title: "x"}) @include(if: true) { issue { number title } } '
    "addStar(input: {starrableId: \"R\"}) { clientMutationId } }"
)


def test_mutation_response_keys_reads_aliases_and_bare_fields_only():
    from app.integrations.providers.github.graphql import mutation_response_keys

    assert mutation_response_keys(_THREE_ALIASES) == ["a1", "a2", "addStar"]
    assert mutation_response_keys("query { viewer { login } }") == []
    assert mutation_response_keys('query { search(query: "mutation { x }") { issueCount } }') == []
    two_ops = 'query Q { viewer { login } } mutation { b: deleteIssue(input: {issueId: "x"}) { clientMutationId } }'
    assert mutation_response_keys(two_ops) == ["b"]


@pytest.mark.asyncio
async def test_a_mutation_5xx_names_the_aliases_its_partial_data_shows_applied():
    fn = _get_fn(_executor(), "github_graphql_query")
    partial = {"a1": {"issue": {"number": 1}}, "a2": None}
    with patch("app.integrations.providers.github._common.gh_graphql", new_callable=AsyncMock) as gql:
        gql.return_value = (partial, "Error: GitHub API error (502): Bad Gateway")
        result = await fn(query=_THREE_ALIASES)
    assert gql.await_args.kwargs["keep_partial"] is True
    assert result.startswith("Error: GitHub API error (502)")
    assert "Applied per the answer's partial data: a1." in result
    assert "Not confirmed: a2, addStar." in result
    assert "never resends a mutation" in result


@pytest.mark.asyncio
async def test_a_mutation_5xx_without_data_advises_a_re_read_and_smaller_batches():
    fn = _get_fn(_executor(), "github_graphql_query")
    document = "mutation { " + " ".join(f'u{n}: updateIssue(input: {{id: "I{n}"}}) {{ clientMutationId }}' for n in range(12)) + " }"
    with patch("app.integrations.providers.github._common.gh_graphql", new_callable=AsyncMock) as gql:
        gql.return_value = (None, "Error: GitHub API error (502): Bad Gateway")
        result = await fn(query=document)
    assert "no field is confirmed either way" in result and "u0, u1" in result and "u11" in result
    assert "at most 10 aliases per mutation" in result


@pytest.mark.asyncio
async def test_a_query_error_is_unchanged_and_asks_no_partial_data():
    fn = _get_fn(_executor(), "query_github_graphql")
    with patch("app.integrations.providers.github._common.gh_graphql", new_callable=AsyncMock) as gql:
        gql.return_value = (None, "Error: GitHub API error (502): Bad Gateway")
        result = await fn(query="query { viewer { login } }")
    assert result == "Error: GitHub API error (502): Bad Gateway"
    assert gql.await_args.kwargs["keep_partial"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("response", "expected"),
    [
        (httpx.Response(502, json={"data": {"a1": {"issue": None}}, "errors": [{"message": "timeout"}]}), {"a1": {"issue": None}}),
        (httpx.Response(502, content=b"<html>502</html>"), None),
        (httpx.Response(200, json={"data": {"a1": {"x": 1}, "a2": None}, "errors": [{"message": "Not found"}]}), {"a1": {"x": 1}, "a2": None}),
    ],
    ids=["5xx with json data", "5xx html", "200 with errors"],
)
async def test_gh_graphql_keep_partial_returns_the_data_next_to_the_error(response, expected):
    from app.integrations.providers.github import _common

    async def fake_request(client, method, url, provider, action, **kwargs):
        assert kwargs["retry_statuses"] is None  # a mutation is never retried
        assert kwargs["expected_statuses"] == set()
        if response.status_code >= 400:
            return response, f"Error: GitHub API error ({response.status_code})"
        return response, None

    with patch("app.integrations.providers.github._common.safe_request", side_effect=fake_request):
        data, err = await _common.gh_graphql(None, {}, _THREE_ALIASES, keep_partial=True)
    assert err is not None and err.startswith("Error:")
    assert data == expected


# ---------------------------------------------------------------------------
# URL-encoding of free-form ref / path segments (slashes preserved)
# ---------------------------------------------------------------------------


def test_enc_preserves_slashes_and_encodes_unsafe_chars():
    from app.integrations.providers.github._common import _enc

    # Hierarchical separators survive; space / '#' / '?' / '%' are encoded so a
    # ref or path cannot break the URL path or inject a query/fragment.
    assert _enc("feature/a b") == "feature/a%20b"
    assert _enc("a#b?c%d") == "a%23b%3Fc%25d"
    assert _enc("docs/plain.txt") == "docs/plain.txt"


async def _capture_url_request(captured: dict):
    async def fake_request(client, method, url, provider, action, **kwargs):
        captured["url"] = url
        return (None, "Error: short-circuit after capturing the URL")

    return fake_request


@pytest.mark.asyncio
async def test_get_github_file_encodes_path_segments():
    fn = _get_fn(_executor(), "get_github_file")
    captured: dict = {}
    with patch(
        "app.integrations.providers.github._common.safe_request",
        side_effect=await _capture_url_request(captured),
    ):
        await fn(repo="octo/repo", path="docs/a b#1.md", branch="main")
    assert "/contents/docs/a%20b%231.md" in captured["url"]


@pytest.mark.asyncio
async def test_compare_commits_encodes_refs_preserving_slashes():
    fn = _get_fn(_executor(), "compare_github_commits")
    captured: dict = {}
    with patch(
        "app.integrations.providers.github._common.safe_request",
        side_effect=await _capture_url_request(captured),
    ):
        await fn(repo="octo/repo", base="feature/a", head="bug fix")
    # Slash kept inside each ref; space encoded; the '...' separator stays literal.
    assert "/compare/feature/a...bug%20fix" in captured["url"]


@pytest.mark.asyncio
async def test_branch_protection_encodes_branch():
    fn = _get_fn(_executor(), "get_github_branch_protection")
    captured: dict = {}
    with patch(
        "app.integrations.providers.github._common.safe_request",
        side_effect=await _capture_url_request(captured),
    ):
        await fn(repo="octo/repo", branch="release/v1 2")
    assert "/branches/release/v1%202/protection" in captured["url"]


# ---------------------------------------------------------------------------
# Projects v2: a genuine resolve error is surfaced, not masked
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_github_project_surfaces_real_resolve_error():
    """A real permission/rate-limit error during project resolution must be
    surfaced rather than masked by the generic 'No project found' message."""
    fn = _get_fn(_executor(), "get_github_project")

    async def fake_gql(client, headers, query, variables=None, *, action="", extra_headers=None):
        return (None, "GitHub GraphQL error: Resource not accessible by integration")

    with patch("app.integrations.providers.github._common.gh_graphql", side_effect=fake_gql):
        result = await fn(login="octo", project_number=5)

    assert "Resource not accessible" in result
    assert "No project #5 found" not in result


@pytest.mark.asyncio
async def test_get_github_project_benign_owner_mismatch_returns_generic():
    """The benign wrong-owner-type 'Could not resolve to an Organization/User'
    mismatch is still tolerated and yields the generic not-found message."""
    fn = _get_fn(_executor(), "get_github_project")

    async def fake_gql(client, headers, query, variables=None, *, action="", extra_headers=None):
        owner = "Organization" if "organization(login" in query else "User"
        return (None, f"GitHub GraphQL error: Could not resolve to a {owner} with the login of 'ghost'.")

    with patch("app.integrations.providers.github._common.gh_graphql", side_effect=fake_gql):
        result = await fn(login="ghost", project_number=9)

    assert "No project #9 found" in result


# ---------------------------------------------------------------------------
# Actions variables - the variable name is a path segment (path-injection safe)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_manage_actions_variable_encodes_name_in_path():
    fn = _get_fn(_executor(), "manage_github_actions_variable")
    # update and delete both interpolate the variable name into the URL path.
    for verb in ("update", "delete"):
        captured: dict = {}
        with patch(
            "app.integrations.providers.github._common.safe_request",
            side_effect=await _capture_url_request(captured),
        ):
            await fn(repo="octo/repo", action=verb, name="weird/name key", value="v")
        # The name is encoded as a single segment - '/' and space are
        # percent-encoded so they cannot change the request path.
        assert "/actions/variables/weird%2Fname%20key" in captured["url"]


# ---------------------------------------------------------------------------
# Notifications - the opaque thread_id is a single path segment (injection-safe)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mark_notification_encodes_thread_id_as_single_segment():
    fn = _get_fn(_executor(), "manage_github_notification")
    captured: dict = {}
    with patch(
        "app.integrations.providers.github._common.safe_request",
        side_effect=await _capture_url_request(captured),
    ):
        # thread_id is opaque: a crafted value containing '/' must be encoded as
        # a single segment (%2F), not preserved, so it cannot traverse to a
        # different notifications path.
        await fn(action="mark_read", thread_id="123/../subscriptions")
    assert "/notifications/threads/123%2F..%2Fsubscriptions" in captured["url"]
    assert "/notifications/threads/123/" not in captured["url"]


# ---------------------------------------------------------------------------
# Repository admin - add_collaborator reports the permission actually sent
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_add_collaborator_reports_effective_permission():
    fn = _get_fn(_executor(), "manage_github_repository")
    captured: dict = {}

    async def fake_request(client, method, url, provider, action, **kwargs):
        captured["json"] = kwargs.get("json")
        return (_mock_response({}), None)

    with patch("app.integrations.providers.github._common.safe_request", side_effect=fake_request):
        # An invalid permission falls back to 'push'; the payload AND the success
        # message must report 'push', not echo the rejected raw input.
        result = await fn(repo="octo/repo", action="add_collaborator", username="alice", permission="superadmin")

    assert captured["json"] == {"permission": "push"}
    assert "'push' permission" in result
    assert "superadmin" not in result


@pytest.mark.asyncio
async def test_fork_posts_to_the_forks_endpoint_and_names_the_fork():
    fn = _get_fn(_executor(), "manage_github_repository")
    captured: dict = {}

    async def fake_request(client, method, url, provider, action, **kwargs):
        captured.update(method=method, url=url, json=kwargs.get("json"))
        return (
            _mock_response(
                {"full_name": "alice/repo", "html_url": "https://github.com/alice/repo", "default_branch": "main"}
            ),
            None,
        )

    with patch("app.integrations.providers.github._common.safe_request", side_effect=fake_request):
        result = await fn(repo="octo/repo", action="fork", default_branch_only=True)

    assert captured["method"] == "post"
    assert captured["url"].endswith("/repos/octo/repo/forks")
    assert captured["json"] == {"default_branch_only": True}
    assert "alice/repo" in result
    assert "enable_actions" in result


@pytest.mark.asyncio
async def test_fork_under_an_org_with_a_new_name():
    fn = _get_fn(_executor(), "manage_github_repository")
    captured: dict = {}

    async def fake_request(client, method, url, provider, action, **kwargs):
        captured["json"] = kwargs.get("json")
        return (_mock_response({"full_name": "acme/repo-fork"}), None)

    with patch("app.integrations.providers.github._common.safe_request", side_effect=fake_request):
        result = await fn(repo="octo/repo", action="fork", org="acme", name="repo-fork")

    assert captured["json"] == {"default_branch_only": False, "organization": "acme", "name": "repo-fork"}
    assert "acme/repo-fork" in result


@pytest.mark.asyncio
async def test_enable_actions_puts_the_actions_permissions():
    fn = _get_fn(_executor(), "manage_github_repository")
    captured: dict = {}

    async def fake_request(client, method, url, provider, action, **kwargs):
        captured.update(method=method, url=url, json=kwargs.get("json"))
        return (_mock_response({}), None)

    with patch("app.integrations.providers.github._common.safe_request", side_effect=fake_request):
        result = await fn(repo="alice/repo", action="enable_actions")

    assert captured["method"] == "put"
    assert captured["url"].endswith("/repos/alice/repo/actions/permissions")
    assert captured["json"] == {"enabled": True, "allowed_actions": "all"}
    assert "Actions enabled on alice/repo" in result


# ---------------------------------------------------------------------------
# per_page clamping - a non-positive count must never send per_page <= 0
# (GitHub rejects per_page < 1 with HTTP 422).
# ---------------------------------------------------------------------------


def _capture_params_request(captured: dict):
    async def fake_request(client, method, url, provider, action, **kwargs):
        captured["params"] = kwargs.get("params") or {}
        return (None, "Error: short-circuit after capturing params")

    return fake_request


@pytest.mark.asyncio
async def test_list_tools_clamp_nonpositive_count_per_page():
    # A representative tool from each affected module (100-cap and 30-cap).
    cases = [
        ("list_github_repos", {"count": 0}),
        ("list_github_branches", {"repo": "octo/repo", "count": 0}),
        ("list_github_prs", {"repo": "octo/repo", "count": -5}),
        ("list_github_workflow_runs", {"repo": "octo/repo", "count": 0}),
        ("search_github_code", {"query": "x", "count": -1}),
        ("list_github_code_scanning_alerts", {"repo": "octo/repo", "count": 0}),
        ("list_github_dependabot_alerts", {"repo": "octo/repo", "count": 0}),
        ("list_github_secret_scanning_alerts", {"repo": "octo/repo", "count": -3}),
    ]
    for tool_name, kwargs in cases:
        fn = _get_fn(_executor(), tool_name)
        captured: dict = {}
        with patch(
            "app.integrations.providers.github._common.safe_request",
            side_effect=_capture_params_request(captured),
        ):
            await fn(**kwargs)
        assert captured["params"]["per_page"] >= 1, f"{tool_name} sent per_page < 1"


# ---------------------------------------------------------------------------
# Security alert 404 - preserve the safe_request error, add a narrow hint
# (the empty case is reported separately, so don't mask 404 as "no alerts").
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_security_alert_404_preserves_error_and_adds_hint():
    original = "GitHub API error (404): Not Found when trying to list code scanning alerts in octo/repo."

    async def fake_request(client, method, url, provider, action, **kwargs):
        return (None, original)

    for tool_name, hint in (
        ("list_github_code_scanning_alerts", "code scanning may not be enabled"),
        ("list_github_dependabot_alerts", "Dependabot alerts may not be enabled"),
        ("list_github_secret_scanning_alerts", "secret scanning may not be enabled"),
    ):
        fn = _get_fn(_executor(), tool_name)
        with patch(
            "app.integrations.providers.github._common.safe_request",
            side_effect=fake_request,
        ):
            result = await fn(repo="octo/repo")
        # Original (actionable) error is preserved and the narrow hint is added;
        # the old misleading "no alerts found" wording is gone.
        assert original in result
        assert hint in result
        assert "no alerts found" not in result


# ---------------------------------------------------------------------------
# Self-action markers - write actions that emit an issue/PR webhook must record
# a marker so assignments with ignore_self_triggered_events don't echo-loop.
# ---------------------------------------------------------------------------


def _get_fn_with_recorder(name: str):
    """Build the tools with a self_action_recorder injected and return
    (handler, recorder) so a test can assert the marker is recorded."""
    ex = _executor()
    recorder = AsyncMock()
    build_tools("fake-token", ex, self_action_recorder=recorder)
    for call in ex.register.call_args_list:
        if call[0][0] == name:
            return call[0][1], recorder
    raise KeyError(f"Tool {name!r} not registered")


@pytest.mark.asyncio
async def test_manage_pr_update_branch_records_self_action():
    # update_branch is a write (PUT) that fires a pull_request 'synchronize'
    # webhook - it must record a self-action marker like the sibling actions.
    fn, recorder = _get_fn_with_recorder("manage_github_pr")
    with patch("app.integrations.providers.github._common.safe_request", new_callable=AsyncMock) as req:
        req.return_value = (_mock_response({"message": "Updating"}), None)
        await fn(repo="octo/repo", number=7, action="update_branch")
    recorder.assert_awaited_once_with("octo/repo", 7)


@pytest.mark.asyncio
async def test_manage_pr_disable_automerge_records_self_action():
    # disable_automerge is a write (GraphQL mutation) that fires a pull_request
    # webhook - it must record a self-action marker too.
    fn, recorder = _get_fn_with_recorder("manage_github_pr")

    async def fake_request(client, method, url, provider, action, **kwargs):
        return (_mock_response({"node_id": "PR_node1"}), None)  # _pr_node_id

    with (
        patch("app.integrations.providers.github._common.safe_request", side_effect=fake_request),
        patch("app.integrations.providers.github._common.gh_graphql", new_callable=AsyncMock) as gql,
    ):
        gql.return_value = ({"disablePullRequestAutoMerge": {"pullRequest": {"number": 7}}}, None)
        await fn(repo="octo/repo", number=7, action="disable_automerge")
    recorder.assert_awaited_once_with("octo/repo", 7)


@pytest.mark.asyncio
async def test_manage_pr_dequeue_removes_a_queued_pr() -> None:
    fn, recorder = _get_fn_with_recorder("manage_github_pr")

    async def fake_request(client: Any, method: str, url: str, provider: str, action: str, **kwargs: Any) -> Any:
        return (_mock_response({"node_id": "PR_node1"}), None)  # _pr_node_id

    with (
        patch("app.integrations.providers.github._common.safe_request", side_effect=fake_request),
        patch("app.integrations.providers.github._common.gh_graphql", new_callable=AsyncMock) as gql,
    ):
        gql.side_effect = [
            ({"node": {"isInMergeQueue": True, "mergeQueueEntry": {"position": 3, "state": "AWAITING_CHECKS"}}}, None),
            ({"dequeuePullRequest": {"mergeQueueEntry": {"state": "UNMERGEABLE"}}}, None),
        ]
        result = await fn(repo="octo/repo", number=7, action="dequeue")
    assert "removed from the merge queue (it was at position 3)" in result
    assert "dequeuePullRequest" in gql.await_args_list[1][0][2]
    assert gql.await_args_list[1][0][3] == {"id": "PR_node1"}
    recorder.assert_awaited_once_with("octo/repo", 7)


@pytest.mark.asyncio
async def test_manage_pr_dequeue_writes_nothing_when_not_queued() -> None:
    fn, recorder = _get_fn_with_recorder("manage_github_pr")

    async def fake_request(client: Any, method: str, url: str, provider: str, action: str, **kwargs: Any) -> Any:
        return (_mock_response({"node_id": "PR_node1"}), None)

    with (
        patch("app.integrations.providers.github._common.safe_request", side_effect=fake_request),
        patch("app.integrations.providers.github._common.gh_graphql", new_callable=AsyncMock) as gql,
    ):
        gql.return_value = ({"node": {"isInMergeQueue": False, "mergeQueueEntry": None}}, None)
        result = await fn(repo="octo/repo", number=7, action="dequeue")
    assert "not in a merge queue" in result
    assert gql.await_count == 1
    recorder.assert_not_awaited()


@pytest.mark.asyncio
async def test_manage_sub_issue_reprioritize_records_self_action():
    # reprioritize is a write on the parent issue and must record a marker for
    # the parent, consistent with the add / remove sub-issue actions.
    fn, recorder = _get_fn_with_recorder("manage_github_sub_issue")

    async def fake_request(client, method, url, provider, action, **kwargs):
        if method == "get":
            return (_mock_response({"id": 100, "number": 9}), None)  # id resolution
        return (_mock_response({}), None)  # the priority PATCH

    with patch("app.integrations.providers.github._common.safe_request", side_effect=fake_request):
        await fn(
            repo="octo/repo",
            parent_number=3,
            action="reprioritize",
            sub_issue_number=9,
            after_number=5,
        )
    recorder.assert_awaited_once_with("octo/repo", 3)


# ---------------------------------------------------------------------------
# Permission-hint helpers - make "Resource not accessible by integration"
# actionable by naming the exact access to grant (the audit gap).
# ---------------------------------------------------------------------------


def test_permission_hint_helpers_detect_and_annotate_denials():
    from app.integrations.providers.github._common import (
        is_permission_error,
        with_permission_hint,
    )

    assert is_permission_error("Error: ... Resource not accessible by integration")
    assert is_permission_error("GitHub API error (403): Forbidden")
    assert is_permission_error("You must be an enterprise owner to view this.")
    # A genuine 404 / empty error is NOT a permission denial.
    assert not is_permission_error("GitHub API error (404): Not Found")
    assert not is_permission_error("")
    assert not is_permission_error(None)

    # The hint is appended only on permission errors, never on other failures.
    hinted = with_permission_hint("403 Resource not accessible by integration", "grant X")
    assert "grant X" in hinted
    assert with_permission_hint("404 Not Found", "grant X") == "404 Not Found"


# ---------------------------------------------------------------------------
# Organization members WITH roles (the membersWithRole gap)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_org_members_annotates_roles_via_graphql():
    fn = _get_fn(_executor(), "list_github_org_members")
    payload = {
        "organization": {
            "membersWithRole": {
                "totalCount": 2,
                "pageInfo": {"hasNextPage": False, "endCursor": None},
                "edges": [
                    {"role": "ADMIN", "node": {"login": "alice", "name": "Alice A"}},
                    {"role": "MEMBER", "node": {"login": "bob", "name": None}},
                ],
            }
        }
    }
    with patch("app.integrations.providers.github._common.gh_graphql", new_callable=AsyncMock) as gql:
        gql.return_value = (payload, None)
        result = await fn(org="acme")

    assert "alice (Alice A) — admin" in result
    assert "bob — member" in result
    assert "2 total" in result


@pytest.mark.asyncio
async def test_list_org_members_admin_role_uses_rest_server_side_filter():
    fn = _get_fn(_executor(), "list_github_org_members")
    captured: dict = {}

    async def fake_request(client, method, url, provider, action, **kwargs):
        captured["url"] = url
        captured["params"] = kwargs.get("params")
        return (_mock_response([{"login": "alice", "type": "User"}]), None)

    with patch("app.integrations.providers.github._common.safe_request", side_effect=fake_request):
        result = await fn(org="acme", role="admin")

    # admin/member go through the REST role filter; every returned member's role
    # is known, so it is labelled without a second lookup.
    assert captured["params"]["role"] == "admin"
    assert captured["url"].endswith("/orgs/acme/members")
    assert "alice (User) — admin" in result


@pytest.mark.asyncio
async def test_list_org_members_rejects_after_for_admin_role():
    # 'after' is a GraphQL cursor (role='all' only); it's meaningless for the
    # REST admin/member filter, so it's rejected rather than silently ignored.
    fn = _get_fn(_executor(), "list_github_org_members")
    result = await fn(org="acme", role="admin", after="CURSOR")
    assert "only applies to role='all'" in result


@pytest.mark.asyncio
async def test_list_org_members_permission_error_falls_back_with_hint():
    fn = _get_fn(_executor(), "list_github_org_members")

    async def fake_gql(client, headers, query, variables=None, *, action="", extra_headers=None):
        return (None, "GitHub GraphQL error: Resource not accessible by integration")

    async def fake_request(client, method, url, provider, action, **kwargs):
        return (_mock_response([{"login": "carol", "type": "User", "html_url": "u"}]), None)

    with (
        patch("app.integrations.providers.github._common.gh_graphql", side_effect=fake_gql),
        patch("app.integrations.providers.github._common.safe_request", side_effect=fake_request),
    ):
        result = await fn(org="acme")

    # The roster still comes back, and the failure to read roles names the exact
    # permission to grant instead of leaving the auditor guessing.
    assert "carol" in result
    assert "member roles could not be retrieved" in result
    assert "Members: Read" in result


@pytest.mark.asyncio
async def test_get_org_membership_reports_role_and_state():
    fn = _get_fn(_executor(), "get_github_org_membership")

    async def fake_request(client, method, url, provider, action, **kwargs):
        assert url.endswith("/orgs/acme/memberships/alice")
        return (
            _mock_response({"role": "admin", "state": "active", "user": {"login": "alice"}}),
            None,
        )

    with patch("app.integrations.providers.github._common.safe_request", side_effect=fake_request):
        result = await fn(org="acme", username="alice")

    assert "Role: admin" in result
    assert "State: active" in result


@pytest.mark.asyncio
async def test_get_org_membership_404_returns_not_a_member():
    """A 404 from GitHub means the user is not in the org — return a clear string, not an error."""
    fn = _get_fn(_executor(), "get_github_org_membership")

    async def fake_request(client, method, url, provider, action, **kwargs):
        assert url.endswith("/orgs/acme/memberships/outsider")
        # The tool MUST opt in via expected_statuses={404}: without it the real
        # safe_request returns (None, err) on 404 and the not-a-member branch
        # can never fire. Asserting the kwarg keeps this test honest.
        assert kwargs.get("expected_statuses") == {404}
        # Simulate the (response, error_string) tuple that safe_request returns
        # when expected_statuses={404} is passed and a 404 is received.
        return (
            _mock_response({"message": "Not Found"}, status=404),
            "Error: GitHub resource not found while trying to get membership of outsider in acme.",
        )

    with patch("app.integrations.providers.github._common.safe_request", side_effect=fake_request):
        result = await fn(org="acme", username="outsider")

    assert "not a member" in result.lower()
    assert "outsider" in result
    assert "acme" in result
    # Must not look like a tool error to the LLM.
    assert "Error:" not in result


@pytest.mark.asyncio
async def test_get_org_membership_non_404_error_keeps_permission_hint():
    """Non-404 failures (e.g. 403) must still surface as errors with the
    permission hint — only 404 maps to the benign "not a member" answer.

    With expected_statuses set, safe_request returns (response, error_string)
    for EVERY non-2xx status, so the 404 short-circuit must not swallow real
    permission failures.
    """
    fn = _get_fn(_executor(), "get_github_org_membership")

    async def fake_request(client, method, url, provider, action, **kwargs):
        # Mirrors format_http_error's real 403 string ("access denied" is one
        # of the _PERMISSION_SIGNATURES that triggers the hint).
        return (
            _mock_response({"message": "Forbidden"}, status=403),
            "Error: GitHub access denied while trying to get membership of alice in acme — "
            "insufficient permissions or scopes. The user should check the integration permissions.",
        )

    with patch("app.integrations.providers.github._common.safe_request", side_effect=fake_request):
        result = await fn(org="acme", username="alice")

    assert "not a member" not in result.lower()
    assert "Error:" in result
    # The org-members permission hint names the exact access to grant.
    assert "Members: Read" in result


@pytest.mark.asyncio
async def test_get_team_labels_maintainers():
    fn = _get_fn(_executor(), "get_github_team")

    async def fake_request(client, method, url, provider, action, **kwargs):
        if action == "fetch team maintainers":
            return (_mock_response([{"login": "alice"}]), None)
        if action == "fetch team members":
            return (_mock_response([{"login": "alice"}, {"login": "bob"}]), None)
        if action == "fetch team repos":
            return (_mock_response([]), None)
        return (_mock_response({"name": "Core", "slug": "core", "privacy": "closed"}), None)

    with patch("app.integrations.providers.github._common.safe_request", side_effect=fake_request):
        result = await fn(org="acme", team_slug="core")

    assert "alice (maintainer)" in result
    assert "bob" in result
    assert "bob (maintainer)" not in result


# ---------------------------------------------------------------------------
# Enterprise governance (GraphQL enterprise(slug:) surface)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_enterprise_owners_formats_owner_and_billing_roles():
    fn = _get_fn(_executor(), "list_github_enterprise_owners")
    payload = {
        "enterprise": {
            "ownerInfo": {
                "admins": {
                    "totalCount": 2,
                    "pageInfo": {"hasNextPage": False, "endCursor": None},
                    "edges": [
                        {"role": "OWNER", "node": {"login": "alice", "name": "Alice"}},
                        {"role": "BILLING_MANAGER", "node": {"login": "bob", "name": None}},
                    ],
                }
            }
        }
    }
    with patch("app.integrations.providers.github._common.gh_graphql", new_callable=AsyncMock) as gql:
        gql.return_value = (payload, None)
        result = await fn(slug="acme-inc")

    assert "alice (Alice) — owner" in result
    assert "bob — billing manager" in result


@pytest.mark.asyncio
async def test_list_enterprise_owners_degrades_when_owner_info_hidden():
    fn = _get_fn(_executor(), "list_github_enterprise_owners")
    with patch("app.integrations.providers.github._common.gh_graphql", new_callable=AsyncMock) as gql:
        gql.return_value = ({"enterprise": {"ownerInfo": None}}, None)
        result = await fn(slug="acme-inc")

    # Non-owners cannot see ownerInfo; the tool explains the access needed.
    assert "not visible" in result.lower()
    assert "read:enterprise" in result


@pytest.mark.asyncio
async def test_list_enterprise_members_permission_error_adds_hint():
    fn = _get_fn(_executor(), "list_github_enterprise_members")

    async def fake_gql(client, headers, query, variables=None, *, action="", extra_headers=None):
        return (None, "GitHub GraphQL error: Resource not accessible by integration")

    with patch("app.integrations.providers.github._common.gh_graphql", side_effect=fake_gql):
        result = await fn(slug="acme-inc")

    assert "Resource not accessible" in result
    assert "read:enterprise" in result


@pytest.mark.asyncio
async def test_get_enterprise_not_found_explains_access():
    fn = _get_fn(_executor(), "get_github_enterprise")
    with patch("app.integrations.providers.github._common.gh_graphql", new_callable=AsyncMock) as gql:
        gql.return_value = ({"enterprise": None}, None)
        result = await fn(slug="ghost-co")

    assert "not found or not accessible" in result
    assert "read:enterprise" in result


@pytest.mark.asyncio
async def test_get_enterprise_formats_profile_counts():
    fn = _get_fn(_executor(), "get_github_enterprise")
    payload = {
        "enterprise": {
            "name": "Acme Inc",
            "slug": "acme-inc",
            "description": "All of Acme",
            "url": "https://github.com/enterprises/acme-inc",
            "viewerIsAdmin": True,
            "organizations": {"totalCount": 12},
            "members": {"totalCount": 340},
        }
    }
    with patch("app.integrations.providers.github._common.gh_graphql", new_callable=AsyncMock) as gql:
        gql.return_value = (payload, None)
        result = await fn(slug="acme-inc")

    assert "Enterprise: Acme Inc" in result
    assert "Organizations: 12 | Members: 340" in result
    assert "enterprise owner: yes" in result


# ---------------------------------------------------------------------------
# Rulesets (modern branch governance) - the change-control evidence gap
# ---------------------------------------------------------------------------

# A representative effective-rules payload (GET /repos/{o}/{r}/rules/branches/{b}).
_BRANCH_RULES_PAYLOAD = [
    {
        "type": "pull_request",
        "ruleset_id": 123,
        "ruleset_source_type": "Organization",
        "parameters": {
            "required_approving_review_count": 2,
            "dismiss_stale_reviews_on_push": True,
            "require_code_owner_review": True,
        },
    },
    {
        "type": "required_status_checks",
        "ruleset_id": 123,
        "ruleset_source_type": "Organization",
        "parameters": {
            "strict_required_status_checks_policy": True,
            "required_status_checks": [{"context": "build"}, {"context": "test"}],
        },
    },
    {"type": "non_fast_forward", "ruleset_id": 123, "ruleset_source_type": "Organization"},
]


def test_summarize_branch_rules_formats_governance():
    from app.integrations.providers.github._common import summarize_branch_rules

    summary = summarize_branch_rules(_BRANCH_RULES_PAYLOAD)
    assert summary is not None
    assert "2 approving review(s)" in summary
    assert "code owner review" in summary
    assert "Required status checks (strict): build, test" in summary
    assert "Force pushes blocked (non-fast-forward)" in summary
    assert "#123 (Organization)" in summary
    # Empty / non-list inputs return None (so callers can branch on "no rules").
    assert summarize_branch_rules([]) is None
    assert summarize_branch_rules(None) is None


@pytest.mark.asyncio
async def test_get_branch_rules_reports_effective_rules():
    fn = _get_fn(_executor(), "get_github_branch_rules")
    captured: dict = {}

    async def fake_request(client, method, url, provider, action, **kwargs):
        captured["url"] = url
        return (_mock_response(_BRANCH_RULES_PAYLOAD), None)

    with patch("app.integrations.providers.github._common.safe_request", side_effect=fake_request):
        result = await fn(repo="octo/repo", branch="main")

    assert captured["url"].endswith("/repos/octo/repo/rules/branches/main")
    assert "Active ruleset rules for branch 'main'" in result
    assert "2 approving review(s)" in result


@pytest.mark.asyncio
async def test_list_rulesets_requires_repo_or_org():
    fn = _get_fn(_executor(), "list_github_rulesets")
    result = await fn()
    assert "provide either 'repo'" in result


@pytest.mark.asyncio
async def test_list_rulesets_repo_includes_parents_param():
    fn = _get_fn(_executor(), "list_github_rulesets")
    captured: dict = {}

    async def fake_request(client, method, url, provider, action, **kwargs):
        captured["url"] = url
        captured["params"] = kwargs.get("params")
        return (
            _mock_response(
                [
                    {
                        "id": 123,
                        "name": "Default branch",
                        "target": "branch",
                        "enforcement": "active",
                        "source_type": "Organization",
                        "source": "octo",
                    }
                ]
            ),
            None,
        )

    with patch("app.integrations.providers.github._common.safe_request", side_effect=fake_request):
        result = await fn(repo="octo/repo")

    assert captured["url"].endswith("/repos/octo/repo/rulesets")
    # includes_parents defaults to true so org rulesets governing the repo show up.
    assert captured["params"]["includes_parents"] == "true"
    assert "#123 Default branch" in result
    assert "enforcement=active" in result


@pytest.mark.asyncio
async def test_get_ruleset_rejects_non_integer_id():
    fn = _get_fn(_executor(), "get_github_ruleset")
    result = await fn(ruleset_id="not-an-int", repo="octo/repo")
    assert "must be an integer" in result


@pytest.mark.asyncio
async def test_list_rulesets_rejects_both_repo_and_org():
    # Passing both scopes is ambiguous; reject rather than silently prefer repo.
    fn = _get_fn(_executor(), "list_github_rulesets")
    result = await fn(repo="octo/repo", org="octo")
    assert "not both" in result


@pytest.mark.asyncio
async def test_get_ruleset_rejects_both_repo_and_org():
    fn = _get_fn(_executor(), "get_github_ruleset")
    result = await fn(ruleset_id=1, repo="octo/repo", org="octo")
    assert "not both" in result


@pytest.mark.asyncio
async def test_get_ruleset_formats_conditions_bypass_and_rules():
    fn = _get_fn(_executor(), "get_github_ruleset")
    payload = {
        "id": 123,
        "name": "Default branch",
        "target": "branch",
        "enforcement": "active",
        "source_type": "Organization",
        "source": "octo",
        "conditions": {"ref_name": {"include": ["~DEFAULT_BRANCH"], "exclude": []}},
        "bypass_actors": [{"actor_type": "Team", "actor_id": 42, "bypass_mode": "pull_request"}],
        "rules": _BRANCH_RULES_PAYLOAD,
    }

    async def fake_request(client, method, url, provider, action, **kwargs):
        assert url.endswith("/orgs/octo/rulesets/123")
        return (_mock_response(payload), None)

    with patch("app.integrations.providers.github._common.safe_request", side_effect=fake_request):
        result = await fn(ruleset_id=123, org="octo")

    assert "Ruleset #123: Default branch" in result
    assert "Enforcement: active" in result
    assert "include ~DEFAULT_BRANCH" in result
    assert "Team#42 (pull_request)" in result
    assert "2 approving review(s)" in result


@pytest.mark.asyncio
async def test_get_ruleset_names_an_enterprise_ruleset_on_a_repository():
    """An enterprise ruleset is served by neither the repo nor the org endpoint:
    the answer names it and points to get_github_branch_rules, not a permission."""
    fn = _get_fn(_executor(), "get_github_ruleset")
    calls: list[str] = []

    async def fake_request(client, method, url, provider, action, **kwargs):
        calls.append(url)
        if url.endswith("/repos/octo/repo/rulesets/77"):
            return (None, "GitHub API error (404): Not Found")
        assert url.endswith("/repos/octo/repo/rulesets")
        assert kwargs["params"]["includes_parents"] == "true"
        return (
            _mock_response([{"id": 77, "name": "Protect", "source_type": "Enterprise", "source": "filigran"}]),
            None,
        )

    with patch("app.integrations.providers.github._common.safe_request", side_effect=fake_request):
        result = await fn(ruleset_id=77, repo="octo/repo")

    assert len(calls) == 2
    assert "enterprise ruleset (Enterprise: filigran)" in result
    assert "get_github_branch_rules" in result
    assert "Administration" not in result


@pytest.mark.asyncio
async def test_get_ruleset_repo_error_keeps_hint_when_not_enterprise():
    fn = _get_fn(_executor(), "get_github_ruleset")

    async def fake_request(client, method, url, provider, action, **kwargs):
        if url.endswith("/rulesets/77"):
            return (None, "GitHub API error (403): Resource not accessible by integration")
        return (_mock_response([{"id": 5, "source_type": "Enterprise", "source": "x"}]), None)

    with patch("app.integrations.providers.github._common.safe_request", side_effect=fake_request):
        result = await fn(ruleset_id=77, repo="octo/repo")

    assert "enterprise ruleset (" not in result
    assert "Administration: Read" in result


@pytest.mark.asyncio
async def test_get_ruleset_org_error_mentions_enterprise_rulesets():
    fn = _get_fn(_executor(), "get_github_ruleset")

    async def fake_request(client, method, url, provider, action, **kwargs):
        return (None, "GitHub API error (403): Resource not accessible by integration")

    with patch("app.integrations.providers.github._common.safe_request", side_effect=fake_request):
        result = await fn(ruleset_id=77, org="octo")

    assert "Organization -> Administration: Read" in result
    assert "source Enterprise" in result
    assert "get_github_branch_rules" in result


@pytest.mark.asyncio
async def test_branch_protection_falls_back_to_ruleset_when_classic_absent():
    """The audit gap: a branch governed only by a Ruleset must NOT be reported
    as 'not protected' - branch protection falls back to the effective rules."""
    fn = _get_fn(_executor(), "get_github_branch_protection")

    async def fake_request(client, method, url, provider, action, **kwargs):
        if url.endswith("/protection"):
            # Classic branch protection absent (404) - returned with expected_statuses.
            return (
                _mock_response({"message": "Branch not protected"}, status=404),
                "GitHub API error (404): Not Found",
            )
        if "/rules/branches/" in url:
            return (_mock_response(_BRANCH_RULES_PAYLOAD), None)
        return (_mock_response({}), None)

    with patch("app.integrations.providers.github._common.safe_request", side_effect=fake_request):
        result = await fn(repo="octo/repo", branch="main")

    assert "no classic branch-protection rule" in result
    assert "governed by a Ruleset" in result
    assert "2 approving review(s)" in result
    assert "is not protected" not in result


@pytest.mark.asyncio
async def test_branch_protection_unprotected_when_no_classic_and_no_ruleset():
    fn = _get_fn(_executor(), "get_github_branch_protection")

    async def fake_request(client, method, url, provider, action, **kwargs):
        if url.endswith("/protection"):
            return (
                _mock_response({"message": "Branch not protected"}, status=404),
                "GitHub API error (404): Not Found",
            )
        if "/rules/branches/" in url:
            return (_mock_response([]), None)  # no rules apply
        return (_mock_response({}), None)

    with patch("app.integrations.providers.github._common.safe_request", side_effect=fake_request):
        result = await fn(repo="octo/repo", branch="main")

    assert "is not protected" in result
    assert "list_github_rulesets" in result


@pytest.mark.asyncio
async def test_branch_protection_reports_unverified_ruleset_on_permission_error():
    """When classic protection is absent AND the ruleset check is denied, the
    tool must NOT claim 'not protected' - it surfaces that ruleset governance is
    unverified plus the permission hint."""
    fn = _get_fn(_executor(), "get_github_branch_protection")

    async def fake_request(client, method, url, provider, action, **kwargs):
        if url.endswith("/protection"):
            return (
                _mock_response({"message": "Branch not protected"}, status=404),
                "GitHub API error (404): Not Found",
            )
        if "/rules/branches/" in url:
            return (None, "GitHub API error (403): Resource not accessible by integration")
        return (_mock_response({}), None)

    with patch("app.integrations.providers.github._common.safe_request", side_effect=fake_request):
        result = await fn(repo="octo/repo", branch="main")

    assert "could not be verified" in result
    assert "Administration: Read" in result
    # Critically, it must NOT assert the branch is unprotected when we couldn't check.
    assert "is not protected" not in result


# ---------------------------------------------------------------------------
# merge_github_pr – HTTP 405 actionable error guidance
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_merge_github_pr_405_base_branch_modified_while_mergeability_unknown():
    """HTTP 405 'Base branch was modified' while GitHub never finishes
    recomputing mergeability: an actionable "NOT merged" naming get_github_pr,
    no branch update suggested for what may be a clean PR, no second PUT."""
    fn = _get_fn(_executor(), "merge_github_pr")

    merge_kwargs: list[dict] = []

    async def fake_request(client, method, url, provider, action, **kwargs):
        if "/pulls/" in url and url.endswith("/merge"):
            merge_kwargs.append(kwargs)
            msg = "Base branch was modified. Review and try the merge again."
            return (
                _mock_response({"message": msg}, status=405),
                f"Error: GitHub returned HTTP 405 while trying to {action}. {msg}",
            )
        return (_mock_response({}), None)

    with (
        patch("app.integrations.providers.github._common.safe_request", side_effect=fake_request),
        patch("app.integrations.providers.github.pulls._BASE_MODIFIED_POLL_DELAYS", (0.0, 0.0)),
    ):
        result = await fn(repo="octo/repo", number=42)

    # The merge call must opt into receiving the failed response with an
    # EMPTY expected_statuses set — receiving the body for branching without
    # suppressing the 405 warning log.
    assert merge_kwargs and merge_kwargs[0].get("expected_statuses") == set()
    assert len(merge_kwargs) == 1

    # Must contain the actionable guidance — not just a raw HTTP error string.
    assert "NOT merged" in result
    assert "get_github_pr" in result
    assert "No branch update is needed for a clean PR" in result
    assert ".." not in result
    # Must not claim success.
    assert result.startswith("Error")


@pytest.mark.asyncio
async def test_merge_github_pr_405_not_mergeable_returns_update_branch_guidance():
    """HTTP 405 with a generic 'not mergeable' message (no conflict keyword)
    should get the same re-fetch + update_branch guidance as the
    'Base branch was modified' case."""
    fn = _get_fn(_executor(), "merge_github_pr")

    async def fake_request(client, method, url, provider, action, **kwargs):
        if "/pulls/" in url and url.endswith("/merge"):
            msg = "Pull Request is not mergeable"
            return (
                _mock_response({"message": msg}, status=405),
                f"Error: GitHub returned HTTP 405 while trying to {action}. {msg}",
            )
        return (_mock_response({}), None)

    with patch("app.integrations.providers.github._common.safe_request", side_effect=fake_request):
        result = await fn(repo="octo/repo", number=99)

    # Non-conflict 405: guided to re-fetch state and sync the branch.
    assert "update_branch" in result
    assert "get_github_pr" in result
    # GitHub's message lacks trailing punctuation — a period must be added
    # so the guidance sentence doesn't run into it.
    assert "not mergeable. Re-fetch" in result
    assert "merged" not in result.lower() or "cannot merge" in result.lower()


@pytest.mark.asyncio
async def test_merge_github_pr_405_merge_conflicts_guidance():
    """HTTP 405 'Pull Request has merge conflicts' should advise manual conflict
    resolution rather than an automatic update_branch retry."""
    fn = _get_fn(_executor(), "merge_github_pr")

    async def fake_request(client, method, url, provider, action, **kwargs):
        if "/pulls/" in url and url.endswith("/merge"):
            msg = "Pull Request has merge conflicts"
            return (
                _mock_response({"message": msg}, status=405),
                f"Error: GitHub returned HTTP 405 while trying to {action}. {msg}",
            )
        return (_mock_response({}), None)

    with patch("app.integrations.providers.github._common.safe_request", side_effect=fake_request):
        result = await fn(repo="octo/repo", number=77)

    # Conflict path: must mention conflicts and NOT suggest update_branch as the fix.
    assert "conflict" in result.lower()
    assert "update_branch" not in result
    # GitHub's message lacks trailing punctuation — a period must be added
    # before the follow-on guidance sentence.
    assert "merge conflicts. The PR" in result


@pytest.mark.asyncio
async def test_merge_github_pr_success_path_unchanged():
    """Successful merges still return the normal success message."""
    fn = _get_fn(_executor(), "merge_github_pr")

    async def fake_request(client, method, url, provider, action, **kwargs):
        if "/pulls/" in url and url.endswith("/merge"):
            return (
                _mock_response(
                    {"sha": "abc123", "merged": True, "message": "Pull Request successfully merged"}, status=200
                ),
                None,
            )
        return (_mock_response({}), None)

    with patch("app.integrations.providers.github._common.safe_request", side_effect=fake_request):
        result = await fn(repo="octo/repo", number=55, merge_method="squash")

    assert "merged" in result.lower()
    assert "squash" in result


# ---------------------------------------------------------------------------
# get_github_pr — 404 returns actionable guidance, no leading "Error"
# (guards against agents hallucinating/guessing PR numbers)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_github_pr_404_returns_not_found_guidance():
    """A 404 on get_github_pr returns a 'PR not found' message (no leading
    'Error' to keep WARNING logs silent) with explicit guidance to use
    list_github_prs instead of guessing/incrementing PR numbers."""
    fn = _get_fn(_executor(), "get_github_pr")
    captured_kwargs: dict = {}

    async def fake_request(client, method, url, provider, action, **kwargs):
        err = "Error: GitHub resource not found while trying to read PR #17135 in OpenCTI-Platform/opencti. Detail: Not Found"
        # Honour the real safe_request contract: the failed httpx.Response is
        # only returned when the caller opts in via expected_statuses — without
        # the kwarg the tool gets (None, err) and the 404 branch can never run.
        if kwargs.get("expected_statuses") is None:
            return (None, err)
        captured_kwargs.update(kwargs)
        return (_mock_response({"message": "Not Found"}, status=404), err)

    with patch(
        "app.integrations.providers.github._common.safe_request",
        side_effect=fake_request,
    ):
        result = await fn(repo="OpenCTI-Platform/opencti", number=17135)

    # The tool must opt into receiving the failed response with the 404 flagged
    # as expected (this is also what suppresses the safe_request WARNING log).
    assert captured_kwargs.get("expected_statuses") == {404}
    # Must NOT start with "Error" — agent_loop logs any tool result starting
    # with "Error" (no colon required) at WARNING level.
    assert not result.startswith("Error")
    # Must contain the PR number and repo for clarity
    assert "17135" in result
    assert "OpenCTI-Platform/opencti" in result
    # Must guide the agent to use the listing tool instead of guessing
    assert "list_github_prs" in result


@pytest.mark.asyncio
async def test_get_github_pr_non_404_error_passes_through():
    """Non-404 errors from get_github_pr (e.g. 403, 500) pass through unchanged.

    Covers both shapes of the safe_request contract with expected_statuses:
    a failed httpx.Response for a non-2xx status, and None for network errors.
    """
    fn = _get_fn(_executor(), "get_github_pr")
    original_err = "Error: GitHub server error (500) while trying to read PR #42 in octo/repo."

    for failed_resp in (_mock_response({"message": "boom"}, status=500), None):

        async def fake_request(client, method, url, provider, action, _resp=failed_resp, **kwargs):
            return (_resp, original_err)

        with patch(
            "app.integrations.providers.github._common.safe_request",
            side_effect=fake_request,
        ):
            result = await fn(repo="octo/repo", number=42)

        assert result == original_err


# ---------------------------------------------------------------------------
# get_github_issue — 404 returns actionable guidance, no leading "Error"
# (guards against agents hallucinating/guessing issue numbers)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_github_issue_404_returns_not_found_guidance():
    """A 404 on get_github_issue returns an 'Issue not found' message (no
    leading 'Error' to keep WARNING logs silent) with explicit guidance to use
    search_github_issues instead of guessing/incrementing issue numbers."""
    fn = _get_fn(_executor(), "get_github_issue")
    captured_kwargs: dict = {}

    async def fake_request(client, method, url, provider, action, **kwargs):
        err = "Error: GitHub resource not found while trying to read issue #17133 in OpenCTI-Platform/opencti. Detail: Not Found"
        # Honour the real safe_request contract: the failed httpx.Response is
        # only returned when the caller opts in via expected_statuses — without
        # the kwarg the tool gets (None, err) and the 404 branch can never run.
        if kwargs.get("expected_statuses") is None:
            return (None, err)
        captured_kwargs.update(kwargs)
        return (_mock_response({"message": "Not Found"}, status=404), err)

    with patch(
        "app.integrations.providers.github._common.safe_request",
        side_effect=fake_request,
    ):
        result = await fn(repo="OpenCTI-Platform/opencti", number=17133)

    # The tool must opt into receiving the failed response with the 404 flagged
    # as expected (this is also what suppresses the safe_request WARNING log).
    assert captured_kwargs.get("expected_statuses") == {404}
    # Must NOT start with "Error" — agent_loop logs any tool result starting
    # with "Error" (no colon required) at WARNING level.
    assert not result.startswith("Error")
    # Must contain the issue number and repo for clarity
    assert "17133" in result
    assert "OpenCTI-Platform/opencti" in result
    # Must guide the agent to use the search tool instead of guessing
    assert "search_github_issues" in result


@pytest.mark.asyncio
async def test_get_github_issue_non_404_error_passes_through():
    """Non-404 errors from get_github_issue (e.g. 403, 500) pass through unchanged.

    Covers both shapes of the safe_request contract with expected_statuses:
    a failed httpx.Response for a non-2xx status, and None for network errors.
    """
    fn = _get_fn(_executor(), "get_github_issue")
    original_err = "Error: GitHub server error (500) while trying to read issue #7 in octo/repo."

    for failed_resp in (_mock_response({"message": "boom"}, status=500), None):

        async def fake_request(client, method, url, provider, action, _resp=failed_resp, **kwargs):
            return (_resp, original_err)

        with patch(
            "app.integrations.providers.github._common.safe_request",
            side_effect=fake_request,
        ):
            result = await fn(repo="octo/repo", number=7)

        assert result == original_err


# ---------------------------------------------------------------------------
# list_github_prs author / head prefix filters and include_preview
# ---------------------------------------------------------------------------


def _pr_row(number: int, login: str, head: str, body: str = "Body text") -> dict:
    return {
        "number": number,
        "title": f"PR {number}",
        "state": "open",
        "user": {"login": login},
        "head": {"ref": head},
        "base": {"ref": "main"},
        "labels": [],
        "requested_reviewers": [],
        "body": body,
    }


def _paged_pulls(pages: list[list[dict]], calls: list[dict]):
    async def fake_request(client, method, url, provider, action, **kwargs):
        params = kwargs.get("params") or {}
        calls.append(params)
        page = params.get("page", 1)
        return (_mock_response(pages[page - 1] if page <= len(pages) else []), None)

    return fake_request


@pytest.mark.asyncio
async def test_list_github_prs_filters_by_author_across_pages():
    fn = _get_fn(_executor(), "list_github_prs")
    page1 = [_pr_row(i, "teammate", f"feat/{i}") for i in range(1, 100)] + [_pr_row(100, "SamuelHassine", "fix/a")]
    page2 = [_pr_row(200, "samuelhassine", "fix/b"), _pr_row(201, "teammate", "feat/x")]
    calls: list[dict] = []
    with patch("app.integrations.providers.github._common.safe_request", side_effect=_paged_pulls([page1, page2], calls)):
        result = await fn(repo="octo/repo", author="@SamuelHassine")

    assert [c["page"] for c in calls] == [1, 2]
    assert calls[0]["per_page"] == 100
    assert "#100 " in result and "#200 " in result
    assert "teammate" not in result
    assert "Scanned" not in result  # the listing was read to its end


@pytest.mark.asyncio
async def test_list_github_prs_head_prefix_and_no_preview():
    fn = _get_fn(_executor(), "list_github_prs")
    rows = [_pr_row(1, "a", "fix/one"), _pr_row(2, "a", "feat/two"), _pr_row(3, "b", "fix/three")]
    calls: list[dict] = []
    with patch("app.integrations.providers.github._common.safe_request", side_effect=_paged_pulls([rows], calls)):
        result = await fn(repo="octo/repo", head_prefix="fix/", include_preview=False)

    assert result.splitlines() == [
        "#1 PR 1 [open] by a (fix/one → main)",
        "#3 PR 3 [open] by b (fix/three → main)",
    ]
    assert "Body text" not in result


@pytest.mark.asyncio
async def test_list_github_prs_filter_says_when_the_scan_stopped_at_its_bound():
    fn = _get_fn(_executor(), "list_github_prs")
    full = [_pr_row(i, "teammate", f"feat/{i}") for i in range(100)]
    calls: list[dict] = []
    with patch("app.integrations.providers.github._common.safe_request", side_effect=_paged_pulls([full] * 5, calls)):
        result = await fn(repo="octo/repo", author="someone")

    assert len(calls) == 3
    assert result.startswith("No open pull requests by someone among the 300 most recent")


@pytest.mark.asyncio
async def test_list_github_prs_unfiltered_keeps_one_page_of_count():
    fn = _get_fn(_executor(), "list_github_prs")
    calls: list[dict] = []
    with patch(
        "app.integrations.providers.github._common.safe_request",
        side_effect=_paged_pulls([[_pr_row(1, "a", "x")]], calls),
    ):
        result = await fn(repo="octo/repo", count=5)

    assert len(calls) == 1
    assert calls[0]["per_page"] == 5
    assert "Body text" in result


# ---------------------------------------------------------------------------
# search_github_issues: its matches are structured, so an activation check
# skips the run when nothing matches
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_search_github_issues_with_no_match_lets_an_activation_check_skip_the_run():
    from app.services.assignment_activation_check import condition_met, structured_result

    fn = _get_fn(_executor(), "search_github_issues")
    request = AsyncMock(return_value=(_mock_response({"items": [], "total_count": 0}), None))
    with patch("app.integrations.providers.github._common.safe_request", request):
        result = await fn(repo="acme/api", query="is:pr label:agent:auto-merge")

    assert result.content == "No issues found."
    data, readable = structured_result(result)
    assert readable is True
    assert condition_met(data, "non_empty") is False


@pytest.mark.asyncio
async def test_search_github_issues_structures_each_match():
    from app.services.assignment_activation_check import condition_met, structured_result

    item = {
        "number": 42,
        "title": "Fix the drawer",
        "state": "open",
        "user": {"login": "octocat"},
        "labels": [{"name": "agent:auto-merge"}],
        "assignees": [],
        "pull_request": {"url": "https://api.github.com/repos/acme/api/pulls/42"},
        "html_url": "https://github.com/acme/api/pull/42",
        "body": "",
    }
    fn = _get_fn(_executor(), "search_github_issues")
    request = AsyncMock(return_value=(_mock_response({"items": [item], "total_count": 1}), None))
    with patch("app.integrations.providers.github._common.safe_request", request):
        result = await fn(repo="acme/api", query="is:pr")

    assert "#42 Fix the drawer" in result.content
    assert result.structured == [
        {
            "number": 42,
            "title": "Fix the drawer",
            "state": "open",
            "author": "octocat",
            "labels": ["agent:auto-merge"],
            "is_pull_request": True,
            "url": "https://github.com/acme/api/pull/42",
        }
    ]
    data, readable = structured_result(result)
    assert readable is True and condition_met(data, "non_empty") is True
