"""A red check the base branch does not require never wakes a work item as a CI failure (#11766).

The live verdict of a head decides on the required checks alone when the base
names any; a delivered failure is confirmed by that verdict before a dispatch;
the inbox settle voids a recorded red the live read calls green. The OpenCTI
#19941 check set (20 required, a Codecov project status red and not required)
is replayed.
"""

from __future__ import annotations

from app.integrations.providers.github.reference_state import head_ci_decision, head_ci_verdict, status_runs
from app.services import work_item_pull_requests as wipr

SHA = "5" * 40


def _run(name: str, status: str = "completed", conclusion: str | None = "success", *, run_id: int = 1) -> dict:
    return {"id": run_id, "name": name, "status": status, "conclusion": conclusion, "html_url": f"https://x/{run_id}"}


# The required checks of OpenCTI-Platform/opencti master on 14 Nov 2026 (get_github_branch_rules).
OPENCTI_REQUIRED = frozenset(
    {
        "License check / License check",
        "Client Python / pycti test - python 3.10",
        "Client Python / pycti test - python 3.11",
        "Client Python / pycti test - python 3.12",
        "Backend / Integration tests",
        "Backend / Rules and Taxii tests",
        "Backend / Unit tests",
        "codecov/patch/opencti-graphql-mandatory",
        "Check if PR author is an OpenCTI-Platform member and add label",
        "Check signed commits in PR",
        "Check that PR title follows convention",
        "Check Pull Request is linked to an issue",
        "Frontend e2e / e2e ff tests",
        "Frontend quality / Frontend quality checks (build, lint, unit tests)",
        "Build platform (fips)",
        "Build platform (standard)",
        "Build platform (ubi9)",
        "Build worker (fips)",
        "Build worker (standard)",
        "Build worker (ubi9)",
    }
)


def _opencti_19941(*, pending: tuple[str, ...] = ()) -> dict:
    runs = [
        _run(name, "in_progress" if name in pending else "completed", None if name in pending else "success", run_id=i)
        for i, name in enumerate(sorted(OPENCTI_REQUIRED), start=1)
    ]
    runs.append(_run("codecov/project/opencti-graphql-mandatory", conclusion="failure", run_id=100))
    runs.append(_run("Label the author's side", conclusion="failure", run_id=101))
    return {"total_count": len(runs), "check_runs": runs}


# -- The live verdict --------------------------------------------------------------


def test_a_non_required_red_with_every_required_check_green_reads_success():
    data = _opencti_19941()
    assert head_ci_verdict(data)[0] == "failure"  # every check counted: the old verdict
    assert head_ci_verdict(data, OPENCTI_REQUIRED)[0] == "success"


def test_a_non_required_red_while_required_checks_run_is_not_decided():
    data = _opencti_19941(pending=("Backend / Unit tests", "Backend / Integration tests"))
    assert head_ci_verdict(data, OPENCTI_REQUIRED) == (None, None)


def test_a_required_red_still_reads_failure():
    data = _opencti_19941()
    data["check_runs"][0] = _run(data["check_runs"][0]["name"], conclusion="failure", run_id=1)
    conclusion, url, decider = head_ci_decision(data, OPENCTI_REQUIRED)
    assert conclusion == "failure" and url == "https://x/1" and decider["check_id"] == 1


def test_a_required_check_never_reported_is_not_decided():
    data = {"check_runs": [_run("CI verdict"), _run("codecov/project", conclusion="failure", run_id=2)]}
    assert head_ci_verdict(data, frozenset({"CI verdict", "Secret scan"})) == (None, None)


def test_no_required_check_keeps_every_check_deciding():
    data = {"check_runs": [_run("CI verdict"), _run("codecov/project", conclusion="failure", run_id=2)]}
    assert head_ci_verdict(data, frozenset())[0] == "failure"


def test_a_required_commit_status_is_placed_like_a_check_run():
    statuses = status_runs(
        {
            "statuses": [
                {"context": "codecov/patch", "state": "success", "target_url": "https://c/1"},
                {"context": "codecov/project", "state": "failure"},
                {"context": "deploy", "state": "pending"},
                {"state": "success"},
            ]
        }
    )
    assert [(s["name"], s["status"], s["conclusion"]) for s in statuses] == [
        ("codecov/patch", "completed", "success"),
        ("codecov/project", "completed", "failure"),
        ("deploy", "in_progress", None),
    ]
    data = {"check_runs": [_run("CI verdict"), *statuses[:2]]}
    assert head_ci_verdict(data, frozenset({"CI verdict", "codecov/patch"}))[0] == "success"


# -- A delivered failure confirmed live --------------------------------------------


def _failure() -> wipr.CiCompleted:
    return wipr.CiCompleted(
        repo="opencti-platform/opencti",
        number=19941,
        head_sha=SHA,
        conclusion="failure",
        run_url="https://codecov",
        run_name="codecov",
        dispatch_key="k",
    )


def test_a_failure_whose_required_checks_are_green_dispatches_success():
    done, skipped = wipr.reconcile_failure(_failure(), {"head_sha": SHA, "conclusion": "success", "run_url": "u"})
    assert skipped is None and done.conclusion == "success" and done.dispatch_key == "k"


def test_a_failure_whose_required_checks_still_run_dispatches_nothing():
    done, skipped = wipr.reconcile_failure(_failure(), {"head_sha": SHA, "conclusion": None})
    assert done.conclusion == "failure" and skipped and skipped.startswith("not dispatched")


def test_a_required_failure_or_an_unread_head_still_dispatches_the_failure():
    assert wipr.reconcile_failure(_failure(), {"head_sha": SHA, "conclusion": "failure"}) == (_failure(), None)
    assert wipr.reconcile_failure(_failure(), None) == (_failure(), None)


def test_a_failure_on_a_head_the_pull_request_moved_past_dispatches_nothing() -> None:
    # A push landed before the red was delivered: that red is spent, the new head's CI decides.
    for live in ("success", "failure", None):
        done, skipped = wipr.reconcile_failure(_failure(), {"head_sha": "6" * 40, "conclusion": live})
        assert done == _failure() and skipped and skipped.startswith("not dispatched")
        assert "666666666666" in skipped


def test_a_spent_red_of_an_old_head_is_forgotten_in_the_payload() -> None:
    payload = {
        "pull_requests": [
            {
                "repo": "opencti-platform/opencti",
                "number": 19941,
                "head_sha": SHA,
                "ci": {
                    "head_sha": SHA,
                    "runs": {"r": {"run_id": 1, "status": "completed", "conclusion": "failure"}},
                    "failed_at": "2026-11-25T00:00:00+00:00",
                },
            }
        ]
    }
    assert wipr.red_pull_requests(payload)
    new = "6" * 40
    out, what = wipr.record_unconfirmed_failure(payload, _failure(), {"head_sha": new, "conclusion": None})
    assert what and "moved from" in what
    binding = out["pull_requests"][0]
    assert binding["head_sha"] == new and binding["ci"] == {"head_sha": new}
    assert wipr.red_pull_requests(out) == []
    # The new head's CI already recorded (its delivery came first): left as it is.
    again, what = wipr.record_unconfirmed_failure(out, _failure(), {"head_sha": new, "conclusion": None})
    assert what is None and again["pull_requests"][0]["ci"] == {"head_sha": new}


def test_a_success_is_never_read_again():
    green = wipr.CiCompleted("a/b", 1, SHA, "success", None, None, "k")
    assert wipr.reconcile_failure(green, {"head_sha": SHA, "conclusion": None}) == (green, None)


# -- The inbox settle --------------------------------------------------------------


def test_a_recorded_red_the_live_read_calls_green_is_voided():
    binding = {
        "repo": "owner/name",
        "number": 12,
        "state": "open",
        "head_sha": SHA,
        "ci": {
            "head_sha": SHA,
            "runs": {"codecov": {"status": "completed", "conclusion": "failure"}},
            "failed_at": "t",
        },
    }
    payload = {"pull_requests": [binding]}
    out, voided = wipr.void_stale_reds(payload, {("owner/name", 12): {"head_sha": SHA, "conclusion": "success"}})
    ci = out["pull_requests"][0]["ci"]
    assert len(voided) == 1 and "requires" in voided[0]
    assert "failed_at" not in ci and wipr._ci_conclusion(ci) == "success"
    assert payload["pull_requests"][0]["ci"]["failed_at"] == "t"  # never in place
    # A live failure on the same head leaves the red as it stands.
    _, voided = wipr.void_stale_reds(payload, {("owner/name", 12): {"head_sha": SHA, "conclusion": "failure"}})
    assert voided == []
