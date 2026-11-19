"""A pending or red commit status holds a head its check runs read green.

A ``check_run`` completion woke a green filter (``enriched.head.checks.all_green``)
on a head whose CircleCI commit status was still pending: the enrichment read
check runs only, and a commit status is never a check run.
"""

from __future__ import annotations

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
