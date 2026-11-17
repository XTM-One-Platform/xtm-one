"""GitHub reads that answer the state, not the whole text.

* ``get_github_pr`` adds the head SHA, the checks summary of the head and the
  review thread counts on an open PR, shows the body's head and one line per
  review, and reads the body by window with ``include_body``.
* ``get_github_issue`` shows the body's head and one line per recent comment.
* ``get_github_job_logs`` answers the failure blocks first, then the log's last
  lines; ``tail=true`` keeps the raw tail.
* ``gh_graphql`` retries a query (never a mutation) on 502/503/504.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import httpx
import pytest

from app.integrations.http import TRANSIENT_RETRY_STATUSES
from app.integrations.providers.github import _common, build_tools

_SAFE_REQUEST = "app.integrations.providers.github._common.safe_request"
_GH_GRAPHQL = "app.integrations.providers.github._common.gh_graphql"


def _tool(name: str):
    executor = MagicMock()
    executor.register = MagicMock()
    build_tools("fake-token", executor)
    return {c[0][0]: c[0][1] for c in executor.register.call_args_list}[name]


def _defs() -> dict[str, Any]:
    executor = MagicMock()
    executor.register = MagicMock()
    return {d.name: d for d in build_tools("fake-token", executor)}


_LONG_BODY = "## Why\n" + "A" * 9_000 + "\nTAIL-MARK"


def _pr(state: str = "open", body: str = _LONG_BODY) -> dict[str, Any]:
    return {
        "title": "fix(x): y",
        "state": state,
        "user": {"login": "SamuelHassine"},
        "created_at": "2026-11-01T22:00:00Z",
        "updated_at": "2026-11-01T22:10:00Z",
        "base": {"ref": "main"},
        "head": {"ref": "fix/y-1", "sha": "abcdef1234567890"},
        "html_url": "https://github.com/o/r/pull/7",
        "mergeable": True,
        "mergeable_state": "blocked",
        "additions": 3,
        "deletions": 1,
        "changed_files": 1,
        "body": body,
    }


class _PrGitHub:
    def __init__(self, pr: dict[str, Any]):
        self.pr = pr
        self.urls: list[str] = []

    async def __call__(self, client, method, url, provider, action, **kwargs):
        self.urls.append(url)
        if url.endswith("/pulls/7"):
            return httpx.Response(200, json=self.pr), None
        if url.endswith("/reviews"):
            return httpx.Response(
                200, json=[{"user": {"login": "aikido"}, "state": "COMMENTED", "submitted_at": "t", "body": "R" * 2_000}]
            ), None
        if url.endswith("/files"):
            return httpx.Response(200, json=[{"filename": "a.py", "status": "modified", "additions": 3, "deletions": 1}]), None
        if url.endswith("/check-runs"):
            runs = [
                {"id": 1, "name": "Lint", "status": "completed", "conclusion": "success", "app": {"slug": "github-actions"}},
                {"id": 2, "name": "Backend 1/4", "status": "completed", "conclusion": "failure", "app": {"slug": "github-actions"}},
                {"id": 3, "name": "E2E core 1/6", "status": "in_progress", "conclusion": None, "app": {"slug": "github-actions"}},
            ]
            return httpx.Response(200, json={"total_count": 3, "check_runs": runs}), None
        if url.endswith("/status"):
            return httpx.Response(200, json={"statuses": []}), None
        return None, f"Error: unexpected {url}"


async def _threads(client, headers, query, variables=None, **kwargs):
    assert "reviewThreads" in query and "mutation" not in query
    return {"repository": {"pullRequest": {"reviewThreads": {"totalCount": 3, "nodes": [
        {"isResolved": True}, {"isResolved": False}, {"isResolved": False},
    ]}}}}, None


@pytest.mark.asyncio
async def test_an_open_pr_reads_its_state_checks_threads_and_the_body_head():
    github = _PrGitHub(_pr())
    with patch(_SAFE_REQUEST, new=github), patch(_GH_GRAPHQL, new=_threads):
        out = await _tool("get_github_pr")(repo="o/r", number=7)
    assert "Head: fix/y-1 @ abcdef123456" in out
    assert "Mergeable state: blocked" in out
    assert "Checks on head: failure, 1 passed, 1 failed, 1 pending, 0 skipped; failed: Backend 1/4" in out
    assert "Review threads: 2 unresolved of 3" in out
    assert "the first 1,500 shown; include_body=true reads it by window" in out
    assert "TAIL-MARK" not in out and "R" * 300 not in out
    assert "aikido (COMMENTED) at t: " in out and "review bodies cut" in out
    assert len(out) < 3_500


@pytest.mark.asyncio
async def test_include_body_reads_the_body_by_window_and_the_reviews_whole():
    github = _PrGitHub(_pr())
    with patch(_SAFE_REQUEST, new=github), patch(_GH_GRAPHQL, new=_threads):
        tool = _tool("get_github_pr")
        first = await tool(repo="o/r", number=7, include_body=True, max_chars=5_000)
        last = await tool(repo="o/r", number=7, include_body=True, offset=5_000)
        past = await tool(repo="o/r", number=7, include_body=True, offset=50_000)
    total = len(_LONG_BODY)
    assert f"Body (characters 0 to 5,000 of {total:,}; {total - 5_000:,} more, continue with offset=5000)" in first
    assert "R" * 2_000 in first
    assert last.count("TAIL-MARK") == 1 and "; the end" in last
    assert f"offset 50,000 is past the end of the pull request body ({total:,} characters)" in past


@pytest.mark.asyncio
async def test_a_closed_pr_makes_no_checks_or_thread_call():
    github = _PrGitHub(_pr(state="closed", body="short"))

    async def no_graphql(*args, **kwargs):
        raise AssertionError("no GraphQL call for a closed PR")

    with patch(_SAFE_REQUEST, new=github), patch(_GH_GRAPHQL, new=no_graphql):
        out = await _tool("get_github_pr")(repo="o/r", number=7)
    assert "Body:\nshort" in out and "Checks on head" not in out
    assert not any(u.endswith(("/check-runs", "/status")) for u in github.urls)


@pytest.mark.asyncio
async def test_a_mergeability_github_is_computing_is_a_transient_answer():
    """``mergeable_state`` unknown (GitHub still computing after a push) is
    answered, but flagged transient: the agent loop never replays it from its
    identical-call cache, so the next read reaches the settled state."""
    computing = {**_pr(), "mergeable": None, "mergeable_state": "unknown"}
    with patch(_SAFE_REQUEST, new=_PrGitHub(computing)), patch(_GH_GRAPHQL, new=_threads):
        out = await _tool("get_github_pr")(repo="o/r", number=7)
    assert out.transient and "Head: fix/y-1 @ abcdef123456" in out.content
    with patch(_SAFE_REQUEST, new=_PrGitHub(_pr())), patch(_GH_GRAPHQL, new=_threads):
        settled = await _tool("get_github_pr")(repo="o/r", number=7)
    assert isinstance(settled, str), "a known merge state stays a plain, cacheable text"


@pytest.mark.asyncio
async def test_an_unreadable_checks_or_thread_count_leaves_the_rest_of_the_answer():
    class Failing(_PrGitHub):
        async def __call__(self, client, method, url, provider, action, **kwargs):
            if url.endswith("/check-runs"):
                return None, "Error: 502"
            return await super().__call__(client, method, url, provider, action, **kwargs)

    async def gql_error(*args, **kwargs):
        return None, "Error: GitHub GraphQL error: boom"

    with patch(_SAFE_REQUEST, new=Failing(_pr())), patch(_GH_GRAPHQL, new=gql_error):
        out = await _tool("get_github_pr")(repo="o/r", number=7)
    assert out.startswith("PR #7: fix(x): y")
    assert "Checks on head" not in out and "Review threads" not in out


@pytest.mark.asyncio
@pytest.mark.parametrize("kwargs", [{"offset": -1}, {"max_chars": -5}])
async def test_a_bad_window_is_refused_before_any_read(kwargs):
    github = _PrGitHub(_pr())
    with patch(_SAFE_REQUEST, new=github):
        out = await _tool("get_github_pr")(repo="o/r", number=7, include_body=True, **kwargs)
    assert out.startswith("Error: ") and github.urls == []


@pytest.mark.asyncio
async def test_an_issue_shows_its_body_head_and_the_latest_comments_on_one_line():
    issue = {
        "title": "t", "state": "open", "user": {"login": "u"}, "created_at": "c", "updated_at": "u",
        "html_url": "h", "body": _LONG_BODY,
    }
    comments = [{"user": {"login": f"p{i}"}, "created_at": f"d{i}", "body": f"comment {i} " + "x" * 600} for i in range(14)]

    async def github(client, method, url, provider, action, **kwargs):
        if url.endswith("/comments"):
            return httpx.Response(200, json=comments), None
        return httpx.Response(200, json=issue), None

    with patch(_SAFE_REQUEST, new=github):
        tool = _tool("get_github_issue")
        out = await tool(repo="o/r", number=3)
        body = await tool(repo="o/r", number=3, include_body=True)
        whole = await tool(repo="o/r", number=3, include_comments=True)
    assert "Comments (14, the latest 10):" in out and "p3 (d3)" not in out and "p13 (d13): comment 13" in out
    assert "TAIL-MARK" not in out and "include_comments=true shows every comment whole" in out and len(out) < 5_500
    # A body window never carries the comments whole.
    assert "TAIL-MARK" in body and body.count("x" * 600) == 0 and "Comments (14, the latest 10):" in body
    assert whole.count("x" * 600) == 14


def _log(failure: bool) -> str:
    body = "".join(f"2026-11-01T22:00:00.0Z tests/test_x.py::test_{i} PASSED\n" for i in range(3_000))
    report = (
        "=========================== short test summary info ============================\n"
        "FAILED tests/test_d.py::test_dedup - AttributeError\n"
        "================== 1 failed, 3000 passed in 60.00s ==================\n"
        "##[error]Process completed with exit code 1.\n"
    ) if failure else "ok\n"
    return "##[group]Run pytest\n##[endgroup]\n" + body + report + "Post job cleanup.\nCleaning up orphan processes\n"


async def _job_log(text: str, **kwargs) -> str:
    async def github(client, method, url, provider, action, **kw):
        return httpx.Response(200, text=text), None

    with patch(_SAFE_REQUEST, new=github):
        return await _tool("get_github_job_logs")(repo="o/r", job_id=9, **kwargs)


@pytest.mark.asyncio
async def test_a_red_job_log_answers_its_failures_first_then_the_last_lines():
    text = _log(failure=True)
    out = await _job_log(text)
    assert out.startswith("Logs for job 9 in o/r: ")
    assert out.index("FAILED tests/test_d.py::test_dedup") < out.index("--- the log's last")
    assert out.rstrip().endswith("Cleaning up orphan processes")
    assert "tail=true" in out and len(out) < 6_000 and len(text) > 150_000


@pytest.mark.asyncio
async def test_failures_only_has_no_last_lines_and_tail_keeps_the_raw_tail():
    text = _log(failure=True)
    only = await _job_log(text, failures_only=True)
    raw = await _job_log(text, tail=True)
    assert "--- the log's last" not in only and "FAILED tests/test_d.py" in only
    assert raw.startswith("Logs for job 9 in o/r:\n\n... [truncated to last 40,000 chars")
    assert "PASSED" in raw


@pytest.mark.asyncio
async def test_a_log_with_no_failure_cue_comes_as_its_tail():
    out = await _job_log(_log(failure=False))
    assert out.startswith("Logs for job 9 in o/r:\n\n... [truncated to last 40,000 chars")


class _Resp:
    status_code = 200

    def json(self):
        return {"data": {"ok": True}}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "query,retried",
    [
        ("query { viewer { login } }", True),
        ("{ viewer { login } }", True),
        ("mutation { addComment(input: {}) { clientMutationId } }", False),
        ("# comment\nmutation M { resolveReviewThread(input: {}) { thread { id } } }", False),
    ],
)
async def test_a_query_is_retried_on_a_gateway_error_and_a_mutation_never(query, retried):
    seen: dict[str, Any] = {}

    async def fake(client, method, url, provider, action, **kwargs):
        seen.update(kwargs)
        return _Resp(), None

    with patch.object(_common, "safe_request", new=fake):
        data, err = await _common.gh_graphql(None, {}, query)
    assert err is None and data == {"ok": True}
    assert seen["retry_statuses"] == (TRANSIENT_RETRY_STATUSES if retried else None)


# -- review follow-ups ------------------------------------


@pytest.mark.asyncio
async def test_the_checks_line_reads_every_page_and_skipped_only_is_a_success():
    def run(i: int, conclusion: str) -> dict[str, Any]:
        return {"id": i, "name": f"c{i}", "status": "completed", "conclusion": conclusion, "app": {"slug": "x"}}

    pages = {1: [run(i, "skipped") for i in range(100)], 2: [run(100, "failure")]}

    class Paged(_PrGitHub):
        async def __call__(self, client, method, url, provider, action, **kwargs):
            if url.endswith("/check-runs"):
                page = kwargs["params"]["page"]
                return httpx.Response(200, json={"total_count": 101, "check_runs": pages[page]}), None
            return await super().__call__(client, method, url, provider, action, **kwargs)

    with patch(_SAFE_REQUEST, new=Paged(_pr())), patch(_GH_GRAPHQL, new=_threads):
        out = await _tool("get_github_pr")(repo="o/r", number=7)
    assert "Checks on head: failure, 0 passed, 1 failed, 0 pending, 100 skipped; failed: c100" in out
    assert "first 100" not in out

    pages[1], pages[2] = [run(1, "skipped"), run(2, "neutral")], []

    class Skipped(Paged):
        async def __call__(self, client, method, url, provider, action, **kwargs):
            if url.endswith("/check-runs"):
                return httpx.Response(200, json={"total_count": 2, "check_runs": pages[1]}), None
            return await super().__call__(client, method, url, provider, action, **kwargs)

    with patch(_SAFE_REQUEST, new=Skipped(_pr())), patch(_GH_GRAPHQL, new=_threads):
        out = await _tool("get_github_pr")(repo="o/r", number=7)
    assert "Checks on head: success, 0 passed, 0 failed, 0 pending, 2 skipped" in out


@pytest.mark.asyncio
async def test_the_thread_count_reads_every_page():
    calls: list[Any] = []

    async def paged(client, headers, query, variables=None, **kwargs):
        calls.append(variables["after"])
        if variables["after"] is None:
            nodes = [{"isResolved": True}] * 99 + [{"isResolved": False}]
            info = {"hasNextPage": True, "endCursor": "c1"}
        else:
            nodes, info = [{"isResolved": False}] * 5, {"hasNextPage": False, "endCursor": "c2"}
        return {"repository": {"pullRequest": {"reviewThreads": {"totalCount": 105, "pageInfo": info, "nodes": nodes}}}}, None

    with patch(_SAFE_REQUEST, new=_PrGitHub(_pr())), patch(_GH_GRAPHQL, new=paged):
        out = await _tool("get_github_pr")(repo="o/r", number=7)
    assert calls == [None, "c1"]
    assert "Review threads: 6 unresolved of 105" in out and "partial" not in out


@pytest.mark.asyncio
async def test_the_default_issue_read_lists_the_newest_comments_from_the_last_page():
    issue = {
        "title": "t", "state": "open", "user": {"login": "u"}, "created_at": "c", "updated_at": "u",
        "html_url": "h", "body": "b", "comments": 204,
    }
    pages_read: list[int] = []

    async def github(client, method, url, provider, action, **kwargs):
        if url.endswith("/comments"):
            page = kwargs["params"]["page"]
            pages_read.append(page)
            first = (page - 1) * 100
            count = min(100, 204 - first)
            return httpx.Response(
                200, json=[{"user": {"login": "p"}, "created_at": f"d{first + i}", "body": f"n{first + i}"} for i in range(count)]
            ), None
        return httpx.Response(200, json=issue), None

    with patch(_SAFE_REQUEST, new=github):
        tool = _tool("get_github_issue")
        out = await tool(repo="o/r", number=3)
        whole = await tool(repo="o/r", number=3, include_comments=True)
    assert pages_read == [3, 2, 1]
    assert "Comments (204, the latest 10):" in out and "(d203): n203" in out and "(d194): n194" in out
    assert "(d193)" not in out
    assert "Comments (204, the first 100):" in whole and "104 later comment(s) not shown" in whole


def test_an_over_long_last_line_is_clipped_not_dropped():
    from app.integrations import ci_logs

    older = "".join(f"older {i}\n" for i in range(10))
    text = older + "z" * 5_000 + "END\n"
    kept = ci_logs.last_lines(text)
    # The over-long newest line is clipped to a quarter of the
    # budget and the earlier lines still come, in log order.
    assert kept[:10] == [f"older {i}" for i in range(10)]
    assert kept[-1].startswith("...") and kept[-1].endswith("END")
    assert len(kept[-1]) == ci_logs.MAX_LAST_CHARS // 4
    assert sum(len(k) + 1 for k in kept) <= ci_logs.MAX_LAST_CHARS
    assert ci_logs.last_lines("a\nb\n") == ["a", "b"]


@pytest.mark.asyncio
async def test_a_failing_commit_status_on_a_second_page_reads_as_failure():
    def status(i: int, state: str) -> dict[str, Any]:
        return {"context": f"s{i}", "state": state, "target_url": ""}

    pages = {1: [status(i, "success") for i in range(100)], 2: [status(100, "failure")]}

    class Statuses(_PrGitHub):
        async def __call__(self, client, method, url, provider, action, **kwargs):
            if url.endswith("/status"):
                page = kwargs["params"]["page"]
                return httpx.Response(200, json={"total_count": 101, "statuses": pages[page]}), None
            if url.endswith("/check-runs"):
                return httpx.Response(200, json={"total_count": 0, "check_runs": []}), None
            return await super().__call__(client, method, url, provider, action, **kwargs)

    with patch(_SAFE_REQUEST, new=Statuses(_pr())), patch(_GH_GRAPHQL, new=_threads):
        out = await _tool("get_github_pr")(repo="o/r", number=7)
    assert "Checks on head: failure, 100 passed, 1 failed" in out and "failed: s100" in out
    assert "commit statuses only" not in out

    # A context rerun to success (newest first) with its old
    # failure on a later page counts once, as its newest state.
    pages[2] = [status(0, "failure")]
    with patch(_SAFE_REQUEST, new=Statuses(_pr())), patch(_GH_GRAPHQL, new=_threads):
        out = await _tool("get_github_pr")(repo="o/r", number=7)
    assert "Checks on head: success, 100 passed, 0 failed" in out


@pytest.mark.asyncio
async def test_the_checks_line_shares_the_ci_verdict_conflict_and_running_workflows():
    green_runs = [{"id": 1, "name": "Lint", "status": "completed", "conclusion": "success", "app": {"slug": "x"}}]

    class Green(_PrGitHub):
        async def __call__(self, client, method, url, provider, action, **kwargs):
            if url.endswith("/check-runs"):
                return httpx.Response(200, json={"total_count": 1, "check_runs": green_runs}), None
            if url.endswith("/actions/runs"):
                return httpx.Response(200, json={"workflow_runs": [{"name": "E2E", "status": "in_progress"}]}), None
            return await super().__call__(client, method, url, provider, action, **kwargs)

    with patch(_SAFE_REQUEST, new=Green(_pr())), patch(_GH_GRAPHQL, new=_threads):
        out = await _tool("get_github_pr")(repo="o/r", number=7)
    assert "Checks on head: pending, 1 passed, 0 failed, 0 pending, 0 skipped; Workflow run(s) not finished: E2E" in out

    dirty = {**_pr(), "mergeable": False, "mergeable_state": "dirty"}
    with patch(_SAFE_REQUEST, new=Green(dirty)), patch(_GH_GRAPHQL, new=_threads):
        out = await _tool("get_github_pr")(repo="o/r", number=7)
    assert "Checks on head: conflict, 1 passed, 0 failed, 0 pending, 0 skipped" in out


def test_the_new_arguments_are_optional_and_the_required_ones_unchanged():
    defs = _defs()
    for name in ("get_github_pr", "get_github_issue"):
        schema = defs[name].input_schema
        assert schema["required"] == ["repo", "number"]
        assert {"include_body", "offset", "max_chars"} <= set(schema["properties"])
    logs = defs["get_github_job_logs"].input_schema
    assert logs["required"] == ["repo", "job_id"] and "tail" in logs["properties"]
