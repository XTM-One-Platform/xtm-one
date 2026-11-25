"""GitHub refusals and redirects name what the caller needs to act (#2155).

- a team slug of an enterprise team (``ent:<slug>``, as ``list_github_teams``
  returns it) is accepted by the team tools;
- a 301 for a transferred or renamed repository names its new ``owner/name``;
- a ref write a ruleset refuses ("Reference update failed") names the active
  rulesets of that kind of ref.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import httpx
import pytest

from app.integrations.http import format_http_error
from app.integrations.providers.github import _common, branches, build_tools

_SAFE_REQUEST = "app.integrations.providers.github._common.safe_request"
_HTTP_SAFE_REQUEST = "app.integrations.providers.github._common._http_safe_request"

API = "https://api.github.com/repos/acme/app"


def _handler(name: str):
    executor = MagicMock()
    executor.register = MagicMock()
    build_tools("fake-token", executor)
    return next(call[0][1] for call in executor.register.call_args_list if call[0][0] == name)


# --- team slugs ---------------------------------------------------------------


@pytest.mark.parametrize("slug", ["ent:openaev", "ENT:open-aev_team", "platform", "dev_team-2"])
def test_a_team_slug_list_github_teams_returns_is_accepted(slug):
    assert _common.team_slug_error(slug) is None


@pytest.mark.parametrize("slug", ["", "ent:", "a/b", "ent:../x", "{{team}}", "two words"])
def test_a_malformed_team_slug_is_refused_with_the_form_to_pass(slug):
    err = _common.team_slug_error(slug)
    assert err and err.startswith("Error:")


@pytest.mark.asyncio
async def test_get_github_team_reads_an_enterprise_team_slug():
    seen: list[str] = []

    async def fake(client, method, url, provider, action, **kwargs):
        seen.append(url)
        if url.endswith("/teams/ent:openaev"):
            return httpx.Response(200, json={"name": "OpenAEV", "slug": "ent:openaev"}), None
        return httpx.Response(200, json=[]), None

    with patch(_SAFE_REQUEST, new=fake):
        result = await _handler("get_github_team")(org="acme", team_slug="ent:openaev")

    assert not result.startswith("Error"), result
    assert seen[0] == "https://api.github.com/orgs/acme/teams/ent:openaev"


# --- moved repositories -------------------------------------------------------


def _redirect(location: str) -> httpx.Response:
    return httpx.Response(
        301,
        headers={"location": location},
        json={"message": "Moved Permanently", "url": location},
        request=httpx.Request("GET", f"{API}"),
    )


def test_a_redirect_error_names_its_location():
    err = format_http_error(_redirect("https://api.github.com/repositories/42"), "GitHub", "get repo")
    assert "HTTP 301" in err and "Moved to: https://api.github.com/repositories/42" in err


@pytest.mark.asyncio
async def test_a_moved_repository_answers_its_new_name():
    async def fake(client, method, url, provider, action, **kwargs):
        if url == f"{API}":
            return None, format_http_error(_redirect("https://api.github.com/repositories/42"), provider, action)
        if url == "https://api.github.com/repositories/42":
            return httpx.Response(200, json={"full_name": "neworg/app"}), None
        raise AssertionError(url)

    with patch(_HTTP_SAFE_REQUEST, new=fake):
        _resp, err = await _common.safe_request(None, "get", API, "GitHub", "get repo", headers={})

    assert err and "it is now neworg/app" in err and "repo='neworg/app'" in err


@pytest.mark.asyncio
async def test_an_unreadable_moved_repository_keeps_the_redirect_error():
    async def fake(client, method, url, provider, action, **kwargs):
        if url == f"{API}":
            return None, format_http_error(_redirect("https://api.github.com/repositories/42"), provider, action)
        return None, "Error: GitHub not found"

    with patch(_HTTP_SAFE_REQUEST, new=fake):
        _resp, err = await _common.safe_request(None, "get", API, "GitHub", "get repo", headers={})

    assert err and "Moved to: https://api.github.com/repositories/42" in err and "it is now" not in err


# --- rulesets behind a ref refusal --------------------------------------------

_RULESETS = [
    {"id": 27712721, "name": "release tags", "target": "tag", "enforcement": "active", "source_type": "Repository"},
    {"id": 1, "name": "main", "target": "branch", "enforcement": "active", "source_type": "Organization"},
    {"id": 2, "name": "old tags", "target": "tag", "enforcement": "disabled", "source_type": "Repository"},
]


def test_rulesets_line_names_the_active_rulesets_of_the_target():
    line = branches.rulesets_line(_RULESETS, "tag")
    assert "'release tags' (id 27712721, repository)" in line
    assert "old tags" not in line and "'main'" not in line
    assert branches.rulesets_line(_RULESETS, "push") == ""


@pytest.mark.asyncio
async def test_a_refused_tag_create_names_the_tag_ruleset():
    calls: list[tuple[str, str]] = []

    async def fake(client, method, url, provider, action, **kwargs):
        calls.append((method, url))
        if method == "post" and url == f"{API}/git/refs":
            return (
                None,
                "Error: GitHub rejected the request to create tag v1 in acme/app — validation error. Reference update failed",
            )
        if method == "get" and url == f"{API}/rulesets":
            assert kwargs["params"]["includes_parents"] == "true"
            return httpx.Response(200, json=_RULESETS), None
        raise AssertionError(f"{method} {url}")

    with patch(_SAFE_REQUEST, new=fake):
        result = await _handler("manage_github_ref")(repo="acme/app", action="create_tag", tag="v1", sha="a" * 40)

    assert "Reference update failed" in result
    assert "'release tags' (id 27712721" in result and "get_github_ruleset" in result


@pytest.mark.asyncio
async def test_another_ref_error_reads_no_ruleset():
    async def fake(client, method, url, provider, action, **kwargs):
        if method == "delete":
            return None, "Error: GitHub resource not found while trying to delete tag v1."
        raise AssertionError(f"{method} {url}")

    with patch(_SAFE_REQUEST, new=fake):
        result = await _handler("manage_github_ref")(repo="acme/app", action="delete_tag", tag="v1")

    assert result == "Error: GitHub resource not found while trying to delete tag v1."
