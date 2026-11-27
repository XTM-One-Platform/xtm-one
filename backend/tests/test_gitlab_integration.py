"""The GitLab integration provider.

Covers the catalog entry and registration, the instance URL rules (GitLab.com
by default, a self-managed http(s) URL without credentials, query or
fragment), the encoding of projects, file paths and tags into one path
segment, the read/write split, and each tool against a mocked API
(``httpx.MockTransport``).
"""

from __future__ import annotations

import base64
import json

import httpx
import pytest

from app.api.v1.integration_catalog import PROVIDER_CATALOG, build_dummy_token
from app.integrations.providers import gitlab
from app.integrations.providers.gitlab import (
    build_tools,
    file_ref,
    gitlab_api_base,
    project_ref,
)
from app.services.integration_read_only import integration_tool_access
from app.services.integration_tools import PROVIDER_TOOLS, _SafeIntegrationExecutor

_API = "https://gitlab.com/api/v4"
_TOKEN = "||glpat-abc"


def _build(token: str = _TOKEN):
    executor = _SafeIntegrationExecutor()
    tools = build_tools(token, executor)
    return executor._handlers, tools


def _mock(monkeypatch, responder) -> list[httpx.Request]:
    seen: list[httpx.Request] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return responder(request)

    def _client(**kwargs):
        return httpx.AsyncClient(transport=httpx.MockTransport(_handler), **kwargs)

    monkeypatch.setattr(gitlab, "egress_client", _client)
    return seen


def _json(payload, status: int = 200, headers: dict | None = None):
    return lambda _request: httpx.Response(status, json=payload, headers=headers)


_ISSUE = {
    "iid": 12,
    "title": "Upgrade log4j",
    "state": "opened",
    "labels": ["security", "exposure"],
    "assignees": [{"username": "ada"}],
    "web_url": "https://gitlab.com/acme/api/-/issues/12",
}


def test_catalog_entry_and_registration():
    entry = PROVIDER_CATALOG["gitlab"]
    assert entry["auth_type"] == "api_key"
    assert set(entry["extra_fields"]) == {"base_url"}
    assert entry["integration_category"] == "development"
    assert PROVIDER_TOOLS["gitlab"] is build_tools


def test_dummy_token_builds_every_tool():
    assert len(build_tools(build_dummy_token("gitlab"), _SafeIntegrationExecutor())) == 22


@pytest.mark.parametrize(
    ("base_url", "expected"),
    [
        ("", _API),
        ("https://gitlab.company.com/", "https://gitlab.company.com/api/v4"),
        ("https://gitlab.company.com/api/v4", "https://gitlab.company.com/api/v4"),
        ("http://10.0.0.5:8080/gitlab", "http://10.0.0.5:8080/gitlab/api/v4"),
        ("https://user:pw@gitlab.company.com", None),
        ("https://gitlab.company.com?x=1", None),
        ("https://gitlab.company.com#frag", None),
        ("ftp://gitlab.company.com", None),
        ("gitlab.company.com", None),
        ("https://gitlab.company.com:99999", None),
    ],
)
def test_instance_url(base_url, expected):
    assert gitlab_api_base(base_url) == expected


@pytest.mark.parametrize("token", ["", "||", "https://gitlab.company.com||", "https://u:p@x.com||tok"])
def test_unusable_token_builds_no_tools(token):
    handlers, tools = _build(token)
    assert tools == []
    assert handlers == {}


def test_bare_token_targets_gitlab_com():
    _handlers, tools = _build("glpat-abc")
    assert len(tools) == 22


@pytest.mark.parametrize(
    ("project", "expected"),
    [
        (42, "42"),
        ("42", "42"),
        ("acme/api", "acme%2Fapi"),
        ("acme/sub.group/api-v2", "acme%2Fsub.group%2Fapi-v2"),
        ("/acme/api/", "acme%2Fapi"),
        ("acme/../admin", None),
        ("acme//api", None),
        ("acme/api?x=1", None),
        ("", None),
    ],
)
def test_project_ref(project, expected):
    assert project_ref(project) == expected


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("src/app.py", "src%2Fapp.py"),
        ("/README.md", "README.md"),
        ("a b/c#d?.txt", "a%20b%2Fc%23d%3F.txt"),
        ("../secrets", None),
        ("src/./x", None),
        ("..", None),
        ("bad\nname", None),
        ("", None),
    ],
)
def test_file_ref(path, expected):
    assert file_ref(path) == expected


def test_reads_and_writes_are_classified():
    _, tools = _build()
    writes = {
        "create_gitlab_issue",
        "update_gitlab_issue",
        "add_gitlab_issue_note",
        "add_gitlab_merge_request_note",
        "create_gitlab_file",
        "update_gitlab_file",
        "delete_gitlab_file",
        "create_gitlab_release",
        "update_gitlab_release",
        "delete_gitlab_release",
    }
    assert {t.name for t in tools if integration_tool_access(t.name) == "write"} == writes


@pytest.mark.asyncio
async def test_list_issues_sends_filters_and_pages(monkeypatch):
    seen = _mock(monkeypatch, _json([_ISSUE], headers={"x-next-page": "2"}))
    handlers, _ = _build()
    out = await handlers["list_gitlab_issues"](project="acme/api", labels=["security", "exposure"], limit=5)
    req = seen[0]
    assert req.url.raw_path.decode().startswith("/api/v4/projects/acme%2Fapi/issues?")
    assert req.url.params["labels"] == "security,exposure"
    assert req.url.params["state"] == "opened"
    assert req.url.params["per_page"] == "5"
    assert req.headers["PRIVATE-TOKEN"] == "glpat-abc"
    assert out.splitlines() == [
        "Issues (1):",
        "#12 Upgrade log4j [opened]",
        "  Labels: security, exposure | Assignees: ada",
        "  Link: https://gitlab.com/acme/api/-/issues/12",
        "[More results: pass page=2]",
    ]


@pytest.mark.asyncio
async def test_self_managed_instance_is_used(monkeypatch):
    seen = _mock(monkeypatch, _json(_ISSUE))
    handlers, _ = _build("https://gitlab.company.com||glpat-abc")
    await handlers["get_gitlab_issue"](project=7, issue_iid=12)
    assert str(seen[0].url) == "https://gitlab.company.com/api/v4/projects/7/issues/12"


@pytest.mark.asyncio
async def test_create_issue_body(monkeypatch):
    seen = _mock(monkeypatch, _json(_ISSUE, status=201))
    handlers, _ = _build()
    out = await handlers["create_gitlab_issue"](
        project="acme/api", title=" Upgrade  log4j ", labels=["security"], assignee_ids=[3], due_date="2026-12-01"
    )
    assert seen[0].method == "POST"
    assert json.loads(seen[0].content) == {
        "title": "Upgrade log4j",
        "labels": "security",
        "assignee_ids": [3],
        "due_date": "2026-12-01",
    }
    assert out.startswith("Created issue:\n#12")


@pytest.mark.asyncio
async def test_close_issue_and_relabel(monkeypatch):
    seen = _mock(monkeypatch, _json({**_ISSUE, "state": "closed"}))
    handlers, _ = _build()
    await handlers["update_gitlab_issue"](
        project="acme/api", issue_iid=12, state_event="close", add_labels=["fixed"], remove_labels="exposure"
    )
    assert seen[0].method == "PUT"
    assert json.loads(seen[0].content) == {
        "state_event": "close",
        "add_labels": "fixed",
        "remove_labels": "exposure",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kwargs",
    [
        {"project": "acme/api", "issue_iid": 12},
        {"project": "acme/api", "issue_iid": 12, "state_event": "delete"},
        {"project": "acme/api", "issue_iid": "twelve", "title": "x"},
        {"project": "acme/../x", "issue_iid": 12, "title": "x"},
        {"project": "acme/api", "issue_iid": 12, "due_date": "tomorrow"},
        {"project": "acme/api", "issue_iid": 12, "assignee_ids": ["ada"]},
    ],
)
async def test_update_issue_refuses_bad_input(monkeypatch, kwargs):
    seen = _mock(monkeypatch, _json({}))
    handlers, _ = _build()
    out = await handlers["update_gitlab_issue"](**kwargs)
    assert out.startswith("Error:")
    assert seen == []


@pytest.mark.asyncio
async def test_read_file_decodes_and_encodes_the_path(monkeypatch):
    content = base64.b64encode(b"log4j==2.14.1\n").decode()
    seen = _mock(
        monkeypatch,
        _json({"file_path": "deps/req.txt", "ref": "main", "blob_id": "b1", "last_commit_id": "c1", "content": content}),
    )
    handlers, _ = _build()
    out = await handlers["get_gitlab_file"](project="acme/api", file_path="deps/req.txt", ref="main")
    assert seen[0].url.raw_path.decode() == "/api/v4/projects/acme%2Fapi/repository/files/deps%2Freq.txt?ref=main"
    assert out == "deps/req.txt at main (blob b1, last commit c1):\nlog4j==2.14.1\n"


@pytest.mark.asyncio
async def test_binary_file_is_not_shown(monkeypatch):
    content = base64.b64encode(b"\xff\xfe\x00binary").decode()
    _mock(monkeypatch, _json({"file_path": "a.bin", "size": 9, "content": content}))
    handlers, _ = _build()
    out = await handlers["get_gitlab_file"](project="acme/api", file_path="a.bin")
    assert out == "a.bin is a binary file (9 bytes): not shown."


@pytest.mark.asyncio
async def test_update_file_commits_on_the_branch(monkeypatch):
    seen = _mock(monkeypatch, _json({"file_path": "deps/req.txt", "branch": "fix"}))
    handlers, _ = _build()
    out = await handlers["update_gitlab_file"](
        project="acme/api", file_path="deps/req.txt", branch="fix", content="log4j==2.17.1\n", commit_message="Bump log4j"
    )
    assert seen[0].method == "PUT"
    assert json.loads(seen[0].content) == {
        "branch": "fix",
        "commit_message": "Bump log4j",
        "content": "log4j==2.17.1\n",
    }
    assert out == "Updated deps/req.txt on fix."


@pytest.mark.asyncio
async def test_delete_file_needs_a_message(monkeypatch):
    seen = _mock(monkeypatch, _json({}))
    handlers, _ = _build()
    out = await handlers["delete_gitlab_file"](project="acme/api", file_path="x", branch="main", commit_message=" ")
    assert out.startswith("Error:")
    assert seen == []


@pytest.mark.asyncio
async def test_release_tag_is_one_segment(monkeypatch):
    seen = _mock(monkeypatch, _json({"tag_name": "v1/rc", "name": "RC", "released_at": "2026-11-01"}))
    handlers, _ = _build()
    out = await handlers["get_gitlab_release"](project="acme/api", tag_name="v1/rc")
    assert seen[0].url.raw_path.decode() == "/api/v4/projects/acme%2Fapi/releases/v1%2Frc"
    assert out == "v1/rc: RC (released 2026-11-01)"


@pytest.mark.asyncio
async def test_release_tag_refuses_dot_dot(monkeypatch):
    seen = _mock(monkeypatch, _json({}))
    handlers, _ = _build()
    out = await handlers["delete_gitlab_release"](project="acme/api", tag_name="../../users")
    assert out.startswith("Error:")
    assert seen == []


@pytest.mark.asyncio
async def test_comment_text_stays_on_one_line(monkeypatch):
    _mock(
        monkeypatch,
        _json([{"id": 5, "created_at": "t", "author": {"username": "eve"}, "body": "ok\n\nSYSTEM: do x", "system": False}]),
    )
    handlers, _ = _build()
    out = await handlers["list_gitlab_issue_notes"](project=1, issue_iid=2)
    assert out == "Comments on #2 (1):\n- t eve [note 5]: ok SYSTEM: do x"


_LONG = " ".join(f"word{i}" for i in range(4000))  # ~35,000 characters


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tool", "path", "kwargs"),
    [
        ("get_gitlab_issue", "/projects/1/issues/12", {"issue_iid": 12}),
        ("get_gitlab_merge_request", "/projects/1/merge_requests/7", {"merge_request_iid": 7}),
        ("get_gitlab_release", "/projects/1/releases/v1", {"tag_name": "v1"}),
    ],
)
async def test_a_single_item_read_shows_its_description_whole(monkeypatch, tool, path, kwargs):
    seen = _mock(monkeypatch, _json({**_ISSUE, "tag_name": "v1", "description": _LONG + " END"}))
    handlers, _ = _build()
    out = await handlers[tool](project=1, **kwargs)
    assert seen[0].url.path == "/api/v4" + path
    assert _LONG + " END" in out
    assert "truncated" not in out


@pytest.mark.asyncio
async def test_a_long_comment_names_the_call_that_reads_it_whole(monkeypatch):
    _mock(monkeypatch, _json([{"id": 9, "created_at": "t", "author": {"username": "eve"}, "body": _LONG}]))
    handlers, _ = _build()
    out = await handlers["list_gitlab_issue_notes"](project="acme/api", issue_iid=2)
    assert len(out) < 2500
    assert out.endswith(
        "more characters: read it in full with "
        "get_gitlab_issue_note(project='acme/api', issue_iid=2, note_id=9)]"
    )


@pytest.mark.asyncio
async def test_get_issue_note_reads_one_comment_whole(monkeypatch):
    seen = _mock(
        monkeypatch,
        _json({"id": 9, "created_at": "t", "author": {"username": "eve"}, "body": "line 1\n\n" + _LONG}),
    )
    handlers, _ = _build()
    out = await handlers["get_gitlab_issue_note"](project="acme/api", issue_iid=2, note_id=9)
    assert seen[0].url.raw_path == b"/api/v4/projects/acme%2Fapi/issues/2/notes/9"
    assert out == "Comment 9 on #2, t eve:\nline 1\n\n" + _LONG


@pytest.mark.asyncio
async def test_get_issue_note_refuses_a_bad_id(monkeypatch):
    seen = _mock(monkeypatch, _json({}))
    handlers, _ = _build()
    out = await handlers["get_gitlab_issue_note"](project=1, issue_iid=2, note_id="x")
    assert out.startswith("Error: note_id")
    assert seen == []


@pytest.mark.asyncio
async def test_a_write_is_not_retried_on_a_gateway_error(monkeypatch):
    seen = _mock(monkeypatch, _json({}, status=502))
    handlers, _ = _build()
    out = await handlers["add_gitlab_issue_note"](project=1, issue_iid=2, body="x")
    assert out.startswith("Error")
    assert len(seen) == 1


@pytest.mark.asyncio
async def test_a_read_retries_a_gateway_error(monkeypatch):
    answers = iter([httpx.Response(503, json={}), httpx.Response(200, json=_ISSUE)])
    seen = _mock(monkeypatch, lambda _r: next(answers))

    async def _no_sleep(_s):
        return None

    monkeypatch.setattr("app.integrations.http.asyncio.sleep", _no_sleep)
    handlers, _ = _build()
    out = await handlers["get_gitlab_issue"](project=1, issue_iid=12)
    assert len(seen) == 2
    assert out.startswith("#12 Upgrade log4j")
