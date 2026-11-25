"""The open pull request and the check state of a GitHub CI or push event, for trigger filters.

``workflow_run``, ``check_suite``, ``check_run``, ``status`` and ``push``
deliveries name a head commit and who pushed it, never the author of the pull
request the commit is the head of (``workflow_run.pull_requests`` holds numbers
and refs only, and is empty for a pull request from a fork), and never the
state of the other checks on that commit. The enricher reads
``GET /repos/{repo}/commits/{sha}/pulls`` with the integration's token and
answers the open pull requests whose head is that commit:

``enriched.pull_request``: ``number``, ``state``, ``draft``, ``user.login``,
``author_association``, ``labels[].name``, ``base.ref``, ``head.ref``,
``head.repo.full_name`` - the one ``workflow_run.pull_requests`` names when it
names one, else the first; ``null`` when the commit heads no open pull request.
``enriched.pull_requests``: every one of them, in that shape.

On the CI events (not ``push``) it also reads
``GET /repos/{repo}/commits/{sha}/check-runs`` (the latest run of each check)
and answers ``enriched.head``: ``sha`` and ``checks`` - ``total``,
``completed``, ``pending`` (queued or in progress), ``failed`` (failure,
cancelled, timed out, action required, stale), ``skipped``, ``neutral``,
``all_completed`` (at least one check and every one completed), ``all_green``
(``all_completed`` and none failed) and ``names[]`` (``name``, ``status``,
``conclusion``). The event that turns ``all_green`` true is the last check to
finish on the head, whatever its workflow name: a filter on
``enriched.head.checks.all_green`` wakes once per fully green head without a
list of workflow names.

Check runs never carry a commit status (CircleCI, Snyk, any app on the Status
API), so when the check runs read ``all_green`` the head's commit statuses are
read too (``GET /repos/{repo}/commits/{sha}/status``, on a candidate green head
only): a pending status counts as a pending check and a red one as a failed
check (``status:<context>``), weighed against the base's required contexts
when they were read, so a status still running never wakes a green filter.
A head whose newest status changed under ``STATUS_SETTLE_SECONDS`` (60 s) ago
is held pending (``status:settling``): a status app creates the statuses of a
later stage only after the earlier one ended, and the head watch reads the
head again before it wakes green.

The red side has its own facts, because a workflow run completes only when its
last job ends (a slow or hung shard holds a failed one back for up to its
``timeout-minutes``): ``failed_names[]`` (the failed checks) and
``first_failed`` (``name``, ``id``, ``completed_at`` of the earliest failed
check, ``null`` when none failed). On a ``check_run`` completion,
``first_failure`` is true when the delivered check is that earliest failed
check, false on every other event: a filter on
``enriched.head.checks.first_failure`` wakes once per red head, on its first
failed job, while the other jobs still run. A re-run of the failed jobs gives
new check runs, so a head that fails again wakes again.

``all_green`` stays true on every completion delivered after the head turned
green: a check run delivered late or redelivered reads green too. On a
completion, ``last_to_complete`` is true when the delivery's own completion
time (``check_run.completed_at``, ``check_suite.updated_at``,
``workflow_run.updated_at``, a status's ``updated_at``) is not older than any
completed check run of the head (ties allowed), false on every other event: a
filter on ``enriched.head.checks.all_green`` and
``enriched.head.checks.last_to_complete`` wakes on the completion that turned
the head green, never on a stale one.

A head whose check runs all read green is not green yet while one of its
GitHub Actions workflow runs is not completed: a re-run of the failed jobs puts
its workflow run back in the queue before the new check runs are listed, and a
watched or late delivery read in that window would wake a head about to run
again. So when the check runs alone read ``all_green``, the head's workflow
runs are read too (``GET /repos/{repo}/actions/runs?head_sha=``) and each one
not completed counts as a pending check (named ``workflow:<name>``): one more
read, on a candidate green head only. GitHub lists an app's check (a draft skip)
seconds after a push, before it creates the push's workflow runs, and creates a
pull request's ``pull_request`` runs up to about 100 seconds after it is opened,
after a ``pull_request_target`` run may already have completed, and up to
several minutes after it is opened (#10879): a head whose first check started
less than :data:`WORKFLOWS_GRACE_SECONDS` ago with no workflow run at all, or
whose open pull request was opened less than :data:`PR_WORKFLOWS_GRACE_SECONDS`
ago without any ``pull_request`` workflow run, counts one
pending ``workflow:not-created``, so it is watched and read again rather than
woken green before its CI exists.

A completion (a ``completed`` workflow run, check suite or check run, a final
commit status) changes the check state it reports, so its cache key carries the
delivery's own identity: it is never answered from a read taken before it.

The check runs are read only for a filter that reads them (:func:`facets`: a
key ``enriched``, ``enriched.head`` or under ``enriched.head.checks``): a
filter on ``enriched.pull_request.*`` alone reads the pull requests only, so a
slow check-run read, a GitHub error on it or a head past the cap never drops it.

When a head heads an open pull request and one of its checks is red, the base
branch's required contexts are read (one more read, on a red head only) and
only a red on a required check counts in ``failed``, ``failed_names``,
``first_failed``, ``first_failure`` and ``all_green``; the others are
``failed_not_required`` and ``failed_not_required_names``, and each of
``names[]`` carries ``required`` (``null`` when not read) and ``provisional``
(a Codecov grade, read before every upload arrived). A base with no required
context, or none readable, leaves every check deciding. The latest run of each
check stands: a red with a later run of the same check on the head (a re-run,
a labeler re-run by the ``labeled`` event) is superseded and never counts
(#11807).

A head with nothing failed whose only pending checks have not moved for over
``checks.STALE_OPTIONAL_SECONDS`` (30 minutes) has its base's required contexts
and commit statuses read (two more reads, on such a head only): when every
required context passed and none of those checks is required, ``all_green``
reads true with ``verdict`` ``green_required_stale_optional`` and
``stale_optional[]`` (``name``, ``since``, ``age_seconds``), ``all_completed``
stays false and the head is settled (xtm-one #195): a third-party check left in
progress no longer holds a head whose branch rules are met.

``all_green`` needs every check of the head, so a non-required third-party
check that never completes (a coverage upload left in progress) keeps a head
whose branch rules are met from ever waking a filter (#11419). A filter key
under ``enriched.head.required`` reads the facet :data:`REQUIRED_FACET`: the
status check contexts the open pull request's base branch requires (rulesets
and classic protection, ``checks.read_required_contexts``, the read behind the
"Required by" line of ``get_github_ci_status``) and the head's latest check
runs and commit statuses placed against them (``checks.summarize_required``):
``total``, ``passed`` (skipped and neutral pass, as on GitHub), ``failed``,
``pending``, ``missing`` (never reported), the names of each, ``all_passed``
(at least one required context and every one passed) and ``last_to_complete``
(no required check finished after this delivery). No open pull request or no
required context reads ``all_passed`` false: a filter on it never wakes blind.

A workflow run can conclude ``failure`` without any job: GitHub never assigned
it a runner (an Actions incident, #11490), so it has no log and nothing to fix.
A filter key under ``enriched.run`` reads the facet :data:`RUN_FACET` on a
``workflow_run`` completion that is not green: ``id``, ``attempt``, ``jobs``
(the run's job count) and ``no_jobs`` (completed, not green, zero jobs). A
red-check filter adds ``"enriched.run.no_jobs": false`` to leave such a run
out, and a listener on ``enriched.run.no_jobs`` true with ``attempt`` 1 re-runs
it once (``manage_github_workflow_run`` action ``rerun``).

A red the head commit did not cause (the same required check already red on its
parent, a bot commit on a pull request red for days) wakes a red-check filter
like a new one. A filter key under ``enriched.head.parent`` reads the facet
:data:`PARENT_FACET`, on a head with a counted red only (two more reads: the
commit, then its first parent's check runs): ``sha`` (the parent, ``null`` when
none or not read), ``failed_names`` (the head's counted reds already red on the
parent), ``new_failed_names`` (the others) and ``unchanged_red`` (at least one
counted red and none new). A green head, no parent or an unreadable parent reads
``unchanged_red`` false, so ``{"enriched.head.parent.unchanged_red": {"ne":
true}}`` never drops a wake blind (xtm-one #752).

A red ``check_run`` completion that reads ``first_failure`` true has the head's
workflow runs read once more: when the delivered check's workflow run (its URL
or check suite) is queued or running again (a re-run of failed jobs), the red
belongs to a superseded attempt, ``first_failure`` reads false and
``superseded_attempt`` names the run (``run_id``, ``attempt``, ``status``)
(xtm-one #2210).
"""

from __future__ import annotations

import asyncio
import re
from datetime import UTC, datetime
from typing import Any

from app.core.egress import egress_client
from app.integrations.event_enrichment import EnrichmentError, EventEnricher, PermanentEnrichmentError

from . import _common
from . import checks as gh_checks
from ._common import _BASE_URL, _headers, parse_repo

#: The events whose head commit is enriched, and where each names it.
_HEAD_SHA_PATHS: dict[str, tuple[str, ...]] = {
    "workflow_run": ("workflow_run", "head_sha"),
    "check_suite": ("check_suite", "head_sha"),
    "check_run": ("check_run", "head_sha"),
    "status": ("sha",),
    "push": ("after",),
}
_NULL_SHA = "0" * 40
#: Where a CI event names its head branch.
_HEAD_BRANCH_PATHS: dict[str, tuple[str, ...]] = {
    "workflow_run": ("workflow_run", "head_branch"),
    "check_suite": ("check_suite", "head_branch"),
    "check_run": ("check_run", "check_suite", "head_branch"),
}
#: The scratch branches the batched commit builds (``contents._SCRATCH_PREFIX``):
#: no pull request ever heads them, so their heads are never watched (#11988).
SCRATCH_BRANCH_PREFIX = "tmp/commit-batch/"
#: The delivery waits on this read: short, and never retried past it.
REQUEST_TIMEOUT_SECONDS = 5.0
MAX_TOTAL_SECONDS = 8.0
PER_PAGE = 30
#: The check runs of one head, read page by page; past the cap the read fails
#: (the filter then drops the event naming why) rather than answer a partial state.
CHECKS_PER_PAGE = 100
MAX_CHECK_PAGES = 10
#: A head whose first check started, or whose open pull request was opened, less than
#: this ago is not green yet: GitHub lists an app's check (a draft skip) seconds after a
#: push, and creates the pull request's workflow runs up to about 100 seconds after the
#: pull request is opened (#10860: 94 s, after a pull_request_target run had completed).
WORKFLOWS_GRACE_SECONDS = 180
#: The window of a head that heads an open pull request: GitHub has created its
#: ``pull_request`` runs over 5 minutes after the pull request was opened (#10879: #10877
#: at 5 min 10 s, #10876 at about 4 min), so 180 s woke such heads green before their CI.
PR_WORKFLOWS_GRACE_SECONDS = 900
#: The conclusions that make a head not green.
FAILED_CONCLUSIONS = frozenset({"failure", "cancelled", "timed_out", "action_required", "stale"})
#: The events whose head carries checks (``push`` names a commit, not a CI result).
_CHECK_EVENTS = frozenset({"workflow_run", "check_suite", "check_run", "status"})
#: The facet naming the head's check runs (``enriched.head.checks``).
CHECKS_FACET = "checks"
#: The filter keys that read the check runs: the facet's own path, and the
#: objects holding it (a filter may compare ``enriched.head`` whole).
_CHECKS_KEYS = frozenset({"enriched", "enriched.head", "enriched.head.checks"})
_CHECKS_PREFIX = "enriched.head.checks."
#: The facet naming the base branch's required checks on the head (``enriched.head.required``).
REQUIRED_FACET = "required"
_REQUIRED_KEY = "enriched.head.required"
_REQUIRED_PREFIX = "enriched.head.required."
#: The facet naming the delivered workflow run's own jobs (``enriched.run``).
RUN_FACET = "run"
_RUN_KEY = "enriched.run"
_RUN_PREFIX = "enriched.run."
#: The facet naming the head's reds against its first parent (``enriched.head.parent``).
PARENT_FACET = "parent"
_PARENT_KEY = "enriched.head.parent"
_PARENT_PREFIX = "enriched.head.parent."
#: The conclusions of a run whose job count is read: a green run is never ``no_jobs``.
_NO_JOBS_CONCLUSIONS = frozenset({"failure", "cancelled", "startup_failure", "timed_out"})
#: Where a completion event names itself: its id and what changes on a re-run.
_COMPLETION_ID_PATHS: dict[str, tuple[tuple[str, ...], ...]] = {
    "workflow_run": (("workflow_run", "id"), ("workflow_run", "run_attempt")),
    "check_suite": (("check_suite", "id"), ("check_suite", "updated_at")),
    "check_run": (("check_run", "id"), ("check_run", "completed_at")),
    "status": (("id",), ("state",)),
}
#: Where a completion event names when it finished.
_COMPLETION_TIME_PATHS: dict[str, tuple[str, ...]] = {
    "workflow_run": ("workflow_run", "updated_at"),
    "check_suite": ("check_suite", "updated_at"),
    "check_run": ("check_run", "completed_at"),
    "status": ("updated_at",),
}


def _event_name(event_key: str) -> str:
    return event_key.split(".", 1)[0]


def _dig(payload: Any, path: tuple[str, ...]) -> Any:
    current = payload
    for part in path:
        if not isinstance(current, dict):
            return None
        current = current.get(part)
    return current


def _head(event_key: str, payload: dict) -> tuple[str, str] | None:
    """``(owner/name, sha)`` of the event's head commit, ``None`` when it names none."""
    path = _HEAD_SHA_PATHS.get(_event_name(event_key))
    if path is None:
        return None
    sha = _dig(payload, path)
    repo = _dig(payload, ("repository", "full_name"))
    if not isinstance(sha, str) or len(sha) < 7 or sha == _NULL_SHA or not isinstance(repo, str):
        return None
    if parse_repo(repo)[2] is not None:
        return None
    return repo, sha


def applies(event_key: str, payload: dict) -> bool:
    return _event_name(event_key) in _HEAD_SHA_PATHS


def facets(event_key: str, payload: dict, keys: tuple[str, ...]) -> frozenset[str]:
    """The optional reads a filter reading *keys* needs: the check runs on a CI event, when a key reads them."""
    if _event_name(event_key) not in _CHECK_EVENTS:
        return frozenset()
    parts: set[str] = set()
    if any(key in _CHECKS_KEYS or key.startswith(_CHECKS_PREFIX) for key in keys):
        parts.add(CHECKS_FACET)
    if any(key == _REQUIRED_KEY or key.startswith(_REQUIRED_PREFIX) for key in keys):
        parts.add(REQUIRED_FACET)
    if _event_name(event_key) == "workflow_run" and any(key == _RUN_KEY or key.startswith(_RUN_PREFIX) for key in keys):
        parts.add(RUN_FACET)
    if any(key == _PARENT_KEY or key.startswith(_PARENT_PREFIX) for key in keys):
        parts.add(PARENT_FACET)
    return frozenset(parts)


def is_completion(event_key: str, payload: dict) -> bool:
    """Whether the event reports a check that finished: the head's check state changed with it."""
    name = _event_name(event_key)
    if name == "status":
        return payload.get("state") in ("success", "failure", "error")
    if name not in _CHECK_EVENTS:
        return False
    action = event_key.split(".", 1)[1] if "." in event_key else payload.get("action")
    return action == "completed"


def cache_key(event_key: str, payload: dict) -> str | None:
    """``owner/name@sha``; a completion adds its own identity, so no earlier read answers it."""
    head = _head(event_key, payload)
    if head is None:
        return None
    key = f"{head[0].lower()}@{head[1].lower()}"
    if is_completion(event_key, payload):
        name = _event_name(event_key)
        marker = ":".join(str(_dig(payload, path)) for path in _COMPLETION_ID_PATHS[name])
        key = f"{key}#{name}:{marker}"
    return key


def _login(value: Any) -> str | None:
    login = value.get("login") if isinstance(value, dict) else None
    return login if isinstance(login, str) else None


def _ref(value: Any) -> dict[str, Any]:
    side = value if isinstance(value, dict) else {}
    repo = side.get("repo") if isinstance(side.get("repo"), dict) else {}
    return {"ref": side.get("ref"), "repo": {"full_name": repo.get("full_name")}}


def pull_request_facts(pull: dict) -> dict[str, Any]:
    """The fields of one pull request the filter reads (GitHub's names)."""
    labels = pull.get("labels") if isinstance(pull.get("labels"), list) else []
    return {
        "number": pull.get("number"),
        "state": pull.get("state"),
        "draft": bool(pull.get("draft")),
        "user": {"login": _login(pull.get("user"))},
        "author_association": pull.get("author_association"),
        "labels": [{"name": label.get("name")} for label in labels if isinstance(label, dict)],
        "base": _ref(pull.get("base")),
        "head": _ref(pull.get("head")),
    }


def _named_numbers(event_key: str, payload: dict) -> list[int]:
    """The pull request numbers the delivery itself names (``workflow_run.pull_requests``...)."""
    holder = payload.get(_event_name(event_key))
    pulls = holder.get("pull_requests") if isinstance(holder, dict) else None
    if not isinstance(pulls, list):
        return []
    return [p["number"] for p in pulls if isinstance(p, dict) and isinstance(p.get("number"), int)]


def facts_from_pulls(event_key: str, payload: dict, pulls: list[Any], sha: str) -> dict[str, Any]:
    """The enrichment of the event from GitHub's answer: the open pull requests headed by *sha*."""
    heads = [
        p
        for p in pulls
        if isinstance(p, dict)
        and p.get("state") == "open"
        and str(_dig(p, ("head", "sha")) or "").lower() == sha.lower()
    ]
    named = _named_numbers(event_key, payload)
    heads.sort(key=lambda p: (p.get("number") not in named, p.get("number") or 0))
    facts = [pull_request_facts(p) for p in heads]
    return {"pull_request": facts[0] if facts else None, "pull_requests": facts}


def _first_failed(runs: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The earliest failed check of a head (by ``completed_at``, then id), ``None`` when none failed."""
    failed = [r for r in runs if r.get("status") == "completed" and r.get("conclusion") in FAILED_CONCLUSIONS]
    if not failed:
        return None
    first = min(
        failed,
        key=lambda r: (
            str(r.get("completed_at") or "~"),
            r["id"] if isinstance(r.get("id"), int) else float("inf"),
        ),
    )
    return {"name": first.get("name"), "id": first.get("id"), "completed_at": first.get("completed_at")}


def _last_to_complete(runs: list[dict[str, Any]], event_completed_at: str | None) -> bool:
    """Whether a completion at *event_completed_at* is not older than any completed check run of the head."""
    moment = _parse_time(event_completed_at)
    if moment is None:
        return False
    finished = [
        when
        for run in runs
        if run.get("status") == "completed"
        for when in (_parse_time(run.get("completed_at")),)
        if when is not None
    ]
    return not finished or moment >= max(finished)


def _provisional(run: dict[str, Any]) -> bool:
    """Whether *run* is a Codecov grade, read before every coverage upload may have arrived. Pure."""
    return gh_checks.is_coverage_grade(run)


def check_facts(
    check_runs: list[Any],
    event_check_run_id: int | None = None,
    event_completed_at: str | None = None,
    required: frozenset[str] | None = None,
) -> dict[str, Any]:
    """The check state of a head from its check runs (GitHub's ``check_runs`` items).

    The latest run of each check (app and name, highest id) stands: a red with
    a later run of the same check on the head is superseded and never counts.
    *required* is the base branch's required contexts, ``None`` when not read:
    when it names any, only a red on one of them counts in ``failed``,
    ``failed_names``, ``first_failed``, ``first_failure`` and ``all_green``; the
    other reds are ``failed_not_required`` / ``failed_not_required_names``.
    Each of ``names[]`` says ``required`` (``None`` when not read) and
    ``provisional`` (a Codecov grade).
    *event_check_run_id* is the check run a ``check_run`` completion delivers:
    ``first_failure`` is true when it is the head's earliest counted failed check.
    ``coverage_pending`` is true while the head has a Codecov grade and another
    check still runs (:func:`checks.coverage_pending`): a red grade is then
    provisional and never ``first_failed`` nor ``first_failure`` (#12050).
    *event_completed_at* is when a completion event finished: ``last_to_complete``
    is true when no completed check run of the head finished after it.
    """
    runs = gh_checks.latest_check_runs([run for run in check_runs if isinstance(run, dict)])
    weighs = bool(required)

    def _counts(run: dict[str, Any]) -> bool:
        return not weighs or run.get("name") in (required or frozenset())

    names = [
        {
            "name": run.get("name"),
            "status": run.get("status"),
            "conclusion": run.get("conclusion"),
            "required": (run.get("name") in required) if required is not None else None,
            "provisional": _provisional(run),
        }
        for run in runs
    ]
    completed = [n for n in names if n["status"] == "completed"]
    red = [n for n in completed if n["conclusion"] in FAILED_CONCLUSIONS]
    counted = [n for n in red if _counts(n)]
    all_completed = bool(names) and len(completed) == len(names)
    # A Codecov red read while another check of the head runs is provisional (#12050):
    # it is no first failure until the coverage producers complete.
    coverage_pending = gh_checks.coverage_pending(runs)
    first_failed = _first_failed([run for run in runs if _counts(run) and not (coverage_pending and _provisional(run))])
    first_failure = (
        first_failed is not None and event_check_run_id is not None and first_failed["id"] == event_check_run_id
    )
    return {
        "total": len(names),
        "completed": len(completed),
        "pending": len(names) - len(completed),
        "failed": len(counted),
        "failed_not_required": len(red) - len(counted),
        "skipped": sum(1 for n in completed if n["conclusion"] == "skipped"),
        "neutral": sum(1 for n in completed if n["conclusion"] == "neutral"),
        "all_completed": all_completed,
        "all_green": all_completed and not counted,
        "failed_names": [n["name"] for n in counted],
        "failed_not_required_names": [n["name"] for n in red if not _counts(n)],
        "first_failed": first_failed,
        "first_failure": first_failure,
        "coverage_pending": coverage_pending,
        "last_to_complete": _last_to_complete(runs, event_completed_at),
        "names": names,
    }


def _json(resp: Any, what: str) -> Any:
    try:
        return resp.json()
    except ValueError as exc:
        raise EnrichmentError(f"GitHub answered something that is not JSON ({what})") from exc


async def _get(client: Any, url: str, action: str, token: str, params: dict[str, str]) -> Any:
    resp, err = await _common.safe_request(
        client,
        "get",
        url,
        "GitHub",
        action,
        headers=_headers(token),
        params=params,
        max_total_seconds=MAX_TOTAL_SECONDS,
    )
    if err or resp is None:
        raise EnrichmentError(err or "no answer from GitHub")
    return _json(resp, action)


async def _pulls(client: Any, base: str, token: str) -> list[Any]:
    params = {"per_page": str(PER_PAGE)}
    pulls = await _get(client, f"{base}/pulls", "read the pull requests of a commit", token, params)
    if not isinstance(pulls, list):
        raise EnrichmentError("GitHub answered something that is not a list of pull requests")
    return pulls


async def _check_runs(client: Any, base: str, token: str) -> list[Any]:
    """Every check run of the head (the latest of each check), page by page."""
    runs: list[Any] = []
    for page in range(1, MAX_CHECK_PAGES + 1):
        body = await _get(
            client,
            f"{base}/check-runs",
            "read the check runs of a commit",
            token,
            {"per_page": str(CHECKS_PER_PAGE), "page": str(page)},
        )
        items = body.get("check_runs") if isinstance(body, dict) else None
        total = body.get("total_count") if isinstance(body, dict) else None
        if not isinstance(items, list) or not isinstance(total, int):
            raise EnrichmentError("GitHub answered something that is not a list of check runs")
        runs.extend(items)
        if len(runs) >= total or len(items) < CHECKS_PER_PAGE:
            return runs
    raise PermanentEnrichmentError(f"the commit has more than {MAX_CHECK_PAGES * CHECKS_PER_PAGE} check runs")


async def _running_workflow_runs(
    client: Any, repo_base: str, sha: str, token: str
) -> tuple[list[dict[str, Any]], list[str], dict[int, str]]:
    """The head's workflow runs not completed, every run's event, and the event of each run's check suite."""
    found: list[dict[str, Any]] = []
    events: list[str] = []
    suites: dict[int, str] = {}
    seen = 0
    for page in range(1, MAX_CHECK_PAGES + 1):
        body = await _get(
            client,
            f"{repo_base}/actions/runs",
            "read the workflow runs of a commit",
            token,
            {"head_sha": sha, "per_page": str(CHECKS_PER_PAGE), "page": str(page)},
        )
        items = body.get("workflow_runs") if isinstance(body, dict) else None
        total = body.get("total_count") if isinstance(body, dict) else None
        if not isinstance(items, list) or not isinstance(total, int):
            raise EnrichmentError("GitHub answered something that is not a list of workflow runs")
        seen += len(items)
        found.extend(r for r in items if isinstance(r, dict) and r.get("status") != "completed")
        events.extend(str(r.get("event") or "") for r in items if isinstance(r, dict))
        suites.update(
            (r["check_suite_id"], str(r.get("event") or ""))
            for r in items
            if isinstance(r, dict) and isinstance(r.get("check_suite_id"), int)
        )
        if seen >= total or len(items) < CHECKS_PER_PAGE:
            return found, events, suites
    raise PermanentEnrichmentError(f"the commit has more than {MAX_CHECK_PAGES * CHECKS_PER_PAGE} workflow runs")


def _now() -> datetime:
    return datetime.now(UTC)


def _parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)


def _opened_at(pulls: list[Any], sha: str) -> datetime | None:
    """When the latest open pull request headed by *sha* was opened, ``None`` without one."""
    opened = [
        moment
        for p in pulls
        if isinstance(p, dict)
        and p.get("state") == "open"
        and str(_dig(p, ("head", "sha")) or "").lower() == sha.lower()
        for moment in (_parse_time(p.get("created_at")),)
        if moment is not None
    ]
    return max(opened) if opened else None


def workflows_not_created(
    check_runs: list[Any],
    workflow_events: list[str],
    opened_at: datetime | None = None,
    now: datetime | None = None,
) -> bool:
    """Whether the head's workflow runs may not all exist yet, inside the grace window.

    *workflow_events* holds the event of each workflow run of the head. The window
    starts at the later of the head's first check and the opening of its open pull
    request (*opened_at*). Inside it the head may lack runs when it has none at all,
    or when it heads an open pull request and none of its runs is a ``pull_request``
    run: a completed ``pull_request_target`` run does not mean the pull request's
    own runs were created. The window is :data:`PR_WORKFLOWS_GRACE_SECONDS` for a
    head of an open pull request, :data:`WORKFLOWS_GRACE_SECONDS` otherwise.
    """
    if workflow_events and (opened_at is None or "pull_request" in workflow_events):
        return False
    starts = [
        moment
        for run in check_runs
        if isinstance(run, dict)
        for moment in (_parse_time(run.get("started_at")) or _parse_time(run.get("completed_at")),)
        if moment is not None
    ]
    if not starts:
        return False
    anchor = max(min(starts), opened_at) if opened_at is not None else min(starts)
    grace = PR_WORKFLOWS_GRACE_SECONDS if opened_at is not None else WORKFLOWS_GRACE_SECONDS
    return ((now or _now()) - anchor).total_seconds() < grace


#: A pending stand-in for the workflow runs of a push GitHub has not created yet.
NOT_CREATED_WORKFLOWS = {"id": "not-created", "name": "not-created", "status": "queued"}
#: How long after a head's newest commit status changed its statuses may still grow
#: (a status app's next stage): CircleCI created the build-* statuses of
#: OpenAEV-Platform/collectors #690 16 s after its last test-* status passed.
STATUS_SETTLE_SECONDS = 60
#: A pending stand-in for the commit statuses a status app has not created yet.
STATUSES_SETTLING = {"name": "status:settling", "status": "pending", "conclusion": None}


def with_running_workflows(checks: dict[str, Any], running: list[dict[str, Any]]) -> dict[str, Any]:
    """*checks* with each workflow run not completed counted as a pending check: never green while one runs."""
    if not running:
        return checks
    extra = [
        {"name": f"workflow:{r.get('name') or r.get('id')}", "status": r.get("status"), "conclusion": None}
        for r in running
    ]
    return {
        **checks,
        "total": checks["total"] + len(extra),
        "pending": checks["pending"] + len(extra),
        "all_completed": False,
        "all_green": False,
        "names": [*checks["names"], *extra],
        **({"verdict": "pending"} if "verdict" in checks else {}),
    }


def reds_of_another_event(
    check_runs: list[Any], suite_events: dict[int, str], required: frozenset[str] | None = None
) -> list[str]:
    """The reds a later run of the same check name, from a workflow of ANOTHER event, hid. Pure.

    A branch pushed with an open pull request runs the same workflow twice on one
    head, its ``push`` run and its ``pull_request`` run, with the same check names.
    :func:`checks.latest_check_runs` keeps the highest id per (app, name), so the
    later run's green superseded the other run's red and the head read green while
    the pull request's own run was red (opencti #20153, 41cb354c). Here the latest
    run stands per (app, event, name), the event read from the check run's suite in
    *suite_events*: a red that is the latest of its own event is named. A re-run in
    the same event still supersedes its red. *required* weighs as in
    :func:`check_facts`.
    """
    by_event: dict[tuple[str, str, str], dict[str, Any]] = {}
    for run in check_runs:
        if not isinstance(run, dict):
            continue
        suite = run.get("check_suite")
        suite_id = suite.get("id") if isinstance(suite, dict) else None
        event = suite_events.get(suite_id) if isinstance(suite_id, int) else None
        if event is None:
            continue
        key = ((run.get("app") or {}).get("slug") or "", event, str(run.get("name") or ""))
        kept = by_event.get(key)
        if kept is None or _as_int(run.get("id")) > _as_int(kept.get("id")):
            by_event[key] = run
    latest_ids = {r.get("id") for r in gh_checks.latest_check_runs([r for r in check_runs if isinstance(r, dict)])}
    return sorted(
        {
            str(run.get("name"))
            for run in by_event.values()
            if run.get("id") not in latest_ids
            and run.get("status") == "completed"
            and run.get("conclusion") in FAILED_CONCLUSIONS
            and (not required or run.get("name") in required)
        }
    )


def _as_int(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def with_reds_of_another_event(checks: dict[str, Any], hidden: list[str]) -> dict[str, Any]:
    """*checks* with each red :func:`reds_of_another_event` named counted as failed: never green. Pure."""
    if not hidden:
        return checks
    return {
        **checks,
        "failed": checks["failed"] + len(hidden),
        "failed_names": [*checks["failed_names"], *hidden],
        "all_green": False,
        **({"verdict": "failure"} if "verdict" in checks else {}),
    }


def with_commit_statuses(
    checks: dict[str, Any],
    statuses: list[Any],
    required: frozenset[str] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """*checks* with the head's commit statuses weighed: a pending or red status holds a green head. Pure.

    Check runs alone never see a commit status (CircleCI, Snyk, a Jenkins job),
    so a head they read green woke a filter while a status was still pending.
    Each pending status (named ``status:<context>``) counts as a pending check,
    each ``error`` / ``failure`` one as a failed check, the latter only when
    *required* names it or names nothing (a red outside the base's required
    contexts is ``failed_not_required``, as for a check run). A status named in
    ``stale_optional`` was already left out by :func:`with_stale_optional`.

    A status app creates the statuses of a later stage only once the earlier
    stage ended (CircleCI: the ``build-*`` jobs about 15 s after the last
    ``test-*`` one), so a head whose statuses all passed is held as pending
    (``status:settling``) while its newest status changed less than
    :data:`STATUS_SETTLE_SECONDS` before *now*: the head watch reads it again,
    and it wakes green only once no new status appeared in that window.
    """
    left_out = {s.get("name") for s in checks.get("stale_optional") or [] if isinstance(s, dict)}
    pending: list[dict[str, Any]] = []
    red: list[str] = []
    red_optional: list[str] = []
    changed: list[datetime] = []
    for status in statuses:
        if not isinstance(status, dict):
            continue
        context = str(status.get("context") or "?")
        state = status.get("state") or "pending"
        moment = _parse_time(status.get("updated_at"))
        if moment is not None:
            changed.append(moment)
        if state == "pending" and context not in left_out:
            pending.append({"name": f"status:{context}", "status": "pending", "conclusion": None})
        elif state in ("error", "failure"):
            (red if not required or context in required else red_optional).append(f"status:{context}")
    if not pending and not red and changed:
        age = ((now or _now()) - max(changed)).total_seconds()
        if age < STATUS_SETTLE_SECONDS:
            pending.append(dict(STATUSES_SETTLING))
    if not pending and not red and not red_optional:
        return checks
    held = bool(pending or red)
    return {
        **checks,
        "total": checks["total"] + len(pending) + len(red) + len(red_optional),
        "pending": checks["pending"] + len(pending),
        "failed": checks["failed"] + len(red),
        "failed_names": [*checks["failed_names"], *red],
        "failed_not_required": checks.get("failed_not_required", 0) + len(red_optional),
        "failed_not_required_names": [*checks.get("failed_not_required_names", []), *red_optional],
        "all_completed": checks["all_completed"] and not pending,
        "all_green": checks["all_green"] and not held,
        "names": [
            *checks["names"],
            *pending,
            *({"name": n, "status": "completed", "conclusion": "failure"} for n in [*red, *red_optional]),
        ],
        **({"verdict": "pending" if pending else "failure"} if "verdict" in checks and held else {}),
    }


def held_by_stale_only(check_runs: list[Any], checks: dict[str, Any], now: datetime | None = None) -> bool:
    """Whether a head reads nothing failed and only checks pending for over the stale age. Pure.

    Only then are the base's required checks read to tell whether a check it
    does not require is all that holds the head (``checks.STALE_OPTIONAL_SECONDS``).
    """
    if checks.get("all_green") or checks.get("failed") or not checks.get("pending"):
        return False
    runs = gh_checks.latest_check_runs([run for run in check_runs if isinstance(run, dict)])
    pending = [r for r in runs if r.get("status") != "completed"]
    return len(gh_checks.stale_checks(pending, frozenset(), now)) == len(pending)


def with_stale_optional(
    checks: dict[str, Any],
    check_runs: list[Any],
    statuses: list[Any],
    required: frozenset[str],
    now: datetime | None = None,
) -> dict[str, Any]:
    """*checks* read green when every *required* context passed and only stale optional checks are pending.

    ``all_green`` then reads true, ``verdict`` :data:`~.checks.GREEN_STALE_OPTIONAL`
    and ``stale_optional[]`` names each left-out check (``name``, ``since``,
    ``age_seconds``); ``all_completed`` stays false. Unchanged otherwise. Pure.
    """
    runs = [run for run in check_runs if isinstance(run, dict)]
    states = [status for status in statuses if isinstance(status, dict)]
    stale = gh_checks.stale_optional(gh_checks.classify_ci(runs, states), required, now)
    latest = gh_checks.latest_check_runs(runs)
    pending = [r for r in latest if r.get("status") != "completed"]
    held = {s["name"] for s in stale}
    if not stale or any(r.get("name") not in held for r in pending):
        return checks
    return {**checks, "all_green": True, "verdict": gh_checks.GREEN_STALE_OPTIONAL, "stale_optional": stale}


def required_facts(
    required: frozenset[str],
    check_runs: list[Any],
    statuses: list[Any],
    event_completed_at: str | None = None,
) -> dict[str, Any]:
    """The base branch's *required* contexts placed against the head's latest check runs and statuses.

    ``all_passed`` needs at least one required context and every one passed
    (skipped and neutral pass, as on GitHub); a check outside *required* never
    counts, pending or failed. ``last_to_complete`` is true when no completed
    required check finished after *event_completed_at*.
    """
    runs = [run for run in check_runs if isinstance(run, dict)]
    states = [status for status in statuses if isinstance(status, dict)]
    summary = gh_checks.summarize_required(gh_checks.classify_ci(runs, states), required)
    finished = [run for run in runs if run.get("name") in required]
    finished += [
        {"status": "completed", "completed_at": s.get("updated_at")}
        for s in states
        if s.get("context") in required and s.get("state") != "pending"
    ]
    total = sum(len(names) for names in summary.values())
    return {
        "total": total,
        "passed": len(summary["passed"]),
        "failed": len(summary["failed"]),
        "pending": len(summary["pending"]),
        "missing": len(summary["missing"]),
        "passed_names": summary["passed"],
        "failed_names": summary["failed"],
        "pending_names": summary["pending"],
        "missing_names": summary["missing"],
        "all_passed": total > 0 and len(summary["passed"]) == total,
        "last_to_complete": _last_to_complete(finished, event_completed_at),
    }


async def _statuses(client: Any, base: str, token: str) -> list[Any]:
    """The head's commit statuses (the latest per context)."""
    body = await _get(client, f"{base}/status", "read the commit statuses of a commit", token, {"per_page": "100"})
    items = body.get("statuses") if isinstance(body, dict) else None
    if not isinstance(items, list):
        raise EnrichmentError("GitHub answered something that is not a list of commit statuses")
    return items


def _base_branch(pull_request: dict[str, Any] | None) -> str | None:
    branch = _dig(pull_request, ("base", "ref"))
    return branch if isinstance(branch, str) and branch else None


def has_red(check_runs: list[Any]) -> bool:
    """Whether the latest run of any check of the head completed red. Pure."""
    runs = gh_checks.latest_check_runs([run for run in check_runs if isinstance(run, dict)])
    return any(r.get("status") == "completed" and r.get("conclusion") in FAILED_CONCLUSIONS for r in runs)


async def _required(
    client: Any,
    base: str,
    token: str,
    branch: str | None,
    required: frozenset[str],
    check_runs: list[Any],
    event_completed_at: str | None,
) -> dict[str, Any]:
    """``enriched.head.required``: the required checks of the open pull request's base branch on the head."""
    if branch is None:
        return {**required_facts(frozenset(), [], []), "base": None}
    statuses = await _statuses(client, base, token)
    return {**required_facts(required, check_runs, statuses, event_completed_at), "base": branch}


def run_facts(workflow_run: Any, jobs_total: int | None) -> dict[str, Any]:
    """``enriched.run`` of a ``workflow_run`` delivery from its job count (``None``: not read, a green run)."""
    run = workflow_run if isinstance(workflow_run, dict) else {}
    attempt = run.get("run_attempt") if isinstance(run.get("run_attempt"), int) else 1
    no_jobs = jobs_total == 0 and run.get("status") == "completed" and run.get("conclusion") in _NO_JOBS_CONCLUSIONS
    return {"id": run.get("id"), "attempt": attempt, "jobs": jobs_total, "no_jobs": no_jobs}


async def _run_jobs_total(client: Any, repo_base: str, run_id: Any, token: str) -> int:
    """The job count of the delivered workflow run (its latest attempt)."""
    body = await _get(
        client, f"{repo_base}/actions/runs/{run_id}/jobs", "read the jobs of a workflow run", token, {"per_page": "1"}
    )
    total = body.get("total_count") if isinstance(body, dict) else None
    if not isinstance(total, int):
        raise EnrichmentError("GitHub answered something that is not a list of jobs")
    return total


def parent_facts(head_failed: list[Any], parent_sha: str | None, parent_runs: list[Any] | None) -> dict[str, Any]:
    """``enriched.head.parent``: the head's counted reds placed against its parent's latest check runs. Pure.

    *parent_runs* ``None`` (no parent, or not read) keeps every red new.
    """
    names = [name for name in head_failed if isinstance(name, str)]
    parent_red: set[str] = set()
    if parent_runs is not None:
        runs = gh_checks.latest_check_runs([run for run in parent_runs if isinstance(run, dict)])
        parent_red = {
            str(run.get("name"))
            for run in runs
            if run.get("status") == "completed" and run.get("conclusion") in FAILED_CONCLUSIONS
        }
    unchanged = [name for name in names if name in parent_red]
    new = [name for name in names if name not in parent_red]
    return {
        "sha": parent_sha if parent_runs is not None else None,
        "failed_names": unchanged,
        "new_failed_names": new,
        "unchanged_red": bool(names) and not new,
    }


async def _parent(client: Any, repo_base: str, sha: str, token: str, head_failed: list[Any]) -> dict[str, Any]:
    """``enriched.head.parent`` read from GitHub; a red-free head reads nothing."""
    if not head_failed:
        return parent_facts([], None, None)
    try:
        commit = await _get(
            client, f"{repo_base}/commits/{sha}", "read the parent of a commit", token, {"per_page": "1"}
        )
        parents = commit.get("parents") if isinstance(commit, dict) else None
        first = parents[0] if isinstance(parents, list) and parents and isinstance(parents[0], dict) else {}
        parent_sha = first.get("sha") if isinstance(first.get("sha"), str) else None
        if parent_sha is None:
            return parent_facts(head_failed, None, None)
        runs = await _check_runs(client, f"{repo_base}/commits/{parent_sha}", token)
    except EnrichmentError:
        # An unreadable parent keeps the wake: every red reads new.
        return parent_facts(head_failed, None, None)
    return parent_facts(head_failed, parent_sha, runs)


def _delivered_check_run_id(event_key: str, payload: dict) -> int | None:
    """The id of the check run a ``check_run`` completion delivers, ``None`` on any other event."""
    if _event_name(event_key) != "check_run" or not is_completion(event_key, payload):
        return None
    run_id = _dig(payload, ("check_run", "id"))
    return run_id if isinstance(run_id, int) else None


_RUN_ID_IN_URL = re.compile(r"/actions/runs/(\d+)(?:/|$)")


def superseded_attempt(delivered: Any, running: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The workflow run, not completed, that re-runs the attempt a delivered red check belongs to. Pure.

    *delivered* is the ``check_run`` of the payload; its workflow run is read from its
    ``html_url`` or ``details_url`` (``/actions/runs/<id>/job/<id>``) or its
    ``check_suite.id``. A red of an earlier attempt whose run is queued or running
    again (a re-run of failed jobs: ``run_attempt`` above 1 and the check started
    before the run's latest attempt, ``run_started_at``) is no first failure
    (xtm-one #2210): ``None`` otherwise, a red of the running attempt included.
    """
    if not isinstance(delivered, dict) or not running:
        return None
    check_started = _parse_time(delivered.get("started_at"))
    run_ids: set[int] = set()
    for field in ("html_url", "details_url"):
        found = _RUN_ID_IN_URL.search(str(delivered.get(field) or ""))
        if found:
            run_ids.add(int(found.group(1)))
    suite = delivered.get("check_suite")
    suite_id = suite.get("id") if isinstance(suite, dict) else None
    for run in running:
        if run.get("status") == "completed":
            continue
        if run.get("id") in run_ids or (isinstance(suite_id, int) and run.get("check_suite_id") == suite_id):
            attempt = run.get("run_attempt")
            attempt_started = _parse_time(run.get("run_started_at"))
            if not isinstance(attempt, int) or attempt < 2:
                continue
            if check_started is not None and attempt_started is not None and check_started >= attempt_started:
                continue
            return {
                "run_id": run.get("id"),
                "attempt": attempt if isinstance(attempt, int) else None,
                "status": run.get("status"),
            }
    return None


def _delivered_completion_time(event_key: str, payload: dict) -> str | None:
    """When a completion event finished (its payload's own time), ``None`` on any other event."""
    if not is_completion(event_key, payload):
        return None
    moment = _dig(payload, _COMPLETION_TIME_PATHS[_event_name(event_key)])
    return moment if isinstance(moment, str) else None


async def fetch(event_key: str, payload: dict, token: str, *, facets: frozenset[str] | None = None) -> dict[str, Any]:
    """The open pull requests of the head, and its check state when *facets* names it (``None``: always)."""
    head = _head(event_key, payload)
    if head is None:
        raise PermanentEnrichmentError("the event names no head commit")
    repo, sha = head
    owner, name, _ = parse_repo(repo)
    repo_base = f"{_BASE_URL}/repos/{owner}/{name}"
    base = f"{repo_base}/commits/{sha}"
    ci_event = _event_name(event_key) in _CHECK_EVENTS
    with_checks = ci_event and (facets is None or CHECKS_FACET in facets)
    with_required = ci_event and facets is not None and REQUIRED_FACET in facets
    with_run = _event_name(event_key) == "workflow_run" and facets is not None and RUN_FACET in facets
    with_parent = ci_event and facets is not None and PARENT_FACET in facets
    checks: dict[str, Any] | None = None
    required: dict[str, Any] | None = None
    run: dict[str, Any] | None = None
    parent: dict[str, Any] | None = None
    async with egress_client(timeout=REQUEST_TIMEOUT_SECONDS) as client:
        check_runs: list[Any] = []
        statuses: list[Any] | None = None
        if with_checks or with_required or with_parent:
            pulls, check_runs = await asyncio.gather(_pulls(client, base, token), _check_runs(client, base, token))
        else:
            pulls = await _pulls(client, base, token)
        facts = facts_from_pulls(event_key, payload, pulls, sha)
        branch = _base_branch(facts["pull_request"])
        contexts: frozenset[str] | None = None
        if branch is not None and (with_required or ((with_checks or with_parent) and has_red(check_runs))):
            # A red is weighed against the base branch's required checks: one read, on a red head only.
            contexts = await gh_checks.read_required_contexts(client, _headers(token), owner, name, branch)
        if with_checks:
            checks = check_facts(
                check_runs,
                _delivered_check_run_id(event_key, payload),
                _delivered_completion_time(event_key, payload),
                contexts or None,
            )
            if checks["first_failure"]:
                # A red of an attempt its re-run supersedes is no first failure (#2210):
                # the head's workflow runs are read, on such a delivery only.
                try:
                    running, _events, _suites = await _running_workflow_runs(client, repo_base, sha, token)
                except EnrichmentError:
                    running = []  # unreadable: the red still wakes, never dropped blind
                superseded = superseded_attempt(payload.get("check_run"), running)
                if superseded is not None:
                    checks = {**checks, "first_failure": False, "superseded_attempt": superseded}
            if branch is not None and held_by_stale_only(check_runs, checks):
                # Nothing failed and only long-pending checks are left: when the base's
                # required checks all passed, a check it does not require no longer
                # holds the head (xtm-one #195). Two more reads, on such a head only.
                if contexts is None:
                    contexts = await gh_checks.read_required_contexts(client, _headers(token), owner, name, branch)
                if contexts:
                    statuses = await _statuses(client, base, token)
                    checks = with_stale_optional(checks, check_runs, statuses, contexts)
            if checks["all_green"]:
                # Check runs never see a commit status (CircleCI, Snyk): a pending
                # or red one holds the head. One more read, on a candidate green head only.
                if statuses is None:
                    statuses = await _statuses(client, base, token)
                checks = with_commit_statuses(checks, statuses, contexts or None)
            if checks["all_green"]:
                running, events, suite_events = await _running_workflow_runs(client, repo_base, sha, token)
                if workflows_not_created(check_runs, events, _opened_at(pulls, sha)):
                    running = [*running, NOT_CREATED_WORKFLOWS]
                checks = with_running_workflows(checks, running)
                # The same check name run by a push and a pull_request workflow: a
                # later green of one never hides a red of the other.
                hidden = reds_of_another_event(check_runs, suite_events)
                if hidden and branch is not None and contexts is None:
                    # Weighed against the base's required checks: one read, on such a head only.
                    contexts = await gh_checks.read_required_contexts(client, _headers(token), owner, name, branch)
                if hidden and contexts:
                    hidden = reds_of_another_event(check_runs, suite_events, contexts)
                checks = with_reds_of_another_event(checks, hidden)
        if with_required:
            required = await _required(
                client,
                base,
                token,
                branch,
                contexts or frozenset(),
                check_runs,
                _delivered_completion_time(event_key, payload),
            )
        if with_parent:
            # Only the head's counted reds are weighed, as in enriched.head.checks.
            counted = check_facts(check_runs, required=contexts or None)["failed_names"]
            parent = await _parent(client, repo_base, sha, token, counted)
        if with_run:
            delivered = payload.get("workflow_run")
            jobs_total = None
            if (
                isinstance(delivered, dict)
                and delivered.get("status") == "completed"
                and delivered.get("conclusion") in _NO_JOBS_CONCLUSIONS
                and isinstance(delivered.get("id"), int)
            ):
                jobs_total = await _run_jobs_total(client, repo_base, delivered["id"], token)
            run = run_facts(delivered, jobs_total)
    if run is not None:
        facts["run"] = run
    facts["head"] = {"sha": sha}
    if checks is not None:
        facts["head"]["checks"] = checks
    if required is not None:
        facts["head"]["required"] = required
    if parent is not None:
        facts["head"]["parent"] = parent
    return facts


def head_subject(event_key: str, payload: dict) -> str | None:
    """``owner/name@sha`` of a CI event's head, without the delivery's identity; ``None`` otherwise."""
    if _event_name(event_key) not in _CHECK_EVENTS:
        return None
    branch = _dig(payload, _HEAD_BRANCH_PATHS.get(_event_name(event_key), ()))
    if isinstance(branch, str) and branch.startswith(SCRATCH_BRANCH_PREFIX):
        return None
    head = _head(event_key, payload)
    return f"{head[0].lower()}@{head[1].lower()}" if head is not None else None


def head_settled(facts: dict) -> bool:
    """Whether every check of the head completed (no check state left to change); true without checks read.

    A head green with only stale optional checks pending (``stale_optional``) is settled too.
    """
    checks = _dig(facts, ("head", "checks"))
    if not isinstance(checks, dict):
        return True
    return bool(checks.get("all_completed") or (checks.get("all_green") and checks.get("stale_optional")))


def head_best_case(facts: dict) -> dict:
    """*facts* with every running check of the head completed successfully (the failed ones kept).

    The watched delivery stands in for the head's last completion, which may
    have been lost: ``last_to_complete`` reads true, settled or not.
    """
    checks = _dig(facts, ("head", "checks"))
    if not isinstance(checks, dict) or not isinstance(facts.get("head"), dict):
        return facts
    pending = checks.get("pending") if isinstance(checks.get("pending"), int) else 0
    total = checks.get("total") if isinstance(checks.get("total"), int) else 0
    failed = checks.get("failed") if isinstance(checks.get("failed"), int) else 0
    names = [
        {**n, "status": "completed", "conclusion": "success"}
        if isinstance(n, dict) and n.get("status") != "completed"
        else n
        for n in (checks.get("names") if isinstance(checks.get("names"), list) else [])
    ]
    best = {
        **checks,
        "completed": total,
        "pending": 0,
        "all_completed": total > 0,
        "all_green": total > 0 and failed == 0,
        "first_failure": False,
        "last_to_complete": True,
        "names": names,
    }
    if pending == 0:
        best = {**checks, "last_to_complete": True}
    return {**facts, "head": {**facts["head"], "checks": best}}


#: A completion whose read failed is evaluated again (``services/event_enrichment_retry.py``):
#: when it was the last check to finish on its head, no later delivery reports the head green.
#: A completion dropped while checks still ran watches its head: the delivery of the
#: last check can be lost during a rollout, and the head is then read again.
GITHUB_EVENT_ENRICHER = EventEnricher(
    applies=applies,
    cache_key=cache_key,
    fetch=fetch,
    retryable=is_completion,
    facets=facets,
    watch_subject=head_subject,
    settled=head_settled,
    best_case=head_best_case,
)
