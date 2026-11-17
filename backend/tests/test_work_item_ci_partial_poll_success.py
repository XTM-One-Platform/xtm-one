"""A partial green never reads as the head's success, the required checks declared or not (#12093).

A base that requires a few fast checks (a CLA) saw them pass while the rest of
the head's CI still ran: the polling fallback recorded ``success`` and woke the
parked item for nothing. The success now waits for every check of the head;
the required set still decides a failure and which reds count.
"""

from __future__ import annotations

from typing import Any

from app.integrations.providers.github.reference_state import head_ci_decision
from app.services import work_item_pull_requests as wipr

REPO = "acme/widget"
SHA = "7" * 40
REQUIRED = frozenset({"cla"})


def _run(
    name: str, status: str = "completed", conclusion: str | None = "success", *, run_id: int = 1
) -> dict[str, Any]:
    return {"id": run_id, "name": name, "status": status, "conclusion": conclusion, "html_url": f"https://x/{run_id}"}


def _head(pending: int, green: int = 12) -> dict[str, Any]:
    runs = [_run("cla", run_id=1)]
    runs += [_run(f"green-{i}", run_id=10 + i) for i in range(green - 1)]
    runs += [_run(f"pending-{i}", "in_progress", None, run_id=100 + i) for i in range(pending)]
    return {"total_count": len(runs), "check_runs": runs}


def _payload() -> dict[str, Any]:
    return wipr.bind_pull_request({}, repo=REPO, number=7, head_branch="feat/x", head_sha=SHA, draft=False)


def _state(data: dict[str, Any], required: frozenset[str]) -> dict[str, Any]:
    conclusion, url, decider = head_ci_decision(data, required)
    return {"head_sha": SHA, "conclusion": conclusion, "run_url": url, **(decider or {})}


def test_twelve_green_and_nineteen_running_is_not_decided() -> None:
    data = _head(pending=19)
    assert head_ci_decision(data, REQUIRED) == (None, None, None)
    assert head_ci_decision(data) == (None, None, None)


def test_the_poll_neither_releases_nor_requeues_on_a_partial_green() -> None:
    payload, done = wipr.apply_polled_ci(_payload(), repo=REPO, number=7, state=_state(_head(pending=19), REQUIRED))
    assert done is None
    ci = payload["pull_requests"][0]["ci"]
    assert not ci.get("dispatched") and wipr.POLLED_RUN_KEY not in (ci.get("runs") or {})


def test_every_check_completed_green_dispatches_one_success() -> None:
    state = _state(_head(pending=0, green=31), REQUIRED)
    assert state["conclusion"] == "success"
    payload, done = wipr.apply_polled_ci(_payload(), repo=REPO, number=7, state=state)
    assert done is not None and done.conclusion == "success"
    # The same read again owes nothing.
    _, again = wipr.apply_polled_ci(payload, repo=REPO, number=7, state=state)
    assert again is None


def test_a_required_red_still_fails_while_other_checks_run() -> None:
    data = _head(pending=19)
    data["check_runs"][0] = _run("cla", conclusion="failure", run_id=1)
    assert head_ci_decision(data, REQUIRED)[0] == "failure"


def test_a_partial_page_with_the_required_checks_green_is_not_decided() -> None:
    data = _head(pending=0)
    data["total_count"] = 150
    assert head_ci_decision(data, REQUIRED) == (None, None, None)
