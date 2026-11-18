"""GitHub checks & commit-status tools - check runs, compact CI status, combined status, create status."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any

from app.core.egress import egress_client
from app.tools.base import CallableToolExecutor, ToolDefinition

from . import _common
from ._common import _BASE_URL, _CONCLUSION_ICONS, _PROVIDER, _coerce_int, _enc, _json_headers, parse_repo

_SOURCE = "integration:github"

# get_github_ci_status: every check run of a ref is read (GitHub pages
# them by 100), bounded so a pathological ref cannot loop for ever.
_CI_PAGE_SIZE = 100
_CI_MAX_PAGES = 10
#: Failing GitHub Actions jobs whose failed step is looked up (one call each).
_CI_MAX_STEP_LOOKUPS = 10
#: Pending checks are listed by name only, at most this many.
_CI_MAX_PENDING_NAMES = 40

#: A completed check run or a commit status that passed or was not run by design.
_CI_PASSED = frozenset({"success"})
_CI_SKIPPED = frozenset({"skipped", "neutral"})
#: Workflow runs listed per head when looking for one not finished.
_CI_RUNS_PAGE = 100

#: A check outside the base's required ones, pending for longer than this, no longer
#: holds a head whose required checks all passed (a third-party app left in progress).
STALE_OPTIONAL_SECONDS = 30 * 60
#: The verdict of such a head: every required check passed, only stale optional checks pending.
GREEN_STALE_OPTIONAL = "green_required_stale_optional"


def is_conflict(mergeable: Any, merge_state: Any) -> bool:
    """Whether a pull request conflicts with its base.

    REST answers ``mergeable`` false and ``mergeable_state`` "dirty"; GraphQL
    ``mergeable`` CONFLICTING and ``mergeStateStatus`` DIRTY. A review block
    (``blocked``) hides a conflict in the merge state, so ``mergeable`` is read too.
    """
    if mergeable is False or str(mergeable or "").upper() == "CONFLICTING":
        return True
    return str(merge_state or "").lower() == "dirty"


def ci_verdict(
    counts: dict[str, int], *, conflict: bool = False, running: list[str] | None = None, stale: int = 0
) -> str:
    """The one CI verdict of ``get_github_ci_status``, ``list_github_pr_states`` and ``get_github_pr``.

    *counts* are the latest-run-per-name buckets. A failing check wins; then a
    pull request conflicting with its base (no CI runs on its merge ref) is
    ``conflict``; then a workflow run of the head not finished (*running*, its
    jobs not reported yet) is ``pending``; then, when every pending check is one
    of the *stale* ones (:func:`stale_optional`: every required check passed),
    :data:`GREEN_STALE_OPTIONAL`; then any other pending check is ``pending``;
    else ``success`` (skipped or neutral only included), or ``none reported``.
    """
    if counts.get("failed"):
        return "failure"
    if conflict:
        return "conflict"
    if running:
        return "pending"
    pending = counts.get("pending") or 0
    if pending and stale >= pending:
        return GREEN_STALE_OPTIONAL
    if pending:
        return "pending"
    return "success" if any(counts.values()) else "none reported"


def running_note(running: list[str] | None) -> str:
    """Name the workflow runs of the head not finished yet (empty when none)."""
    if not running:
        return ""
    names = sorted(set(running))
    return f"Workflow run(s) not finished: {', '.join(names[:10])}" + (
        f" (+{len(names) - 10} more)" if len(names) > 10 else ""
    )


async def running_workflows(client: Any, headers: dict[str, str], owner: str, name: str, sha: str) -> list[str] | None:
    """Names of the GitHub Actions workflow runs of commit *sha* not completed, ``None`` when unreadable.

    A queued workflow run has no check run yet: every check reported can be
    green while jobs are still to come. The first page (100 runs, newest first)
    is read; a head with more runs than that is not a case met in practice.
    """
    resp, err = await _common.safe_request(
        client,
        "get",
        f"{_BASE_URL}/repos/{owner}/{name}/actions/runs",
        _PROVIDER,
        f"list workflow runs of {sha[:12]} in {owner}/{name}",
        headers=headers,
        params={"head_sha": sha, "per_page": _CI_RUNS_PAGE},
    )
    if err or resp is None:
        return None
    runs = (resp.json() or {}).get("workflow_runs") or []
    return [
        str(r.get("name") or r.get("display_title") or "?")
        for r in runs
        if isinstance(r, dict) and (r.get("status") or "completed") != "completed"
    ]


def latest_check_runs(check_runs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep one check run per (app, name): the latest, the highest id.

    A re-run adds a new check run under the same name; the older one still
    lists on the ref and would read as a failure that was already fixed.
    """
    latest: dict[tuple[str, str], dict[str, Any]] = {}
    for run in check_runs:
        key = ((run.get("app") or {}).get("slug") or "", run.get("name") or "")
        kept = latest.get(key)
        if kept is None or (_coerce_int(run.get("id")) or 0) > (_coerce_int(kept.get("id")) or 0):
            latest[key] = run
    return list(latest.values())


#: The app and check-name prefix of a coverage grade (Codecov), read before every upload may have arrived.
COVERAGE_APP_SLUG = "codecov"
COVERAGE_NAME_PREFIX = "codecov/"


def is_coverage_grade(run: dict[str, Any]) -> bool:
    """Whether *run* (a check run, or a commit status read as one) is a Codecov grade. Pure."""
    app = run.get("app") if isinstance(run.get("app"), dict) else {}
    return app.get("slug") == COVERAGE_APP_SLUG or str(run.get("name") or "").startswith(COVERAGE_NAME_PREFIX)


def coverage_pending(runs: list[dict[str, Any]]) -> bool:
    """Whether the coverage grades among the latest *runs* of a head are not final yet. Pure.

    Codecov grades a head as soon as a first upload lands and grades it again
    as the others arrive, so a red it reports while a coverage-producing check
    of the same head still runs is provisional: it can turn green with no push
    (#12050). Any other check of the head not completed counts as a producer.
    A red grade decides only once every other check completed.
    """
    if not any(is_coverage_grade(r) for r in runs):
        return False
    return any(not is_coverage_grade(r) and (r.get("status") != "completed" or not r.get("conclusion")) for r in runs)


def _moment(value: Any) -> datetime | None:
    """A GitHub ISO time as an aware datetime, ``None`` when absent or unreadable. Pure."""
    if not isinstance(value, str) or not value:
        return None
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)


def pending_since(check: dict[str, Any]) -> str | None:
    """When a pending check last moved: a check run's ``started_at``, a status's ``updated_at``. Pure.

    A ``classify_ci`` entry carries it as ``since``.
    """
    for key in ("since", "started_at", "updated_at"):
        value = check.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def stale_checks(
    pending: Iterable[dict[str, Any]],
    required: frozenset[str] = frozenset(),
    now: datetime | None = None,
    stale_after: int = STALE_OPTIONAL_SECONDS,
) -> list[dict[str, Any]]:
    """The *pending* checks outside *required* that have not moved for over *stale_after* seconds. Pure.

    Each is ``{name, since, age_seconds}``. A check with no readable start time is
    never stale: only a check seen pending for that long is left out of a verdict.
    """
    moment = now or datetime.now(UTC)
    out: list[dict[str, Any]] = []
    for check in pending:
        name = str(check.get("name") or check.get("context") or "")
        if not name or name in required:
            continue
        since = pending_since(check)
        started = _moment(since)
        if started is None:
            continue
        age = int((moment - started).total_seconds())
        if age > stale_after:
            out.append({"name": name, "since": since, "age_seconds": age})
    return out


def stale_optional(
    buckets: dict[str, list[dict[str, Any]]], required: frozenset[str], now: datetime | None = None
) -> list[dict[str, Any]]:
    """The stale pending checks of a head whose required checks all passed, else none. Pure.

    Empty unless the base requires at least one context, every required context
    reported and passed (:func:`summarize_required`) and no check failed: only then
    can a non-required check pending for over :data:`STALE_OPTIONAL_SECONDS` stop
    holding the head (:data:`GREEN_STALE_OPTIONAL`).
    """
    if not required or buckets.get("failed") or not buckets.get("pending"):
        return []
    summary = summarize_required(buckets, required)
    if summary["failed"] or summary["pending"] or summary["missing"]:
        return []
    return stale_checks(buckets["pending"], required, now)


def format_age(seconds: int) -> str:
    """``5h30m`` / ``42m``: the age of a stale check. Pure."""
    minutes = max(0, seconds) // 60
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h{minutes:02d}m" if hours else f"{minutes}m"


def stale_note(stale: list[dict[str, Any]] | None) -> str:
    """Name the stale optional checks left out of the verdict, with their age (empty when none). Pure."""
    if not stale:
        return ""
    names = ", ".join(
        f"{s['name']} (pending {format_age(int(s.get('age_seconds') or 0))})"
        for s in sorted(stale, key=lambda s: s["name"])
    )
    return f"Stale optional check(s), not counted: {names}"


def classify_ci(check_runs: list[dict[str, Any]], statuses: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """Sort the latest check runs and commit statuses of a ref into four buckets.

    Each entry is ``{name, state, url, app, id}``. ``passed``: success;
    ``skipped``: skipped or neutral; ``pending``: not completed yet; ``failed``:
    every other conclusion (failure, timed_out, cancelled, action_required,
    startup_failure, stale) and the error / failure commit statuses.
    """
    buckets: dict[str, list[dict[str, Any]]] = {"failed": [], "pending": [], "passed": [], "skipped": []}
    for run in latest_check_runs(check_runs):
        status = run.get("status") or ""
        conclusion = run.get("conclusion") or ""
        entry = {
            "name": run.get("name") or "?",
            "state": conclusion or status or "pending",
            "url": run.get("html_url") or run.get("details_url") or "",
            "app": (run.get("app") or {}).get("slug") or "",
            "id": _coerce_int(run.get("id")),
            "since": run.get("started_at"),
        }
        if status != "completed" or not conclusion:
            buckets["pending"].append(entry)
        elif conclusion in _CI_PASSED:
            buckets["passed"].append(entry)
        elif conclusion in _CI_SKIPPED:
            buckets["skipped"].append(entry)
        else:
            buckets["failed"].append(entry)
    for status in statuses:
        state = status.get("state") or "pending"
        entry = {
            "name": status.get("context") or "?",
            "state": state,
            "url": status.get("target_url") or "",
            "app": "status",
            "id": None,
            "since": status.get("updated_at"),
        }
        if state == "pending":
            buckets["pending"].append(entry)
        elif state in _CI_PASSED:
            buckets["passed"].append(entry)
        else:
            buckets["failed"].append(entry)
    return buckets


async def read_ci(
    client: Any, headers: dict[str, str], owner: str, name: str, ref: str
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], bool, str | None]:
    """Every check run (paged) and every commit status of *ref*: ``(check_runs, statuses, truncated, error)``.

    The one read behind ``get_github_ci_status`` and the re-read of
    ``list_github_pr_states``; ``classify_ci`` turns it into the verdict both
    tools answer. *truncated* says the page bound was hit; on an error the
    lists are empty and *error* is the tool-ready message.
    """
    repo = f"{owner}/{name}"
    check_runs: list[dict[str, Any]] = []
    truncated = False
    for page in range(1, _CI_MAX_PAGES + 1):
        resp, err = await _common.safe_request(
            client,
            "get",
            f"{_BASE_URL}/repos/{owner}/{name}/commits/{_enc(ref)}/check-runs",
            _PROVIDER,
            f"get check runs for {ref} in {repo}",
            headers=headers,
            params={"per_page": _CI_PAGE_SIZE, "page": page},
        )
        if err:
            return [], [], False, err
        data = resp.json()
        batch = data.get("check_runs") or []
        check_runs.extend(batch)
        total = _coerce_int(data.get("total_count")) or 0
        if len(batch) < _CI_PAGE_SIZE or len(check_runs) >= total:
            break
    else:
        truncated = True
    resp, err = await _common.safe_request(
        client,
        "get",
        f"{_BASE_URL}/repos/{owner}/{name}/commits/{_enc(ref)}/status",
        _PROVIDER,
        f"get commit status for {ref} in {repo}",
        headers=headers,
        params={"per_page": _CI_PAGE_SIZE},
    )
    if err:
        return [], [], False, err
    statuses = resp.json().get("statuses") or []
    return check_runs, statuses, truncated, None


def required_contexts(rules: Any, protection: Any) -> frozenset[str]:
    """The status check contexts a branch requires.

    ``rules`` is the effective-rules array of ``GET /repos/{o}/{r}/rules/branches/{b}``
    (every active ruleset, repository, organization and enterprise); ``protection``
    the classic ``.../protection/required_status_checks`` object. Either may be
    missing or malformed: what cannot be read adds nothing.
    """
    contexts: set[str] = set()
    for rule in rules if isinstance(rules, list) else []:
        if not isinstance(rule, dict) or rule.get("type") != "required_status_checks":
            continue
        for check in (rule.get("parameters") or {}).get("required_status_checks") or []:
            if isinstance(check, dict) and check.get("context"):
                contexts.add(str(check["context"]))
    if isinstance(protection, dict):
        for context in protection.get("contexts") or []:
            if context:
                contexts.add(str(context))
        for check in protection.get("checks") or []:
            if isinstance(check, dict) and check.get("context"):
                contexts.add(str(check["context"]))
    return frozenset(contexts)


async def read_required_contexts(
    client: Any, headers: dict[str, str], owner: str, name: str, branch: str
) -> frozenset[str]:
    """The required status checks of a branch, rulesets and classic protection.

    A read that fails (no rule, no protection, no permission) adds nothing:
    a tool built on it never fails on it.
    """
    rules: Any = None
    protection: Any = None
    resp, err = await _common.safe_request(
        client,
        "get",
        f"{_BASE_URL}/repos/{owner}/{name}/rules/branches/{_enc(branch)}",
        _PROVIDER,
        f"get effective rules for {branch} in {owner}/{name}",
        headers=headers,
        params={"per_page": _CI_PAGE_SIZE},
    )
    if not err and resp is not None:
        rules = resp.json()
    resp, err = await _common.safe_request(
        client,
        "get",
        f"{_BASE_URL}/repos/{owner}/{name}/branches/{_enc(branch)}/protection/required_status_checks",
        _PROVIDER,
        f"get required status checks of {branch} in {owner}/{name}",
        headers=headers,
    )
    if not err and resp is not None:
        protection = resp.json()
    return required_contexts(rules, protection)


def summarize_required(buckets: dict[str, list[dict[str, Any]]], required: frozenset[str]) -> dict[str, list[str]]:
    """Place each required context in the bucket of the check that reported it.

    A skipped or neutral required check satisfies the rule on GitHub, so it counts
    as passed; a required context no check run or status reported is ``missing``.
    """
    seen: dict[str, str] = {}
    for bucket in ("passed", "skipped", "pending", "failed"):
        for entry in buckets[bucket]:
            seen[entry["name"]] = bucket
    summary: dict[str, list[str]] = {"passed": [], "failed": [], "pending": [], "missing": []}
    for context in sorted(required):
        found = seen.get(context)
        if found is None:
            summary["missing"].append(context)
        elif found in ("passed", "skipped"):
            summary["passed"].append(context)
        else:
            summary[found].append(context)
    return summary


def format_required(base: str, summary: dict[str, list[str]]) -> str:
    """One line: how many of the base branch's required checks passed, and what blocks."""
    total = sum(len(v) for v in summary.values())
    line = f"Required by {base}: {len(summary['passed'])} of {total} passed"
    parts = [
        f"{len(summary['failed'])} failing" if summary["failed"] else "",
        f"{len(summary['pending'])} pending" if summary["pending"] else "",
        (f"not reported: {', '.join(summary['missing'])}" if summary["missing"] else ""),
    ]
    rest = ", ".join(p for p in parts if p)
    return f"{line}, {rest}" if rest else line


_OVERALL = {
    "failure": "✗ FAILURE",
    "conflict": "⚠ CONFLICT",
    "pending": "⟳ PENDING",
    GREEN_STALE_OPTIONAL: "✓ GREEN - every required check passed, only stale optional check(s) pending",
    "success": "✓ SUCCESS - all passed",
    "none reported": "none reported",
}


def format_ci_status(
    label: str,
    buckets: dict[str, list[dict[str, Any]]],
    failed_steps: dict[int, list[str]] | None = None,
    *,
    truncated: bool = False,
    required: frozenset[str] | None = None,
    base: str = "",
    conflict: str = "",
    running: list[str] | None = None,
    stale: list[dict[str, Any]] | None = None,
) -> str:
    """Render the compact CI status: counts always, detail only for what did not pass.

    With ``required`` (the contexts the base branch requires, a pull request read),
    a ``Required by main`` line (the base branch's name) follows the counts and a failed or pending
    required check is tagged ``(required)``. *conflict* (the PR's merge state when it
    conflicts with its base) and *running* (workflow runs of the head not finished)
    feed :func:`ci_verdict`, so green checks never read as success over them. *stale*
    (:func:`stale_optional`) names the non-required checks pending too long to hold
    a head whose required checks passed: the verdict is :data:`GREEN_STALE_OPTIONAL`.
    """
    required = required or frozenset()
    failed, pending = buckets["failed"], buckets["pending"]
    total = sum(len(v) for v in buckets.values())
    counts = (
        f"{len(buckets['passed'])} passed, {len(failed)} failed, {len(pending)} pending, "
        f"{len(buckets['skipped'])} skipped ({total} checks)"
    )
    verdict = ci_verdict(
        {k: len(v) for k, v in buckets.items()}, conflict=bool(conflict), running=running, stale=len(stale or [])
    )
    conflict_line = (
        f"Conflict: the PR conflicts with its base (merge state {conflict}); no CI runs on its merge ref "
        "until the conflict is resolved."
        if conflict
        else ""
    )
    if total == 0:
        if verdict == "none reported":
            return f"CI for {label}: no check runs and no commit statuses reported (yet)."
        lines = [f"CI for {label}: {_OVERALL[verdict]}, no check reported yet"]
        lines += [line for line in (conflict_line, running_note(running)) if line]
        return "\n".join(lines)
    lines = [f"CI for {label}: {_OVERALL[verdict]}", counts]
    if conflict_line:
        lines.append(conflict_line)
    if running and not failed:
        lines.append(running_note(running))
    if verdict == GREEN_STALE_OPTIONAL:
        lines.append(stale_note(stale))
    if required:
        lines.append(format_required(base or "the base branch", summarize_required(buckets, required)))
    if truncated:
        lines.append(f"Note: only the first {_CI_MAX_PAGES * _CI_PAGE_SIZE} check runs were read.")
    if failed:
        lines.append(f"\nFailed ({len(failed)}):")
        for entry in sorted(failed, key=lambda e: e["name"]):
            icon = _CONCLUSION_ICONS.get(entry["state"], "✗")
            tag = " (required)" if entry["name"] in required else ""
            lines.append(f"  {icon} {entry['name']}{tag} [{entry['state']}] {entry['url']}".rstrip())
            steps = (failed_steps or {}).get(entry["id"] or 0)
            if steps:
                lines.append(f"      failed step(s): {', '.join(steps)}")
            if entry["app"] == "github-actions" and entry["id"]:
                lines.append(f"      job_id {entry['id']}: get_github_job_logs(failures_only=true) for the cause")
    if pending:
        names = sorted(e["name"] + (" (required)" if e["name"] in required else "") for e in pending)
        shown = ", ".join(names[:_CI_MAX_PENDING_NAMES])
        more = f" (+{len(names) - _CI_MAX_PENDING_NAMES} more)" if len(names) > _CI_MAX_PENDING_NAMES else ""
        lines.append(f"\nPending ({len(pending)}): {shown}{more}")
    return "\n".join(lines)


def register(
    headers: dict[str, str],
    executor: CallableToolExecutor,
    mark_self_action: Any,
) -> list[ToolDefinition]:
    """Build the GitHub checks tools and register their callables."""

    async def _required_for(client: Any, owner: str, name: str, branch: str) -> frozenset[str]:
        return await read_required_contexts(client, headers, owner, name, branch)

    async def get_github_check_runs(repo: str, ref: str) -> str:
        """Get check runs for a specific git ref (branch, tag, or commit SHA).

        Shows CI/CD check results including status, conclusion, and duration.
        """
        owner, name, rerr = parse_repo(repo)
        if rerr:
            return rerr
        async with egress_client(timeout=30) as client:
            resp, err = await _common.safe_request(
                client,
                "get",
                f"{_BASE_URL}/repos/{owner}/{name}/commits/{_enc(ref)}/check-runs",
                _PROVIDER,
                f"get check runs for {ref} in {repo}",
                headers=headers,
                params={"per_page": 50},
            )
            if err:
                return err
            data = resp.json()
            checks = data.get("check_runs", [])
            total = data.get("total_count", 0)
            if not checks:
                return f"No check runs found for {ref} in {repo}."
            conclusions: dict[str, int] = {}
            for c in checks:
                key = c.get("conclusion") or c.get("status", "pending")
                conclusions[key] = conclusions.get(key, 0) + 1
            summary = ", ".join(f"{v} {k}" for k, v in conclusions.items())
            lines = [f"Check runs for {ref} ({total} total: {summary}):"]
            for c in checks:
                conclusion = c.get("conclusion") or c.get("status", "pending")
                icon = _CONCLUSION_ICONS.get(conclusion, "?")
                app_name = (c.get("app") or {}).get("name", "")
                lines.append(f"  {icon} {c['name']} [{conclusion}] ({app_name})")
            return "\n".join(lines)

    async def get_github_commit_status(repo: str, ref: str) -> str:
        """Get the combined commit status for a git ref.

        Returns the overall state (success, pending, failure) and individual
        status checks from CI systems.
        """
        owner, name, rerr = parse_repo(repo)
        if rerr:
            return rerr
        async with egress_client(timeout=30) as client:
            resp, err = await _common.safe_request(
                client,
                "get",
                f"{_BASE_URL}/repos/{owner}/{name}/commits/{_enc(ref)}/status",
                _PROVIDER,
                f"get commit status for {ref} in {repo}",
                headers=headers,
            )
            if err:
                return err
            data = resp.json()
            state = data.get("state", "unknown")
            statuses = data.get("statuses", [])
            icon = _CONCLUSION_ICONS.get(state, "?")
            lines = [f"Combined status for {ref}: {icon} {state.upper()}"]
            if statuses:
                lines.append(f"\nIndividual statuses ({len(statuses)}):")
                for s in statuses:
                    s_icon = _CONCLUSION_ICONS.get(s.get("state", ""), "?")
                    context = s.get("context", "?")
                    desc = s.get("description", "")
                    lines.append(f"  {s_icon} {context}: {desc}")
            else:
                lines.append("\nNo individual status checks found.")
            return "\n".join(lines)

    async def get_github_ci_status(repo: str, ref: str = "", pr_number: int = 0) -> str:
        """Compact CI status of a ref or a PR head: counts, and detail only for what did not pass."""
        owner, name, rerr = parse_repo(repo)
        if rerr:
            return rerr
        ref = (ref or "").strip() if isinstance(ref, str) else ""
        number = _coerce_int(pr_number) or 0
        if bool(ref) == bool(number):
            return "Error: give exactly one of 'ref' (branch, tag or commit SHA) or 'pr_number'."
        async with egress_client(timeout=30) as client:
            label = f"{owner}/{name}@{ref}"
            base = ""
            conflict = ""
            required: frozenset[str] = frozenset()
            if number:
                resp, err = await _common.safe_request(
                    client,
                    "get",
                    f"{_BASE_URL}/repos/{owner}/{name}/pulls/{number}",
                    _PROVIDER,
                    f"get pull request #{number} in {repo}",
                    headers=headers,
                )
                if err:
                    return err
                pr = resp.json()
                ref = ((pr.get("head") or {}).get("sha")) or ""
                if not ref:
                    return f"Error: pull request #{number} in {repo} has no head commit."
                state = "merged" if pr.get("merged_at") else (pr.get("state") or "?")
                label = f"{owner}/{name} PR #{number} ({state}) head {ref[:8]}"
                base = str((pr.get("base") or {}).get("ref") or "")
                if state == "open" and is_conflict(pr.get("mergeable"), pr.get("mergeable_state")):
                    conflict = str(pr.get("mergeable_state") or "dirty")
                if base:
                    required = await _required_for(client, owner, name, base)
            check_runs, statuses, truncated, err = await read_ci(client, headers, owner, name, ref)
            if err:
                return err
            head_sha = next((str(r["head_sha"]) for r in check_runs if r.get("head_sha")), "")
            if not number and head_sha and not head_sha.startswith(ref):
                label = f"{label} (head {head_sha[:8]})"
            buckets = classify_ci(check_runs, statuses)
            # Every required check passed and only checks the base does not require,
            # pending for too long, are left: they no longer hold the head.
            stale = stale_optional(buckets, required)
            running: list[str] | None = None
            if not buckets["failed"] and len(buckets["pending"]) <= len(stale):
                # Every reported check passed: a workflow run still queued has
                # not created its check runs yet, so the head is not green.
                running = await running_workflows(client, headers, owner, name, ref)
            failed_steps: dict[int, list[str]] = {}
            actions_failed = [e for e in buckets["failed"] if e["app"] == "github-actions" and e["id"]]
            for entry in actions_failed[:_CI_MAX_STEP_LOOKUPS]:
                # A GitHub Actions check run id is its job id: the job lists its steps.
                resp, err = await _common.safe_request(
                    client,
                    "get",
                    f"{_BASE_URL}/repos/{owner}/{name}/actions/jobs/{entry['id']}",
                    _PROVIDER,
                    f"get job {entry['id']} in {repo}",
                    headers=headers,
                )
                if err:
                    continue
                steps = [
                    s.get("name") or "?"
                    for s in (resp.json().get("steps") or [])
                    if s.get("conclusion") in ("failure", "timed_out", "cancelled")
                ]
                if steps:
                    failed_steps[entry["id"]] = steps
            return format_ci_status(
                label,
                buckets,
                failed_steps,
                truncated=truncated,
                required=required,
                base=base,
                conflict=conflict,
                running=running,
                stale=stale,
            )

    async def create_github_commit_status(
        repo: str,
        sha: str,
        state: str,
        context: str = "default",
        description: str = "",
        target_url: str = "",
    ) -> str:
        """Set a commit status (write). state=error|failure|pending|success; context identifies the check."""
        owner, name, rerr = parse_repo(repo)
        if rerr:
            return rerr
        if state not in ("error", "failure", "pending", "success"):
            return "Error: state must be one of error, failure, pending, success."
        payload: dict[str, Any] = {"state": state, "context": context or "default"}
        if description:
            payload["description"] = description[:140]
        if target_url:
            payload["target_url"] = target_url
        async with egress_client(timeout=30) as client:
            _resp, err = await _common.safe_request(
                client,
                "post",
                f"{_BASE_URL}/repos/{owner}/{name}/statuses/{_enc(sha)}",
                _PROVIDER,
                f"set commit status on {sha} in {repo}",
                headers=_json_headers(headers),
                json=payload,
            )
            if err:
                return err
            return f"Status '{context or 'default'}' = {state} set on {sha[:8]} in {owner}/{name}."

    executor.register("get_github_check_runs", get_github_check_runs)
    executor.register("get_github_commit_status", get_github_commit_status)
    executor.register("get_github_ci_status", get_github_ci_status)
    executor.register("create_github_commit_status", create_github_commit_status)

    _repo = {"type": "string", "description": "Repository in format owner/name"}

    return [
        ToolDefinition(
            name="get_github_check_runs",
            # State other actors move (CI, reviews, other runs): a re-read later is a new question.
            live_read=True,
            description=(
                "Get CI/CD check runs for a specific git ref (branch, tag, or commit SHA). Shows each check's name, "
                "status, conclusion, and the CI app that ran it. Use to verify CI status before merging a PR."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "repo": _repo,
                    "ref": {"type": "string", "description": "Branch name, tag, or commit SHA"},
                },
                "required": ["repo", "ref"],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="get_github_commit_status",
            # Polled: the same call later answers a state that moved on its own.
            live_state=True,
            description=(
                "Get the combined commit status for a git ref. Returns the overall state (success, pending, failure) "
                "and all individual status checks. Covers external CI systems that report via the Status API."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "repo": _repo,
                    "ref": {"type": "string", "description": "Branch name, tag, or commit SHA"},
                },
                "required": ["repo", "ref"],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="get_github_ci_status",
            description=(
                "Compact CI status of a ref or of a pull request's head commit: the overall verdict (success, failure, "
                "pending), counts of passed / failed / pending / skipped checks, then only the checks that did not "
                "pass (name, conclusion, link, the failed step and job_id of a GitHub Actions job) and the pending "
                "ones by name. Reads every check run (the latest run per name) and every commit status. With pr_number, "
                "a 'Required by' line (the base branch named) says how many of the checks the base branch requires passed and names the "
                "required ones that never reported; a failed or pending required check is tagged (required). Use it to "
                "poll CI and before merging; each call reads GitHub live. get_github_check_runs lists every check instead."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "repo": _repo,
                    "ref": {
                        "type": "string",
                        "description": "Branch name, tag, or commit SHA (give this or pr_number)",
                        "default": "",
                    },
                    "pr_number": {
                        "type": "integer",
                        "description": "Pull request number: its head commit is read (give this or ref)",
                        "default": 0,
                    },
                },
                "required": ["repo"],
            },
            source=_SOURCE,
            priority=0,
            live_state=True,
        ),
        ToolDefinition(
            name="create_github_commit_status",
            description=(
                "Set a commit status on a SHA (the Status API used by external CI). state=error|failure|pending|success; "
                "context names the check (e.g. 'ci/my-check'); description is a short summary; target_url links to details."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "repo": _repo,
                    "sha": {"type": "string", "description": "Commit SHA to attach the status to"},
                    "state": {"type": "string", "enum": ["error", "failure", "pending", "success"]},
                    "context": {"type": "string", "description": "Status context/name", "default": "default"},
                    "description": {"type": "string", "description": "Short description (<=140 chars)", "default": ""},
                    "target_url": {"type": "string", "description": "URL with details (optional)", "default": ""},
                },
                "required": ["repo", "sha", "state"],
            },
            source=_SOURCE,
            priority=1,
        ),
    ]
