"""GitHub people tools: engineering metrics, AI usage, identities (synthetic data)."""

from __future__ import annotations

from datetime import date
from typing import Any
from unittest.mock import MagicMock, patch

import httpx
import pytest

from app.integrations.providers.github import build_tools
from app.integrations.providers.github.people import collapse_backports, period_buckets
from app.integrations.providers.github.scope import DROPPED_WHEN_SCOPED


def _fn(name: str):
    ex = MagicMock()
    ex.register = MagicMock()
    build_tools("fake-token", ex)
    for call in ex.register.call_args_list:
        if call[0][0] == name:
            return call[0][1]
    raise KeyError(name)


def _resp(payload: Any) -> httpx.Response:
    return httpx.Response(200, json=payload, request=httpx.Request("GET", "https://api.github.com/x"))


def test_period_buckets_quarters_clip_to_range():
    buckets = period_buckets(date(2024, 2, 10), date(2024, 8, 5), "quarter")
    assert [b[0] for b in buckets] == ["2024-Q1", "2024-Q2", "2024-Q3"]
    assert buckets[0][1] == date(2024, 2, 10)
    assert buckets[-1][2] == date(2024, 8, 5)
    assert len(period_buckets(date(2023, 1, 1), date(2025, 12, 31), "year")) == 3
    assert len(period_buckets(date(2025, 1, 1), date(2025, 12, 31), "month")) == 12


def test_collapse_backports_counts_each_closed_issue_once():
    prs = [
        {"mergedAt": "2025-01-01T00:00:00Z", "issues": [10]},
        {"mergedAt": "2025-01-02T00:00:00Z", "issues": [10]},  # backport of the same fix
        {"mergedAt": "2025-01-03T00:00:00Z", "issues": []},  # no issue: always counts
        {"mergedAt": "2025-01-04T00:00:00Z", "issues": [10, 11]},  # a new issue: counts
    ]
    assert collapse_backports(prs) == 3


@pytest.mark.asyncio
async def test_engineering_metrics_cohort_table():
    fn = _fn("get_github_engineering_metrics")
    seen_queries: list[str] = []

    async def fake_gql(client, headers, query, variables=None, *, action="", **_kw):
        if "issueCount}" in query and "nodes" not in query:
            seen_queries.extend(variables.values())
            return {alias.replace("v", "q"): {"issueCount": 2} for alias in variables}, None
        return (
            {
                "search": {
                    "pageInfo": {"hasNextPage": False, "endCursor": None},
                    "nodes": [
                        {
                            "createdAt": "2025-02-01T00:00:00Z",
                            "mergedAt": "2025-02-02T00:00:00Z",
                            "additions": 10,
                            "deletions": 2,
                            "reviews": {"totalCount": 3},
                            "closingIssuesReferences": {"nodes": [{"number": 5}]},
                        },
                        {
                            "createdAt": "2025-02-03T00:00:00Z",
                            "mergedAt": "2025-02-04T12:00:00Z",
                            "additions": 1,
                            "deletions": 1,
                            "reviews": {"totalCount": 1},
                            "closingIssuesReferences": {"nodes": [{"number": 5}]},
                        },
                    ],
                }
            },
            None,
        )

    with patch("app.integrations.providers.github._common.gh_graphql", side_effect=fake_gql):
        out = await fn(logins=["dev-one", "dev-two"], org="acme", since="2025-01-01", until="2025-06-30")

    assert "| dev-one | 2025-Q1 | 2 | 2 | 1 | 2 | 30.0 | 7.0 | 4 | 2 |" in out
    assert "| dev-one | 2025-Q2 | 2 | 0 | 0 | 2 | - | - | 0 | 2 |" in out
    assert "| **dev-two** | **total** |" in out
    assert any(q.startswith("is:pr reviewed-by:dev-one -author:dev-one org:acme") for q in seen_queries)
    assert "Incomplete" not in out


@pytest.mark.asyncio
async def test_engineering_metrics_total_collapses_a_backport_in_the_next_bucket() -> None:
    fn = _fn("get_github_engineering_metrics")

    def _pr(merged_at: str) -> dict[str, Any]:
        return {
            "createdAt": merged_at,
            "mergedAt": merged_at,
            "additions": 1,
            "deletions": 1,
            "reviews": {"totalCount": 0},
            "closingIssuesReferences": {"nodes": [{"number": 7}]},
        }

    async def fake_gql(
        client: Any, headers: Any, query: str, variables: dict[str, str] | None = None, *, action: str = "", **_kw: Any
    ) -> tuple[dict[str, Any], None]:
        if "issueCount}" in query and "nodes" not in query:
            return {alias.replace("v", "q"): {"issueCount": 0} for alias in variables or {}}, None
        # The fix on the last day of January, its backport on the first day of February.
        nodes = [_pr("2025-01-31T10:00:00Z"), _pr("2025-02-01T10:00:00Z")]
        return {"search": {"pageInfo": {"hasNextPage": False, "endCursor": None}, "nodes": nodes}}, None

    with patch("app.integrations.providers.github._common.gh_graphql", side_effect=fake_gql):
        out = await fn(logins=["dev-one"], org="acme", since="2025-01-01", until="2025-02-28", bucket="month")

    def _unique(label: str) -> str:
        row = next(line for line in out.splitlines() if line.startswith(f"| {label} |"))
        return str(row.split("|")[5].strip())

    assert _unique("dev-one | 2025-01") == "1"
    assert _unique("dev-one | 2025-02") == "1"
    total = next(line for line in out.splitlines() if line.startswith("| **dev-one** | **total** |"))
    assert total.split("|")[4].strip() == "2"  # merged: both PRs
    assert total.split("|")[5].strip() == "1"  # unique: one closed issue


@pytest.mark.asyncio
async def test_engineering_metrics_validates_inputs():
    fn = _fn("get_github_engineering_metrics")
    assert "exactly one" in await fn(logins=["dev-one"])
    assert "exactly one" in await fn(logins=["dev-one"], org="acme", repo="acme/x")
    assert "bucket" in await fn(logins=["dev-one"], org="acme", bucket="week")
    assert "at most 20" in await fn(logins=[f"dev-{i}" for i in range(21)], org="acme")
    assert "before since" in await fn(logins=["dev-one"], org="acme", since="2025-02-01", until="2025-01-01")


@pytest.mark.asyncio
async def test_ai_usage_seats_report_and_trailers():
    fn = _fn("get_github_ai_usage")

    async def fake_request(client, method, url, provider, action, **kwargs):
        if url.endswith("/copilot/billing/seats"):
            return _resp(
                {
                    "seats": [
                        {
                            "assignee": {"login": "dev-one"},
                            "plan_type": "business",
                            "created_at": "2025-01-01T00:00:00Z",
                            "last_activity_at": "2025-06-01T00:00:00Z",
                            "last_activity_editor": "vscode",
                        }
                    ]
                }
            ), None
        if "users-28-day" in url:
            return None, "GitHub API error (403): Resource not accessible by integration"
        q = kwargs["params"]["q"]
        if q.startswith('"co-authored-by"'):
            return _resp(
                {
                    "items": [
                        {"commit": {"message": "fix: a\n\nCo-authored-by: Claude <noreply@anthropic.com>"}},
                        {"commit": {"message": "fix: b\n\nCo-authored-by: a-human <h@example.com>"}},
                    ]
                }
            ), None
        return _resp({"total_count": 4}), None

    with patch("app.integrations.providers.github._common.safe_request", side_effect=fake_request):
        out = await fn(org="acme", logins=["dev-one", "dev-two"], since="2025-01-01", until="2025-06-30")

    assert "dev-one | plan business" in out
    assert "No Copilot seat: dev-two" in out
    assert "Copilot usage report: not readable" in out
    assert "dev-one: 4 commit(s), 1 with an AI co-author (25%): Claude 1" in out


@pytest.mark.asyncio
async def test_lookup_identities_saml_then_verified_email():
    fn = _fn("lookup_github_identities")

    async def fake_gql(client, headers, query, variables=None, *, action="", **_kw):
        if "samlIdentityProvider" in query:
            return {
                "organization": {
                    "samlIdentityProvider": {
                        "externalIdentities": {
                            "pageInfo": {"hasNextPage": False},
                            "nodes": [
                                {
                                    "samlIdentity": {"nameId": "a@example.com", "emails": []},
                                    "user": {"login": "dev-one"},
                                }
                            ],
                        }
                    }
                }
            }, None
        return {
            "organization": {
                "membersWithRole": {
                    "pageInfo": {"hasNextPage": False},
                    "nodes": [{"login": "dev-two", "email": "", "organizationVerifiedDomainEmails": ["b@example.com"]}],
                }
            }
        }, None

    with patch("app.integrations.providers.github._common.gh_graphql", side_effect=fake_gql):
        out = await fn(org="acme", emails=["a@example.com", "b@example.com", "c@example.com"], logins=["dev-one"])

    assert "a@example.com -> dev-one (SAML)" in out
    assert "b@example.com -> dev-two (verified domain email)" in out
    assert "c@example.com -> not found" in out
    assert "dev-one -> a@example.com (SAML)" in out


@pytest.mark.asyncio
async def test_lookup_identities_without_saml_says_why():
    fn = _fn("lookup_github_identities")

    async def fake_gql(client, headers, query, variables=None, *, action="", **_kw):
        if "samlIdentityProvider" in query:
            return {"organization": {"samlIdentityProvider": None}}, None
        return {"organization": {"membersWithRole": {"pageInfo": {"hasNextPage": False}, "nodes": []}}}, None

    with patch("app.integrations.providers.github._common.gh_graphql", side_effect=fake_gql):
        out = await fn(org="acme", emails=["a@example.com"])

    assert "a@example.com -> not found" in out
    assert "no SAML identity provider" in out


def test_org_wide_people_tools_withdrawn_under_a_repository_scope():
    assert {"get_github_engineering_metrics", "get_github_ai_usage"} <= DROPPED_WHEN_SCOPED
