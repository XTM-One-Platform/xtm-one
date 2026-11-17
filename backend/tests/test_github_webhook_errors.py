"""GitHub 403s on webhook creation map to the one fix that applies."""

import httpx
import pytest

from app.api.v1.integration_github_webhooks import _forbidden_detail


def _resp(message: str, headers: dict[str, str] | None = None) -> httpx.Response:
    return httpx.Response(403, json={"message": message}, headers=headers or {})


@pytest.mark.parametrize(
    ("message", "headers", "token", "expected"),
    [
        ("API rate limit exceeded for installation.", {}, "ghu_x", "rate limit reached"),
        ("Forbidden", {"x-ratelimit-remaining": "0"}, "ghu_x", "rate limit reached"),
        (
            "Resource protected by organization SAML enforcement.",
            {"x-github-sso": "required; url=https://github.com/orgs/acme/sso?authorization_request=1"},
            "ghu_x",
            "https://github.com/orgs/acme/sso?authorization_request=1",
        ),
        (
            "Although you appear to have the correct authorization credentials, the `acme` "
            "organization has an IP allow list enabled, and your IP address is not permitted.",
            {},
            "ghu_x",
            "IP allow list",
        ),
        (
            "Although you appear to have the correct authorization credentials, the `acme` "
            "organization has enabled OAuth App access restrictions.",
            {},
            "gho_x",
            "restricts third-party OAuth apps",
        ),
        ("Must have admin rights to Repository.", {}, "ghu_x", "not an administrator"),
        ("Resource not accessible by personal access token", {}, "github_pat_x", "fine-grained"),
        ("Forbidden", {}, "ghp_x", "admin:repo_hook"),
        ("Resource not accessible by integration", {}, "ghu_x", "Webhooks to 'Read and write'"),
    ],
)
def test_forbidden_detail_names_the_fix(message, headers, token, expected):
    detail = _forbidden_detail(_resp(message, headers), "acme/repo", token)
    assert expected in detail
    assert f'GitHub said: "{message}"' in detail


def test_github_app_fallback_names_the_repository():
    detail = _forbidden_detail(_resp("Resource not accessible by integration"), "acme/repo", "ghu_x")
    assert "'acme/repo' is in Repository access" in detail
    assert "Re-authorize this integration" in detail


def test_non_json_body_does_not_crash():
    resp = httpx.Response(403, text="<html>Forbidden</html>")
    assert "GitHub App cannot create webhooks" in _forbidden_detail(resp, "acme/repo", "ghu_x")
