"""The polling fallback never reads a head green before the pull request's own workflows ran.

A ``pull_request_target`` workflow (the base's workflow, a feature branch
deploy) completes on a new head before GitHub creates the pull request's own
workflow runs. Its check runs alone read ``success`` (every check reported so
far passed), so a work item parked on the pull request's CI was released
"CI on <head> already success" a minute before the head's CI started. The
webhook path already refuses that (an advisory run never makes a head green
alone); the live read now applies the same rule.
"""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator
from typing import Any

import pytest

from app.integrations.providers.github import reference_state as rs

REPO = "acme/widget"
SHA = "a" * 40
TARGET_SUITE = 501
OWN_SUITE = 502


def _check(
    name: str, suite: int, *, status: str = "completed", conclusion: str | None = "success", run_id: int = 1
) -> dict[str, Any]:
    return {
        "id": run_id,
        "name": name,
        "status": status,
        "conclusion": conclusion,
        "html_url": f"https://x/{run_id}",
        "completed_at": "2026-01-01T00:00:00Z",
        "app": {"slug": "github-actions"},
        "check_suite": {"id": suite},
    }


def _runs(*pairs: tuple[str, int]) -> dict[str, Any]:
    return {"workflow_runs": [{"event": event, "check_suite_id": suite} for event, suite in pairs]}


TARGET_CHECKS: list[dict[str, Any]] = [
    _check("Feature branch deploy", TARGET_SUITE, run_id=1),
    _check("Preview", TARGET_SUITE, conclusion="skipped", run_id=2),
]
TARGET_ONLY: dict[str, Any] = {"total_count": 2, "check_runs": TARGET_CHECKS}


def test_the_target_run_alone_reads_success_on_the_checks() -> None:
    # The defect's input: every reported check passed, so the checks alone say success.
    assert rs.head_ci_decision(TARGET_ONLY)[0] == "success"


def test_the_target_suite_is_not_the_head_own_ci() -> None:
    suites = rs.own_ci_suites(_runs(("pull_request_target", TARGET_SUITE)))
    assert suites == frozenset()
    assert rs.own_ci_passed(TARGET_ONLY, frozenset()) is False


def test_a_passed_check_of_the_pull_request_workflow_counts() -> None:
    data = {"check_runs": [*TARGET_CHECKS, _check("Backend", OWN_SUITE, run_id=3)]}
    suites = rs.own_ci_suites(_runs(("pull_request_target", TARGET_SUITE), ("pull_request", OWN_SUITE)))
    assert suites == frozenset({OWN_SUITE})
    assert rs.own_ci_passed(data, frozenset({OWN_SUITE})) is True


def test_a_skipped_own_check_or_another_app_never_counts() -> None:
    data = {
        "check_runs": [
            _check("Backend", OWN_SUITE, conclusion="skipped", run_id=3),
            {**_check("Codecov", OWN_SUITE, run_id=4), "app": {"slug": "codecov"}},
        ]
    }
    assert rs.own_ci_passed(data, frozenset({OWN_SUITE})) is False


def test_a_head_with_no_workflow_run_keeps_the_checks_verdict() -> None:
    assert rs.own_ci_suites({"workflow_runs": []}) is None
    assert rs.own_ci_suites({}) is None


class _Resp:
    def __init__(self, data: Any) -> None:
        self._data = data

    def json(self) -> Any:
        return self._data


def _fake_github(monkeypatch: pytest.MonkeyPatch, checks: dict[str, Any], runs: dict[str, Any] | None) -> None:
    pull = {"state": "open", "draft": True, "head": {"sha": SHA, "ref": "feat/x"}, "base": {"ref": "main"}}

    async def fake_request(_client: object, _method: str, url: str, *_a: object, **_k: object) -> tuple[Any, Any]:
        if url.endswith("/pulls/7"):
            return _Resp(pull), None
        if "/check-runs" in url:
            return _Resp(checks), None
        if url.endswith("/actions/runs"):
            return (_Resp(runs), None) if runs is not None else (None, "unreadable")
        if "/rules/branches/" in url:
            return _Resp([]), None
        return None, "unexpected"

    @contextlib.asynccontextmanager
    async def fake_client(**_k: object) -> AsyncIterator[object]:
        yield object()

    monkeypatch.setattr("app.integrations.providers.github._common.safe_request", fake_request)
    monkeypatch.setattr(rs, "egress_client", fake_client)


@pytest.mark.asyncio
async def test_the_live_read_is_undecided_while_only_the_target_run_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_github(monkeypatch, TARGET_ONLY, _runs(("pull_request_target", TARGET_SUITE)))
    state = await rs.read_pull_request_ci(REPO, 7, "token")
    assert state is not None and state["head_sha"] == SHA
    assert state["conclusion"] is None


@pytest.mark.asyncio
async def test_the_live_read_is_green_once_the_own_ci_passed(monkeypatch: pytest.MonkeyPatch) -> None:
    checks = {"total_count": 3, "check_runs": [*TARGET_CHECKS, _check("Backend", OWN_SUITE, run_id=3)]}
    _fake_github(monkeypatch, checks, _runs(("pull_request_target", TARGET_SUITE), ("pull_request", OWN_SUITE)))
    state = await rs.read_pull_request_ci(REPO, 7, "token")
    assert state is not None and state["conclusion"] == "success"


@pytest.mark.asyncio
async def test_an_unreadable_runs_list_keeps_the_checks_verdict(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_github(monkeypatch, TARGET_ONLY, None)
    state = await rs.read_pull_request_ci(REPO, 7, "token")
    assert state is not None and state["conclusion"] == "success"
