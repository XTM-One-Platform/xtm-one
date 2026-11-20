"""A pending or red commit status holds a head its check runs read green.

A ``check_run`` completion woke a green filter (``enriched.head.checks.all_green``)
on a head whose CircleCI commit status was still pending: the enrichment read
check runs only, and a commit status is never a check run.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from app.integrations.providers.github import event_enrichment as gh_enrichment


def _runs() -> list[dict[str, Any]]:
    return [
        {
            "id": 1,
            "name": "build",
            "status": "completed",
            "conclusion": "success",
            "completed_at": "2026-11-18T11:00:00Z",
        },
        {
            "id": 2,
            "name": "lint",
            "status": "completed",
            "conclusion": "success",
            "completed_at": "2026-11-18T11:01:00Z",
        },
    ]


def _status(context: str, state: str) -> dict[str, Any]:
    return {"context": context, "state": state, "updated_at": "2026-11-18T11:02:00Z"}


def test_no_status_leaves_the_green_head_unchanged() -> None:
    facts = gh_enrichment.check_facts(_runs())
    assert facts["all_green"]
    assert gh_enrichment.with_commit_statuses(facts, []) == facts
    assert gh_enrichment.with_commit_statuses(facts, [_status("ci/circleci", "success")]) == facts


def test_a_pending_status_holds_the_head() -> None:
    facts = gh_enrichment.check_facts(_runs())
    held = gh_enrichment.with_commit_statuses(facts, [_status("ci/circleci: build_docker_images", "pending")])
    assert not held["all_green"] and not held["all_completed"]
    assert held["pending"] == 1 and held["total"] == 3
    assert {"name": "status:ci/circleci: build_docker_images", "status": "pending", "conclusion": None} in held["names"]
    assert not gh_enrichment.head_settled({"head": {"checks": held}})


def test_a_red_status_fails_the_head() -> None:
    facts = gh_enrichment.check_facts(_runs())
    red = gh_enrichment.with_commit_statuses(facts, [_status("snyk", "failure")])
    assert not red["all_green"] and red["all_completed"]
    assert red["failed"] == 1 and red["failed_names"] == ["status:snyk"]


def test_a_red_status_outside_the_required_contexts_does_not_hold() -> None:
    facts = gh_enrichment.check_facts(_runs(), required=frozenset({"build"}))
    out = gh_enrichment.with_commit_statuses(facts, [_status("snyk", "error")], frozenset({"build"}))
    assert out["all_green"] and out["failed"] == 0
    assert out["failed_not_required_names"] == ["status:snyk"]


def test_a_required_red_status_holds() -> None:
    facts = gh_enrichment.check_facts(_runs(), required=frozenset({"build", "ci/circleci"}))
    out = gh_enrichment.with_commit_statuses(
        facts, [_status("ci/circleci", "failure")], frozenset({"build", "ci/circleci"})
    )
    assert not out["all_green"] and out["failed_names"] == ["status:ci/circleci"]


def test_a_stale_optional_status_already_left_out_does_not_hold() -> None:
    facts = {**gh_enrichment.check_facts(_runs()), "verdict": "green_required_stale_optional"}
    facts["stale_optional"] = [{"name": "ci/slow", "since": "2026-11-18T08:00:00Z", "age_seconds": 9000}]
    assert gh_enrichment.with_commit_statuses(facts, [_status("ci/slow", "pending")]) == facts
    held = gh_enrichment.with_commit_statuses(facts, [_status("ci/other", "pending")])
    assert not held["all_green"] and held["verdict"] == "pending"


def test_statuses_that_just_passed_hold_the_head_until_they_settle() -> None:
    # OpenAEV-Platform/collectors #690: the last test-* status passed at 09:29:23Z,
    # the green wake read the head at 09:29:33Z, CircleCI created the build-*
    # statuses (pending) at 09:29:39Z.
    facts = gh_enrichment.check_facts(_runs())
    statuses = [{"context": "ci/circleci: test-a", "state": "success", "updated_at": "2026-11-20T09:29:23Z"}]
    held = gh_enrichment.with_commit_statuses(facts, statuses, now=datetime(2026, 11, 20, 9, 29, 33, tzinfo=UTC))
    assert not held["all_green"] and not held["all_completed"] and held["pending"] == 1
    assert gh_enrichment.STATUSES_SETTLING in held["names"]
    assert not gh_enrichment.head_settled({"head": {"checks": held}})
    assert gh_enrichment.head_best_case({"head": {"checks": held}})["head"]["checks"]["all_green"]
    settled = gh_enrichment.with_commit_statuses(facts, statuses, now=datetime(2026, 11, 20, 9, 31, 0, tzinfo=UTC))
    assert settled == facts


def test_a_red_status_is_not_held_as_settling() -> None:
    facts = gh_enrichment.check_facts(_runs())
    statuses = [{"context": "snyk", "state": "failure", "updated_at": "2026-11-20T09:29:23Z"}]
    red = gh_enrichment.with_commit_statuses(facts, statuses, now=datetime(2026, 11, 20, 9, 29, 33, tzinfo=UTC))
    assert red["failed_names"] == ["status:snyk"] and red["pending"] == 0 and red["all_completed"]
