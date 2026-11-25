"""Three wake paths of a work item bound to a pull request, without a database (#200).

* a merge queue removal (``pull_request`` ``dequeued`` without a merge) wakes
  the item once per removal;
* a red on a check the base does not require gives its dispatch key back, so
  the later red of a required check on the same head wakes the item;
* a conflict with the base, read by the CI poll, wakes the item once per head.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from app.integrations.providers.github.pr_events import parse_github_pull_request_event
from app.integrations.pull_request_events import PullRequestEvent
from app.services import work_item_pull_requests as wipr

NOW = datetime(2026, 11, 18, 19, 0, tzinfo=UTC)
REPO = "acme/widget"
SHA = "b" * 40


def _bound() -> dict[str, Any]:
    return wipr.bind_pull_request({}, repo="Acme/Widget", number=7, head_branch="feat/x", head_sha=SHA)


def _dequeued_payload(reason: str, updated_at: str = "2026-11-18T16:56:59Z", merged: bool = False) -> dict[str, Any]:
    return {
        "action": "dequeued",
        "reason": reason,
        "repository": {"full_name": "Acme/Widget"},
        "pull_request": {
            "number": 7,
            "merged": merged,
            "updated_at": updated_at,
            "html_url": "https://github.com/Acme/Widget/pull/7",
            "head": {"ref": "feat/x", "sha": SHA},
        },
    }


def _dequeue(reason: str, updated_at: str = "2026-11-18T16:56:59Z") -> PullRequestEvent:
    event = parse_github_pull_request_event("pull_request.dequeued", _dequeued_payload(reason, updated_at))
    assert event is not None
    return event


def _wakes(out: list[wipr.CiCompleted | wipr.PullRequestClosed]) -> list[wipr.CiCompleted]:
    assert all(isinstance(o, wipr.CiCompleted) for o in out)
    return [o for o in out if isinstance(o, wipr.CiCompleted)]


def _ci(name: str, conclusion: str, run_id: int) -> PullRequestEvent:
    return PullRequestEvent(
        kind="ci",
        repo=REPO,
        pr_numbers=(7,),
        head_branch="feat/x",
        head_sha=SHA,
        run_key=f"workflow:{name}",
        run_id=run_id,
        run_name=name,
        status="completed",
        conclusion=conclusion,
        run_url=f"https://example.test/runs/{run_id}",
    )


# -- 1. Merge queue removal ------------------------------------------------------------


def test_a_dequeued_delivery_without_a_merge_is_a_dequeued_event() -> None:
    event = parse_github_pull_request_event("pull_request.dequeued", _dequeued_payload("CI_FAILURE"))
    assert event is not None
    assert (event.kind, event.repo, event.pr_numbers, event.head_sha) == ("dequeued", REPO, (7,), SHA)
    assert event.conclusion == "ci_failure"
    assert event.closed_at == "2026-11-18T16:56:59Z"


def test_a_dequeue_that_merged_is_no_event() -> None:
    assert parse_github_pull_request_event("pull_request.dequeued", _dequeued_payload("MERGE")) is None
    assert parse_github_pull_request_event("pull_request.dequeued", _dequeued_payload("X", merged=True)) is None


def test_each_removal_wakes_the_item_once() -> None:
    first = _dequeue("CI_FAILURE")
    payload, out = wipr.apply_pull_request_event(_bound(), first, NOW)
    assert [(o.conclusion, o.run_name) for o in _wakes(out)] == [("dequeued", "merge queue: removed (ci_failure)")]
    # The same delivery again: dropped.
    payload, again = wipr.apply_pull_request_event(payload, first, NOW)
    assert again == []
    # A second removal of the same head, later: a new wake.
    second = _dequeue("CI_FAILURE", updated_at="2026-11-18T17:30:00Z")
    payload, out = wipr.apply_pull_request_event(payload, second, NOW)
    assert [o.conclusion for o in _wakes(out)] == ["dequeued"]


def test_a_merge_conflict_removal_says_it_may_be_the_group_not_the_base() -> None:
    _, out = wipr.apply_pull_request_event(_bound(), _dequeue("MERGE_CONFLICT"), NOW)
    (wake,) = _wakes(out)
    assert wake.run_name is not None
    assert wake.run_name.startswith("merge queue: removed (merge_conflict); ")
    assert "ahead of it in the queue's group" in wake.run_name
    assert "enqueue the same head again" in wake.run_name


def test_a_check_failure_removal_carries_no_hint() -> None:
    assert wipr.dequeued_run_name("ci_failure") == "merge queue: removed (ci_failure)"
    assert wipr.dequeued_run_name(None) == "merge queue: removed (unknown)"


# -- 2. A required red after an unrequired red on the same head ---------------------


def test_an_unrequired_red_gives_its_key_back_so_a_required_red_wakes() -> None:
    payload, out = wipr.apply_pull_request_event(_bound(), _ci("Flow decision", "failure", 1), NOW)
    first = _wakes(out)
    assert [o.conclusion for o in first] == ["failure"]
    # The live read says the red is on a check the base does not require.
    live = {"head_sha": SHA, "conclusion": None}
    payload, why = wipr.record_unconfirmed_failure(payload, first[0], live)
    assert why is not None and "does not require" in why
    # The required check goes red four minutes later on the same head: one wake.
    payload, out = wipr.apply_pull_request_event(payload, _ci("Flow RACI", "failure", 2), NOW)
    assert [(o.conclusion, o.run_name) for o in _wakes(out)] == [("failure", "Flow RACI")]


# -- 3. A conflict with the base, read by the poll --------------------------------------


def test_a_conflict_read_wakes_the_item_once_per_head() -> None:
    state = {"head_sha": SHA, "head_branch": "feat/x", "draft": False, "conclusion": None, "conflict": True}
    payload, done = wipr.apply_polled_ci(_bound(), repo=REPO, number=7, state=state)
    assert done is not None and (done.conclusion, done.head_sha) == ("conflict", SHA)
    payload, again = wipr.apply_polled_ci(payload, repo=REPO, number=7, state=state)
    assert again is None
    # A new head that conflicts again is a new wake.
    other = {**state, "head_sha": "c" * 40}
    payload, done = wipr.apply_polled_ci(payload, repo=REPO, number=7, state=other)
    assert done is not None and done.conclusion == "conflict"


def test_no_conflict_keeps_the_verdict_path() -> None:
    state = {"head_sha": SHA, "conclusion": "success", "conflict": False}
    _, done = wipr.apply_polled_ci(_bound(), repo=REPO, number=7, state=state)
    assert done is not None and done.conclusion == "success"
