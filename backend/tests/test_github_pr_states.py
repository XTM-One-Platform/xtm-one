"""The merge state of several pull requests in one call.

A fake ``gh_graphql`` answers each batch from a table of pull requests and
records the documents: the tests check the line per PR, the verdicts, the
batching, a batch that fails alone and the refusals that send nothing.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import httpx
import pytest

from app.integrations.providers.github import build_tools, pr_states
from app.integrations.providers.github.checks import classify_ci

_GH_GRAPHQL = "app.integrations.providers.github._common.gh_graphql"
_SAFE_REQUEST = "app.integrations.providers.github._common.safe_request"


def _pr(
    number: int,
    *,
    state: str = "OPEN",
    draft: bool = False,
    merge: str = "CLEAN",
    checks: list[tuple[str, int]] | None = None,
    statuses: list[tuple[str, int]] | None = None,
    threads: list[bool] | None = None,
    decision: str | None = "REVIEW_REQUIRED",
) -> dict:
    rollup = {
        "state": "SUCCESS",
        "contexts": {
            "checkRunCountsByState": [{"state": s, "count": c} for s, c in (checks or [])],
            "statusContextCountsByState": [{"state": s, "count": c} for s, c in (statuses or [])],
        },
    }
    resolved = threads or []
    return {
        "number": number,
        "title": f"feat(x): change {number} (#1)",
        "state": state,
        "isDraft": draft,
        "mergeStateStatus": merge,
        "reviewDecision": decision,
        "headRefOid": "abcdef0123456789",
        "reviewThreads": {"totalCount": len(resolved), "nodes": [{"isResolved": r} for r in resolved]},
        "commits": {"nodes": [{"commit": {"statusCheckRollup": rollup}}]},
    }


class FakeGraphQL:
    def __init__(self, prs: dict[int, dict], fail_on: int | None = None) -> None:
        self.prs = prs
        self.fail_on = fail_on
        self.queries: list[str] = []

    async def __call__(self, client, headers, query, variables=None, *, action="", extra_headers=None):
        self.queries.append(query)
        numbers = [int(part.split(":")[0]) for part in query.split("pr")[1:] if part.split(":")[0].isdigit()]
        if self.fail_on in numbers:
            return None, "Error: GitHub GraphQL error: Could not resolve to a PullRequest"
        return {"repository": {f"pr{n}": self.prs.get(n) for n in numbers}}, None


def _tools():
    executor = MagicMock()
    executor.register = MagicMock()
    definitions = build_tools("fake-token", executor)
    handlers = {call[0][0]: call[0][1] for call in executor.register.call_args_list}
    return handlers, {d.name: d for d in definitions}


@pytest.mark.asyncio
async def test_one_line_per_pull_request_with_verdict_counts_and_threads():
    handlers, _ = _tools()
    fake = FakeGraphQL(
        {
            7: _pr(7, checks=[("SUCCESS", 30), ("SKIPPED", 1), ("COMPLETED", 31)], threads=[True]),
            8: _pr(8, draft=True, merge="UNSTABLE", checks=[("SUCCESS", 19), ("IN_PROGRESS", 12)], threads=[False, True]),
            9: _pr(9, merge="BLOCKED", checks=[("SUCCESS", 5), ("FAILURE", 1), ("QUEUED", 2)], statuses=[("ERROR", 1)]),
        }
    )
    with patch(_GH_GRAPHQL, new=fake), patch(_SAFE_REQUEST, new=FakeRest(error="Error: 502 Bad Gateway")):
        result = await handlers["list_github_pr_states"](repo="acme/app", numbers=[7, 8, 9])

    lines = result.splitlines()
    assert lines[0] == "Pull requests of acme/app (3):"
    assert lines[1] == (
        "#7 | open | head abcdef012345 | merge clean | checks success (30 passed, 0 failed, 0 pending, 1 skipped)"
        " | threads 0 unresolved of 1 | review review_required | feat(x): change 7 (#1)"
    )
    assert "| draft |" in lines[2] and "merge unstable" in lines[2]
    assert "checks pending (19 passed, 0 failed, 12 pending, 0 skipped)" in lines[2]
    assert "threads 1 unresolved of 2" in lines[2]
    # #9's re-read failed: the rollup counts stay, and the line says so.
    assert (
        "checks failure (5 passed, 2 failed, 2 pending, 0 skipped, rollup counts: re-read failed: Error: 502"
        in lines[3]
    )
    assert len(fake.queries) == 1


def _run(run_id: int, name: str, conclusion: str = "success") -> dict:
    return {
        "id": run_id,
        "name": name,
        "status": "completed",
        "conclusion": conclusion,
        "app": {"slug": "github-actions"},
    }


class FakeRest:
    """Answers the check-runs and status reads of ``checks.read_ci`` per head SHA."""

    def __init__(
        self,
        runs: dict[str, list[dict]] | None = None,
        error: str | None = None,
        workflows: dict[str, list[dict]] | None = None,
    ) -> None:
        self.runs = runs or {}
        self.error = error
        self.workflows = workflows or {}
        self.urls: list[str] = []

    async def __call__(self, client, method, url, provider, action, **kwargs):
        self.urls.append(url)
        if self.error:
            return None, self.error
        if url.endswith("/actions/runs"):
            sha = kwargs["params"]["head_sha"]
            self.urls.append(f"workflow-runs:{sha}")
            return httpx.Response(200, json={"workflow_runs": self.workflows.get(sha, [])}), None
        sha = url.split("/commits/")[1].split("/")[0]
        if url.endswith("/check-runs"):
            batch = self.runs.get(sha, [])
            return httpx.Response(200, json={"total_count": len(batch), "check_runs": batch}), None
        return httpx.Response(200, json={"state": "success", "statuses": []}), None


@pytest.mark.asyncio
async def test_a_superseded_failed_run_reads_as_get_github_ci_status_reads_it():
    handlers, _ = _tools()
    # GitHub's rollup counts both attempts of "Backend": the failed one and its re-run.
    node = _pr(3, checks=[("SUCCESS", 3), ("FAILURE", 1)])
    node["headRefOid"] = "feedface00000000"
    runs = [_run(1, "Backend", "failure"), _run(2, "Backend"), _run(3, "Lint"), _run(4, "Docs")]
    rest = FakeRest({"feedface00000000": runs})
    with patch(_GH_GRAPHQL, new=FakeGraphQL({3: node})), patch(_SAFE_REQUEST, new=rest):
        result = await handlers["list_github_pr_states"](repo="acme/app", numbers=[3])

    assert pr_states.verdict(pr_states.rollup_counts(node)) == "failure"
    expected = classify_ci(runs, [])
    assert {k: len(v) for k, v in expected.items()} == {"failed": 0, "pending": 0, "passed": 3, "skipped": 0}
    assert "checks success (3 passed, 0 failed, 0 pending, 0 skipped)" in result.splitlines()[1]
    assert any(url.endswith("/commits/feedface00000000/check-runs") for url in rest.urls)


@pytest.mark.asyncio
async def test_a_head_still_failing_on_its_latest_run_stays_a_failure_and_green_heads_cost_no_rest_read():
    handlers, _ = _tools()
    red = _pr(4, checks=[("SUCCESS", 1), ("FAILURE", 1)])
    red["headRefOid"] = "bad0000000000000"
    green = _pr(5, checks=[("SUCCESS", 2)])
    green["headRefOid"] = "good000000000000"
    rest = FakeRest({"bad0000000000000": [_run(1, "Backend"), _run(2, "Lint", "failure")]})
    with patch(_GH_GRAPHQL, new=FakeGraphQL({4: red, 5: green})), patch(_SAFE_REQUEST, new=rest):
        result = await handlers["list_github_pr_states"](repo="acme/app", numbers=[4, 5])

    lines = result.splitlines()
    assert "checks failure (1 passed, 1 failed, 0 pending, 0 skipped)" in lines[1]
    assert "checks success (2 passed, 0 failed, 0 pending, 0 skipped)" in lines[2]
    # A green head costs no check-runs re-read, only the workflow-runs read; a red one no workflow read.
    assert not any("/commits/good000000000000" in url for url in rest.urls)
    assert "workflow-runs:good000000000000" in rest.urls
    assert "workflow-runs:bad0000000000000" not in rest.urls


@pytest.mark.asyncio
async def test_a_conflicting_pr_is_never_green_even_behind_a_review_block():
    handlers, _ = _tools()
    dirty = _pr(1, merge="DIRTY", checks=[("SUCCESS", 4)])
    dirty["mergeable"] = "CONFLICTING"
    blocked = _pr(2, merge="BLOCKED", checks=[("SUCCESS", 4)])
    blocked["mergeable"] = "CONFLICTING"
    rest = FakeRest()
    with patch(_GH_GRAPHQL, new=FakeGraphQL({1: dirty, 2: blocked})), patch(_SAFE_REQUEST, new=rest):
        result = await handlers["list_github_pr_states"](repo="acme/app", numbers=[1, 2])

    lines = result.splitlines()
    assert "merge dirty |" in lines[1] and "checks conflict (4 passed, 0 failed, 0 pending, 0 skipped)" in lines[1]
    assert "merge blocked (conflicting) |" in lines[2] and "checks conflict (4 passed" in lines[2]
    # A conflicting head costs no workflow-runs read: the verdict is already not green.
    assert rest.urls == []


@pytest.mark.asyncio
async def test_a_green_head_with_a_workflow_run_still_queued_is_pending_and_named():
    handlers, _ = _tools()
    node = _pr(6, checks=[("SUCCESS", 3)])
    node["headRefOid"] = "cafe000000000000"
    rest = FakeRest(
        workflows={
            "cafe000000000000": [
                {"name": "CI", "status": "completed"},
                {"name": "E2E", "status": "queued"},
            ]
        }
    )
    with patch(_GH_GRAPHQL, new=FakeGraphQL({6: node})), patch(_SAFE_REQUEST, new=rest):
        result = await handlers["list_github_pr_states"](repo="acme/app", numbers=[6])

    assert (
        "checks pending (3 passed, 0 failed, 0 pending, 0 skipped; Workflow run(s) not finished: E2E)"
        in result.splitlines()[1]
    )


@pytest.mark.asyncio
async def test_an_unreadable_workflow_list_keeps_the_checks_verdict():
    handlers, _ = _tools()
    node = _pr(6, checks=[("SUCCESS", 3)])
    with patch(_GH_GRAPHQL, new=FakeGraphQL({6: node})), patch(_SAFE_REQUEST, new=FakeRest(error="Error: 403")):
        result = await handlers["list_github_pr_states"](repo="acme/app", numbers=[6])

    assert "checks success (3 passed, 0 failed, 0 pending, 0 skipped)" in result.splitlines()[1]


def test_verdict_matches_get_github_ci_status():
    assert pr_states.verdict({"passed": 0, "failed": 0, "pending": 0, "skipped": 0}) == "none reported"
    assert pr_states.verdict({"passed": 0, "failed": 0, "pending": 0, "skipped": 2}) == "success"
    assert pr_states.verdict({"passed": 3, "failed": 0, "pending": 1, "skipped": 0}) == "pending"
    assert pr_states.verdict({"passed": 3, "failed": 1, "pending": 1, "skipped": 0}) == "failure"
    green = {"passed": 3, "failed": 0, "pending": 0, "skipped": 0}
    assert pr_states.verdict(green, conflict=True) == "conflict"
    assert pr_states.verdict(green, running=["E2E"]) == "pending"
    assert pr_states.verdict({**green, "failed": 1}, conflict=True) == "failure"


def test_a_merged_pull_request_shows_no_checks_or_threads():
    line = pr_states.format_pr(5, _pr(5, state="MERGED", decision=None))
    assert line == "#5 | merged | head abcdef012345 | feat(x): change 5 (#1)"
    assert pr_states.format_pr(6, None) == "#6: not found"


@pytest.mark.asyncio
async def test_numbers_are_batched_by_ten_and_a_failed_batch_does_not_hide_the_others():
    handlers, _ = _tools()
    prs = {n: _pr(n, checks=[("SUCCESS", 1)]) for n in range(1, 26)}
    fake = FakeGraphQL(prs, fail_on=14)
    with patch(_GH_GRAPHQL, new=fake), patch(_SAFE_REQUEST, new=FakeRest()):
        result = await handlers["list_github_pr_states"](repo="acme/app", numbers=list(range(1, 26)))

    assert len(fake.queries) == 3
    lines = result.splitlines()
    assert lines[0] == "Pull requests of acme/app (25):"
    assert any(line.startswith("#11, #12, #13, #14") and "not read (Error: GitHub GraphQL error" in line for line in lines)
    assert sum(1 for line in lines if "checks success" in line) == 15


@pytest.mark.asyncio
async def test_refusals_send_nothing():
    handlers, _ = _tools()
    fake = FakeGraphQL({})
    with patch(_GH_GRAPHQL, new=fake):
        empty = await handlers["list_github_pr_states"](repo="acme/app", numbers=[])
        bad = await handlers["list_github_pr_states"](repo="acme/app", numbers=[3, "x"])
        too_many = await handlers["list_github_pr_states"](repo="acme/app", numbers=list(range(1, 32)))
        bad_repo = await handlers["list_github_pr_states"](repo="nope", numbers=[1])
    assert empty.startswith("Error: 'numbers'")
    assert bad.startswith("Error: 'x'")
    assert "at most 30" in too_many
    assert bad_repo.startswith("Error")
    assert fake.queries == []


def test_duplicates_are_read_once_in_order():
    assert pr_states.parse_numbers([9, 3, 9, "3", 4]) == ([9, 3, 4], None)
    assert pr_states.parse_numbers(7) == ([7], None)


def test_the_query_asks_counts_never_check_contexts():
    query = pr_states.build_query([1, 2])
    assert "pr1: pullRequest(number: 1)" in query and "pr2: pullRequest(number: 2)" in query
    assert "checkRunCountsByState" in query
    assert "nodes { ... on CheckRun" not in query
    assert "mutation" not in query


class SequencedGraphQL:
    """Answers the n-th request touching a PR with its n-th state, the last one after that."""

    def __init__(self, states: dict[int, list[dict]]) -> None:
        self.states = states
        self.seen: dict[int, int] = {}
        self.queries: list[list[int]] = []

    async def __call__(self, client, headers, query, variables=None, *, action="", extra_headers=None):
        numbers = [int(part.split(":")[0]) for part in query.split("pr")[1:] if part.split(":")[0].isdigit()]
        self.queries.append(numbers)
        answer = {}
        for n in numbers:
            index = self.seen.get(n, 0)
            self.seen[n] = index + 1
            answer[f"pr{n}"] = self.states[n][min(index, len(self.states[n]) - 1)]
        return {"repository": answer}, None


def _unknown(number: int) -> dict:
    node = _pr(number, merge="UNKNOWN", checks=[("SUCCESS", 3)])
    node["mergeable"] = "UNKNOWN"
    return node


def _known(number: int, merge: str, mergeable: str) -> dict:
    node = _pr(number, merge=merge, checks=[("SUCCESS", 3)])
    node["mergeable"] = mergeable
    return node


@pytest.mark.asyncio
async def test_a_merge_state_github_is_computing_is_read_again_until_known(monkeypatch):
    monkeypatch.setattr(pr_states, "_UNKNOWN_BACKOFF", (0.0, 0.0))
    handlers, _ = _tools()
    fake = SequencedGraphQL(
        {
            1: [_unknown(1), _known(1, "DIRTY", "CONFLICTING")],
            2: [_known(2, "CLEAN", "MERGEABLE")],
            3: [_unknown(3), _unknown(3), _known(3, "BEHIND", "MERGEABLE")],
        }
    )
    with patch(_GH_GRAPHQL, new=fake), patch(_SAFE_REQUEST, new=FakeRest()):
        result = await handlers["list_github_pr_states"](repo="acme/app", numbers=[1, 2, 3])

    lines = result.splitlines()
    assert "merge dirty |" in lines[1]
    assert "merge clean |" in lines[2]
    assert "merge behind |" in lines[3]
    # The first read covers all three, each re-read only the PRs still computing.
    assert fake.queries == [[1, 2, 3], [1, 3], [3]]


@pytest.mark.asyncio
async def test_a_merge_state_still_unknown_after_the_rereads_says_so(monkeypatch):
    monkeypatch.setattr(pr_states, "_UNKNOWN_BACKOFF", (0.0, 0.0))
    handlers, _ = _tools()
    fake = SequencedGraphQL({4: [_unknown(4)]})
    with patch(_GH_GRAPHQL, new=fake), patch(_SAFE_REQUEST, new=FakeRest()):
        result = await handlers["list_github_pr_states"](repo="acme/app", numbers=[4])

    assert result.transient, "an unknown merge state is never kept in the loop's cache"
    assert "merge unknown (GitHub still computing after 2 re-read(s))" in result.content.splitlines()[1]
    assert len(fake.queries) == 3


@pytest.mark.asyncio
async def test_a_queued_pull_request_answers_its_queue_position_without_a_reread(monkeypatch):
    """#2155: a queued PR reads UNKNOWN for as long as the queue holds it; its entry is its state."""

    async def no_sleep(_seconds):
        raise AssertionError("a queued pull request is never re-read")

    monkeypatch.setattr(pr_states.asyncio, "sleep", no_sleep)
    handlers, _ = _tools()
    queued = _unknown(5)
    queued["isInMergeQueue"] = True
    queued["mergeQueueEntry"] = {"position": 2, "state": "AWAITING_CHECKS"}
    fake = SequencedGraphQL({5: [queued]})
    with patch(_GH_GRAPHQL, new=fake), patch(_SAFE_REQUEST, new=FakeRest()):
        result = await handlers["list_github_pr_states"](repo="acme/app", numbers=[5])

    line = result.content.splitlines()[1]
    assert "in merge queue position 2 (awaiting_checks)" in line
    assert "unknown" not in line
    assert fake.queries == [[5]]
    assert result.transient, "a queue position is never kept in the loop's cache"


def test_the_query_reads_the_merge_queue_entry():
    query = pr_states.build_query([1])
    assert "isInMergeQueue" in query and "mergeQueueEntry { position state }" in query


def test_queue_entry_reads_open_pull_requests_only():
    assert pr_states.queue_entry({"state": "OPEN", "isInMergeQueue": True}) == {}
    assert pr_states.queue_entry({"state": "OPEN", "mergeQueueEntry": {"position": 1}}) == {"position": 1}
    assert pr_states.queue_entry({"state": "MERGED", "isInMergeQueue": True}) is None
    assert pr_states.queue_entry({"state": "OPEN"}) is None


@pytest.mark.asyncio
async def test_known_merge_states_cost_no_reread(monkeypatch):
    async def no_sleep(_seconds):
        raise AssertionError("no pause expected")

    monkeypatch.setattr(pr_states.asyncio, "sleep", no_sleep)
    handlers, _ = _tools()
    fake = SequencedGraphQL({5: [_known(5, "CLEAN", "MERGEABLE")], 6: [_pr(6, state="MERGED", merge="UNKNOWN")]})
    with patch(_GH_GRAPHQL, new=fake), patch(_SAFE_REQUEST, new=FakeRest()):
        result = await handlers["list_github_pr_states"](repo="acme/app", numbers=[5, 6])

    assert fake.queries == [[5, 6]]
    assert isinstance(result, str), "a settled answer stays a plain, cacheable text"
    assert "still computing" not in result


def test_the_default_reread_window_outlasts_a_busy_base():
    """Nine PRs of one base read unknown on three calls of a 3 s window: the default waits longer."""
    pauses = pr_states._UNKNOWN_BACKOFF
    assert sum(pauses) >= 12.0, "a base recomputing many PRs needs more than a few seconds"
    assert sum(pauses) <= 20.0, "the call stays well inside a tool call's budget"
    assert list(pauses) == sorted(pauses), "the pauses grow, so a quick settle costs little"


def test_is_computing_reads_open_pull_requests_only():
    assert pr_states.is_computing(_unknown(1))
    assert pr_states.is_computing(_pr(1, merge="UNKNOWN"))
    assert not pr_states.is_computing(_known(1, "CLEAN", "MERGEABLE"))
    assert not pr_states.is_computing(_pr(1, state="CLOSED", merge="UNKNOWN"))
    assert not pr_states.is_computing(None)


def test_the_definition_is_a_read_with_both_arguments_required():
    _, definitions = _tools()
    definition = definitions["list_github_pr_states"]
    assert definition.input_schema["required"] == ["repo", "numbers"]
    assert definition.input_schema["properties"]["numbers"]["type"] == "array"
