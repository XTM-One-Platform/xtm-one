"""The closing of a GitHub pull request or issue, read for the polling fallback.

``GET /repos/{repo}/pulls/{n}`` for a pull request (``merged`` says how it
closed), ``GET /repos/{repo}/issues/{n}`` for an issue (an issue number that
is a pull request answers nothing: the reference named the wrong kind). The
answer is the :class:`PullRequestEvent` the ``pull_request`` closed or
``issues`` closed delivery would have produced; ``None`` while open, or when
GitHub cannot be read (no access, an error, a timeout): an unreadable
reference keeps its item blocked, it never releases it.

:func:`read_commit_containment` answers whether a build carries a merge commit:
``GET /repos/{repo}/compare/{commit}...{build}`` reads ``ahead`` or
``identical`` when the build is that commit or a descendant of it.

:func:`read_successful_workflow_run` answers the first successful run of a
workflow created after a time (a release run deploying an infrastructure change):
``GET /repos/{repo}/actions/workflows/{workflow}/runs?status=success&created=>=<after>``.

:func:`read_pull_request_ci` answers the head of an open pull request and the
verdict of its checks (:func:`head_ci_verdict`), for a work item parked on it
whose CI delivery never came.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from app.core.egress import egress_client
from app.integrations.pull_request_events import PullRequestEvent

from . import _common
from ._common import _BASE_URL, _headers, parse_repo

REQUEST_TIMEOUT_SECONDS = 5.0
MAX_TOTAL_SECONDS = 8.0


def closing_event(repo: str, kind: str, number: int, data: Any) -> PullRequestEvent | None:
    """The closing event GitHub's answer *data* describes, ``None`` while open. Pure."""
    if not isinstance(data, dict) or data.get("state") != "closed":
        return None
    url = data.get("html_url") if isinstance(data.get("html_url"), str) else None
    closed_at = _text(data.get("closed_at"))
    if kind == "pull_request":
        merged = bool(data.get("merged") or data.get("merged_at"))
        sha = data.get("merge_commit_sha") if merged and isinstance(data.get("merge_commit_sha"), str) else None
        # The merge time when it merged, else the close time.
        when = (_text(data.get("merged_at")) if merged else None) or closed_at
        head = data.get("head") if isinstance(data.get("head"), dict) else {}
        return PullRequestEvent(
            kind="closed",
            repo=repo,
            pr_numbers=(number,),
            head_branch=_text(head.get("ref")),
            merged=merged,
            merge_sha=sha,
            url=url,
            closed_at=when,
        )
    if data.get("pull_request"):
        return None
    reason = data.get("state_reason") if isinstance(data.get("state_reason"), str) else None
    return PullRequestEvent(
        kind="issue_closed",
        repo=repo,
        pr_numbers=(number,),
        url=url,
        closed_at=closed_at,
        extra={"state_reason": reason},
    )


def _text(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def contains_commit(data: Any) -> bool | None:
    """Whether GitHub's compare answer *data* (base = the commit, head = the build) says the build carries it. Pure."""
    status = data.get("status") if isinstance(data, dict) else None
    if status in ("ahead", "identical"):
        return True
    if status in ("behind", "diverged"):
        return False
    return None


def _commitish(value: str) -> str | None:
    value = (value or "").strip().lower()
    return value if value and len(value) <= 64 and all(c in "0123456789abcdef" for c in value) else None


async def read_commit_containment(repo: str, commit: str, build_commit: str, token: str) -> bool | None:
    owner, name, err = parse_repo(repo)
    base, head = _commitish(commit), _commitish(build_commit)
    if err or base is None or head is None:
        return None
    async with egress_client(timeout=REQUEST_TIMEOUT_SECONDS) as client:
        resp, err = await _common.safe_request(
            client,
            "get",
            f"{_BASE_URL}/repos/{owner}/{name}/compare/{base}...{head}?per_page=1",
            "GitHub",
            f"read whether {head[:12]} carries {base[:12]} in {repo}",
            headers=_headers(token),
            max_total_seconds=MAX_TOTAL_SECONDS,
        )
    if err or resp is None:
        return None
    try:
        data = resp.json()
    except ValueError:
        return None
    return contains_commit(data)


_WORKFLOW_RE = re.compile(r"^[A-Za-z0-9._-]{1,200}$")
_BRANCH_RE = re.compile(r"^[A-Za-z0-9._/-]{1,200}$")


def first_successful_run(data: Any, after: str, title_contains: str | None = None) -> dict[str, Any] | None:
    """The oldest successful run of GitHub's runs answer *data* created at or after *after*; ``None`` when none. Pure.

    *after* and ``created_at`` are both ``YYYY-MM-DDTHH:MM:SSZ``, so they compare as text. *title_contains*, when
    given, keeps only the runs whose ``display_title`` holds it (the tag build of a release workflow, #34).
    """
    runs = data.get("workflow_runs") if isinstance(data, dict) else None
    found: dict[str, Any] | None = None
    for run in runs if isinstance(runs, list) else []:
        if not isinstance(run, dict) or run.get("conclusion") != "success":
            continue
        if title_contains and title_contains not in str(run.get("display_title") or ""):
            continue
        created = _text(run.get("created_at"))
        if created is None or created < after:
            continue
        if found is None or created < found["created_at"]:
            found = {
                "id": run.get("id"),
                "url": _text(run.get("html_url")),
                "created_at": created,
                "updated_at": _text(run.get("updated_at")),
                "head_sha": _text(run.get("head_sha")),
                "display_title": _text(run.get("display_title")),
            }
    return found


async def read_successful_workflow_run(
    repo: str, workflow: str, after: str, branch: str | None, title_contains: str | None, token: str
) -> dict[str, Any] | None:
    owner, name, err = parse_repo(repo)
    if err or not _WORKFLOW_RE.match(workflow or "") or (branch and not _BRANCH_RE.match(branch)):
        return None
    params = {"status": "success", "created": f">={after}", "per_page": "100", "exclude_pull_requests": "true"}
    if branch:
        params["branch"] = branch
    async with egress_client(timeout=REQUEST_TIMEOUT_SECONDS) as client:
        resp, err = await _common.safe_request(
            client,
            "get",
            f"{_BASE_URL}/repos/{owner}/{name}/actions/workflows/{workflow}/runs",
            "GitHub",
            f"read the successful runs of {workflow} in {repo}",
            headers=_headers(token),
            params=params,
            max_total_seconds=MAX_TOTAL_SECONDS,
            expected_statuses={404},
        )
    if resp is not None and resp.status_code == 404:
        # A workflow that does not exist (a typo, a renamed or removed file, a repository
        # the credential cannot read) never runs: a read failure, never "no run yet" (#386).
        raise LookupError(f"workflow {workflow} not found in {repo} (404)")
    if err or resp is None:
        return None
    try:
        data = resp.json()
    except ValueError:
        return None
    found = first_successful_run(data, after, title_contains)
    # A workflow with no run at all may be a reusable one: it never runs on its own,
    # so the reference would wait forever (#2211). A read failure, told on the item.
    if (
        found is None
        and _no_runs(data)
        and _WORKFLOW_FILE_RE.match(workflow)
        and await _call_only_workflow(owner, name, workflow, branch, token)
    ):
        raise LookupError(
            f"workflow {workflow} in {repo} only runs on workflow_call: it never has a run of its own; "
            "name the workflow that calls it instead"
        )
    return found


_WORKFLOW_FILE_RE = re.compile(r"^[A-Za-z0-9_.-]+\.ya?ml$")


def _no_runs(data: Any) -> bool:
    """Whether GitHub's runs answer *data* lists no run at all. Pure."""
    runs = data.get("workflow_runs") if isinstance(data, dict) else None
    return isinstance(runs, list) and not runs


def workflow_call_only(text: str) -> bool:
    """Whether the workflow file *text* is triggered by ``workflow_call`` alone (#2211). Pure.

    ``on`` reads as a string, a list or a mapping; YAML 1.1 loads the bare key
    ``on`` as ``True``, read too. Unparseable text answers ``False``.
    """
    import yaml

    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError:
        return False
    if not isinstance(data, dict):
        return False
    on = data.get("on", data.get(True))
    if isinstance(on, str):
        events = {on}
    elif isinstance(on, list | dict):
        events = {str(e) for e in on}
    else:
        return False
    return events == {"workflow_call"}


async def _call_only_workflow(owner: str, name: str, workflow: str, branch: str | None, token: str) -> bool:
    """Whether ``.github/workflows/<workflow>`` of *owner/name* only runs on ``workflow_call``; ``False`` when unreadable."""
    params = {"ref": branch} if branch else None
    async with egress_client(timeout=REQUEST_TIMEOUT_SECONDS) as client:
        resp, err = await _common.safe_request(
            client,
            "get",
            f"{_BASE_URL}/repos/{owner}/{name}/contents/.github/workflows/{workflow}",
            "GitHub",
            f"read the triggers of {workflow} in {owner}/{name}",
            headers={**_headers(token), "Accept": "application/vnd.github.raw+json"},
            params=params,
            max_total_seconds=MAX_TOTAL_SECONDS,
        )
    if err or resp is None:
        return False
    return workflow_call_only(getattr(resp, "text", "") or "")


#: Check run conclusions that do not fail a head (the webhook path's rule).
_PASSING = frozenset({"success", "neutral", "skipped"})


def head_ci_verdict(
    data: Any, required: frozenset[str] = frozenset(), now: datetime | None = None
) -> tuple[str | None, str | None]:
    """``(conclusion, url)`` of GitHub's check-runs answer *data* for one head commit. Pure.

    :func:`head_ci_decision` with the deciding check run left out.
    """
    conclusion, url, _ = head_ci_decision(data, required, now)
    return conclusion, url


def held_by_stale_only(data: Any, now: datetime | None = None) -> bool:
    """Whether a head's check-runs answer *data* has nothing failed and only stale pending checks. Pure.

    Every check not completed has been pending for over
    :data:`~.checks.STALE_OPTIONAL_SECONDS`: the base's required checks are read
    to tell whether they all passed (:func:`head_ci_decision`), since a check the
    base does not require may be all that holds the head.
    """
    from .checks import stale_checks

    runs = data.get("check_runs") if isinstance(data, dict) else None
    latest = latest_runs_by_name(runs if isinstance(runs, list) else [])
    pending = [r for r in latest if r.get("status") != "completed"]
    if not pending:
        return False
    failed = [r for r in latest if r.get("status") == "completed" and (r.get("conclusion") or "") not in _PASSING]
    return not failed and len(stale_checks(pending, frozenset(), now)) == len(pending)


def head_ci_decision(
    data: Any, required: frozenset[str] = frozenset(), now: datetime | None = None
) -> tuple[str | None, str | None, dict[str, Any] | None]:
    """``(conclusion, url, decider)`` of GitHub's check-runs answer *data* for one head commit. Pure.

    *decider* is ``{"check_id", "completed_at"}`` of the last check run to
    complete among those that decided (the failed ones on ``failure``, every
    one on ``success``): a re-run that ends with the same conclusion on the
    same head has a new id and a later time, so a reader can tell it from the
    conclusion it repeats (#11185).

    The latest run per check name decides (a re-run replaces the run it
    repeats): ``failure`` as soon as one completed without passing, ``success``
    once every one completed, ``None`` while one runs or when none reported.
    *required* holds the check names the base branch requires. When it names
    any, they decide alone (#11766): ``failure`` once one of them completed
    without passing, ``success`` once every one reported and passed, whatever
    a check the base does not require says (a Codecov project status read
    before the uploads, a labeler app). While one has not reported the head is
    not decided (a ``pull_request_target`` run completes before the pull
    request's own CI is created). Their ``success`` also waits for every other
    check of the head to complete: a base that requires one fast check (a CLA)
    would otherwise read green while the CI still runs (#12093), except a check
    the base does not require that has been pending for over
    :data:`~.checks.STALE_OPTIONAL_SECONDS` at *now* (a third-party app left in
    progress, xtm-one #195): it no longer holds the head. Without any, every
    check decides.
    A Codecov red read while another check of the head runs
    (:func:`~.checks.coverage_pending`) is provisional and never fails the head
    by itself: the coverage uploads may still turn it green (#12050).
    The url is the failing run's, else the last completed one's.
    """
    from .checks import coverage_pending, is_coverage_grade, stale_checks

    runs = data.get("check_runs") if isinstance(data, dict) else None
    latest = {r["name"]: r for r in latest_runs_by_name(runs if isinstance(runs, list) else [])}
    if not latest:
        return None, None, None
    provisional = coverage_pending(list(latest.values()))
    total = data.get("total_count") if isinstance(data, dict) else None
    partial_page = isinstance(total, int) and total > len(runs or [])
    if required:
        decision = _required_decision(latest, required, provisional)
        running = [r for r in latest.values() if r.get("status") != "completed"]
        if decision[0] == "success" and (partial_page or len(stale_checks(running, required, now)) < len(running)):
            # The required checks passed but the head's CI still runs: not decided yet.
            return None, None, None
        return decision
    done = [r for r in latest.values() if r.get("status") == "completed"]
    failed = [
        r for r in done if (r.get("conclusion") or "") not in _PASSING and not (provisional and is_coverage_grade(r))
    ]
    if failed:
        return "failure", _text(failed[0].get("html_url")), _decider(failed)
    if len(done) != len(latest) or partial_page:
        # One still runs, or the page did not hold every check: not decided.
        return None, None, None
    if required - latest.keys():
        # A required check has not reported: the head's own CI may not exist yet.
        return None, None, None
    return "success", _text(done[-1].get("html_url")), _decider(done)


def latest_runs_by_name(runs: list[Any]) -> list[dict]:
    """The latest run (highest id) of each check name of *runs*. Pure."""
    latest: dict[str, dict] = {}
    for run in runs:
        if not isinstance(run, dict) or not isinstance(run.get("name"), str):
            continue
        known = latest.get(run["name"])
        if known is None or (run.get("id") or 0) > (known.get("id") or 0):
            latest[run["name"]] = run
    return list(latest.values())


def _required_decision(
    latest: dict[str, dict[str, Any]], required: frozenset[str], provisional: bool = False
) -> tuple[str | None, str | None, dict[str, Any] | None]:
    """The verdict of the *required* checks among the *latest* run per name, the others left out. Pure.

    *provisional*: the head's coverage grades are not final, so a red one does not fail it.
    """
    from .checks import is_coverage_grade

    reported = [latest[name] for name in sorted(required) if name in latest]
    done = [r for r in reported if r.get("status") == "completed"]
    failed = [
        r for r in done if (r.get("conclusion") or "") not in _PASSING and not (provisional and is_coverage_grade(r))
    ]
    if failed:
        return "failure", _text(failed[0].get("html_url")), _decider(failed)
    if len(reported) != len(required) or len(done) != len(reported):
        return None, None, None
    return "success", _text(done[-1].get("html_url")), _decider(done)


def status_runs(data: Any) -> list[dict[str, Any]]:
    """The commit statuses of GitHub's combined-status answer *data* as check runs (name, status, conclusion). Pure.

    A required context may be a commit status rather than a check run (a
    Codecov project or patch status): read this way, the required checks are
    placed whichever API reported them.
    """
    statuses = data.get("statuses") if isinstance(data, dict) else None
    out: list[dict[str, Any]] = []
    for status in statuses if isinstance(statuses, list) else []:
        if not isinstance(status, dict) or not isinstance(status.get("context"), str):
            continue
        state = status.get("state")
        out.append(
            {
                "id": 0,
                "name": status["context"],
                "status": "in_progress" if state == "pending" else "completed",
                "conclusion": None if state == "pending" else ("success" if state == "success" else "failure"),
                "html_url": status.get("target_url"),
                "completed_at": status.get("updated_at"),
                "started_at": status.get("updated_at"),
            }
        )
    return out


def _decider(runs: list[dict]) -> dict:
    """The id and completion time of the last of *runs* to complete. Pure."""
    last = max(runs, key=lambda r: (str(r.get("completed_at") or ""), r.get("id") or 0))
    check_id = last.get("id")
    return {
        "check_id": check_id if isinstance(check_id, int) else None,
        "completed_at": _text(last.get("completed_at")),
    }


#: The GitHub app whose check suites are the head's workflow runs.
_ACTIONS_APP_SLUG = "github-actions"


def own_ci_suites(data: Any) -> frozenset[int] | None:
    """The check suite ids of GitHub's workflow runs answer *data* that are the head's own CI. Pure.

    A ``pull_request_target`` run is the base's workflow: GitHub starts it
    before the pull request's own workflows exist, so it is left out, as the
    webhook path marks it advisory (``pr_events``). ``None`` when the head has
    no workflow run at all (a repository whose CI is not GitHub Actions): its
    checks decide as before.
    """
    runs = data.get("workflow_runs") if isinstance(data, dict) else None
    if not isinstance(runs, list) or not runs:
        return None
    out: set[int] = set()
    for run in runs:
        if not isinstance(run, dict) or run.get("event") == "pull_request_target":
            continue
        suite = run.get("check_suite_id")
        if isinstance(suite, int):
            out.add(suite)
    return frozenset(out)


def own_ci_passed(data: Any, suites: frozenset[int]) -> bool:
    """Whether one check run of GitHub's check-runs answer *data* passed in the head's own CI *suites*. Pure.

    The polling twin of the webhook rule (``_ci_conclusion``): an advisory run
    (a ``pull_request_target`` workflow, an app other than GitHub Actions, a
    commit status) and a skipped or neutral run never make a head green alone.
    """
    runs = data.get("check_runs") if isinstance(data, dict) else None
    for run in latest_runs_by_name(runs if isinstance(runs, list) else []):
        app = run.get("app")
        suite = run.get("check_suite")
        slug = app.get("slug") if isinstance(app, dict) else None
        suite_id = suite.get("id") if isinstance(suite, dict) else None
        if (
            slug == _ACTIONS_APP_SLUG
            and isinstance(suite_id, int)
            and suite_id in suites
            and run.get("status") == "completed"
            and run.get("conclusion") == "success"
        ):
            return True
    return False


async def _own_ci_suites(owner: str, name: str, sha: str, token: str) -> frozenset[int] | None:
    """The check suites of *sha*'s own workflow runs (:func:`own_ci_suites`); ``None`` when unreadable."""
    async with egress_client(timeout=REQUEST_TIMEOUT_SECONDS) as client:
        resp, err = await _common.safe_request(
            client,
            "get",
            f"{_BASE_URL}/repos/{owner}/{name}/actions/runs",
            "GitHub",
            f"read the workflow runs of {sha[:12]} in {owner}/{name}",
            headers=_headers(token),
            params={"head_sha": sha, "per_page": "100"},
            max_total_seconds=MAX_TOTAL_SECONDS,
        )
    if err or resp is None:
        return None
    try:
        return own_ci_suites(resp.json())
    except ValueError:
        return None


async def _required_checks(owner: str, name: str, branch: str, token: str) -> frozenset[str]:
    """The check names *branch*'s rulesets require; empty when they cannot be read."""
    from .checks import required_contexts

    async with egress_client(timeout=REQUEST_TIMEOUT_SECONDS) as client:
        resp, err = await _common.safe_request(
            client,
            "get",
            f"{_BASE_URL}/repos/{owner}/{name}/rules/branches/{branch}",
            "GitHub",
            f"read the rules of {branch} in {owner}/{name}",
            headers=_headers(token),
            params={"per_page": "100"},
            max_total_seconds=MAX_TOTAL_SECONDS,
        )
    if err or resp is None:
        return frozenset()
    try:
        return required_contexts(resp.json(), None)
    except ValueError:
        return frozenset()


async def _commit_statuses(owner: str, name: str, sha: str, token: str) -> list[dict[str, Any]]:
    """The commit statuses of *sha* as check runs (:func:`status_runs`); empty when they cannot be read."""
    async with egress_client(timeout=REQUEST_TIMEOUT_SECONDS) as client:
        resp, err = await _common.safe_request(
            client,
            "get",
            f"{_BASE_URL}/repos/{owner}/{name}/commits/{sha}/status",
            "GitHub",
            f"read the commit statuses of {sha[:12]} in {owner}/{name}",
            headers=_headers(token),
            params={"per_page": "100"},
            max_total_seconds=MAX_TOTAL_SECONDS,
        )
    if err or resp is None:
        return []
    try:
        return status_runs(resp.json())
    except ValueError:
        return []


async def read_pull_request_ci(repo: str, number: int, token: str) -> dict | None:
    """The head of an open pull request and its CI verdict (``PullRequestCiReader``).

    Two reads: ``GET /repos/{repo}/pulls/{n}`` for the head, then
    ``GET /repos/{repo}/commits/{head}/check-runs`` (one page of 100) for the verdict;
    then ``GET /repos/{repo}/rules/branches/{base}`` and the head's commit
    statuses on a head that reads decided: a head whose required checks have not
    reported stays undecided, and a red the base does not require decides
    nothing (#11766). A head that reads ``success`` is also read for its
    workflow runs (``GET /repos/{repo}/actions/runs?head_sha=``): it stays
    undecided until one check of its own CI passed (:func:`own_ci_passed`), so
    a ``pull_request_target`` run that completes before the pull request's own
    workflows start never releases a parked item. ``conflict`` says whether it
    conflicts with its base (``mergeable`` false or ``mergeable_state`` dirty),
    which no CI event reports.
    """
    owner, name, err = parse_repo(repo)
    if err or number <= 0:
        return None
    async with egress_client(timeout=REQUEST_TIMEOUT_SECONDS) as client:
        resp, err = await _common.safe_request(
            client,
            "get",
            f"{_BASE_URL}/repos/{owner}/{name}/pulls/{number}",
            "GitHub",
            f"read the head of {repo}#{number}",
            headers=_headers(token),
            max_total_seconds=MAX_TOTAL_SECONDS,
        )
        if err or resp is None:
            return None
        try:
            pull = resp.json()
        except ValueError:
            return None
        if not isinstance(pull, dict) or pull.get("state") != "open":
            return None
        head = pull.get("head") if isinstance(pull.get("head"), dict) else {}
        sha = _commitish(str(head.get("sha") or ""))
        if sha is None:
            return None
        resp, err = await _common.safe_request(
            client,
            "get",
            f"{_BASE_URL}/repos/{owner}/{name}/commits/{sha}/check-runs?per_page=100",
            "GitHub",
            f"read the checks of {repo}#{number} at {sha[:12]}",
            headers=_headers(token),
            max_total_seconds=MAX_TOTAL_SECONDS,
        )
    if err or resp is None:
        return None
    try:
        checks = resp.json()
    except ValueError:
        return None
    required: frozenset[str] = frozenset()
    base = pull.get("base") if isinstance(pull.get("base"), dict) else {}
    base_ref = _text(base.get("ref"))
    if (
        base_ref
        and _BRANCH_RE.match(base_ref)
        and (head_ci_verdict(checks)[0] in ("success", "failure") or held_by_stale_only(checks))
    ):
        required = await _required_checks(owner, name, base_ref, token)
        if required and isinstance(checks, dict) and isinstance(checks.get("check_runs"), list):
            statuses = await _commit_statuses(owner, name, sha, token)
            known = {r.get("name") for r in checks["check_runs"] if isinstance(r, dict)}
            extra = [r for r in statuses if r["name"] not in known]
            if extra:
                checks = {**checks, "check_runs": [*checks["check_runs"], *extra]}
    conclusion, url, decider = head_ci_decision(checks, required)
    if conclusion == "success":
        # Every check reported so far passed, but they may all be advisory: the
        # pull request's own workflows not started yet. Not decided until one of
        # them passed; an unreadable runs list keeps the verdict (the webhook decides).
        suites = await _own_ci_suites(owner, name, sha, token)
        if suites is not None and not own_ci_passed(checks, suites):
            conclusion, url, decider = None, None, None
    draft = pull.get("draft")
    from .checks import coverage_pending, is_conflict

    listed = checks.get("check_runs") if isinstance(checks, dict) else None
    full_ci = None
    merge_queue = False
    review_decision = None
    queue_base = False
    can_bypass = False
    if conclusion == "success" and draft is False and base_ref and _BRANCH_RE.match(base_ref):
        full_ci = await _full_ci_state(owner, name, base_ref, sha, token)
        merge_queue, review_decision, queue_base, can_bypass = await _merge_gates(owner, name, number, base_ref, token)
    return {
        "head_sha": sha,
        "head_branch": _text(head.get("ref")),
        "draft": draft if isinstance(draft, bool) else None,
        "conclusion": conclusion,
        "run_url": url,
        # A conflict with the base sends no CI event: this read is its only wake.
        "conflict": is_conflict(pull.get("mergeable"), pull.get("mergeable_state")),
        # A Codecov grade read before its uploads: its red is provisional (#12050).
        "coverage_pending": coverage_pending(
            latest_runs_by_name([r for r in listed if isinstance(r, dict)] if isinstance(listed, list) else [])
        ),
        **(decider or {}),
        **({"full_ci": full_ci} if full_ci else {}),
        # In the base's merge queue: its merge (or its removal) is a wake still to come (#404).
        **({"merge_queue": True} if merge_queue else {}),
        # Waiting on a review: the review event, not a green hand-off, is its next wake (#2213).
        **({"review_decision": review_decision} if review_decision else {}),
        # A base with a merge queue, or an account GitHub lets bypass the base's
        # requirements: a missing review is not a block, GitHub decides (#2324).
        **({"queue_base": True} if queue_base else {}),
        **({"can_bypass": True} if can_bypass else {}),
    }


_IN_MERGE_QUEUE_QUERY = (
    "query($owner: String!, $name: String!, $number: Int!, $base: String!) {"
    " repository(owner: $owner, name: $name) { mergeQueue(branch: $base) { id }"
    " pullRequest(number: $number) { isInMergeQueue reviewDecision viewerCanMergeAsAdmin } } }"
)


async def _merge_gates(
    owner: str, name: str, number: int, base: str, token: str
) -> tuple[bool, str | None, bool, bool]:
    """The merge gates of pull request *number*, read from GitHub in one query.

    Whether it is in its base's merge queue (#404), its review decision (#2213),
    whether its base *base* has a merge queue and whether GitHub lets this
    account bypass the base's requirements (``viewerCanMergeAsAdmin``, #2324).
    ``(False, None, False, False)`` when unreadable.
    """
    async with egress_client(timeout=REQUEST_TIMEOUT_SECONDS) as client:
        data, err = await _common.gh_graphql(
            client,
            _headers(token),
            _IN_MERGE_QUEUE_QUERY,
            {"owner": owner, "name": name, "number": number, "base": base},
            action=f"read whether {owner}/{name}#{number} is in the merge queue",
        )
    if err or not isinstance(data, dict):
        return False, None, False, False
    repo_raw = data.get("repository")
    repo: dict[str, Any] = repo_raw if isinstance(repo_raw, dict) else {}
    pull_raw = repo.get("pullRequest")
    pull: dict[str, Any] = pull_raw if isinstance(pull_raw, dict) else {}
    decision = pull.get("reviewDecision")
    return (
        pull.get("isInMergeQueue") is True,
        decision if isinstance(decision, str) and decision else None,
        isinstance(repo.get("mergeQueue"), dict),
        pull.get("viewerCanMergeAsAdmin") is True,
    )


async def _full_ci_state(owner: str, name: str, base_ref: str, sha: str, token: str) -> str | None:
    """The state of the full CI run ``[merge] full_ci_workflows`` requires on *sha* (#12040).

    ``running`` while one gated workflow is queued or running on the head (its
    completion is a wake still to come), else the first non-green state
    (``red``, ``absent``, ``unreadable``), ``green`` when all are green;
    ``None`` when the base declares no gate or the gate cannot be read.
    """
    from .full_ci_gate import read_gate, workflow_verdict

    headers = _headers(token)
    async with egress_client(timeout=REQUEST_TIMEOUT_SECONDS) as client:
        workflows, problem = await read_gate(client, owner, name, base_ref, headers)
        if problem or not workflows:
            return None
        states = [(await workflow_verdict(client, owner, name, w, sha, headers)).state for w in workflows]
    if "running" in states:
        return "running"
    return next((s for s in states if s != "green"), "green")


#: Pull requests of one head branch read per call: a release branch reopens one at a time.
BRANCH_PULLS_PER_PAGE = 10


def branch_closings(repo: str, branch: str, data: Any) -> list[PullRequestEvent]:
    """The closings of GitHub's pulls answer *data* whose head is *branch*, oldest number first. Pure."""
    out: list[PullRequestEvent] = []
    for pull in data if isinstance(data, list) else []:
        if not isinstance(pull, dict) or not isinstance(pull.get("number"), int):
            continue
        head = pull.get("head") if isinstance(pull.get("head"), dict) else {}
        if head.get("ref") != branch:
            continue
        event = closing_event(repo, "pull_request", pull["number"], pull)
        if event is not None:
            out.append(event)
    return sorted(out, key=lambda e: e.pr_numbers[0])


async def read_branch_closings(repo: str, branch: str, token: str) -> list[PullRequestEvent]:
    """The closed pull requests of head *branch* in *repo* (``BranchClosingsReader``); ``[]`` when unreadable.

    ``GET /repos/{repo}/pulls?head={owner}:{branch}&state=closed``, newest first, one page.
    """
    owner, name, err = parse_repo(repo)
    if err or not _BRANCH_RE.match(branch or ""):
        return []
    async with egress_client(timeout=REQUEST_TIMEOUT_SECONDS) as client:
        resp, err = await _common.safe_request(
            client,
            "get",
            f"{_BASE_URL}/repos/{owner}/{name}/pulls",
            "GitHub",
            f"read the closed pull requests of {branch} in {repo}",
            headers=_headers(token),
            params={
                "head": f"{owner}:{branch}",
                "state": "closed",
                "sort": "created",
                "direction": "desc",
                "per_page": str(BRANCH_PULLS_PER_PAGE),
            },
            max_total_seconds=MAX_TOTAL_SECONDS,
        )
    if err or resp is None:
        return []
    try:
        data = resp.json()
    except ValueError:
        return []
    return branch_closings(repo.lower(), branch, data)


async def read_reference_state(repo: str, kind: str, number: int, token: str) -> PullRequestEvent | None:
    owner, name, err = parse_repo(repo)
    if err or kind not in ("pull_request", "issue") or number <= 0:
        return None
    path = "pulls" if kind == "pull_request" else "issues"
    async with egress_client(timeout=REQUEST_TIMEOUT_SECONDS) as client:
        resp, err = await _common.safe_request(
            client,
            "get",
            f"{_BASE_URL}/repos/{owner}/{name}/{path}/{number}",
            "GitHub",
            f"read the state of {repo}#{number}",
            headers=_headers(token),
            max_total_seconds=MAX_TOTAL_SECONDS,
        )
    if err or resp is None:
        return None
    try:
        data = resp.json()
    except ValueError:
        return None
    closing = closing_event(repo.lower(), kind, number, data)
    if closing is None and kind == "pull_request" and isinstance(data, dict) and data.get("state") == "open":
        # Open: a merge queue removal sends no closing, and its webhook may never come (#606).
        return await _merge_queue_removal(owner, name, number, token)
    return closing


_REMOVED_FROM_QUEUE_QUERY = (
    "query($owner: String!, $name: String!, $number: Int!) {"
    " repository(owner: $owner, name: $name) { pullRequest(number: $number) {"
    " state isInMergeQueue url headRefName headRefOid"
    " timelineItems(last: 1, itemTypes: [REMOVED_FROM_MERGE_QUEUE_EVENT]) {"
    " nodes { ... on RemovedFromMergeQueueEvent { createdAt reason } } } } } }"
)


def dequeued_from_timeline(repo: str, number: int, data: Any) -> PullRequestEvent | None:
    """The ``dequeued`` event of an open pull request out of its merge queue, ``None`` otherwise. Pure.

    The latest removal of its timeline counts only while the pull request is open
    and not back in the queue; its time stamps the event, so one removal wakes once.
    """
    repo_data = data.get("repository") if isinstance(data, dict) else None
    pull = repo_data.get("pullRequest") if isinstance(repo_data, dict) else None
    if not isinstance(pull, dict) or pull.get("state") != "OPEN" or pull.get("isInMergeQueue") is True:
        return None
    raw_timeline = pull.get("timelineItems")
    timeline: dict[str, Any] = raw_timeline if isinstance(raw_timeline, dict) else {}
    raw_nodes = timeline.get("nodes")
    nodes: list[Any] = raw_nodes if isinstance(raw_nodes, list) else []
    removal = nodes[-1] if nodes and isinstance(nodes[-1], dict) else None
    if removal is None:
        return None
    when = _text(removal.get("createdAt"))
    if not when:
        return None
    reason = (_text(removal.get("reason")) or "unknown").lower()
    from .pr_events import queue_run_hint

    hint = queue_run_hint(repo.lower(), reason, when)
    return PullRequestEvent(
        kind="dequeued",
        repo=repo.lower(),
        pr_numbers=(number,),
        head_branch=_text(pull.get("headRefName")),
        head_sha=_text(pull.get("headRefOid")),
        conclusion=reason,
        url=_text(pull.get("url")),
        closed_at=when,
        extra={"queue_run_hint": hint} if hint else {},
    )


async def _merge_queue_removal(owner: str, name: str, number: int, token: str) -> PullRequestEvent | None:
    """The latest merge queue removal of open pull request *number*, ``None`` when none or unreadable."""
    async with egress_client(timeout=REQUEST_TIMEOUT_SECONDS) as client:
        data, err = await _common.gh_graphql(
            client,
            _headers(token),
            _REMOVED_FROM_QUEUE_QUERY,
            {"owner": owner, "name": name, "number": number},
            action=f"read the merge queue removals of {owner}/{name}#{number}",
        )
    if err or not isinstance(data, dict):
        return None
    return dequeued_from_timeline(f"{owner}/{name}", number, data)
