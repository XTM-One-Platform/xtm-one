"""A later green of the push run never hides a red of the pull request run (opencti #20153).

A branch pushed while its pull request is open runs the same workflow twice on one
head: the ``push`` run and the ``pull_request`` run, with the same check names.
The latest run per (app, name) used to stand, so the push run's later greens
superseded the pull request run's reds and ``enriched.head.checks.all_green``
read true on a red pull request (opencti 41cb354c, 23 Nov 2026).
"""

from __future__ import annotations

from typing import Any

import pytest

from app.integrations.providers.github import _common as gh_common
from app.integrations.providers.github import checks as gh_checks
from app.integrations.providers.github import event_enrichment as gh_enrichment

REPO = "OpenCTI-Platform/opencti"
SHA = "4" * 40
PUSH_SUITE = 100
PR_SUITE = 200


def _run(run_id: int, name: str, conclusion: str, suite: int) -> dict[str, Any]:
    return {
        "id": run_id,
        "name": name,
        "status": "completed",
        "conclusion": conclusion,
        "started_at": "2026-11-23T14:06:18Z",
        "completed_at": f"2026-11-23T14:{10 + run_id % 40:02d}:00Z",
        "app": {"slug": "github-actions"},
        "check_suite": {"id": suite},
    }


SUITES = {PUSH_SUITE: "push", PR_SUITE: "pull_request"}


def test_a_red_of_the_pull_request_run_hidden_by_a_later_push_green_is_named() -> None:
    runs = [
        _run(1, "Rules tests", "failure", PR_SUITE),
        _run(2, "Rules tests", "success", PUSH_SUITE),
        _run(3, "Unit tests", "success", PR_SUITE),
        _run(4, "Unit tests", "success", PUSH_SUITE),
    ]
    assert gh_enrichment.check_facts(runs)["all_green"] is True
    assert gh_enrichment.reds_of_another_event(runs, SUITES) == ["Rules tests"]


def test_a_rerun_in_the_same_event_still_supersedes_its_red() -> None:
    runs = [
        _run(1, "Rules tests", "failure", PR_SUITE),
        _run(2, "Rules tests", "success", PR_SUITE),
        _run(3, "Rules tests", "success", PUSH_SUITE),
    ]
    assert gh_enrichment.reds_of_another_event(runs, SUITES) == []


def test_a_hidden_red_outside_the_required_checks_does_not_count() -> None:
    runs = [_run(1, "e2e shard", "failure", PR_SUITE), _run(2, "e2e shard", "success", PUSH_SUITE)]
    assert gh_enrichment.reds_of_another_event(runs, SUITES, frozenset({"Unit tests"})) == []
    assert gh_enrichment.reds_of_another_event(runs, SUITES, frozenset({"e2e shard"})) == ["e2e shard"]


def test_a_check_run_of_an_unknown_suite_is_left_to_the_latest_run_rule() -> None:
    runs = [_run(1, "Codecov", "failure", 999), _run(2, "Codecov", "success", 998)]
    assert gh_enrichment.reds_of_another_event(runs, SUITES) == []


def test_the_hidden_red_makes_the_head_red() -> None:
    runs = [_run(1, "Rules tests", "failure", PR_SUITE), _run(2, "Rules tests", "success", PUSH_SUITE)]
    checks = gh_enrichment.with_reds_of_another_event(
        gh_enrichment.check_facts(runs), gh_enrichment.reds_of_another_event(runs, SUITES)
    )
    assert checks["all_green"] is False
    assert checks["failed"] == 1
    assert checks["failed_names"] == ["Rules tests"]


class _Resp:
    def __init__(self, body: Any) -> None:
        self._body = body

    def json(self) -> Any:
        return self._body


@pytest.mark.asyncio
async def test_fetch_reads_the_hidden_red_on_a_candidate_green_head(monkeypatch: pytest.MonkeyPatch) -> None:
    runs = [
        _run(1, "Rules tests", "failure", PR_SUITE),
        _run(2, "Rules tests", "success", PUSH_SUITE),
    ]
    pr = {
        "number": 20153,
        "state": "open",
        "draft": False,
        "created_at": "2026-11-23T14:02:00Z",
        "user": {"login": "SamuelHassine"},
        "head": {"sha": SHA, "ref": "issue/20135", "repo": {"full_name": REPO}},
        "base": {"ref": "master", "repo": {"full_name": REPO}},
        "labels": [],
    }
    workflow_runs = [
        {"id": 11, "event": "push", "status": "completed", "check_suite_id": PUSH_SUITE},
        {"id": 12, "event": "pull_request", "status": "completed", "check_suite_id": PR_SUITE},
    ]

    async def _request(
        _client: Any, _method: str, url: str, *_a: Any, params: Any = None, **_k: Any
    ) -> tuple[_Resp, None]:
        if url.endswith("/pulls"):
            return _Resp([pr]), None
        if url.endswith("/actions/runs"):
            return _Resp({"total_count": len(workflow_runs), "workflow_runs": workflow_runs}), None
        if url.endswith("/status"):
            return _Resp({"state": "success", "statuses": []}), None
        if url.endswith("/check-runs"):
            return _Resp({"total_count": len(runs), "check_runs": runs}), None
        return _Resp({}), None

    async def _no_required(*_a: Any, **_k: Any) -> frozenset[str]:
        return frozenset()

    monkeypatch.setattr(gh_common, "safe_request", _request)
    monkeypatch.setattr(gh_checks, "read_required_contexts", _no_required)
    payload = {
        "action": "completed",
        "check_run": {"id": 2, "head_sha": SHA, "completed_at": "2026-11-23T14:12:00Z"},
        "repository": {"full_name": REPO},
    }
    facts = await gh_enrichment.fetch("check_run.completed", payload, "tok", facets=frozenset({"checks"}))
    checks = facts["head"]["checks"]
    assert checks["all_green"] is False
    assert checks["failed_names"] == ["Rules tests"]
