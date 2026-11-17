"""A bare HTTP status number counts only where it reads as a status.

``is_auth_or_permission_error`` / ``is_rate_limit_error`` matched ``401`` /
``429`` anywhere a word boundary allowed, so an identifier the platform itself
writes into an error statement read as a status: ``safe_request`` passes the
tool's ``action`` to ``format_http_error``, the GitHub tools interpolate
``PR #{number}`` / ``project #{num}`` into it, and a transient 502 on PR #401
took the tool away for the 8-iteration auth cooldown. The number now counts
only in the contexts real failure messages use.

The contexts come from the shapes the platform meets: its own formatters and
wrappers, the MCP client, and the httpx / requests / aiohttp / axios / LLM SDK
messages a tool can surface. Every shape below lacks an auth / rate-limit
keyword on purpose, so it is the NUMBER that is classified.
"""

from __future__ import annotations

import httpx
import pytest

from app.integrations.http import format_http_error
from app.tools.base import (
    is_auth_failure_result,
    is_auth_or_permission_error,
    is_error_shaped_result,
    is_rate_limit_error,
)

# ``{n}`` is replaced by 401 for the auth classifier and 429 for the
# rate-limit classifier: the two share one rule.
_STATUS_SHAPES = {
    "opens_the_message": "{n}",
    "opens_with_reason_text": "{n}, message='Bad key', url=URL('https://api.example.test/x')",
    "requests_client_error": "{n} Client Error: for url: https://api.example.test/x",
    "parenthesised": "Request rejected ({n})",
    "http": "HTTP {n}",
    "http_version": "HTTP/1.1 {n}",
    "status": "upstream answered with status {n}",
    "status_code_axios": "Request failed with status code {n}",
    "status_code_assign": "status_code={n}",
    "status_code_json": '{{"statusCode": {n}, "message": "rejected"}}',
    "status_json": '{{"status": {n}, "detail": "rejected"}}',
    "code_json_nested": '{{"error": {{"code": {n}, "message": "rejected"}}}}',
    "sdk_error_code": "Error code: {n} - {{'error': {{'message': 'Incorrect API key provided'}}}}",
    "api_error": "GitHub API error: {n}",
    "error_number": "Error {n} on GET /repos",
    "response_code": "Response code {n}",
}

# Identifiers and counts that merely CONTAIN the digits.
_ID_SHAPES = {
    "github_pr_in_action": (
        "server error (502) while trying to enable auto-merge on PR #{n}. "
        "The service may be temporarily unavailable."
    ),
    "github_project_in_action": "server error (502) while trying to resolve project #{n} for octo.",
    "jira_key": "resource not found while trying to get issue PROJ-{n}.",
    "url_path": "returned HTTP 409 while trying to merge /repos/o/r/pulls/{n}. Conflict",
    "github_graphql_number": "Could not resolve to a PullRequest with the number of {n}.",
    "thousands": "Context window exceeded by ~12,{n} tokens",
    "issue_number_in_statement": "Error: Issue {n} not found",
}


@pytest.mark.parametrize("shape", _STATUS_SHAPES.values(), ids=_STATUS_SHAPES.keys())
def test_a_status_401_is_still_an_auth_failure(shape):
    assert is_auth_or_permission_error(shape.format(n=401)) is True


@pytest.mark.parametrize("shape", _STATUS_SHAPES.values(), ids=_STATUS_SHAPES.keys())
def test_a_status_429_is_still_a_rate_limit(shape):
    assert is_rate_limit_error(shape.format(n=429)) is True


@pytest.mark.parametrize("shape", _ID_SHAPES.values(), ids=_ID_SHAPES.keys())
def test_an_identifier_401_is_not_an_auth_failure(shape):
    content = shape.format(n=401)
    assert is_auth_or_permission_error(content) is False
    assert is_auth_failure_result(content) is False


@pytest.mark.parametrize("shape", _ID_SHAPES.values(), ids=_ID_SHAPES.keys())
def test_an_identifier_429_is_not_a_rate_limit(shape):
    assert is_rate_limit_error(shape.format(n=429)) is False


def _http_error(status: int, body: dict, action: str) -> str:
    response = httpx.Response(
        status,
        request=httpx.Request("POST", "https://api.github.com/graphql"),
        json=body,
    )
    return format_http_error(response, "GitHub", action)


@pytest.mark.parametrize("number", [401, 429])
def test_a_transient_github_failure_on_a_numbered_pr_is_neither(number):
    """The reported shape: a 502 on ``enable auto-merge on PR #<n>``."""
    content = _http_error(502, {}, f"enable auto-merge on PR #{number}")
    assert f"PR #{number}" in content
    # Still an error - only the auth / rate-limit reading changes.
    assert is_error_shaped_result(content) is True
    assert is_auth_or_permission_error(content) is False
    assert is_rate_limit_error(content) is False
    assert is_auth_failure_result(content) is False
