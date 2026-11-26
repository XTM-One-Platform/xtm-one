"""A red of an attempt its re-run already supersedes is no first failure (xtm-one #2210).

A ``check_run`` completion of an attempt 1 job, delivered while attempt 2 of the
same workflow run is queued or running, read ``first_failure`` true on the check
runs alone and woke the red-check assignment on a head whose failure was being
re-run. The enrichment now reads the head's workflow runs on such a delivery and
names the superseding run in ``superseded_attempt``.
"""

from __future__ import annotations

import pytest

from app.integrations.providers.github import event_enrichment as gh_enrichment
from app.services.integration_event_filters import payload_filter_reason

SHA = "c" * 40
REPO = "OpenAEV-Platform/openaev"
RUN_ID = 44808631813
JOB_URL = f"https://github.com/{REPO}/actions/runs/{RUN_ID}/job/1"


class _Resp:
    def __init__(self, body):
        self._body = body

    def json(self):
        return self._body


def _red_check() -> dict:
    return {
        "id": 7,
        "name": "Gate",
        "status": "completed",
        "conclusion": "failure",
        "started_at": "2026-11-24T20:30:00Z",
        "completed_at": "2026-11-24T20:35:00Z",
        "html_url": JOB_URL,
        "check_suite": {"id": 900},
    }


def _run(status: str, attempt: int, started: str) -> dict:
    return {
        "id": RUN_ID,
        "name": "CI",
        "status": status,
        "run_attempt": attempt,
        "run_started_at": started,
        "check_suite_id": 900,
        "event": "pull_request",
    }


def _github(monkeypatch, runs: list[dict] | None) -> None:
    """GitHub answering one pull request, the red check, and *runs* (None: an error)."""
    check = _red_check()

    async def _request(_client, _method, url, *_a, params=None, **_k):
        if url.endswith("/pulls"):
            return _Resp([]), None
        if url.endswith("/actions/runs"):
            if runs is None:
                return None, "GitHub answered 502"
            return _Resp({"total_count": len(runs), "workflow_runs": runs}), None
        return _Resp({"total_count": 1, "check_runs": [check]}), None

    monkeypatch.setattr(gh_enrichment._common, "safe_request", _request)


def _delivery() -> dict:
    return {"action": "completed", "check_run": {**_red_check(), "head_sha": SHA}, "repository": {"full_name": REPO}}


def test_a_red_of_attempt_1_with_attempt_2_running_is_superseded():
    running = [_run("in_progress", 2, "2026-11-24T20:36:00Z")]
    found = gh_enrichment.superseded_attempt(_red_check(), running)
    assert found == {"run_id": RUN_ID, "attempt": 2, "status": "in_progress"}
    # Matched by the check suite alone too (a check with no URL).
    bare = {k: v for k, v in _red_check().items() if k != "html_url"}
    assert gh_enrichment.superseded_attempt(bare, running) is not None


def test_a_red_of_the_running_attempt_or_a_first_attempt_is_not_superseded():
    # The run is on attempt 2 and the red check started after that attempt: its own red.
    late = {**_red_check(), "started_at": "2026-11-24T20:40:00Z"}
    assert gh_enrichment.superseded_attempt(late, [_run("in_progress", 2, "2026-11-24T20:36:00Z")]) is None
    # Attempt 1 still running: other jobs of the same attempt, the red stands.
    assert gh_enrichment.superseded_attempt(_red_check(), [_run("in_progress", 1, "2026-11-24T20:29:00Z")]) is None
    # Another workflow run of the head runs: unrelated.
    other = {**_run("in_progress", 2, "2026-11-24T20:36:00Z"), "id": 1, "check_suite_id": 1}
    assert gh_enrichment.superseded_attempt(_red_check(), [other]) is None
    assert gh_enrichment.superseded_attempt(_red_check(), []) is None


@pytest.mark.asyncio
async def test_the_fetch_drops_the_first_failure_of_a_superseded_attempt(monkeypatch):
    red = {"action": "completed", "enriched.head.checks.first_failure": True}
    _github(monkeypatch, [_run("queued", 2, "2026-11-24T20:36:00Z")])
    facts = await gh_enrichment.fetch("check_run.completed", _delivery(), "tok")
    checks = facts["head"]["checks"]
    assert checks["first_failure"] is False
    assert checks["superseded_attempt"]["attempt"] == 2
    assert payload_filter_reason({**_delivery(), "enriched": facts}, red) is not None


@pytest.mark.asyncio
async def test_the_red_still_wakes_when_the_run_is_done_or_unreadable(monkeypatch):
    _github(monkeypatch, [_run("completed", 1, "2026-11-24T20:29:00Z")])
    facts = await gh_enrichment.fetch("check_run.completed", _delivery(), "tok")
    assert facts["head"]["checks"]["first_failure"] is True
    assert "superseded_attempt" not in facts["head"]["checks"]
    _github(monkeypatch, None)
    facts = await gh_enrichment.fetch("check_run.completed", _delivery(), "tok")
    assert facts["head"]["checks"]["first_failure"] is True
