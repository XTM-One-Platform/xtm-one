"""get_github_ci_status: the compact CI read of a ref or a PR head.

Polling CI with ``get_github_check_runs`` returned every check (about 25 KB on an
xtm-one PR head), read only the first 50 check runs and listed each re-run
twice. The compact tool reads every page, keeps the latest run per name, and
details only what did not pass. These tests pin green, red and in-progress
heads, the PR-number path, pagination, re-runs, commit statuses, the argument
contract, ``live_state`` and that the existing tools are unchanged.
"""

from unittest.mock import MagicMock, patch

import httpx
import pytest

from app.integrations.providers.github import build_tools
from app.integrations.providers.github.checks import (
    classify_ci,
    format_ci_status,
    required_contexts,
    summarize_required,
)

_SAFE_REQUEST = "app.integrations.providers.github._common.safe_request"


def _tools():
    executor = MagicMock()
    executor.register = MagicMock()
    defs = build_tools("fake-token", executor)
    fns = {c[0][0]: c[0][1] for c in executor.register.call_args_list}
    return fns, {d.name: d for d in defs}


def _run(run_id, name, conclusion="success", status="completed", app="github-actions"):
    return {
        "id": run_id,
        "name": name,
        "status": status,
        "conclusion": conclusion if status == "completed" else None,
        "html_url": f"https://github.com/o/r/actions/runs/1/job/{run_id}",
        "app": {"slug": app, "name": app},
        "head_sha": "0123456789abcdef",
    }


class _GitHub:
    """Answer the GitHub REST paths the tool reads from recorded payloads."""

    def __init__(
        self, check_runs, statuses=(), jobs=None, pull=None, page_size=100, rules=None, protection=None, workflows=None
    ):
        self.workflows = workflows
        self.check_runs = list(check_runs)
        self.statuses = list(statuses)
        self.jobs = jobs or {}
        self.pull = pull
        self.page_size = page_size
        self.rules = rules
        self.protection = protection
        self.calls: list[tuple[str, dict]] = []

    async def __call__(self, client, method, url, provider, action, **kwargs):
        params = kwargs.get("params") or {}
        self.calls.append((url, params))
        if url.endswith("/check-runs"):
            page = params.get("page", 1)
            per_page = params.get("per_page", 30)
            start = (page - 1) * per_page
            batch = self.check_runs[start : start + per_page]
            return httpx.Response(200, json={"total_count": len(self.check_runs), "check_runs": batch}), None
        if url.endswith("/status"):
            return httpx.Response(200, json={"state": "success", "statuses": self.statuses}), None
        if "/rules/branches/" in url:
            if self.rules is None:
                return None, "Error: 403 forbidden"
            return httpx.Response(200, json=self.rules), None
        if url.endswith("/protection/required_status_checks"):
            if self.protection is None:
                return None, "Error: 404 Branch not protected"
            return httpx.Response(200, json=self.protection), None
        if "/actions/jobs/" in url:
            job_id = int(url.rsplit("/", 1)[1])
            return httpx.Response(200, json=self.jobs.get(job_id, {"steps": []})), None
        if "/pulls/" in url:
            return httpx.Response(200, json=self.pull), None
        if url.endswith("/actions/runs"):
            if self.workflows is None:
                return None, "Error: 403 forbidden"
            return httpx.Response(200, json={"workflow_runs": self.workflows}), None
        raise AssertionError(f"unexpected request {url}")


async def _call(github, **kwargs):
    fns, _ = _tools()
    with patch(_SAFE_REQUEST, new=github):
        return await fns["get_github_ci_status"](**kwargs)


@pytest.mark.asyncio
async def test_a_green_head_answers_counts_and_all_passed_under_one_kilobyte():
    runs = [_run(i, f"Backend shard {i}") for i in range(1, 70)] + [_run(70, "Docs", "skipped")]
    github = _GitHub(runs, statuses=[{"context": "deploy/preview", "state": "success", "target_url": ""}])

    result = await _call(github, repo="o/r", ref="abc123")

    assert "SUCCESS - all passed" in result
    assert "70 passed, 0 failed, 0 pending, 1 skipped (71 checks)" in result
    assert "Backend shard" not in result, "a passed check is never listed"
    assert len(result.encode()) < 1024


@pytest.mark.asyncio
async def test_a_red_head_lists_only_the_failed_and_pending_checks_with_the_failed_step():
    runs = [
        _run(1, "Lint"),
        _run(2, "Backend unit", "failure"),
        _run(3, "Chromium", status="in_progress"),
        _run(4, "Title", "failure", app="some-app"),
        _run(5, "Bootstrap", "skipped"),
    ]
    jobs = {2: {"steps": [{"name": "Checkout", "conclusion": "success"}, {"name": "Run pytest", "conclusion": "failure"}]}}
    github = _GitHub(runs, jobs=jobs)

    result = await _call(github, repo="o/r", ref="abc123")

    assert "FAILURE" in result
    assert "1 passed, 2 failed, 1 pending, 1 skipped (5 checks)" in result
    assert "Backend unit [failure] https://github.com/o/r/actions/runs/1/job/2" in result
    assert "failed step(s): Run pytest" in result
    assert "job_id 2: get_github_job_logs" in result
    assert "Title [failure]" in result
    assert "job_id 4" not in result, "only a GitHub Actions check run is a job"
    assert "Pending (1): Chromium" in result
    assert "Lint" not in result and "Bootstrap" not in result
    # The failed step is looked up for the Actions job only.
    assert [u for u, _ in github.calls if "/actions/jobs/" in u] == ["https://api.github.com/repos/o/r/actions/jobs/2"]


@pytest.mark.asyncio
async def test_an_in_progress_head_is_pending_with_names_only():
    runs = [_run(1, "Lint"), _run(2, "Backend unit", status="queued"), _run(3, "E2E", status="in_progress")]

    result = await _call(_GitHub(runs), repo="o/r", ref="abc123")

    assert "PENDING" in result
    assert "1 passed, 0 failed, 2 pending, 0 skipped (3 checks)" in result
    assert "Pending (2): Backend unit, E2E" in result
    assert "https://" not in result, "a pending check is listed by name only"


@pytest.mark.asyncio
async def test_every_page_is_read_and_a_rerun_replaces_its_failed_attempt():
    runs = [_run(i, f"Job {i}") for i in range(1, 150)]
    runs.append(_run(150, "Job 7", "failure"))  # an older failed attempt...
    runs[6] = _run(200, "Job 7")  # ...re-run green under the same name
    github = _GitHub(runs)

    result = await _call(github, repo="o/r", ref="main")

    pages = [p.get("page") for u, p in github.calls if u.endswith("/check-runs")]
    assert pages == [1, 2]
    assert "o/r@main (head 01234567)" in result, "a branch ref names the commit it read"
    assert "SUCCESS - all passed" in result
    assert "149 passed, 0 failed" in result


@pytest.mark.asyncio
async def test_a_pr_number_reads_its_head_commit():
    pull = {"head": {"sha": "deadbeefcafe0000"}, "state": "open", "merged_at": None}
    github = _GitHub([_run(1, "Lint")], pull=pull)

    result = await _call(github, repo="o/r", pr_number=42)

    assert "PR #42 (open) head deadbeef" in result
    assert any(u.endswith("/commits/deadbeefcafe0000/check-runs") for u, _ in github.calls)


@pytest.mark.asyncio
async def test_a_failed_commit_status_counts_as_failed():
    statuses = [{"context": "ci/jenkins", "state": "error", "target_url": "https://ci.example/1"}]

    result = await _call(_GitHub([_run(1, "Lint")], statuses=statuses), repo="o/r", ref="abc")

    assert "FAILURE" in result
    assert "ci/jenkins [error] https://ci.example/1" in result


@pytest.mark.asyncio
@pytest.mark.parametrize("kwargs", [{}, {"ref": "main", "pr_number": 3}])
async def test_exactly_one_of_ref_or_pr_number(kwargs):
    github = _GitHub([])

    result = await _call(github, repo="o/r", **kwargs)

    assert result.startswith("Error: give exactly one of")
    assert github.calls == []


@pytest.mark.asyncio
async def test_a_ref_with_no_check_says_so():
    result = await _call(_GitHub([]), repo="o/r", ref="abc")

    assert "no check runs and no commit statuses" in result


def test_the_tool_is_live_state_and_the_existing_tools_are_unchanged():
    _, defs = _tools()

    ci = defs["get_github_ci_status"]
    assert ci.live_state, "the desk polls CI: never replayed from the identical-call cache"
    assert ci.input_schema["required"] == ["repo"]
    assert "poll" in ci.description
    assert not defs["get_github_check_runs"].live_state
    assert defs["get_github_check_runs"].input_schema["required"] == ["repo", "ref"]
    assert defs["get_github_commit_status"].live_state, "a polled status read is declared, never inferred from its name"


def test_classify_keeps_the_latest_run_per_app_and_name():
    runs = [_run(1, "Lint", "failure"), _run(2, "Lint"), _run(3, "Lint", "failure", app="other")]

    buckets = classify_ci(runs, [])

    assert [e["id"] for e in buckets["passed"]] == [2]
    assert [e["app"] for e in buckets["failed"]] == ["other"]


def _rule(*contexts):
    return {
        "type": "required_status_checks",
        "ruleset_id": 1,
        "parameters": {"required_status_checks": [{"context": c} for c in contexts]},
    }


_PULL = {"head": {"sha": "deadbeefcafe0000"}, "base": {"ref": "main"}, "state": "open", "merged_at": None}


@pytest.mark.asyncio
async def test_a_pr_read_says_how_many_required_checks_passed():
    github = _GitHub(
        [_run(1, "Lint"), _run(2, "Backend")],
        statuses=[{"context": "filigran/cla", "state": "success", "target_url": ""}],
        pull=_PULL,
        rules=[_rule("filigran/cla"), {"type": "deletion"}],
        protection={"contexts": ["Lint"], "checks": [{"context": "Lint", "app_id": None}]},
    )

    result = await _call(github, repo="o/r", pr_number=42)

    assert "Required by main: 2 of 2 passed" in result
    assert any("/rules/branches/main" in u for u, _ in github.calls)


@pytest.mark.asyncio
async def test_a_failing_pending_or_missing_required_check_is_named():
    github = _GitHub(
        [_run(1, "Lint", "failure"), _run(2, "Backend", status="in_progress"), _run(3, "Docs", "failure")],
        pull=_PULL,
        rules=[_rule("Lint", "Backend", "filigran/cla")],
    )

    result = await _call(github, repo="o/r", pr_number=42)

    assert "Required by main: 0 of 3 passed, 1 failing, 1 pending, not reported: filigran/cla" in result
    assert "Lint (required) [failure]" in result
    assert "Docs [failure]" in result and "Docs (required)" not in result
    assert "Pending (1): Backend (required)" in result


@pytest.mark.asyncio
async def test_unreadable_rules_and_protection_leave_the_answer_unchanged():
    github = _GitHub([_run(1, "Lint")], pull=_PULL)

    result = await _call(github, repo="o/r", pr_number=42)

    assert "SUCCESS - all passed" in result
    assert "Required" not in result


@pytest.mark.asyncio
async def test_a_ref_read_never_reads_the_rules():
    github = _GitHub([_run(1, "Lint")], rules=[_rule("Lint")])

    result = await _call(github, repo="o/r", ref="abc")

    assert "Required" not in result
    assert not any("/rules/" in u or "/protection/" in u for u, _ in github.calls)


def test_required_contexts_merges_rulesets_and_classic_protection_and_ignores_garbage():
    rules = [_rule("a", "b"), {"type": "required_status_checks", "parameters": None}, "junk"]
    protection = {"contexts": ["c", ""], "checks": [{"context": "b"}, {"app_id": 1}]}

    assert required_contexts(rules, protection) == frozenset({"a", "b", "c"})
    assert required_contexts({"message": "Not Found"}, None) == frozenset()


def test_a_skipped_required_check_counts_as_passed():
    buckets = classify_ci([_run(1, "Lint", "skipped")], [])

    assert summarize_required(buckets, frozenset({"Lint"}))["passed"] == ["Lint"]


@pytest.mark.asyncio
async def test_a_green_head_with_a_workflow_run_still_queued_is_pending():
    workflows = [{"name": "CI", "status": "completed"}, {"name": "E2E", "status": "queued"}]
    github = _GitHub([_run(1, "Lint"), _run(2, "Backend")], workflows=workflows)

    result = await _call(github, repo="o/r", ref="abc")

    assert "CI for o/r@abc (head 01234567): ⟳ PENDING" in result
    assert "2 passed, 0 failed, 0 pending, 0 skipped (2 checks)" in result
    assert "Workflow run(s) not finished: E2E" in result
    assert any(p.get("head_sha") == "abc" for u, p in github.calls if u.endswith("/actions/runs"))


@pytest.mark.asyncio
async def test_a_queued_workflow_with_no_check_yet_is_pending_not_none_reported():
    result = await _call(_GitHub([], workflows=[{"name": "CI", "status": "queued"}]), repo="o/r", ref="abc")

    assert "PENDING, no check reported yet" in result and "Workflow run(s) not finished: CI" in result


@pytest.mark.asyncio
async def test_a_red_or_pending_head_never_reads_the_workflow_runs():
    github = _GitHub([_run(1, "Lint", "failure"), _run(2, "E2E", status="queued")], workflows=[])

    await _call(github, repo="o/r", ref="abc")

    assert not any(u.endswith("/actions/runs") for u, _ in github.calls)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mergeable", "merge_state"), [(False, "dirty"), (False, "blocked"), (None, "dirty")]
)
async def test_a_conflicting_pr_with_green_checks_answers_conflict(mergeable, merge_state):
    pull = {**_PULL, "mergeable": mergeable, "mergeable_state": merge_state}
    github = _GitHub([_run(1, "Lint")], pull=pull, workflows=[])

    result = await _call(github, repo="o/r", pr_number=42)

    assert "⚠ CONFLICT" in result and "SUCCESS" not in result
    assert f"Conflict: the PR conflicts with its base (merge state {merge_state})" in result


@pytest.mark.asyncio
async def test_a_failure_still_wins_over_a_conflict_and_a_clean_pr_stays_green():
    red = {**_PULL, "mergeable": False, "mergeable_state": "dirty"}
    clean = {**_PULL, "mergeable": True, "mergeable_state": "clean"}

    failing = await _call(_GitHub([_run(1, "Lint", "failure")], pull=red), repo="o/r", pr_number=42)
    green = await _call(_GitHub([_run(1, "Lint")], pull=clean, workflows=[]), repo="o/r", pr_number=42)

    assert "FAILURE" in failing and "Conflict:" in failing
    assert "SUCCESS - all passed" in green and "Conflict" not in green


def test_the_three_tools_share_one_verdict():
    from app.integrations.providers.github import pr_states
    from app.integrations.providers.github.checks import ci_verdict, is_conflict

    green = {"passed": 2, "failed": 0, "pending": 0, "skipped": 0}
    for kwargs in ({}, {"conflict": True}, {"running": ["E2E"]}):
        assert pr_states.verdict(green, **kwargs) == ci_verdict(green, **kwargs)
    assert is_conflict("CONFLICTING", "BLOCKED") and is_conflict(False, "blocked") and is_conflict(None, "DIRTY")
    assert not is_conflict("MERGEABLE", "BLOCKED") and not is_conflict(None, "unknown")


def test_format_caps_the_pending_names():
    buckets = {"failed": [], "passed": [], "skipped": [], "pending": [
        {"name": f"p{i:02d}", "state": "queued", "url": "", "app": "", "id": i} for i in range(45)
    ]}

    text = format_ci_status("o/r@x", buckets)

    assert "Pending (45)" in text
    assert "(+5 more)" in text
