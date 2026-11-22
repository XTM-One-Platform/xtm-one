"""The merge state of several pull requests in one compact read.

An agent babysitting its pull requests reads, every sweep, the state of tens
of them: draft or not, mergeable state, the checks verdict on the head, the
open review threads and the review decision. Hand-written GraphQL asking each
PR's ``statusCheckRollup`` contexts and ``reviewThreads`` nodes hit GitHub's
"Resource limits exceeded" or a 502 in 6 of 8 measured sweeps, and the answers
that came back were 25 to 60 KB of nested JSON.

``list_github_pr_states`` asks one repository for up to 30 numbers, by
batches of 10 aliased ``pullRequest`` fields: only scalars, the rollup's
counts by state (never its contexts) and each thread's resolution flag. The
answer is one line per pull request.

Right after a push to the base branch GitHub recomputes each open PR's
mergeability lazily, and ``mergeable`` / ``mergeStateStatus`` read ``UNKNOWN``
for a few seconds, longer when many PRs of the base are recomputed at once (a
sweep of nine connectors PRs read ``unknown`` on three successive calls after a
three-second window). The tool re-reads only those pull requests (batched,
after ``_UNKNOWN_BACKOFF`` pauses, about 15 s in all, never from a cache) and
answers ``unknown`` only when GitHub is still computing after the last re-read,
saying so.

The verdict follows ``get_github_ci_status``: any failing state is a failure,
else anything not finished is pending, else success; skipped and neutral only
is a success; nothing reported is "none reported". The counts are GitHub's
rollup counts; ``get_github_ci_status`` names each check.

The rollup counts every check run on the head, a superseded attempt of a
re-run check included, so a failure fixed by a re-run still counts as failed.
The rollup cannot be reduced per name without its contexts, so an open pull
request whose rollup reports a failure (the only case a superseded run can
flip) is read again through ``checks.read_ci`` and ``checks.classify_ci``,
the read of ``get_github_ci_status`` keeping the latest run per name: its
verdict and counts are the ones answered, so both tools never disagree. A
re-read that fails keeps the rollup counts and says so.

The verdict is ``checks.ci_verdict``, shared with ``get_github_ci_status`` and
``get_github_pr``: a pull request conflicting with its base (``mergeable``
CONFLICTING, a review block included, or DIRTY) is ``conflict``, never a
success, since no CI runs on its merge ref; a head whose checks all passed but
with a workflow run still queued or in progress (its jobs not reported yet,
read through ``checks.running_workflows``) is ``pending``, the workflows named.
"""

from __future__ import annotations

import asyncio
from typing import Any

from app.core.egress import egress_client
from app.tools.base import CallableToolExecutor, ToolDefinition, ToolResult

from . import _common, checks
from ._common import _coerce_int, one_line, parse_repo

_SOURCE = "integration:github"

#: Numbers one call reads.
MAX_NUMBERS = 30
#: Aliased pull requests per GraphQL request: small enough for GitHub's cost limits.
_BATCH = 10
#: Review threads counted per pull request (GitHub pages them by 100).
_THREADS = 100
#: Characters of a title shown.
_TITLE_CHARS = 80
#: Pauses (seconds) before each re-read of the pull requests whose mergeability
#: GitHub is still computing; one re-read per pause, about 15 s in all. The
#: loop stops at the first read where none is computing, so a settled answer
#: costs no pause.
_UNKNOWN_BACKOFF: tuple[float, ...] = (1.0, 2.0, 4.0, 8.0)

_PR_FIELDS = f"""
      number title state isDraft mergeable mergeStateStatus reviewDecision headRefOid baseRefName
      reviewThreads(first: {_THREADS}) {{ totalCount nodes {{ isResolved }} }}
      commits(last: 1) {{ nodes {{ commit {{ statusCheckRollup {{
        state
        contexts {{
          checkRunCount checkRunCountsByState {{ state count }}
          statusContextCount statusContextCountsByState {{ state count }}
        }}
      }} }} }} }}
"""

#: Check run states (GitHub ``CheckStatusState`` / ``CheckConclusionState``).
_PASSED = frozenset({"SUCCESS"})
_SKIPPED = frozenset({"SKIPPED", "NEUTRAL"})
_PENDING = frozenset({"QUEUED", "IN_PROGRESS", "PENDING", "WAITING", "REQUESTED", "EXPECTED"})


def build_query(numbers: list[int]) -> str:
    """One GraphQL document reading *numbers* as aliases ``pr<N>``."""
    fields = "\n".join(f"    pr{n}: pullRequest(number: {n}) {{{_PR_FIELDS}    }}" for n in numbers)
    return (
        "query($owner: String!, $name: String!) {\n"
        "  repository(owner: $owner, name: $name) {\n"
        f"{fields}\n"
        "  }\n"
        "}\n"
    )


def _counts(rollup: dict[str, Any] | None) -> dict[str, int]:
    """The rollup's checks bucketed as passed / failed / pending / skipped."""
    buckets = {"passed": 0, "failed": 0, "pending": 0, "skipped": 0}
    contexts = (rollup or {}).get("contexts") or {}
    pairs = list(contexts.get("checkRunCountsByState") or []) + list(contexts.get("statusContextCountsByState") or [])
    for pair in pairs:
        if not isinstance(pair, dict):
            continue
        state = str(pair.get("state") or "").upper()
        count = _coerce_int(pair.get("count")) or 0
        if state == "COMPLETED":
            # A completed run is also counted under its conclusion.
            continue
        if state in _PASSED:
            buckets["passed"] += count
        elif state in _SKIPPED:
            buckets["skipped"] += count
        elif state in _PENDING:
            buckets["pending"] += count
        else:
            buckets["failed"] += count
    return buckets


def latest_counts(check_runs: list[dict[str, Any]], statuses: list[dict[str, Any]]) -> dict[str, int]:
    """Bucket counts of ``get_github_ci_status``: the latest run per name, then the commit statuses."""
    return {bucket: len(entries) for bucket, entries in checks.classify_ci(check_runs, statuses).items()}


def rollup_counts(node: dict[str, Any] | None) -> dict[str, int]:
    """The rollup's bucket counts of an open pull request's head commit."""
    commits = ((node or {}).get("commits") or {}).get("nodes") or []
    commit = (commits[0] or {}).get("commit") if commits and isinstance(commits[0], dict) else None
    rollup = (commit or {}).get("statusCheckRollup") if isinstance(commit, dict) else None
    return _counts(rollup if isinstance(rollup, dict) else None)


def verdict(
    buckets: dict[str, int], *, conflict: bool = False, running: list[str] | None = None, stale: int = 0
) -> str:
    """The checks verdict of ``get_github_ci_status`` from bucket counts (``checks.ci_verdict``)."""
    return checks.ci_verdict(buckets, conflict=conflict, running=running, stale=stale)


def waits_on_pending(node: dict[str, Any] | None) -> bool:
    """Whether an open pull request's rollup reads nothing failed and something pending. Pure.

    Such a head is re-read through REST with its base's required checks: a check
    the base does not require, pending for too long, may be all that holds it
    (``checks.GREEN_STALE_OPTIONAL``).
    """
    if not isinstance(node, dict) or str(node.get("state") or "").upper() != "OPEN" or not node.get("headRefOid"):
        return False
    counts = rollup_counts(node)
    return not counts["failed"] and bool(counts["pending"]) and not node_conflict(node)


def _answered_counts(node: dict[str, Any] | None, latest: dict[str, int] | str | None) -> dict[str, int]:
    """The counts a line answers: the REST re-read's when it succeeded, else the rollup's."""
    return latest if isinstance(latest, dict) else rollup_counts(node)


def node_conflict(node: dict[str, Any] | None) -> bool:
    """Whether an open pull request conflicts with its base, a review block included."""
    if not isinstance(node, dict) or str(node.get("state") or "").upper() != "OPEN":
        return False
    return checks.is_conflict(node.get("mergeable"), node.get("mergeStateStatus"))


def is_computing(node: dict[str, Any] | None) -> bool:
    """Whether GitHub is still computing this open pull request's mergeability."""
    if not isinstance(node, dict) or str(node.get("state") or "").upper() != "OPEN":
        return False
    return "UNKNOWN" in (str(node.get("mergeable") or "").upper(), str(node.get("mergeStateStatus") or "").upper())


def format_pr(
    number: int,
    node: dict[str, Any] | None,
    rereads: int = 0,
    latest: dict[str, int] | str | None = None,
    running: list[str] | None = None,
    stale: list[dict[str, Any]] | None = None,
) -> str:
    """One line for one pull request (or why it could not be read).

    *rereads* is the number of re-reads made while GitHub computed the
    mergeability; a pull request still unknown after them says so. *latest*
    holds the latest-run-per-name counts of a head re-read through the REST
    checks (they replace the rollup's), or the error of that re-read.
    *running* names the workflow runs of a green head not finished yet; *stale*
    the checks the base does not require, pending too long to hold a head whose
    required checks all passed (``checks.stale_optional``).
    """
    if not isinstance(node, dict):
        return f"#{number}: not found"
    state = str(node.get("state") or "?").lower()
    parts = [f"#{number}", state]
    if node.get("isDraft"):
        parts.append("draft")
    sha = str(node.get("headRefOid") or "")
    if sha:
        parts.append(f"head {sha[:12]}")
    if state == "open":
        merge_state = str(node.get("mergeStateStatus") or "").lower()
        if merge_state:
            still = f" (GitHub still computing after {rereads} re-read(s))" if rereads and is_computing(node) else ""
            conflicting = " (conflicting)" if node_conflict(node) and merge_state != "dirty" else ""
            parts.append(f"merge {merge_state}{conflicting}{still}")
        buckets = _answered_counts(node, latest)
        note = f", rollup counts: re-read failed: {one_line(latest, _TITLE_CHARS)}" if isinstance(latest, str) else ""
        conflict = node_conflict(node)
        if running and not conflict:
            note += "; " + checks.running_note(running)
        answered = verdict(buckets, conflict=conflict, running=running, stale=len(stale or []))
        if answered == checks.GREEN_STALE_OPTIONAL:
            note += "; " + checks.stale_note(stale)
        parts.append(
            f"checks {answered} ({buckets['passed']} passed, "
            f"{buckets['failed']} failed, {buckets['pending']} pending, {buckets['skipped']} skipped{note})"
        )
        threads = node.get("reviewThreads") or {}
        nodes = [t for t in threads.get("nodes") or [] if isinstance(t, dict)]
        unresolved = sum(1 for t in nodes if not t.get("isResolved"))
        total = _coerce_int(threads.get("totalCount")) or len(nodes)
        partial = f" (first {len(nodes)} read)" if total > len(nodes) else ""
        parts.append(f"threads {unresolved} unresolved of {total}{partial}")
        decision = str(node.get("reviewDecision") or "").lower()
        if decision:
            parts.append(f"review {decision}")
    title = one_line(node.get("title"), _TITLE_CHARS)
    return " | ".join(parts) + (f" | {title}" if title else "")


def parse_numbers(raw: Any) -> tuple[list[int], str | None]:
    """Distinct positive PR numbers in the given order, or an error."""
    if isinstance(raw, (int, str)):
        raw = [raw]
    if not isinstance(raw, list) or not raw:
        return [], "Error: 'numbers' must be a non-empty list of pull request numbers."
    seen: list[int] = []
    for item in raw:
        number = _coerce_int(item)
        if number is None or number <= 0:
            return [], f"Error: '{item}' is not a pull request number."
        if number not in seen:
            seen.append(number)
    if len(seen) > MAX_NUMBERS:
        return [], f"Error: at most {MAX_NUMBERS} pull requests per call ({len(seen)} given); split the list."
    return seen, None


def register(
    headers: dict[str, str],
    executor: CallableToolExecutor,
    mark_self_action: Any,
) -> list[ToolDefinition]:
    """Build the multi pull request state tool and register its callable."""

    async def list_github_pr_states(repo: str, numbers: list[int]) -> str | ToolResult:
        """One line of merge state per pull request of *repo*."""
        owner, name, rerr = parse_repo(repo)
        if rerr:
            return rerr
        wanted, nerr = parse_numbers(numbers)
        if nerr:
            return nerr

        async def read(client: Any, subset: list[int]) -> list[tuple[list[int], dict[str, Any] | None, str | None]]:
            """Each batch of *subset* with its repository object, or its error."""
            batches = [subset[i : i + _BATCH] for i in range(0, len(subset), _BATCH)]
            results = await asyncio.gather(
                *(
                    _common.gh_graphql(
                        client, headers, build_query(batch), {"owner": owner, "name": name},
                        action=f"read the state of {len(batch)} pull request(s) of {repo}",
                    )
                    for batch in batches
                )
            )
            return [
                (batch, None if err else ((data or {}).get("repository") or {}), err)
                for batch, (data, err) in zip(batches, results, strict=True)
            ]

        rereads = 0
        async with egress_client(timeout=30) as client:
            first = await read(client, wanted)
            nodes: dict[int, Any] = {}
            for batch, repository, _err in first:
                if repository is not None:
                    nodes.update({n: repository.get(f"pr{n}") for n in batch})
            for pause in _UNKNOWN_BACKOFF:
                computing = [n for n, node in nodes.items() if is_computing(node)]
                if not computing:
                    break
                await asyncio.sleep(pause)
                rereads += 1
                for batch, repository, _err in await read(client, computing):
                    # A re-read that fails keeps the first answer for its batch.
                    for number in batch if repository is not None else []:
                        node = repository.get(f"pr{number}")
                        if isinstance(node, dict):
                            nodes[number] = node
            # A failure in the rollup may be a superseded run: re-read those heads
            # the way get_github_ci_status does (latest run per name).
            suspect = [
                (n, str(node.get("headRefOid")))
                for n, node in nodes.items()
                if isinstance(node, dict)
                and str(node.get("state") or "").upper() == "OPEN"
                and node.get("headRefOid")
                and rollup_counts(node)["failed"]
            ]
            reads = await asyncio.gather(
                *(checks.read_ci(client, headers, owner, name, sha) for _n, sha in suspect)
            )
            latest: dict[int, dict[str, int] | str] = {}
            for (number, _sha), (runs, statuses, _truncated, err) in zip(suspect, reads, strict=True):
                latest[number] = err if err else latest_counts(runs, statuses)
            # A head with nothing failed but something pending: re-read it with its
            # base's required checks, a stale optional check may be all that holds it.
            waiting = [
                (n, str(node.get("headRefOid")), str(node.get("baseRefName") or ""))
                for n, node in nodes.items()
                if waits_on_pending(node)
            ]
            bases = sorted({base for _n, _sha, base in waiting if base})
            contexts = await asyncio.gather(
                *(checks.read_required_contexts(client, headers, owner, name, base) for base in bases)
            )
            required_of = dict(zip(bases, contexts, strict=True))
            pending_reads = await asyncio.gather(
                *(
                    checks.read_ci(client, headers, owner, name, sha)
                    for _n, sha, base in waiting
                    if required_of.get(base)
                )
            )
            stale_of: dict[int, list[dict[str, Any]]] = {}
            for (number, _sha, base), (runs, statuses, _truncated, err) in zip(
                [w for w in waiting if required_of.get(w[2])], pending_reads, strict=True
            ):
                if err:
                    continue
                buckets = checks.classify_ci(runs, statuses)
                found = checks.stale_optional(buckets, required_of[base])
                if found:
                    latest[number] = {bucket: len(entries) for bucket, entries in buckets.items()}
                    stale_of[number] = found
            # A head whose reported checks all passed may still have a workflow
            # run queued (its jobs not reported yet): read those heads' runs.
            green = [
                (n, str(node.get("headRefOid")))
                for n, node in nodes.items()
                if isinstance(node, dict)
                and str(node.get("state") or "").upper() == "OPEN"
                and node.get("headRefOid")
                and not node_conflict(node)
                and verdict(_answered_counts(node, latest.get(n)), stale=len(stale_of.get(n, [])))
                in ("success", checks.GREEN_STALE_OPTIONAL)
            ]
            runs_of = await asyncio.gather(
                *(checks.running_workflows(client, headers, owner, name, sha) for _n, sha in green)
            )
        running = {n: r for (n, _sha), r in zip(green, runs_of, strict=True) if r}
        lines = [f"Pull requests of {owner}/{name} ({len(wanted)}):"]
        for batch, repository, err in first:
            if repository is None:
                # GitHub answers a missing number as an error for the whole request;
                # the other batches still answer.
                lines.append(f"#{', #'.join(str(n) for n in batch)}: not read ({err})")
                continue
            for number in batch:
                lines.append(
                    format_pr(
                        number,
                        nodes.get(number),
                        rereads,
                        latest.get(number),
                        running.get(number),
                        stale_of.get(number),
                    )
                )
        text = "\n".join(lines)
        if any(is_computing(node) for node in nodes.values()):
            # A merge state GitHub is still computing is not an answer to keep:
            # transient, so the agent loop never replays it from its cache.
            return ToolResult(tool_call_id="", content=text, transient=True)
        return text

    executor.register("list_github_pr_states", list_github_pr_states)

    return [
        ToolDefinition(
            name="list_github_pr_states",
            description=(
                "Read the merge state of up to 30 pull requests of one repository in one call, one line each: "
                "state, draft, head SHA, merge state (clean, dirty, blocked, unstable, behind), the checks "
                "verdict on the head with passed / failed / pending / skipped counts (the latest run per check name, "
                "as get_github_ci_status counts them), unresolved review threads "
                "and the review decision. A merge state GitHub is still computing (right after a push to the "
                "base) is re-read for up to about 15 s before it is answered unknown. "
                "Use it to sweep many PRs instead of a GraphQL query or one "
                "get_github_pr per PR; get_github_ci_status names the checks of one PR."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "repo": {"type": "string", "description": "Repository in format owner/name"},
                    "numbers": {
                        "type": "array",
                        "items": {"type": "integer"},
                        "description": f"Pull request numbers (at most {MAX_NUMBERS})",
                    },
                },
                "required": ["repo", "numbers"],
            },
            source=_SOURCE,
            priority=0,
            # PR state moves with CI and the base: a re-read later is a new question.
            live_read=True,
        ),
    ]
