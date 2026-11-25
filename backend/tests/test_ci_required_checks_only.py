"""Only the base branch's required checks make a head red (#11807).

The enrichment of a CI event (``enriched.head.checks``) and the CI recorded on
a work item's pull request weigh only the checks the base branch requires: a
red on another check (a labeler whose app token read 404, a provisional
Codecov grade) neither blocks ``all_green``, nor wakes a red run through
``first_failure``, nor leaves the item's pull request red for the red release.
A red with a later run of the same check on the same head is superseded.
"""

from __future__ import annotations

import pytest

from app.integrations.providers.github import event_enrichment as gh_enrichment
from app.services import work_item_pull_requests as wipr
from app.services.integration_event_filters import payload_filter_reason

REPO = "OpenCRQ-Platform/opencrq"
SHA = "c" * 40
GATE = "Flow checkers"
LABEL = "Label the author's side"


def _done(run_id: int, name: str, conclusion: str = "success", at: str = "2026-11-14T06:20:00Z", app: str = "") -> dict:
    return {
        "id": run_id,
        "name": name,
        "status": "completed",
        "conclusion": conclusion,
        "completed_at": at,
        "app": {"slug": app or "github-actions"},
    }


# -- enriched.head.checks --------------------------------------------------------------


def test_a_non_required_red_beside_green_required_checks_is_green():
    runs = [_done(1, GATE), _done(2, LABEL, "failure", "2026-11-14T06:21:00Z")]
    facts = gh_enrichment.check_facts(runs, 2, "2026-11-14T06:21:00Z", frozenset({GATE}))
    assert facts["all_green"] is True and facts["failed"] == 0
    assert facts["first_failure"] is False and facts["first_failed"] is None
    assert facts["failed_not_required"] == 1 and facts["failed_not_required_names"] == [LABEL]
    by_name = {n["name"]: n for n in facts["names"]}
    assert by_name[GATE]["required"] is True and by_name[LABEL]["required"] is False
    # Not read: every check decides, as before.
    old = gh_enrichment.check_facts(runs, 2, "2026-11-14T06:21:00Z")
    assert old["all_green"] is False and old["first_failure"] is True and old["names"][0]["required"] is None


def test_a_required_red_still_wakes():
    runs = [_done(1, GATE, "failure"), _done(2, LABEL, "failure", "2026-11-14T06:21:00Z")]
    facts = gh_enrichment.check_facts(runs, 1, "2026-11-14T06:20:00Z", frozenset({GATE}))
    assert facts["first_failure"] is True and facts["all_green"] is False
    assert facts["failed_names"] == [GATE]
    assert gh_enrichment.check_facts(runs, 2, None, frozenset({GATE}))["first_failure"] is False


def test_a_red_superseded_by_a_later_green_run_of_the_same_check_does_not_count():
    runs = [_done(1, GATE), _done(2, LABEL, "failure"), _done(3, LABEL, "success", "2026-11-14T06:25:00Z")]
    facts = gh_enrichment.check_facts(runs, 2)
    assert facts["first_failure"] is False and facts["failed"] == 0 and facts["all_green"] is True
    assert [n["name"] for n in facts["names"]] == [GATE, LABEL]


def test_a_codecov_grade_is_provisional():
    runs = [_done(1, "codecov/project/opencti", "failure", app="codecov"), _done(2, GATE)]
    names = {n["name"]: n for n in gh_enrichment.check_facts(runs)["names"]}
    assert names["codecov/project/opencti"]["provisional"] is True and names[GATE]["provisional"] is False


class _Resp:
    def __init__(self, body):
        self._body = body

    def json(self):
        return self._body


def _github(monkeypatch, runs: list[dict], required: list[str]) -> list[str]:
    calls: list[str] = []
    pull = {
        "number": 3149,
        "state": "open",
        "draft": False,
        "user": {"login": "SamuelHassine"},
        "head": {"sha": SHA, "ref": "chore/x", "repo": {"full_name": REPO}},
        "base": {"ref": "main", "repo": {"full_name": REPO}},
        "labels": [],
        "created_at": "2026-11-14T05:00:00Z",
    }

    async def _request(_client, _method, url, *_a, params=None, **_k):
        calls.append(url)
        if url.endswith("/pulls"):
            return _Resp([pull]), None
        if url.endswith("/check-runs"):
            return _Resp({"total_count": len(runs), "check_runs": runs}), None
        if "/rules/branches/" in url:
            checks = [{"context": c} for c in required]
            return _Resp([{"type": "required_status_checks", "parameters": {"required_status_checks": checks}}]), None
        if url.endswith("/protection/required_status_checks"):
            return None, "Error: 404 Not Found"
        if url.endswith("/actions/runs"):
            return _Resp({"total_count": 0, "workflow_runs": []}), None
        if url.endswith("/status"):
            return _Resp({"state": "success", "statuses": []}), None
        raise AssertionError(url)

    monkeypatch.setattr(gh_enrichment._common, "safe_request", _request)
    monkeypatch.setattr(gh_enrichment.gh_checks._common, "safe_request", _request)
    return calls


def _delivery(run: dict) -> dict:
    return {
        "action": "completed",
        "check_run": {
            "id": run["id"],
            "head_sha": SHA,
            "conclusion": run["conclusion"],
            "completed_at": run["completed_at"],
        },
        "repository": {"full_name": REPO},
    }


@pytest.mark.asyncio
async def test_the_red_check_filter_drops_a_non_required_red(monkeypatch):
    runs = [_done(1, GATE), _done(2, LABEL, "failure", "2026-11-14T06:21:00Z")]
    calls = _github(monkeypatch, runs, [GATE])
    delivery = _delivery(runs[1])
    facts = await gh_enrichment.fetch("check_run.completed", delivery, "tok")
    red = {"action": "completed", "enriched.head.checks.first_failure": True}
    assert payload_filter_reason({**delivery, "enriched": facts}, red) is not None
    assert facts["head"]["checks"]["failed_not_required_names"] == [LABEL]
    assert sum("/rules/branches/" in u for u in calls) == 1


@pytest.mark.asyncio
async def test_a_required_red_is_delivered_and_a_green_head_reads_no_rule(monkeypatch):
    runs = [_done(1, GATE, "failure")]
    _github(monkeypatch, runs, [GATE])
    delivery = _delivery(runs[0])
    facts = await gh_enrichment.fetch("check_run.completed", delivery, "tok")
    red = {"action": "completed", "enriched.head.checks.first_failure": True}
    assert payload_filter_reason({**delivery, "enriched": facts}, red) is None
    green = [_done(1, GATE)]
    calls = _github(monkeypatch, green, [GATE])
    await gh_enrichment.fetch("check_run.completed", _delivery(green[0]), "tok")
    assert not any("/rules/branches/" in u for u in calls)


# -- The CI recorded on a work item ------------------------------------------------------


def _failure() -> wipr.CiCompleted:
    return wipr.CiCompleted(REPO.lower(), 3149, SHA, "failure", "https://x/2", LABEL, "k")


def _payload() -> dict:
    return {
        "pull_requests": [
            {
                "repo": REPO.lower(),
                "number": 3149,
                "state": "open",
                "head_sha": SHA,
                "ci": {
                    "head_sha": SHA,
                    "runs": {
                        "workflow:1": {"run_id": 1, "status": "completed", "conclusion": "success"},
                        "workflow:2": {"run_id": 2, "status": "completed", "conclusion": "failure"},
                    },
                    "failed_at": "2026-11-14T06:21:00Z",
                },
            }
        ]
    }


def test_an_unconfirmed_red_no_longer_reads_red_on_the_item():
    payload = _payload()
    assert wipr.red_pull_requests(payload)
    out, what = wipr.record_unconfirmed_failure(payload, _failure(), {"head_sha": SHA, "conclusion": None})
    assert what and "does not require" in what
    assert wipr.red_pull_requests(out) == []
    ci = out["pull_requests"][0]["ci"]
    assert "failed_at" not in ci and ci["runs"]["workflow:2"]["not_required"] is True
    assert wipr._ci_conclusion(ci) == "success"
    assert payload["pull_requests"][0]["ci"]["failed_at"]  # never in place


def test_a_live_green_is_recorded_and_a_live_red_left_as_it_stands():
    out, what = wipr.record_unconfirmed_failure(_payload(), _failure(), {"head_sha": SHA, "conclusion": "success"})
    assert what and wipr._ci_conclusion(out["pull_requests"][0]["ci"]) == "success"
    for state in ({"head_sha": SHA, "conclusion": "failure"}, None):
        out, what = wipr.record_unconfirmed_failure(_payload(), _failure(), state)
        assert what is None and wipr.red_pull_requests(out)
    # A head the pull request moved past: its red is spent, the live head starts clean (#2014).
    out, what = wipr.record_unconfirmed_failure(_payload(), _failure(), {"head_sha": "d" * 40, "conclusion": "success"})
    assert what and "moved from" in what and wipr.red_pull_requests(out) == []


def test_a_new_red_run_after_a_marked_one_reads_red_again():
    out, _ = wipr.record_unconfirmed_failure(_payload(), _failure(), {"head_sha": SHA, "conclusion": None})
    ci = out["pull_requests"][0]["ci"]
    ci["runs"]["workflow:3"] = {"run_id": 3, "status": "completed", "conclusion": "failure"}
    assert wipr._ci_conclusion(ci) == "failure"
