"""The full CI run a repository requires on a pull request's head before a merge.

A repository whose pull request CI skips its suites (they used to run in a merge
queue) says which workflows must have run in full on the exact head before
``merge_github_pr`` lands it, next to its sandbox checks in ``.xtm-one/checks.toml``::

    [merge]
    full_ci_workflows = ["ci.yml"]

Each name is a workflow file of ``.github/workflows`` whose ``workflow_dispatch``
run executes every job (no path filter). Before the merge, ``merge_github_pr``
reads the table on the base branch and, for each workflow, the newest
``workflow_dispatch`` run on the pull request's head SHA:

- none on that SHA (never run, or only on an older head): the workflow is
  dispatched on the head branch and the merge is refused, naming what to follow;
- queued or running: refused, the run named;
- completed and not successful: refused, the run and its conclusion named; a fix
  pushed is a new SHA, so the gate starts again there;
- successful: the merge goes on, pinned to that SHA, and its answer names the run.

No file, no table, no list, or a file that cannot be read: no gate, the merge
goes on as before. A malformed table is named in the merge answer, never a refusal.
"""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

from . import _common
from ._common import _BASE_URL, _PROVIDER, _json_headers

#: Where a repository declares the gate, next to its sandbox checks and title rules.
RULES_FILE = ".xtm-one/checks.toml"

#: The table and its key.
TABLE = "merge"
KEY = "full_ci_workflows"

#: The header line of the table, so a file without it is never parsed here.
_TABLE_LINE = re.compile(r"^\s*\[\s*merge\s*\]", re.MULTILINE)

#: At most this many workflows are gated: one or two full pipelines, not a list of every job.
MAX_WORKFLOWS = 5

#: Runs read per workflow on the head SHA, newest first.
MAX_RUNS = 20


@dataclass(frozen=True)
class GateVerdict:
    """One workflow's state on the head: ``green`` lets the merge go on."""

    workflow: str
    state: str  # "green" | "absent" | "running" | "red" | "unreadable"
    run_id: int | None = None
    run_url: str = ""
    conclusion: str = ""

    @property
    def green(self) -> bool:
        return self.state == "green"


def parse_gate(text: str) -> tuple[list[str], str | None]:
    """The workflows ``[merge] full_ci_workflows`` names: ``(names, None)``,
    ``([], None)`` when the file declares none, ``([], why)`` when it is malformed."""
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        return [], f"{RULES_FILE} is not valid TOML: {exc}"
    table = data.get(TABLE)
    if table is None:
        return [], None
    if not isinstance(table, dict):
        return [], f"{RULES_FILE}: {TABLE} must be a table."
    raw = table.get(KEY)
    if raw is None:
        return [], None
    if not isinstance(raw, list) or not all(isinstance(w, str) and w.strip() for w in raw):
        return [], f"{RULES_FILE}: [{TABLE}] {KEY} must be a list of workflow file names."
    names = list(dict.fromkeys(w.strip() for w in raw))
    if len(names) > MAX_WORKFLOWS:
        return [], f"{RULES_FILE}: [{TABLE}] {KEY} names at most {MAX_WORKFLOWS} workflows."
    return names, None


def run_verdict(workflow: str, runs: list[dict[str, Any]], head_sha: str) -> GateVerdict:
    """The verdict of *workflow* from its ``workflow_dispatch`` *runs* (newest first) on *head_sha*.

    A run on another SHA never counts: a stale green is no green.
    """
    on_head = [r for r in runs if isinstance(r, dict) and r.get("head_sha") == head_sha]
    if not on_head:
        return GateVerdict(workflow, "absent")
    newest = max(on_head, key=lambda r: (str(r.get("created_at") or ""), _common._coerce_int(r.get("id")) or 0))
    run_id = _common._coerce_int(newest.get("id"))
    url = str(newest.get("html_url") or "")
    if newest.get("status") != "completed":
        return GateVerdict(workflow, "running", run_id, url)
    conclusion = str(newest.get("conclusion") or "")
    if conclusion == "success":
        return GateVerdict(workflow, "green", run_id, url, conclusion)
    return GateVerdict(workflow, "red", run_id, url, conclusion or "unknown")


async def read_gate(
    client: Any, owner: str, name: str, ref: str, headers: dict[str, str]
) -> tuple[list[str], str | None]:
    """The gated workflows *owner/name* declares on *ref*; ``([], None)`` when none or unreadable."""
    params = {"ref": ref} if ref else None
    resp, err = await _common.safe_request(
        client,
        "get",
        f"{_BASE_URL}/repos/{owner}/{name}/contents/{RULES_FILE}",
        _PROVIDER,
        f"read the merge gate of {owner}/{name}",
        headers={**headers, "Accept": "application/vnd.github.raw+json"},
        params=params,
    )
    if err or resp is None:
        return [], None
    text = resp.text
    # A file with no [merge] table declares no gate: its other tables (checks,
    # title rules) are judged by their own readers, never named here.
    if not _TABLE_LINE.search(text):
        return [], None
    return parse_gate(text)


async def workflow_verdict(
    client: Any, owner: str, name: str, workflow: str, head_sha: str, headers: dict[str, str]
) -> GateVerdict:
    """The newest ``workflow_dispatch`` run of *workflow* on *head_sha*, as a verdict."""
    resp, err = await _common.safe_request(
        client,
        "get",
        f"{_BASE_URL}/repos/{owner}/{name}/actions/workflows/{quote(workflow, safe='')}/runs",
        _PROVIDER,
        f"list the full runs of {workflow} on {head_sha[:12]}",
        headers=headers,
        params={"head_sha": head_sha, "event": "workflow_dispatch", "per_page": MAX_RUNS},
    )
    if err or resp is None:
        return GateVerdict(workflow, "unreadable")
    try:
        runs = (resp.json() or {}).get("workflow_runs") or []
    except ValueError:
        return GateVerdict(workflow, "unreadable")
    return run_verdict(workflow, runs if isinstance(runs, list) else [], head_sha)


async def dispatch(
    client: Any, owner: str, name: str, workflow: str, branch: str, headers: dict[str, str]
) -> str | None:
    """Dispatch *workflow* on *branch*; the error, or ``None`` once GitHub accepted it."""
    _resp, err = await _common.safe_request(
        client,
        "post",
        f"{_BASE_URL}/repos/{owner}/{name}/actions/workflows/{quote(workflow, safe='')}/dispatches",
        _PROVIDER,
        f"dispatch {workflow} on {branch} before a merge",
        headers=_json_headers(headers),
        json={"ref": branch},
    )
    return err


async def start_on_push(
    client: Any, owner: str, name: str, pr: dict[str, Any], headers: dict[str, str], head_sha: str = ""
) -> str:
    """Start the gated full runs on the head of open pull request *pr* right after a push (#12067).

    Called by the tools that move a pull request's head (a commit, an update of
    the branch) or open one: each workflow of ``[merge] full_ci_workflows`` with
    no run on the new head yet is dispatched on the head branch now, so the full
    run executes next to the pull_request checks instead of after them, and
    ``merge_github_pr`` finds it running or green. A newer push cancels the stale
    run through the workflow's concurrency group. The line to append to the tool
    answer; ``""`` when the base declares no gate, the head is in a fork or the
    PR is not open. *head_sha*, when given, is the commit the push just wrote
    (a pulls listing read right after a push can still name the previous head).
    Never raises: a failed dispatch is named, the gate at merge time dispatches
    again.
    """
    try:
        if pr.get("state") not in (None, "open"):
            return ""
        base = pr.get("base") or {}
        head = pr.get("head") or {}
        base_ref = str(base.get("ref") or "")
        head_ref = str(head.get("ref") or "")
        head_sha = head_sha or str(head.get("sha") or "")
        same_repo = ((head.get("repo") or {}).get("full_name") or "") == (
            (base.get("repo") or {}).get("full_name") or ""
        )
        if not (base_ref and head_ref and head_sha and same_repo):
            return ""
        workflows, problem = await read_gate(client, owner, name, base_ref, headers)
        if problem or not workflows:
            return ""
        started: list[str] = []
        failed: list[str] = []
        for workflow in workflows:
            verdict = await workflow_verdict(client, owner, name, workflow, head_sha, headers)
            if verdict.state != "absent":
                continue
            err = await dispatch(client, owner, name, workflow, head_ref, headers)
            (failed if err else started).append(f"{workflow}: {err}" if err else workflow)
        number = pr.get("number")
        lines = []
        if started:
            lines.append(
                f"Full CI dispatched on PR #{number} head {head_sha[:12]} ({', '.join(started)}), in parallel with "
                "its pull request checks; merge_github_pr merges once both are green on this head."
            )
        if failed:
            lines.append(
                f"Full CI not dispatched on PR #{number} ({'; '.join(failed)}); merge_github_pr will dispatch it."
            )
        return "\n".join(lines)
    except Exception:  # best effort: a push never fails on the early dispatch
        return ""


def _run_ref(v: GateVerdict) -> str:
    return f"run {v.run_id} ({v.run_url})" if v.run_url else f"run {v.run_id}"


#: Completed runs of a gated workflow read on the base branch, newest first.
BASE_RUNS = 10

#: Events whose run on the base branch tests the base itself (never a pull request's head).
_BASE_EVENTS = frozenset({"push", "workflow_dispatch", "schedule", "merge_group"})


async def failed_jobs(client: Any, owner: str, name: str, run_id: int, headers: dict[str, str]) -> set[str] | None:
    """The names of the failed jobs of run *run_id* (latest attempt); ``None`` when unreadable."""
    resp, err = await _common.safe_request(
        client,
        "get",
        f"{_BASE_URL}/repos/{owner}/{name}/actions/runs/{run_id}/jobs",
        _PROVIDER,
        f"list the jobs of run {run_id}",
        headers=headers,
        params={"filter": "latest", "per_page": 100},
    )
    if err or resp is None:
        return None
    try:
        jobs = (resp.json() or {}).get("jobs") or []
    except ValueError:
        return None
    return {
        str(j.get("name"))
        for j in jobs
        if isinstance(j, dict) and j.get("name") and j.get("conclusion") in ("failure", "timed_out")
    }


def base_overlap(head_failed: set[str], base_runs: list[tuple[dict[str, Any], set[str]]]) -> tuple[str, int | None]:
    """Whether the head's failed jobs are the base's own red. Pure.

    *base_runs* are the completed base runs read, newest first, each with its
    failed job names (empty for a green run). Answers ``(state, run_id)``:

    - ``red_on_base``: the newest base run failed on at least one of the head's
      failed jobs (that run named): the head waits for the base to turn green;
    - ``base_fixed``: the newest base run is green, and an older one failed on
      one of them: the base was red when the head ran, it is green now;
    - ``own``: no overlap, the red is the change's.
    """
    if not head_failed or not base_runs:
        return "own", None
    newest, newest_failed = base_runs[0]
    if str(newest.get("conclusion") or "") != "success":
        if head_failed & newest_failed:
            return "red_on_base", _common._coerce_int(newest.get("id"))
        return "own", None
    for _older, failed in base_runs[1:]:
        if head_failed & failed:
            return "base_fixed", _common._coerce_int(newest.get("id"))
    return "own", None


async def base_red_verdict(
    client: Any, owner: str, name: str, workflow: str, base_ref: str, head_run_id: int, headers: dict[str, str]
) -> tuple[str, int | None, set[str]]:
    """``(state, base run id, overlapping jobs)`` of a red head run against its base (see :func:`base_overlap`)."""
    head_failed = await failed_jobs(client, owner, name, head_run_id, headers)
    if not head_failed or not base_ref:
        return "own", None, set()
    resp, err = await _common.safe_request(
        client,
        "get",
        f"{_BASE_URL}/repos/{owner}/{name}/actions/workflows/{quote(workflow, safe='')}/runs",
        _PROVIDER,
        f"list the completed runs of {workflow} on {base_ref}",
        headers=headers,
        params={"branch": base_ref, "status": "completed", "per_page": BASE_RUNS},
    )
    if err or resp is None:
        return "own", None, set()
    try:
        runs = (resp.json() or {}).get("workflow_runs") or []
    except ValueError:
        return "own", None, set()
    runs = [r for r in runs if isinstance(r, dict) and r.get("event") in _BASE_EVENTS and r.get("id")]
    read: list[tuple[dict[str, Any], set[str]]] = []
    overlap: set[str] = set()
    for run in runs:
        if run.get("conclusion") == "success":
            read.append((run, set()))
            if len(read) > 1:
                break  # a green older than the newest green: older reds no longer explain the head's
            continue
        failed = await failed_jobs(client, owner, name, int(run["id"]), headers) or set()
        read.append((run, failed))
        overlap |= head_failed & failed
        if read[0][0] is run:
            break  # the newest run is red: it alone decides
    state, run_id = base_overlap(head_failed, read)
    return state, run_id, (overlap if state != "own" else set())


#: Reads of the head after ``update-branch`` before the dispatch, two seconds apart.
_UPDATE_POLLS = 5


async def redispatch(repo: str, number: int, head_sha: str, workflow: str, token: str) -> str:
    """Run *workflow* again on PR *number* of *repo* parked on *head_sha* (``FullCiRedispatcher``).

    Its base turned green: the head branch is first brought up to date with the
    base (``update-branch``, the base's fix is in the next run), then the
    workflow is dispatched on it. A closed PR, or one whose head moved since it
    was parked (a push starts its own run), is left alone. Answers one line.
    """
    import asyncio

    from app.core.egress import egress_client

    from ._common import _headers

    owner, _, name = repo.partition("/")
    headers = _headers(token)
    async with egress_client(timeout=30) as client:
        url = f"{_BASE_URL}/repos/{owner}/{name}/pulls/{number}"
        resp, err = await _common.safe_request(client, "get", url, _PROVIDER, f"read PR #{number}", headers=headers)
        if err or resp is None:
            return f"{repo}#{number}: not read ({err}), not re-dispatched"
        pr = resp.json() or {}
        head = pr.get("head") or {}
        branch = str(head.get("ref") or "")
        if pr.get("state") != "open" or head.get("sha") != head_sha or not branch:
            return f"{repo}#{number}: closed or pushed since it was parked, not re-dispatched"
        _resp, err = await _common.safe_request(
            client,
            "put",
            f"{url}/update-branch",
            _PROVIDER,
            f"update the branch of PR #{number}",
            headers=_json_headers(headers),
            json={"expected_head_sha": head_sha},
        )
        updated = err is None
        if updated:
            # update-branch answers 202: the merge commit lands a moment later.
            for _ in range(_UPDATE_POLLS):
                await asyncio.sleep(2)
                resp, err = await _common.safe_request(
                    client, "get", url, _PROVIDER, f"read PR #{number}", headers=headers
                )
                if not err and resp is not None and ((resp.json() or {}).get("head") or {}).get("sha") != head_sha:
                    break
        err = await dispatch(client, owner, name, workflow, branch, headers)
    if err:
        return f"{repo}#{number}: the dispatch of {workflow} on {branch} failed: {err}"
    return f"{repo}#{number}: {workflow} dispatched on {branch}" + (" after update-branch" if updated else "")


async def _red_line(
    client: Any,
    owner: str,
    name: str,
    number: int,
    v: GateVerdict,
    base_ref: str,
    head_sha: str,
    headers: dict[str, str],
    park: Any,
) -> str:
    """The refusal line of a red full run: the change's own, or its base's (parked, or run again)."""
    own = (
        f"- {v.workflow}: {v.conclusion}, {_run_ref(v)}. Read its failed job, push the fix "
        "(the gate starts again on the new head), or rerun it with `manage_github_workflow_run` "
        "if the failure is not the change's."
    )
    if not v.run_id:
        return own
    state, base_run, jobs = await base_red_verdict(client, owner, name, v.workflow, base_ref, v.run_id, headers)
    named = ", ".join(sorted(jobs)[:3])
    if state == "base_fixed":
        return (
            f"- {v.workflow}: {v.conclusion}, {_run_ref(v)}, on jobs ({named}) that also failed on {base_ref}, "
            f"which is green again (run {base_run}): update the branch (`manage_github_pr` update_branch) "
            "or rerun the full run, then merge at its green."
        )
    if state != "red_on_base":
        return own
    parked = False
    if park is not None:
        try:
            parked = bool(await park(f"{owner}/{name}", base_ref, v.workflow, number, head_sha))
        except Exception:
            parked = False
    then = (
        f"parked: the platform runs it again on this PR (after update-branch) the moment {v.workflow} "
        f"is green on {base_ref}; nothing to push or rerun."
        if parked
        else f"rerun it once {v.workflow} is green on {base_ref}."
    )
    return (
        f"- {v.workflow}: {v.conclusion}, {_run_ref(v)}: red on {base_ref} too (run {base_run} fails on "
        f"{named}), not the change's. {then}"
    )


async def gate(
    client: Any,
    owner: str,
    name: str,
    number: int,
    pr: dict[str, Any],
    headers: dict[str, str],
    park: Any = None,
) -> tuple[str | None, str]:
    """``(refusal, note)`` for the merge of PR *number* (its REST object *pr*).

    *park* is the platform's ``(repo, base, workflow, number, head_sha) -> bool``
    that parks a pull request whose red is its base's own until the base turns
    green (``services/full_ci_parking.py``); ``None`` outside an agent's run.

    *refusal* is the whole answer when the merge must not go on (nothing merged).
    *note* ends a merged answer: the forced runs it was gated on, or why no gate applied.
    """
    base = pr.get("base") or {}
    head = pr.get("head") or {}
    base_ref = str(base.get("ref") or "")
    head_sha = str(head.get("sha") or "")
    head_ref = str(head.get("ref") or "")
    workflows, problem = await read_gate(client, owner, name, base_ref, headers)
    if problem:
        return None, f" Full CI gate not checked: {problem}"
    if not workflows:
        return None, ""
    repo = f"{owner}/{name}"
    if not head_sha:
        return (
            f"Error: PR #{number} in {repo} NOT merged: its head SHA could not be read, so the full CI "
            f"gate ({RULES_FILE} [{TABLE}]) cannot be checked. Read it with `get_github_pr` and call again."
        ), ""
    verdicts = [await workflow_verdict(client, owner, name, w, head_sha, headers) for w in workflows]
    if all(v.green for v in verdicts):
        named = ", ".join(f"{v.workflow} {_run_ref(v)}" for v in verdicts)
        return None, f" Full CI on head {head_sha[:12]}: {named}, green."
    same_repo = ((head.get("repo") or {}).get("full_name") or "") == ((base.get("repo") or {}).get("full_name") or "")
    lines = [
        f"PR #{number} in {repo} NOT merged: {RULES_FILE} [{TABLE}] requires a full run of "
        f"{', '.join(workflows)} green on head {head_sha[:12]} first (the pull request CI skips the suites)."
    ]
    for v in verdicts:
        if v.green:
            lines.append(f"- {v.workflow}: green, {_run_ref(v)}.")
        elif v.state == "running":
            lines.append(f"- {v.workflow}: running, {_run_ref(v)}; merge again once it is green.")
        elif v.state == "red":
            lines.append(await _red_line(client, owner, name, number, v, base_ref, head_sha, headers, park))
        elif v.state == "unreadable":
            lines.append(f"- {v.workflow}: its runs could not be read; call again.")
        elif not same_repo or not head_ref:
            lines.append(
                f"- {v.workflow}: no full run on this head, and the head branch is not in {repo}, so it "
                "cannot be dispatched from here."
            )
        else:
            err = await dispatch(client, owner, name, v.workflow, head_ref, headers)
            if err:
                lines.append(f"- {v.workflow}: no full run on this head; the dispatch on {head_ref} failed: {err}")
            else:
                lines.append(
                    f"- {v.workflow}: no full run on this head: dispatched now on {head_ref}. Follow it with "
                    f"`list_github_workflow_runs` (workflow='{v.workflow}', head_sha='{head_sha}') and merge "
                    "again once it is green."
                )
    lines.append("Nothing was merged.")
    return "\n".join(lines), ""
