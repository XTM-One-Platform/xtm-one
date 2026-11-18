"""A check the base does not require, pending for too long, no longer holds a green head (#195).

A head whose required checks all passed, with nothing failed, stayed ``pending``
for hours because one third-party app check ("Validate PR Title (optional)") was
left in progress: no CI completion was raised and the work item parked on it
waited for a person. Past ``STALE_OPTIONAL_SECONDS`` such a check is left out:
the verdict reads ``green_required_stale_optional`` and names it with its age.
A pending REQUIRED check, or an optional one pending for less than 30 minutes,
still holds the head.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from app.integrations.providers.github import checks as gh_checks
from app.integrations.providers.github import event_enrichment as gh_enrichment
from app.integrations.providers.github import pr_states
from app.integrations.providers.github.reference_state import head_ci_decision, held_by_stale_only
from app.services import work_item_pull_requests as wipr

NOW = datetime(2026, 11, 18, 18, 50, tzinfo=UTC)
REQUIRED = frozenset({"build"})
OPTIONAL = "Validate PR Title (optional)"


def _iso(minutes_ago: int) -> str:
    return (NOW - timedelta(minutes=minutes_ago)).isoformat().replace("+00:00", "Z")


def _run(
    name: str, status: str = "completed", conclusion: str | None = "success", *, run_id: int, started: int = 120
) -> dict[str, Any]:
    return {
        "id": run_id,
        "name": name,
        "status": status,
        "conclusion": conclusion,
        "started_at": _iso(started),
        "completed_at": _iso(1) if status == "completed" else None,
        "html_url": f"https://x/{run_id}",
        "app": {"slug": "github-actions" if name != OPTIONAL else "openaev-pr-checks"},
    }


def _head(*, optional_minutes: int | None = 314, build_status: str = "completed") -> list[dict[str, Any]]:
    runs = [
        _run("build", build_status, "success" if build_status == "completed" else None, run_id=1),
        _run("lint", run_id=2),
    ]
    if optional_minutes is not None:
        runs.append(_run(OPTIONAL, "in_progress", None, run_id=3, started=optional_minutes))
    return runs


# --- the verdict of get_github_ci_status and list_github_pr_states ---------------------------------------------


def test_required_green_and_a_stale_optional_check_is_its_own_verdict() -> None:
    buckets = gh_checks.classify_ci(_head(), [])
    stale = gh_checks.stale_optional(buckets, REQUIRED, NOW)
    assert [s["name"] for s in stale] == [OPTIONAL]
    assert stale[0]["age_seconds"] == 314 * 60
    counts = {k: len(v) for k, v in buckets.items()}
    assert gh_checks.ci_verdict(counts, stale=len(stale)) == gh_checks.GREEN_STALE_OPTIONAL


def test_a_pending_required_check_holds_the_head() -> None:
    buckets = gh_checks.classify_ci(_head(build_status="in_progress"), [])
    assert gh_checks.stale_optional(buckets, REQUIRED, NOW) == []
    assert gh_checks.ci_verdict({k: len(v) for k, v in buckets.items()}) == "pending"


def test_an_optional_check_pending_under_thirty_minutes_holds_the_head() -> None:
    buckets = gh_checks.classify_ci(_head(optional_minutes=12), [])
    assert gh_checks.stale_optional(buckets, REQUIRED, NOW) == []


def test_no_required_check_and_no_start_time_never_make_a_check_stale() -> None:
    buckets = gh_checks.classify_ci(_head(), [])
    assert gh_checks.stale_optional(buckets, frozenset(), NOW) == []
    assert gh_checks.stale_checks([{"name": OPTIONAL}], frozenset(), NOW) == []


def test_a_failure_or_a_running_workflow_still_wins() -> None:
    counts = {"passed": 2, "failed": 1, "pending": 1, "skipped": 0}
    assert gh_checks.ci_verdict(counts, stale=1) == "failure"
    counts["failed"] = 0
    assert gh_checks.ci_verdict(counts, stale=1, running=["CI"]) == "pending"


def test_the_ci_status_names_the_stale_check_and_its_age() -> None:
    buckets = gh_checks.classify_ci(_head(), [])
    stale = gh_checks.stale_optional(buckets, REQUIRED, NOW)
    text = gh_checks.format_ci_status("acme/widget PR #7", buckets, required=REQUIRED, base="main", stale=stale)
    assert "every required check passed, only stale optional check(s) pending" in text
    assert f"Stale optional check(s), not counted: {OPTIONAL} (pending 5h14m)" in text
    assert "Required by main: 1 of 1 passed" in text


def test_the_pr_states_line_names_the_verdict_and_the_stale_check() -> None:
    buckets = gh_checks.classify_ci(_head(), [])
    stale = gh_checks.stale_optional(buckets, REQUIRED, NOW)
    node = {"number": 7, "state": "OPEN", "headRefOid": "a" * 40, "mergeStateStatus": "CLEAN", "title": "x"}
    line = pr_states.format_pr(7, node, latest={k: len(v) for k, v in buckets.items()}, stale=stale)
    assert f"checks {gh_checks.GREEN_STALE_OPTIONAL} (2 passed, 0 failed, 1 pending" in line
    assert f"{OPTIONAL} (pending 5h14m)" in line


# --- the work item CI poll (reference_state) --------------------------------------------------------------------


def _data(runs: list[dict[str, Any]]) -> dict[str, Any]:
    return {"total_count": len(runs), "check_runs": runs}


def test_the_poll_decides_success_over_a_stale_optional_check() -> None:
    data = _data(_head())
    assert held_by_stale_only(data, NOW)
    conclusion, _url, decider = head_ci_decision(data, REQUIRED, NOW)
    assert conclusion == "success" and decider is not None
    payload = wipr.bind_pull_request({}, repo="acme/widget", number=7, head_branch="x", head_sha="7" * 40, draft=True)
    state = {"head_sha": "7" * 40, "conclusion": conclusion, "run_url": _url, **decider}
    _payload, done = wipr.apply_polled_ci(payload, repo="acme/widget", number=7, state=state)
    assert done is not None and done.conclusion == "success"


def test_the_poll_waits_on_a_pending_required_check() -> None:
    data = _data(_head(build_status="in_progress"))
    assert head_ci_decision(data, REQUIRED, NOW) == (None, None, None)


def test_the_poll_waits_on_a_fresh_optional_check() -> None:
    data = _data(_head(optional_minutes=12))
    assert not held_by_stale_only(data, NOW)
    assert head_ci_decision(data, REQUIRED, NOW) == (None, None, None)


# --- the CI event enrichment (green-check wake) ------------------------------------------------------------------


def test_the_enrichment_reads_the_head_green_with_the_stale_check_named() -> None:
    runs = _head()
    facts = gh_enrichment.check_facts(runs)
    assert not facts["all_green"] and gh_enrichment.held_by_stale_only(runs, facts, NOW)
    green = gh_enrichment.with_stale_optional(facts, runs, [], REQUIRED, NOW)
    assert green["all_green"] and not green["all_completed"]
    assert green["verdict"] == gh_checks.GREEN_STALE_OPTIONAL
    assert [s["name"] for s in green["stale_optional"]] == [OPTIONAL]
    assert gh_enrichment.head_settled({"head": {"checks": green}})
    held = gh_enrichment.with_running_workflows(green, [{"name": "CI", "status": "queued"}])
    assert not held["all_green"] and held["verdict"] == "pending"
    assert not gh_enrichment.head_settled({"head": {"checks": held}})


def test_the_enrichment_keeps_a_pending_required_check_pending() -> None:
    runs = _head(build_status="in_progress", optional_minutes=314)
    facts = gh_enrichment.check_facts(runs)
    # The required check started 120 minutes ago too: stale by age, but required.
    assert gh_enrichment.with_stale_optional(facts, runs, [], REQUIRED, NOW) == facts


def test_the_enrichment_keeps_a_fresh_optional_check_pending() -> None:
    runs = _head(optional_minutes=12)
    facts = gh_enrichment.check_facts(runs)
    assert not gh_enrichment.held_by_stale_only(runs, facts, NOW)
    assert gh_enrichment.with_stale_optional(facts, runs, [], REQUIRED, NOW) == facts
