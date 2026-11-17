"""A Codecov red read before the coverage uploads land is provisional (#12050).

Codecov grades a head as soon as a first upload arrives and grades it again as
the others land, so a red ``codecov/*`` check read while a coverage-producing
check of the same head still runs can turn green with no push. Until the other
checks complete, such a red is no ``first_failure`` of the enrichment, does not
decide the live verdict of the head, and fails no work item's pull request; once
they complete and Codecov is still red, it decides as before. A first failure of
any other check is unchanged.
"""

from __future__ import annotations

from app.integrations.providers.github import checks as gh_checks
from app.integrations.providers.github import event_enrichment as gh_enrichment
from app.integrations.providers.github.reference_state import head_ci_decision
from app.integrations.pull_request_events import PullRequestEvent
from app.services import work_item_pull_requests as wipr

REPO = "opencti-platform/opencti"
SHA = "9" * 40
PATCH = "codecov/patch/opencti"
UNIT = "Backend Unit tests"
LINT = "Lint"


def _run(run_id: int, name: str, conclusion: str | None = "success", at: str = "2026-11-15T07:44:00Z", app="") -> dict:
    return {
        "id": run_id,
        "name": name,
        "status": "completed" if conclusion else "in_progress",
        "conclusion": conclusion,
        "completed_at": at if conclusion else None,
        "html_url": f"https://github.com/{REPO}/runs/{run_id}",
        "app": {"slug": app or "github-actions"},
    }


def _codecov(run_id: int, conclusion: str | None, at: str = "2026-11-15T07:44:00Z") -> dict:
    return _run(run_id, PATCH, conclusion, at, app="codecov")


# -- the shared rule -----------------------------------------------------------------------


def test_coverage_is_pending_while_another_check_runs():
    assert gh_checks.coverage_pending([_codecov(1, "failure"), _run(2, UNIT, None)]) is True
    assert gh_checks.coverage_pending([_codecov(1, "failure"), _run(2, UNIT)]) is False
    # No coverage grade on the head: nothing is provisional.
    assert gh_checks.coverage_pending([_run(1, LINT, "failure"), _run(2, UNIT, None)]) is False


def test_a_codecov_commit_status_is_a_coverage_grade():
    assert gh_checks.is_coverage_grade({"name": "codecov/project", "app": None}) is True
    assert gh_checks.is_coverage_grade({"name": "Backend", "app": {"slug": "codecov"}}) is True
    assert gh_checks.is_coverage_grade({"name": "Backend", "app": {"slug": "github-actions"}}) is False


# -- enriched.head.checks ------------------------------------------------------------------


def test_a_codecov_red_with_backend_tests_pending_is_no_first_failure():
    runs = [_codecov(1, "failure"), _run(2, UNIT, None), _run(3, LINT)]
    facts = gh_enrichment.check_facts(runs, 1, "2026-11-15T07:44:00Z", frozenset({PATCH, UNIT}))
    assert facts["coverage_pending"] is True
    assert facts["first_failure"] is False and facts["first_failed"] is None
    # Still reported as failed: all_green stays false, nothing reads it green early.
    assert facts["failed_names"] == [PATCH] and facts["all_green"] is False


def test_the_same_codecov_red_after_the_tests_completed_decides():
    runs = [_codecov(1, "failure"), _run(2, UNIT, "success", "2026-11-15T07:50:00Z"), _run(3, LINT)]
    facts = gh_enrichment.check_facts(runs, 1, "2026-11-15T07:44:00Z", frozenset({PATCH, UNIT}))
    assert facts["coverage_pending"] is False
    assert facts["first_failed"]["name"] == PATCH and facts["first_failure"] is True


def test_a_non_codecov_first_failure_is_unchanged():
    runs = [_codecov(1, "failure"), _run(2, UNIT, None), _run(3, LINT, "failure", "2026-11-15T07:45:00Z")]
    facts = gh_enrichment.check_facts(runs, 3, "2026-11-15T07:45:00Z")
    assert facts["coverage_pending"] is True
    assert facts["first_failed"]["name"] == LINT and facts["first_failure"] is True


# -- the live verdict (work-item CI dispatch, poll, red release) ---------------------------


def test_the_live_verdict_waits_for_the_coverage_producers():
    pending = {"check_runs": [_codecov(1, "failure"), _run(2, UNIT, None)], "total_count": 2}
    assert head_ci_decision(pending)[0] is None
    assert head_ci_decision(pending, frozenset({PATCH, UNIT}))[0] is None
    done = {"check_runs": [_codecov(1, "failure"), _run(2, UNIT)], "total_count": 2}
    assert head_ci_decision(done)[0] == "failure"
    assert head_ci_decision(done, frozenset({PATCH, UNIT}))[0] == "failure"
    lint = {"check_runs": [_codecov(1, "failure"), _run(2, UNIT, None), _run(3, LINT, "failure")], "total_count": 3}
    assert head_ci_decision(lint)[0] == "failure"


def _ci_event(
    status: str, conclusion: str | None, run_id: int, key: str, name: str, advisory: bool
) -> PullRequestEvent:
    return PullRequestEvent(
        kind="ci",
        repo=REPO,
        pr_numbers=(19965,),
        head_sha=SHA,
        run_key=key,
        run_id=run_id,
        run_name=name,
        status=status,
        conclusion=conclusion,
        advisory=advisory,
    )


def _bound() -> dict:
    return {"pull_requests": [{"repo": REPO, "number": 19965, "state": "open", "head_sha": SHA}]}


def test_a_provisional_red_fails_no_pull_request_until_the_head_settles():
    payload = _bound()
    payload, _ = wipr.apply_pull_request_event(
        payload, _ci_event("in_progress", None, 10, "workflow:backend", "Backend", False)
    )
    payload, outcomes = wipr.apply_pull_request_event(
        payload, _ci_event("completed", "failure", 20, "check_suite:codecov", "Codecov", True)
    )
    [delivered] = [o for o in outcomes if isinstance(o, wipr.CiCompleted)]
    assert delivered.conclusion == "failure"
    state = {"head_sha": SHA, "conclusion": None, "coverage_pending": True}
    confirmed, skipped = wipr.reconcile_failure(delivered, state)
    assert confirmed is delivered and "coverage grade" in (skipped or "")
    payload, what = wipr.record_unconfirmed_failure(payload, delivered, state)
    assert what and wipr.red_pull_requests(payload) == []
    # The workflow completes and Codecov is still red: the head is red once, a new dispatch is owed.
    payload, outcomes = wipr.apply_pull_request_event(
        payload, _ci_event("completed", "success", 10, "workflow:backend", "Backend", False)
    )
    [settled] = [o for o in outcomes if isinstance(o, wipr.CiCompleted)]
    assert settled.conclusion == "failure"
    assert [b["number"] for b in wipr.red_pull_requests(payload)] == [19965]
