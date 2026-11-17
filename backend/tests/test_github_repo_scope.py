"""The GitHub repository Scope(s) is a hard boundary.

A scheduled assignment scoped to one repository used to enumerate and work on
every repository the personal access token could reach: the scope was prompt
text only. With a repository scope set, the GitHub tools refuse any other
repository, list only the scoped ones, pin free-text searches to them and
withdraw the tools that reach repositories without naming one.
"""

from __future__ import annotations

import base64
import contextlib
import json
import uuid
from collections.abc import Callable, Iterator
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from app.integrations.providers.github import _common, build_tools
from app.integrations.providers.github.scope import (
    DROPPED_WHEN_SCOPED,
    GITHUB_REPOSITORY_SCOPE_KEY,
    repo_scope_from_bindings,
)
from app.services.integration_tools import _SafeIntegrationExecutor
from app.tools.base import ToolCall

SCOPE = frozenset({"acme/api"})


def _binding(full_name: str, *, via_metadata: bool = True) -> dict:
    item = {"id": full_name, "name": full_name.split("/")[-1]}
    if via_metadata:
        item["metadata"] = {"full_name": full_name}
    return item


# ---------------------------------------------------------------------------
# Reading the scope off the element bindings
# ---------------------------------------------------------------------------


def test_no_repository_binding_means_no_scope():
    assert repo_scope_from_bindings(None) is None
    assert repo_scope_from_bindings({}) is None
    assert repo_scope_from_bindings({"slack:slack_channel": [{"id": "C1"}]}) is None


def test_an_empty_stored_selection_is_no_scope_but_a_narrowed_one_is_nothing():
    """A stored ``[]`` is "not selected" (the cards drop an emptied type); an
    empty list in an EFFECTIVE scope comes from a narrowing and allows nothing."""
    from app.services.element_scope import selected_elements

    assert repo_scope_from_bindings(selected_elements({GITHUB_REPOSITORY_SCOPE_KEY: []})) is None
    assert repo_scope_from_bindings({GITHUB_REPOSITORY_SCOPE_KEY: []}) == frozenset()
    assert repo_scope_from_bindings({GITHUB_REPOSITORY_SCOPE_KEY: "junk"}) == frozenset()


def test_scope_reads_full_names_case_insensitively():
    scope = repo_scope_from_bindings(
        {
            GITHUB_REPOSITORY_SCOPE_KEY: [
                _binding("Acme/API"),
                _binding("acme/web", via_metadata=False),
            ]
        }
    )
    assert scope == frozenset({"acme/api", "acme/web"})


def test_a_scope_of_unusable_entries_allows_nothing_instead_of_everything():
    scope = repo_scope_from_bindings(
        {GITHUB_REPOSITORY_SCOPE_KEY: [{"id": "not a repo"}, "junk"]}
    )
    assert scope == frozenset()


# ---------------------------------------------------------------------------
# The toolbox under a scope
# ---------------------------------------------------------------------------


def _scoped_tools(scope=SCOPE):
    executor = _SafeIntegrationExecutor()
    defs = build_tools("gh-token", executor, repo_scope=scope)
    return {d.name: d for d in defs}, executor


def test_tools_that_reach_repositories_without_naming_one_are_withdrawn():
    defs, executor = _scoped_tools()
    for name in DROPPED_WHEN_SCOPED:
        assert name not in defs
        assert name not in executor.tool_names
    # Everything else is still offered.
    assert "get_github_repository" in defs
    assert "list_github_repos" in defs


def test_without_a_scope_the_toolbox_is_unchanged():
    executor = _SafeIntegrationExecutor()
    names = {d.name for d in build_tools("gh-token", executor)}
    assert names >= DROPPED_WHEN_SCOPED


def _ok(payload):
    return httpx.Response(200, json=payload), None


async def _call(executor, tool, **arguments):
    return await executor.execute_tool(ToolCall(id="c1", name=tool, arguments=arguments))


@pytest.mark.asyncio
async def test_a_repository_outside_the_scope_is_refused_before_any_request():
    _, executor = _scoped_tools()
    request = AsyncMock(return_value=_ok({}))
    with patch.object(_common, "safe_request", request):
        result = await _call(executor, "list_github_prs", repo="acme/secret-infra")

    assert result.is_error is True
    assert "outside the Scope(s)" in result.content
    assert "acme/api" in result.content
    request.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_write_outside_the_scope_is_refused():
    _, executor = _scoped_tools()
    request = AsyncMock(return_value=_ok({}))
    with patch.object(_common, "safe_request", request):
        result = await _call(
            executor, "create_github_issue", repo="other/repo", title="x", body="y"
        )
    assert result.is_error is True
    request.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_repository_inside_the_scope_runs_whatever_its_case():
    _, executor = _scoped_tools()
    request = AsyncMock(
        return_value=_ok({"full_name": "acme/api", "default_branch": "main"})
    )
    with patch.object(_common, "safe_request", request):
        result = await _call(executor, "get_github_repository", repo="ACME/api")
    assert result.is_error is False
    request.assert_awaited()


@pytest.mark.asyncio
async def test_creating_a_repository_is_refused_under_a_scope():
    _, executor = _scoped_tools()
    request = AsyncMock(return_value=_ok({}))
    with patch.object(_common, "safe_request", request):
        result = await _call(
            executor, "manage_github_repository", action="create", name="new-repo"
        )
    assert result.is_error is True
    assert "creating a repository" in result.content
    request.assert_not_awaited()


@pytest.mark.asyncio
async def test_forking_a_repository_is_refused_under_a_scope():
    _, executor = _scoped_tools()
    request = AsyncMock(return_value=_ok({}))
    with patch.object(_common, "safe_request", request):
        result = await _call(executor, "manage_github_repository", action="fork", repo="acme/api")
    assert result.is_error is True
    assert "forking a repository" in result.content
    request.assert_not_awaited()


@pytest.mark.asyncio
async def test_list_repos_returns_only_the_scoped_repositories():
    _, executor = _scoped_tools(frozenset({"acme/api", "acme/missing"}))
    listing = [
        {"full_name": "acme/api", "private": True, "language": "Python"},
        {"full_name": "acme/secret-infra", "private": True, "language": "HCL"},
        {"full_name": "someone/else", "private": False},
    ]
    request = AsyncMock(return_value=_ok(listing))
    with patch.object(_common, "safe_request", request):
        result = await _call(executor, "list_github_repos")

    assert result.is_error is False
    assert "acme/api [private]" in result.content
    assert "secret-infra" not in result.content
    assert "someone/else" not in result.content
    # A scoped repository the listing did not return is still named.
    assert "acme/missing (in scope" in result.content


@pytest.mark.asyncio
async def test_free_text_search_is_pinned_to_the_scope():
    _, executor = _scoped_tools()
    request = AsyncMock(return_value=_ok({"items": [], "total_count": 0}))
    with patch.object(_common, "safe_request", request):
        await _call(
            executor,
            "search_github_code",
            query="token repo:acme/secret-infra org:acme user:someone",
        )

    q = request.await_args.kwargs["params"]["q"]
    assert q.startswith("repo:acme/api ")
    assert "secret-infra" not in q
    assert "org:" not in q and "user:" not in q
    assert "token" in q


@pytest.mark.asyncio
async def test_a_multi_repository_scope_searches_only_those_repositories():
    _, executor = _scoped_tools(frozenset({"acme/api", "acme/web"}))
    request = AsyncMock(return_value=_ok({"items": [], "total_count": 0}))
    with patch.object(_common, "safe_request", request):
        await _call(executor, "search_github_commits", query="fix repo:evil/x")

    q = request.await_args.kwargs["params"]["q"]
    assert "evil/x" not in q
    assert "repo:acme/api" in q and "repo:acme/web" in q


@pytest.mark.asyncio
async def test_search_issues_drops_a_widening_qualifier_from_the_query():
    _, executor = _scoped_tools()
    request = AsyncMock(return_value=_ok({"items": [], "total_count": 0}))
    with patch.object(_common, "safe_request", request):
        await _call(
            executor, "search_github_issues", repo="acme/api", query="bug repo:evil/x"
        )
    q = request.await_args.kwargs["params"]["q"]
    assert "evil/x" not in q
    assert q.startswith("repo:acme/api ")


@pytest.mark.asyncio
async def test_an_empty_scope_refuses_every_repository_and_search():
    _, executor = _scoped_tools(frozenset())
    request = AsyncMock(return_value=_ok({}))
    with patch.object(_common, "safe_request", request):
        refused = await _call(executor, "get_github_repository", repo="acme/api")
        searched = await _call(executor, "search_github_code", query="token")
        listed = await _call(executor, "list_github_repos")
    assert refused.is_error is True
    assert searched.is_error is True
    assert "No repository is available" in listed.content
    request.assert_not_awaited()


def test_the_guard_keeps_the_handler_signature_for_argument_filtering():
    """A hallucinated argument is still filtered before the guarded handler."""
    _, executor = _scoped_tools()
    accepted = executor._accepted_params["get_github_repository"]
    assert accepted == {"repo"}


# ---------------------------------------------------------------------------
# Wiring: build_integration_tools reads the scope from the caller's context
# ---------------------------------------------------------------------------


def test_the_ui_offers_enforcement_for_exactly_the_enforceable_scopes():
    """``ENFORCEABLE_ELEMENT_SCOPES`` in the frontend decides which Scope(s)
    selections get an "Enforce as a boundary" switch; every other card
    says "Guidance only".

    The enforceable keys are the ``*_SCOPE_KEY = "provider:element_type"``
    constants a provider guard declares under ``app/integrations/providers``,
    which is also what the backend accepts (``enforceable_scope_keys``): a
    guard added without its switch, or a switch without its guard, fails here."""
    import re
    from pathlib import Path

    from app.services.element_scope import enforceable_scope_keys

    backend = Path(__file__).resolve().parents[1]
    module = (backend.parent / "frontend/src/lib/element-scope.ts").read_text(encoding="utf-8")
    match = re.search(r"ENFORCEABLE_ELEMENT_SCOPES[^=]*=\s*new Set\(\[([^\]]*)\]\)", module)
    assert match, "ENFORCEABLE_ELEMENT_SCOPES not found in frontend/src/lib/element-scope.ts"
    enforceable = set(re.findall(r'"([^"]+)"', match.group(1)))
    declared = {
        key
        for path in (backend / "app/integrations/providers").rglob("*.py")
        for key in re.findall(
            r'(?m)^[A-Z_]+_SCOPE_KEY\s*=\s*"([a-z_]+:[a-z_]+)"', path.read_text(encoding="utf-8")
        )
    }
    assert GITHUB_REPOSITORY_SCOPE_KEY in declared
    assert enforceable == declared == enforceable_scope_keys()


def test_assignment_and_chat_pass_their_scope_to_the_tool_builder():
    """Pinned at the source level: both entry points hand their run's scope
    to ``build_integration_tools`` as ``element_scope`` (the other paths, and
    the handovers, are covered in ``test_element_scope.py``)."""
    from pathlib import Path

    backend = Path(__file__).resolve().parents[1]
    engine = (backend / "app/services/assignment_engine.py").read_text(encoding="utf-8")
    chat = (backend / "app/services/agent_chat.py").read_text(encoding="utf-8")
    tools = (backend / "app/services/integration_tools.py").read_text(encoding="utf-8")
    assert '"element_scope": enforced_scope' in engine
    assert '"element_scope": _element_scope' in chat
    assert 'context.get("element_scope")' in tools


# ---------------------------------------------------------------------------
# Bypasses: every way a scoped call could still reach another repository
# ---------------------------------------------------------------------------

_RealAsyncClient = httpx.AsyncClient


@contextlib.contextmanager
def _github_api(
    responder: Callable[[httpx.Request], httpx.Response],
) -> Iterator[list[httpx.Request]]:
    """Serve the GitHub API from *responder* and record what reaches the wire.

    The requests are recorded as httpx SENDS them - after it built the URL
    and collapsed its dot segments - which is what GitHub would act on, not
    what the tool meant to ask for.
    """
    sent: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return responder(request)

    class _Client(_RealAsyncClient):
        def __init__(self, *args, **kwargs):
            kwargs["transport"] = httpx.MockTransport(handler)
            super().__init__(*args, **kwargs)

    with patch("httpx.AsyncClient", _Client):
        yield sent


def _graphql(request: httpx.Request) -> str:
    return json.loads(request.content or b"{}").get("query") or ""


def _reached(sent: list[httpx.Request], owner_name: str) -> bool:
    return any(r.url.path.lower().startswith(f"/repos/{owner_name}") for r in sent)


def _file(request: httpx.Request) -> httpx.Response:
    if request.method == "GET" and "/contents/" in request.url.path:
        return httpx.Response(
            200,
            json={
                "type": "file",
                "encoding": "base64",
                "content": base64.b64encode(b"TOP SECRET").decode(),
                "path": "notes.txt",
                "sha": "abc",
                "size": 10,
            },
        )
    return httpx.Response(200, json={"content": {"sha": "def"}, "commit": {"sha": "123"}})


@pytest.mark.asyncio
async def test_a_dot_segment_path_cannot_walk_out_of_the_scoped_repository():
    """httpx collapses ``..`` segments, so a ``path`` of ``../../../evil/x/...``
    turned ``/repos/acme/api/contents/`` into ``/repos/evil/x/contents/``."""
    _, executor = _scoped_tools()
    with _github_api(_file) as sent:
        result = await _call(
            executor,
            "get_github_file",
            repo="acme/api",
            path="../../../evil/secret/contents/notes.txt",
            branch="main",
        )
    # Refusals a tool RETURNS (request layer, project items) are plain
    # ``Error: ...`` strings, the shape every provider error has.
    assert result.content.startswith("Error:")
    assert "TOP SECRET" not in result.content
    assert not _reached(sent, "evil/")


@pytest.mark.asyncio
async def test_a_dot_segment_path_cannot_write_into_another_repository():
    _, executor = _scoped_tools()
    with _github_api(_file) as sent:
        result = await _call(
            executor,
            "create_or_update_github_file",
            repo="acme/api",
            path="../../../evil/secret/contents/.github/workflows/x.yml",
            content="on: push",
            message="ci",
            branch="main",
        )
    assert result.content.startswith("Error:")
    assert not _reached(sent, "evil/")
    assert not any(r.method == "PUT" for r in sent)


@pytest.mark.asyncio
async def test_a_dot_segment_ref_is_refused_even_without_a_scope():
    """A ``.`` / ``..`` segment is never a real GitHub path, so the request
    layer refuses it whatever the scope: it would reach another endpoint."""
    executor = _SafeIntegrationExecutor()
    build_tools("gh-token", executor)
    with _github_api(lambda r: httpx.Response(200, json={})) as sent:
        result = await _call(
            executor,
            "get_github_branch_protection",
            repo="acme/api",
            branch="../../../../user/keys",
        )
    assert result.content.startswith("Error:")
    assert sent == []


@pytest.mark.asyncio
async def test_a_single_dot_segment_stays_harmless():
    """``./README.md`` (a common model spelling) cannot leave the repository,
    so it still works - while a ``.`` repository name cannot pass for a
    scoped repository (httpx would read ``acme/./x`` as ``acme/x``)."""
    _, executor = _scoped_tools()
    with _github_api(_file) as sent:
        ok = await _call(executor, "get_github_file", repo="acme/api", path="./notes.txt", branch="main")
        refused = await _call(executor, "get_github_file", repo="acme/.", path="api/contents/x", branch="main")
    assert "TOP SECRET" in ok.content
    assert [r.url.path for r in sent] == ["/repos/acme/api/contents/notes.txt"]
    assert refused.is_error is True


@pytest.mark.asyncio
async def test_a_file_path_that_mentions_repos_is_not_mistaken_for_a_repository():
    """The request guard reads the repository off the start of the path; a
    file path that happens to contain ``repos/x/y`` segments is a file."""
    _, executor = _scoped_tools()
    with _github_api(_file) as sent:
        result = await _call(
            executor,
            "get_github_file",
            repo="acme/api",
            path="docs/repos/other/thing.md",
            branch="main",
        )
    assert result.is_error is False
    assert "TOP SECRET" in result.content
    assert [r.url.path for r in sent] == ["/repos/acme/api/contents/docs/repos/other/thing.md"]


def test_empty_intersection_of_agent_and_assignment_allows_nothing():
    """An assignment refining to a repository its agent no longer offers used
    to produce an EMPTY list, read as "no scope": the run was unrestricted."""
    from app.services.element_scope import assignment_run_scope

    merged = assignment_run_scope(
        {GITHUB_REPOSITORY_SCOPE_KEY: [_binding("acme/web")]},
        [GITHUB_REPOSITORY_SCOPE_KEY],
        {GITHUB_REPOSITORY_SCOPE_KEY: [_binding("acme/api")]},
        None,
    ).enforced
    assert repo_scope_from_bindings(merged) == frozenset()


def _search_items(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    if path == "/search/code":
        return httpx.Response(
            200,
            json={
                "total_count": 57,
                "items": [
                    {"path": "leak.py", "repository": {"full_name": "evil/x"}, "score": 1.0},
                    {"path": "ok.py", "repository": {"full_name": "acme/api"}, "score": 1.0},
                ],
            },
        )
    if path == "/search/commits":
        return httpx.Response(
            200,
            json={
                "total_count": 2,
                "items": [
                    {"sha": "aaaaaaaa1", "commit": {"message": "secret fix"}, "repository": {"full_name": "evil/x"}},
                    {"sha": "bbbbbbbb2", "commit": {"message": "our fix"}, "repository": {"full_name": "acme/api"}},
                ],
            },
        )
    if path == "/search/issues":
        issue = {"state": "open", "labels": [], "assignees": [], "user": {"login": "a"}, "body": ""}
        return httpx.Response(
            200,
            json={
                "total_count": 2,
                "items": [
                    {**issue, "number": 1, "title": "our bug", "repository_url": "https://api.github.com/repos/acme/api"},
                    {**issue, "number": 9, "title": "SECRET plan", "repository_url": "https://api.github.com/repos/evil/x"},
                ],
            },
        )
    return httpx.Response(404, json={"message": "Not Found"})


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "query",
    [
        '(repo:evil/x OR token) AND ORG:"evil"',
        'token repo:"evil/x"',
        "token (org:evil)",
        "-repo:acme/api repo:evil/x token",
        "token user:evil owner:evil",
    ],
)
async def test_a_grouped_qualifier_is_scrubbed_from_the_query(query):
    """``(repo:other/x OR token)`` kept its qualifier: the scrubber only
    matched a qualifier at the start of a token."""
    _, executor = _scoped_tools()
    with _github_api(_search_items) as sent:
        await _call(executor, "search_github_code", query=query)
    q = sent[0].url.params["q"]
    assert "evil" not in q.lower()
    assert q.startswith("repo:acme/api ")


@pytest.mark.asyncio
async def test_issue_search_filters_cannot_smuggle_a_qualifier_in():
    """``state``, ``labels`` and ``assignee`` are written into the query too:
    ``assignee="me repo:evil/y"`` added a repository to the search."""
    _, executor = _scoped_tools()
    with _github_api(_search_items) as sent:
        await _call(
            executor,
            "search_github_issues",
            repo="acme/api",
            state="open org:evil",
            labels='bug" repo:evil/z "',
            assignee="me repo:evil/y",
        )
    q = sent[0].url.params["q"]
    assert "evil" not in q.lower()
    assert q.startswith("repo:acme/api ")


@pytest.mark.asyncio
async def test_a_multi_repository_pin_comes_first_so_the_query_cannot_negate_it():
    """Appended after the query, ``fix NOT`` turned the pin into
    ``fix NOT repo:acme/api repo:acme/web``."""
    _, executor = _scoped_tools(frozenset({"acme/api", "acme/web"}))
    with _github_api(_search_items) as sent:
        await _call(executor, "search_github_commits", query="fix NOT")
    assert sent[0].url.params["q"].startswith("repo:acme/api repo:acme/web ")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tool", "arguments", "ours", "theirs"),
    [
        ("search_github_code", {"query": "token OR secret"}, "acme/api/ok.py", "leak.py"),
        ("search_github_commits", {"query": "fix OR hotfix"}, "our fix", "secret fix"),
        ("search_github_issues", {"repo": "acme/api", "query": "bug OR label:x"}, "our bug", "SECRET plan"),
    ],
)
async def test_search_results_outside_the_scope_are_dropped(tool, arguments, ours, theirs):
    """Boolean operators can widen a pinned search on GitHub's side; whatever
    the query did, a result from another repository never reaches the agent,
    nor does GitHub's total over the widened search."""
    _, executor = _scoped_tools()
    with _github_api(_search_items):
        result = await _call(executor, tool, **arguments)
    assert ours in result.content
    assert theirs not in result.content
    assert "57" not in result.content


def _project(request: httpx.Request) -> httpx.Response:
    query = _graphql(request)
    if query.startswith("mutation"):
        return httpx.Response(200, json={"data": {"x": {"item": {"id": "done"}, "deletedItemId": "done"}}})
    if "items(first" in query:
        base = {"isArchived": False, "fieldValues": {"nodes": []}}
        nodes = [
            {**base, "id": "PVTI_ours", "content": {"number": 1, "title": "our card", "state": "OPEN", "repository": {"nameWithOwner": "acme/api"}}},
            {**base, "id": "PVTI_theirs", "content": {"number": 7, "title": "SECRET roadmap", "state": "OPEN", "repository": {"nameWithOwner": "evil/x"}}},
            {**base, "id": "PVTI_draft", "content": {"title": "draft idea"}},
            {**base, "id": "PVTI_hidden", "content": None},
        ]
        return httpx.Response(200, json={"data": {"node": {"items": {"pageInfo": {"hasNextPage": False}, "nodes": nodes}}}})
    if "fields(first: 50)" in query:
        field = {"id": "F1", "name": "Status", "dataType": "SINGLE_SELECT", "options": [{"id": "O1", "name": "Done"}]}
        return httpx.Response(200, json={"data": {"node": {"fields": {"nodes": [field]}}}})
    # The item's own repository, as the scope resolves it before a write.
    variables = json.loads(request.content).get("variables") or {}
    repo = {"PVTI_ours": "acme/api", "PVTI_theirs": "evil/x"}.get(variables.get("i"))
    content = (
        {"__typename": "Issue", "repository": {"nameWithOwner": repo}}
        if repo
        else {"__typename": "DraftIssue"}
    )
    return httpx.Response(200, json={"data": {"node": {"content": content}}})


def _mutations(sent: list[httpx.Request]) -> list[str]:
    return [q for q in (_graphql(r) for r in sent) if q.startswith("mutation")]


@pytest.mark.asyncio
async def test_project_items_from_other_repositories_are_not_listed():
    _, executor = _scoped_tools()
    with _github_api(_project):
        result = await _call(executor, "list_github_project_items", project_id="PVT_1")
    assert "our card" in result.content
    assert "draft idea" in result.content
    assert "SECRET roadmap" not in result.content
    assert "PVTI_theirs" not in result.content
    assert "PVTI_hidden" not in result.content


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tool", "arguments"),
    [
        ("set_github_project_field", {"field_name": "Status", "value": "Done"}),
        ("manage_github_project_item", {"action": "delete"}),
    ],
)
async def test_a_project_item_of_another_repository_is_not_changed(tool, arguments):
    """The item tools take an opaque ``PVTI_`` id: its repository is resolved
    before any write."""
    _, executor = _scoped_tools()
    with _github_api(_project) as sent:
        refused = await _call(executor, tool, item_id="PVTI_theirs", project_id="PVT_1", **arguments)
    assert refused.content.startswith("Error:")
    assert "outside the Scope(s)" in refused.content
    assert _mutations(sent) == []

    with _github_api(_project) as sent:
        ours = await _call(executor, tool, item_id="PVTI_ours", project_id="PVT_1", **arguments)
        draft = await _call(executor, tool, item_id="PVTI_draft", project_id="PVT_1", **arguments)
    assert ours.is_error is False
    assert draft.is_error is False
    assert len(_mutations(sent)) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tool", "arguments", "expected"),
    [
        (
            "manage_github_repository",
            {"repo": "acme/api", "action": "transfer", "new_owner": "evil"},
            "transferring",
        ),
        (
            "manage_github_team_membership",
            {"org": "acme", "team_slug": "core", "action": "add_member", "username": "mallory"},
            "team membership",
        ),
        (
            "manage_github_team_membership",
            {"org": "acme", "team_slug": "core", "action": "remove_member", "username": "alice"},
            "team membership",
        ),
        (
            "compare_github_commits",
            {"repo": "acme/api", "base": "main", "head": "evil:main"},
            "another repository",
        ),
        (
            "create_github_pr",
            {"repo": "acme/api", "title": "t", "head": "evil:feature", "base": "main"},
            "another repository",
        ),
        (
            "search_github_code",
            {"query": "token", "repo": ["evil/x"]},
            "'repo'",
        ),
    ],
)
async def test_calls_that_reach_or_open_another_repository_are_refused(tool, arguments, expected):
    """Transferring the scoped repository, changing a team's membership (it
    opens or closes every repository of the team), a cross-repository ref and
    a non-string ``repo`` all left the scope while naming only scoped values."""
    _, executor = _scoped_tools()
    with _github_api(lambda r: httpx.Response(200, json={})) as sent:
        result = await _call(executor, tool, **arguments)
    assert result.is_error is True
    assert expected in result.content
    assert sent == []


@pytest.mark.asyncio
async def test_granting_a_team_the_scoped_repository_still_works():
    _, executor = _scoped_tools()
    with _github_api(lambda r: httpx.Response(204)) as sent:
        result = await _call(
            executor,
            "manage_github_team_membership",
            org="acme",
            team_slug="core",
            action="add_repo",
            repo="acme/api",
        )
    assert result.is_error is False
    assert [r.url.path for r in sent] == ["/orgs/acme/teams/core/repos/acme/api"]


@pytest.mark.asyncio
async def test_a_team_lists_only_its_repositories_in_the_scope():
    def team(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/repos"):
            return httpx.Response(200, json=[{"full_name": "acme/api"}, {"full_name": "acme/secret-infra"}])
        if request.url.path.endswith("/members"):
            return httpx.Response(200, json=[])
        return httpx.Response(200, json={"name": "Core", "slug": "core", "repos_count": 2})

    _, executor = _scoped_tools()
    with _github_api(team):
        result = await _call(executor, "get_github_team", org="acme", team_slug="core")
    assert "acme/api" in result.content
    assert "secret-infra" not in result.content


@pytest.mark.asyncio
async def test_sub_issues_from_other_repositories_are_not_listed():
    def subs(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=[
                {"number": 2, "title": "our part", "state": "open", "repository_url": "https://api.github.com/repos/acme/api"},
                {"number": 9, "title": "SECRET part", "state": "open", "repository_url": "https://api.github.com/repos/evil/x"},
            ],
        )

    _, executor = _scoped_tools()
    with _github_api(subs):
        result = await _call(executor, "list_github_sub_issues", repo="acme/api", issue_number=1)
    assert "our part" in result.content
    assert "SECRET part" not in result.content


@pytest.mark.asyncio
async def test_a_failed_repository_listing_is_reported_not_masked():
    """A 401 used to come back as a successful-looking list of scoped
    repositories "not in this listing": the model never saw the failure."""
    _, executor = _scoped_tools()
    with _github_api(lambda r: httpx.Response(401, json={"message": "Bad credentials"})):
        result = await _call(executor, "list_github_repos")
    assert result.content.startswith("Error:")
    assert "not in this listing" not in result.content


@pytest.mark.asyncio
async def test_every_instance_of_a_multi_instance_integration_is_guarded():
    """Suffixed tools (``tool__slug``) re-route the GUARDED handler, keep its
    signature for argument filtering, and never re-expose a withdrawn tool."""
    from app.services.integration_tools import _suffix_instance_tools

    executor = _SafeIntegrationExecutor()
    used: dict[str, set[str]] = {}
    names: list[str] = []
    for label in ("Work", "Personal"):
        defs = build_tools(f"token-{label}", executor, repo_scope=SCOPE)
        suffixed = _suffix_instance_tools(
            defs,
            executor,
            provider="github",
            integ_name=label,
            integ_desc=None,
            integration_id=str(uuid.uuid4()),
            used_slugs=used,
        )
        names += [d.name for d in suffixed]

    repo_tools = [n for n in names if n.startswith("get_github_repository__")]
    assert len(repo_tools) == 2
    assert not any(n.split("__")[0] in DROPPED_WHEN_SCOPED for n in executor.tool_names)
    for name in repo_tools:
        assert executor._accepted_params[name] == {"repo"}
        with _github_api(lambda r: httpx.Response(200, json={})) as sent:
            result = await _call(executor, name, repo="evil/x", hallucinated="x")
        assert result.is_error is True
        assert sent == []
