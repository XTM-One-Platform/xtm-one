"""``enriched.head.required``: the base branch's required checks on a CI head (#11419).

``enriched.head.checks.all_green`` needs every check of the head: a
non-required third-party check left in progress (a coverage upload) kept an
approved pull request whose only required check was green from ever waking the
merge filter. The required facet reads the base branch's required contexts and
places the head's latest check runs and statuses against them.
"""

from __future__ import annotations

import pytest

from app.integrations.providers.github import event_enrichment as gh_enrichment
from app.services.integration_event_filters import payload_filter_reason

REPO = "OpenAEV-Platform/openaev"
SHA = "d" * 40
GATE = "pipeline / Pipeline Gate"


def _run(run_id: int, name: str, status: str = "completed", conclusion: str | None = "success", at: str = "") -> dict:
    return {
        "id": run_id,
        "name": name,
        "status": status,
        "conclusion": conclusion,
        "completed_at": at or None,
        "app": {"slug": "github-actions"},
    }


def _check_run_event(run_id: int = 1, completed_at: str = "2026-11-12T07:20:00Z") -> dict:
    return {
        "action": "completed",
        "check_run": {"id": run_id, "head_sha": SHA, "completed_at": completed_at},
        "repository": {"full_name": REPO},
    }


def _pr(number: int = 8868, base: str = "main") -> dict:
    return {
        "number": number,
        "state": "open",
        "draft": False,
        "user": {"login": "SamuelHassine"},
        "head": {"sha": SHA, "ref": "issue/3842-x", "repo": {"full_name": REPO}},
        "base": {"ref": base, "repo": {"full_name": REPO}},
        "labels": [],
    }


class _Resp:
    def __init__(self, body):
        self._body = body

    def json(self):
        return self._body


def _github(monkeypatch, *, runs: list[dict], pulls: list[dict] | None = None, required: list[str] | None = None):
    calls: list[str] = []

    async def _request(_client, _method, url, *_a, params=None, **_k):
        calls.append(url)
        if url.endswith("/pulls"):
            return _Resp([_pr()] if pulls is None else pulls), None
        if url.endswith("/check-runs"):
            return _Resp({"total_count": len(runs), "check_runs": runs}), None
        if url.endswith("/status"):
            return _Resp({"state": "pending", "statuses": []}), None
        if "/rules/branches/" in url:
            rule = {"type": "required_status_checks", "parameters": {"required_status_checks": []}}
            rule["parameters"]["required_status_checks"] = [{"context": c} for c in (required or [])]
            return _Resp([rule]), None
        if url.endswith("/protection/required_status_checks"):
            return None, "Error: 404 Not Found"
        if url.endswith("/actions/runs"):
            return _Resp({"total_count": 0, "workflow_runs": []}), None
        raise AssertionError(url)

    monkeypatch.setattr(gh_enrichment._common, "safe_request", _request)
    monkeypatch.setattr(gh_enrichment.gh_checks._common, "safe_request", _request)
    return calls


@pytest.mark.parametrize(
    ("keys", "expected"),
    [
        (("enriched.head.required.all_passed",), frozenset({"required"})),
        (("enriched.head.required",), frozenset({"required"})),
        (("enriched.head.checks.all_green", "enriched.head.required.all_passed"), frozenset({"checks", "required"})),
        (("enriched.head.checks.all_green",), frozenset({"checks"})),
        (("enriched.pull_request.number",), frozenset()),
    ],
)
def test_the_required_facet_follows_the_filter_keys(keys, expected):
    assert gh_enrichment.facets("check_run.completed", _check_run_event(), keys) == expected


def test_a_non_required_check_left_in_progress_does_not_hold_the_required_verdict():
    runs = [
        _run(1, GATE, at="2026-11-12T07:20:00Z"),
        _run(2, "codecov/patch/backend", status="in_progress", conclusion=None),
        _run(3, "codecov/project/e2e", conclusion="failure", at="2026-11-12T07:25:00Z"),
    ]
    facts = gh_enrichment.required_facts(frozenset({GATE}), runs, [], "2026-11-12T07:20:00Z")
    assert facts["all_passed"] is True
    assert (facts["total"], facts["passed"], facts["pending"], facts["failed"]) == (1, 1, 0, 0)
    assert facts["last_to_complete"] is True
    # The full-head verdict still reads not green on the same runs.
    assert gh_enrichment.check_facts(runs)["all_green"] is False


@pytest.mark.parametrize(
    ("runs", "bucket"),
    [
        ([_run(1, GATE, status="in_progress", conclusion=None)], "pending"),
        ([_run(1, GATE, conclusion="failure")], "failed"),
        ([_run(1, "other")], "missing"),
    ],
)
def test_a_required_check_pending_failed_or_never_reported_is_never_passed(runs, bucket):
    facts = gh_enrichment.required_facts(frozenset({GATE}), runs, [])
    assert facts["all_passed"] is False
    assert facts[f"{bucket}_names"] == [GATE]


def test_no_required_context_never_reads_passed():
    facts = gh_enrichment.required_facts(frozenset(), [_run(1, GATE)], [])
    assert facts["all_passed"] is False and facts["total"] == 0


def test_a_required_commit_status_counts_and_a_skipped_required_check_passes():
    statuses = [{"context": "ci/legacy", "state": "success", "updated_at": "2026-11-12T07:00:00Z"}]
    facts = gh_enrichment.required_facts(
        frozenset({"ci/legacy", GATE}), [_run(1, GATE, conclusion="skipped")], statuses
    )
    assert facts["all_passed"] is True and facts["passed"] == 2


def test_an_earlier_required_completion_is_not_the_last_to_complete():
    runs = [_run(1, GATE, at="2026-11-12T07:20:00Z"), _run(2, "lint", at="2026-11-12T07:30:00Z")]
    required = frozenset({GATE, "lint"})
    assert gh_enrichment.required_facts(required, runs, [], "2026-11-12T07:20:00Z")["last_to_complete"] is False
    assert gh_enrichment.required_facts(required, runs, [], "2026-11-12T07:30:00Z")["last_to_complete"] is True


@pytest.mark.asyncio
async def test_fetch_reads_the_base_branch_rules_and_a_filter_admits_the_head(monkeypatch):
    runs = [_run(1, GATE, at="2026-11-12T07:20:00Z"), _run(2, "codecov/patch/backend", "in_progress", None)]
    calls = _github(monkeypatch, runs=runs, required=[GATE])
    event = _check_run_event()
    facts = await gh_enrichment.fetch("check_run.completed", event, "tok", facets=frozenset({"required"}))
    required = facts["head"]["required"]
    assert required["all_passed"] is True and required["base"] == "main"
    assert "checks" not in facts["head"]
    assert any("/rules/branches/main" in url for url in calls)
    flt = {"action": "completed", "enriched.head.required.all_passed": True, "enriched.pull_request.number": 8868}
    assert payload_filter_reason({**event, "enriched": facts}, flt) is None


@pytest.mark.asyncio
async def test_a_head_with_no_open_pull_request_reads_no_rules_and_never_passes(monkeypatch):
    calls = _github(monkeypatch, runs=[_run(1, GATE)], pulls=[], required=[GATE])
    facts = await gh_enrichment.fetch("check_run.completed", _check_run_event(), "tok", facets=frozenset({"required"}))
    assert facts["head"]["required"]["all_passed"] is False and facts["head"]["required"]["base"] is None
    assert not any("/rules/branches/" in url for url in calls)


@pytest.mark.asyncio
async def test_a_checks_only_or_pull_request_only_filter_reads_no_rules(monkeypatch):
    calls = _github(monkeypatch, runs=[_run(1, GATE)], required=[GATE])
    for facets in (frozenset({"checks"}), frozenset(), None):
        facts = await gh_enrichment.fetch("check_run.completed", _check_run_event(), "tok", facets=facets)
        assert "required" not in facts["head"]
    assert not any("/rules/branches/" in url or url.endswith("/status") for url in calls)
