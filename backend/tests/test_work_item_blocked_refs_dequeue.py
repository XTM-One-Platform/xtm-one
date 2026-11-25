"""A merge queue removal surfaces an item blocked on that pull request's merge by reference (#397).

GitHub sends no closing when its merge queue drops a pull request without
merging it: an item whose ``blocked_on_refs`` waits for that merge, and whose
payload does not bind the pull request (#202 wakes those), would wait forever.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from app.integrations.pull_request_events import PullRequestEvent
from app.services.work_item_blocked_refs import apply_dequeue_event, dequeued_note

NOW = datetime(2026, 11, 19, 9, 30, tzinfo=UTC)
REPO = "xtm-one-platform/xtm-one"


def _dequeued(number: int, removed_at: str = "2026-11-19T09:25:23Z", reason: str = "failed_checks") -> PullRequestEvent:
    return PullRequestEvent(kind="dequeued", repo=REPO, pr_numbers=(number,), conclusion=reason, closed_at=removed_at)


def _pr(number: int, release_on: str = "merged_or_closed") -> dict[str, Any]:
    return {"repo": REPO, "kind": "pull_request", "number": number, "release_on": release_on}


def test_a_removal_names_the_reference_waiting_for_that_merge() -> None:
    refs, removed = apply_dequeue_event([_pr(109), _pr(110)], _dequeued(109))
    assert removed == [f"{REPO}#109"]
    assert refs[0]["dequeued_at"] == "2026-11-19T09:25:23Z"
    assert "dequeued_at" not in refs[1]


def test_a_redelivery_names_nothing_and_a_second_removal_names_it_again() -> None:
    refs, _ = apply_dequeue_event([_pr(109)], _dequeued(109))
    _, again = apply_dequeue_event(refs, _dequeued(109))
    assert again == []
    _, later = apply_dequeue_event(refs, _dequeued(109, removed_at="2026-11-19T10:05:00Z"))
    assert later == [f"{REPO}#109"]


def test_a_reference_that_does_not_wait_for_a_merge_is_left_alone() -> None:
    issue = {"repo": REPO, "kind": "issue", "number": 109, "release_on": "closed"}
    closed_only = _pr(109, release_on="closed")
    merged = {**_pr(109), "state": "merged"}
    deployed_merge_known = {
        "repo": REPO,
        "kind": "deployment",
        "number": 109,
        "release_on": "deployed",
        "merge_sha": "a" * 40,
    }
    other_repo = {**_pr(109), "repo": "opencti-platform/opencti"}
    refs = [issue, closed_only, merged, deployed_merge_known, other_repo]
    out, removed = apply_dequeue_event(refs, _dequeued(109))
    assert removed == []
    assert out == refs


def test_a_deployment_reference_before_its_merge_is_named() -> None:
    dep = {"repo": REPO, "kind": "deployment", "number": 109, "release_on": "deployed"}
    _, removed = apply_dequeue_event([dep], _dequeued(109))
    assert removed == [f"deployment of {REPO}#109"]


def test_an_event_other_than_a_removal_names_nothing() -> None:
    closed = PullRequestEvent(kind="closed", repo=REPO, pr_numbers=(109,), merged=False)
    refs, removed = apply_dequeue_event([_pr(109)], closed)
    assert removed == []
    assert refs == [_pr(109)]


def test_the_note_names_the_removal_its_reason_and_what_to_do() -> None:
    note = dequeued_note([f"{REPO}#109"], _dequeued(109), NOW, still_blocked=False)
    assert note.startswith(f"surfaced: {REPO}#109 removed from the merge queue without a merge (failed_checks)")
    assert "at 2026-11-19T09:25:23Z" in note
    assert "back in the queue" in note
    assert note.endswith("surfaced at 2026-11-19T09:30:00Z")
    waiting = dequeued_note([f"{REPO}#109"], _dequeued(109), NOW, still_blocked=True)
    assert "stays blocked until the items it waits for are done" in waiting


def test_a_merge_conflict_note_says_the_conflict_may_be_in_the_group() -> None:
    note = dequeued_note([f"{REPO}#109"], _dequeued(109, reason="merge_conflict"), NOW, still_blocked=False)
    assert "without a merge (merge_conflict; the conflict may be with a pull request ahead of it" in note
    assert "mergeable means enqueue the same head again)" in note
